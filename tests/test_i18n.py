"""recorder.i18n: both languages, formatting, toggle/listeners, persistence, safe stdio."""
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from recorder import i18n
from recorder.i18n import L, both


class I18nTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_env = {k: os.environ.get(k) for k in ("DSTUDIO_SETTINGS", "DSTUDIO_LANG")}
        self.saved_lang = i18n._lang
        self.saved_listeners = list(i18n._listeners)
        self.settings_file = os.path.join(self.tmp.name, "sub", "settings.json")
        os.environ["DSTUDIO_SETTINGS"] = self.settings_file
        os.environ.pop("DSTUDIO_LANG", None)
        i18n.reset()

    def tearDown(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        i18n._lang = self.saved_lang          # other test modules keep their language
        i18n._listeners[:] = self.saved_listeners
        self.tmp.cleanup()

    def saved(self):
        with open(self.settings_file, encoding="utf-8") as fh:
            return json.load(fh)

    def test_both_languages(self):
        i18n.set_language("zh", persist=False)
        self.assertEqual(L("停止并打包", "Stop & package"), "停止并打包")
        i18n.set_language("en", persist=False)
        self.assertEqual(L("停止并打包", "Stop & package"), "Stop & package")

    def test_format_arguments(self):
        i18n.set_language("en", persist=False)
        self.assertEqual(L("已录制 {n} 帧", "Recorded {n} frames", n=42), "Recorded 42 frames")
        i18n.set_language("zh", persist=False)
        self.assertEqual(L("已录制 {n} 帧", "Recorded {n} frames", n=42), "已录制 42 帧")
        self.assertEqual(both("{n} 帧", "{n} frames", n=3), {"zh": "3 帧", "en": "3 frames"})

    def test_broken_translation_never_raises(self):
        i18n.set_language("en", persist=False)
        self.assertEqual(L("{missing}", "{missing} x"), "{missing} x")       # no kwargs: no format
        self.assertEqual(L("{a}", "{b}", a=1), "{b}")                         # typo in one language
        self.assertEqual(L("{", "{", a=1), "{")
        self.assertEqual(both("{a}", "{b}", a=1), {"zh": "1", "en": "{b}"})

    def test_toggle_and_listeners(self):
        seen = []
        i18n.set_language("zh", persist=False)
        cb = i18n.on_change(seen.append)
        self.assertEqual(cb, seen.append)
        self.assertEqual(i18n.toggle_language(), "en")
        self.assertEqual(i18n.get_language(), "en")
        self.assertEqual(i18n.toggle_language(), "zh")
        self.assertEqual(seen, ["en", "zh"])
        i18n.off_change(seen.append)
        i18n.off_change(seen.append)  # removing twice is harmless
        i18n.toggle_language()
        self.assertEqual(seen, ["en", "zh"])

    def test_failing_listener_does_not_break_others(self):
        seen = []

        def bad(_lang):
            raise RuntimeError("widget already destroyed")

        i18n.on_change(bad)
        i18n.on_change(seen.append)
        i18n.set_language("en", persist=False)
        self.assertEqual(seen, ["en"])

    def test_invalid_language_rejected(self):
        with self.assertRaises(ValueError):
            i18n.set_language("fr")
        i18n.set_language(" EN ", persist=False)
        self.assertEqual(i18n.get_language(), "en")

    def test_persisted_choice_survives_restart(self):
        i18n.set_language("en")
        self.assertEqual(self.saved()["language"], "en")
        i18n.reset()                       # "restart": forget the cached value
        self.assertEqual(i18n.get_language(), "en")
        i18n.toggle_language()
        self.assertEqual(self.saved()["language"], "zh")
        i18n.reset()
        self.assertEqual(L("中", "EN"), "中")

    def test_persist_false_keeps_saved_preference(self):
        i18n.set_language("zh")
        i18n.set_language("en", persist=False)  # e.g. --lang en / CI screenshot
        self.assertEqual(self.saved()["language"], "zh")
        i18n.reset()
        self.assertEqual(i18n.get_language(), "zh")

    def test_env_overrides_settings(self):
        i18n.set_language("zh")
        os.environ["DSTUDIO_LANG"] = "en"
        i18n.reset()
        self.assertEqual(i18n.get_language(), "en")

    def test_default_follows_system_when_unset(self):
        i18n.reset()
        self.assertIn(i18n.get_language(), i18n.SUPPORTED)
        self.assertFalse(os.path.exists(self.settings_file))  # reading never writes


class _Stream(io.TextIOWrapper):
    """A cp1252 stdout as the installed app gets it (``python -E``, English Windows)."""

    def __init__(self, tty=False):
        super().__init__(io.BytesIO(), encoding="cp1252", errors="strict")
        self._tty = tty

    def isatty(self):
        return self._tty


class SafeStdioTest(unittest.TestCase):
    """Bilingual output never raises UnicodeEncodeError (pipes / files / consoles)."""

    def test_pipe_becomes_utf8_and_console_never_raises(self):
        pipe, console = _Stream(), _Stream(tty=True)
        with self.assertRaises(UnicodeEncodeError):
            print("核验通过 / checks passed", file=pipe)
        with patch.object(sys, "stdout", pipe), patch.object(sys, "stderr", console):
            i18n.safe_stdio()
            print("核验通过 / checks passed")
            print("错误 / error", file=sys.stderr)
        pipe.flush()
        console.flush()
        self.assertEqual(pipe.encoding.lower().replace("-", ""), "utf8")
        self.assertIn("核验通过 / checks passed", pipe.buffer.getvalue().decode("utf-8"))
        self.assertEqual(console.encoding, "cp1252")                 # a console keeps its code page...
        self.assertIn(b"?? / error", console.buffer.getvalue())     # ...but replaces what it cannot show
        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            i18n.safe_stdio()                                        # pythonw: no streams at all

    def test_python_m_recorder_calls_it_first(self):
        calls = []
        safe = Mock(side_effect=lambda: calls.append("safe_stdio"))

        def gui_main(argv):
            calls.append(("gui", list(argv)))
            return 0

        with patch.object(i18n, "safe_stdio", safe), patch("recorder.gui.main", side_effect=gui_main), \
                patch.object(sys, "argv", ["recorder", "gui", "--open-data"]):
            with self.assertRaises(SystemExit) as ctx:
                runpy.run_module("recorder", run_name="__main__")
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(calls, ["safe_stdio", ("gui", ["--open-data"])])

    def test_gui_main_calls_it_first(self):
        from recorder import gui

        order = []
        with patch.object(i18n, "safe_stdio", side_effect=lambda: order.append("safe")), \
                patch.object(gui, "open_data_folder", side_effect=lambda: order.append("open") or 0):
            self.assertEqual(gui.main(["--open-data"]), 0)
        self.assertEqual(order, ["safe", "open"])


if __name__ == "__main__":
    unittest.main()
