"""Unit tests for dstudio.pyw (the launcher behind the Start-menu shortcuts).

Portable: runs on macOS/Linux/Windows (python tests_windows/run_unit.py).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(ROOT, "dstudio.pyw")


def load_launcher():
    loader = importlib.machinery.SourceFileLoader("dstudio_launcher_under_test", LAUNCHER)
    spec = importlib.util.spec_from_file_location("dstudio_launcher_under_test", LAUNCHER, loader=loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class LauncherBasics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_launcher()

    def test_version_comes_from_version_txt(self):
        with open(os.path.join(ROOT, "VERSION.txt"), encoding="utf-8-sig") as fh:
            first = fh.readline().strip()
        self.assertRegex(first, r"^\d+\.\d+\.\d+")
        self.assertEqual(self.mod.app_version(), re.match(r"\d+\.\d+\.\d+\S*", first).group(0))
        self.assertNotEqual(self.mod.app_version(), "0.0.0")

    def test_gui_args_pass_through(self):
        opts, rest = self.mod.parse_launcher_args(["--backend", "gdigrab", "--codec", "libx264", "--lang", "zh"])
        self.assertFalse(opts.viewer or opts.selftest)
        self.assertEqual(rest, ["--backend", "gdigrab", "--codec", "libx264"])
        self.assertEqual(opts.lang, "zh")

    def test_viewer_and_selftest_flags(self):
        opts, rest = self.mod.parse_launcher_args(["--viewer", "--port", "8123", "--no-browser"])
        self.assertTrue(opts.viewer)
        self.assertEqual((opts.port, opts.no_browser, rest), (8123, True, []))
        opts, _ = self.mod.parse_launcher_args(["--selftest", "--json", "x.json", "--quick", "--no-pause"])
        self.assertTrue(opts.selftest and opts.quick and opts.no_pause)
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            self.mod.parse_launcher_args(["--viewer", "--selftest"])

    def test_no_abbreviated_options_swallow_gui_flags(self):
        # allow_abbrev=False: '--no' style GUI flags must reach the GUI untouched
        _, rest = self.mod.parse_launcher_args(["--no-single-instance", "--state", "idle"])
        self.assertEqual(rest, ["--no-single-instance", "--state", "idle"])

    def test_dev_interpreter_is_not_bundled(self):
        self.assertFalse(self.mod.is_bundled_interpreter())
        before = list(sys.path)
        self.mod.sanitize_sys_path()           # no-op outside the bundled python
        self.assertEqual(sys.path, before)

    def test_hide_child_consoles_only_on_windows(self):
        original = subprocess.Popen
        try:
            self.mod.hide_child_consoles()
            if sys.platform == "win32":
                self.assertTrue(getattr(subprocess.Popen, "_dstudio_no_window", False))
                proc = subprocess.Popen([sys.executable, "-c", "pass"])
                self.assertEqual(proc.wait(30), 0)
            else:
                self.assertIs(subprocess.Popen, original)
        finally:
            subprocess.Popen = original

    def test_format_report(self):
        report = {"version": "0.2.0", "time": "t", "platform": "p",
                  "checks": [{"id": "a", "name": "A", "status": "ok", "detail": "fine"},
                             {"id": "b", "name": "B", "status": "fail", "detail": "broken"}]}
        text = self.mod.format_report(report)
        self.assertIn("[ OK ] A", text)
        self.assertIn("[FAIL] B", text)

    def test_serve_module_and_ping(self):
        serve = self.mod.load_serve_module()
        self.assertTrue(hasattr(serve, "start_server"))
        with tempfile.TemporaryDirectory() as tmp:
            server, _thread = serve.start_server(port=0, data_dir=tmp, open_browser=False)
            try:
                port = server.server_address[1]
                info = self.mod._ping_viewer(port, timeout=3)
                self.assertIsNotNone(info)
                self.assertTrue(self.mod._same_dir(info.get("data_dir"), tmp))
            finally:
                serve.stop_server(server)
        self.assertIsNone(self.mod._ping_viewer(1, timeout=0.3))


class LauncherV021(unittest.TestCase):
    """K7 (?lang= on annotator URLs), K8 (--open-data), Tcl/Tk pinning on the bundled Python."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_launcher()

    def test_with_lang(self):
        w = self.mod.with_lang
        self.assertEqual(w("http://127.0.0.1:8000/viewer/", "zh"), "http://127.0.0.1:8000/viewer/?lang=zh")
        self.assertEqual(w("http://127.0.0.1:8000/viewer/#dataset=rec-1", "en"),
                         "http://127.0.0.1:8000/viewer/?lang=en#dataset=rec-1")
        self.assertEqual(w("http://h/viewer/?a=1&lang=en#x", "zh"), "http://h/viewer/?a=1&lang=zh#x")
        self.assertEqual(w("http://h/viewer/?lang=zh&b=2", "en"), "http://h/viewer/?b=2&lang=en")
        self.assertRegex(w("http://h/viewer/"), r"\?lang=(zh|en)$")          # current UI language
        self.assertRegex(w("http://h/viewer/", "fr"), r"\?lang=(zh|en)$")    # unknown -> current language
        with mock.patch("recorder.i18n.get_language", return_value="zh"):
            self.assertEqual(self.mod.current_lang(), "zh")
            self.assertEqual(w("http://h/viewer/"), "http://h/viewer/?lang=zh")

    def test_open_data_flag(self):
        opts, rest = self.mod.parse_launcher_args(["--open-data"])
        self.assertTrue(opts.open_data)
        self.assertEqual(rest, [])
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            self.mod.parse_launcher_args(["--open-data", "--viewer"])
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "moved recordings")
            with mock.patch.dict(os.environ, {"DSTUDIO_DATA_DIR": target}), \
                    mock.patch.object(self.mod, "open_path", return_value=True) as op, mock.patch("sys.stdout"):
                self.assertEqual(self.mod.run_open_data(), 0)
            op.assert_called_once()
            self.assertTrue(self.mod._same_dir(op.call_args[0][0], target))
            self.assertTrue(os.path.isdir(target))          # created, so Explorer opens a real folder
            with mock.patch.dict(os.environ, {"DSTUDIO_DATA_DIR": target}), \
                    mock.patch.object(self.mod, "open_path", return_value=False), \
                    mock.patch.object(self.mod, "message_box") as box, mock.patch("sys.stdout"):
                self.assertEqual(self.mod.run_open_data(), 1)
            box.assert_called_once()

    def test_main_dispatches_open_data_before_any_ui(self):
        with mock.patch.object(self.mod, "run_open_data", return_value=0) as rod, \
                mock.patch.object(self.mod, "set_app_identity") as ident, \
                mock.patch.object(self.mod, "setup_console_io"), mock.patch.object(self.mod, "setup_windowed_io"), \
                mock.patch.object(self.mod, "hide_child_consoles"):
            self.assertEqual(self.mod.main(["--open-data"]), 0)
        rod.assert_called_once()
        ident.assert_not_called()

    def test_viewer_urls_carry_lang(self):
        data_dir = self.mod.safe_data_dir()
        opened = []
        opts, _ = self.mod.parse_launcher_args(["--viewer", "--port", "8123"])
        with mock.patch.object(self.mod, "_ping_viewer", side_effect=lambda port, timeout=0.8: (
                {"app": "dstudio-viewer", "data_dir": data_dir} if port == 8123 else None)), \
                mock.patch("webbrowser.open", side_effect=opened.append), mock.patch("sys.stdout"):
            self.assertEqual(self.mod.run_viewer(opts), 0)
        self.assertEqual(len(opened), 1)
        self.assertRegex(opened[0], r"^http://127\.0\.0\.1:8123/viewer/\?lang=(zh|en)$")

        class FakeThread:
            def is_alive(self):
                return False

        server = mock.Mock(url="http://127.0.0.1:8124/viewer/#dataset=rec-x")
        fake_serve = mock.Mock(start_server=mock.Mock(return_value=(server, FakeThread())), stop_server=mock.Mock())
        opened.clear()
        opts, _ = self.mod.parse_launcher_args(["--viewer", "--port", "8124", "--no-window"])
        with mock.patch.object(self.mod, "_ping_viewer", return_value=None), \
                mock.patch.object(self.mod, "load_serve_module", return_value=fake_serve), \
                mock.patch("webbrowser.open", side_effect=opened.append), mock.patch("sys.stdout"):
            self.assertEqual(self.mod.run_viewer(opts), 0)
        self.assertRegex(opened[0], r"^http://127\.0\.0\.1:8124/viewer/\?lang=(zh|en)#dataset=rec-x$")
        fake_serve.stop_server.assert_called_once_with(server)
        # the control window shows/opens the same, re-evaluated after the 中/EN toggle
        import inspect

        self.assertIn("webbrowser.open(with_lang(self.url))", inspect.getsource(self.mod.ViewerWindow.open_browser))
        self.assertIn("self.url_lbl.configure(text=with_lang(self.url))",
                      inspect.getsource(self.mod.ViewerWindow.render))
        self.assertIn("self.open_browser()", inspect.getsource(self.mod.ViewerWindow.toggle_lang))

    def _fake_prefix(self, tmp, tcl=True, tk=True):
        if tcl:
            os.makedirs(os.path.join(tmp, "tcl", "tcl8.6"))
            open(os.path.join(tmp, "tcl", "tcl8.6", "init.tcl"), "w").close()
        if tk:
            os.makedirs(os.path.join(tmp, "tcl", "tk8.6"))
            open(os.path.join(tmp, "tcl", "tk8.6", "tk.tcl"), "w").close()
        os.makedirs(os.path.join(tmp, "tcl", "dde1.4"), exist_ok=True)
        return tmp

    def test_pin_bundled_tcl_replaces_foreign_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._fake_prefix(tmp)
            env = {"TCL_LIBRARY": r"C:\Program Files\SomeTool\lib\tcl8.6", "TK_LIBRARY": r"C:\Other\tk8.5",
                   "TIX_LIBRARY": r"C:\Other\tix", "PATH": "x"}
            replaced = self.mod.pin_bundled_tcl(env, prefix)
            self.assertEqual(env["TCL_LIBRARY"], os.path.join(prefix, "tcl", "tcl8.6"))
            self.assertEqual(env["TK_LIBRARY"], os.path.join(prefix, "tcl", "tk8.6"))
            self.assertNotIn("TIX_LIBRARY", env)
            self.assertEqual(env["PATH"], "x")
            self.assertEqual(set(replaced), {"TCL_LIBRARY", "TK_LIBRARY", "TIX_LIBRARY"})
            # already ours (or unset): nothing reported, values pinned
            env2 = {"TCL_LIBRARY": os.path.join(prefix, "tcl", "tcl8.6")}
            self.assertEqual(self.mod.pin_bundled_tcl(env2, prefix), {})
            self.assertEqual(env2["TK_LIBRARY"], os.path.join(prefix, "tcl", "tk8.6"))

    def test_pin_bundled_tcl_without_bundled_libs_drops_foreign_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = self._fake_prefix(tmp, tcl=False, tk=False)
            env = {"TCL_LIBRARY": "/elsewhere/tcl8.6", "TK_LIBRARY": "/elsewhere/tk8.6"}
            self.assertEqual(set(self.mod.pin_bundled_tcl(env, prefix)), {"TCL_LIBRARY", "TK_LIBRARY"})
            self.assertEqual(env, {})

    def test_pin_bundled_tcl_is_a_no_op_on_other_interpreters(self):
        env = {"TCL_LIBRARY": "/dev/python/tcl"}
        self.assertFalse(self.mod.is_bundled_interpreter())
        self.assertEqual(self.mod.pin_bundled_tcl(env), {})
        self.assertEqual(env, {"TCL_LIBRARY": "/dev/python/tcl"})

    def test_tcl_pinned_before_tkinter_can_load(self):
        import inspect

        src = inspect.getsource(self.mod.main)
        self.assertLess(src.index("pin_bundled_tcl()"), src.index("setup_windowed_io()"))
        self.assertLess(src.index("pin_bundled_tcl()"), src.index("set_app_identity("))


class SelfTestProcess(unittest.TestCase):
    def test_selftest_writes_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DSTUDIO_DATA_DIR=os.path.join(tmp, "data"),
                       DSTUDIO_SETTINGS=os.path.join(tmp, "settings.json"), DSTUDIO_LANG="en",
                       PYTHONIOENCODING="utf-8")
            out = os.path.join(tmp, "report.json")
            proc = subprocess.run([sys.executable, LAUNCHER, "--selftest", "--quick", "--no-tk-window",
                                   "--no-pause", "--json", out], env=env, capture_output=True, timeout=300)
            self.assertTrue(os.path.isfile(out), proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace"))
            with open(out, encoding="utf-8") as fh:
                report = json.load(fh)
            ids = [c["id"] for c in report["checks"]]
            for cid in ("python", "tkinter", "ffmpeg", "ffprobe", "encoder", "data_dir", "settings", "viewer", "dxgi"):
                self.assertIn(cid, ids)
            self.assertTrue(all(c["status"] in ("ok", "warn", "fail", "skip") for c in report["checks"]))
            failed = any(c["status"] == "fail" for c in report["checks"])
            self.assertEqual(proc.returncode, 1 if failed else 0)
            self.assertEqual(report["exit_code"], proc.returncode)
            self.assertEqual({c["id"]: c["status"] for c in report["checks"]}["data_dir"], "ok")
            self.assertTrue(os.path.isfile(os.path.join(tmp, "data", "dstudio-selftest.log")))
            self.assertIn("self-test", proc.stdout.decode("utf-8", "replace"))

    def test_version_flag(self):
        proc = subprocess.run([sys.executable, LAUNCHER, "--version"], capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.decode().strip(), load_launcher().app_version())


if __name__ == "__main__":
    unittest.main()
