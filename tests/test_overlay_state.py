"""Recorder bar: pure state logic, recorder argv, headless state machine, screenshot PNG.

Runs without a display: the OverlayUI tests replace every Tk widget with a Mock.
An optional real-Tk smoke test runs only where a display is available.
"""
import io
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from recorder import gui, i18n, reclock
from recorder.gui import (OverlayUI, LiveFps, build_record_argv, button_states, display_state,
                          fmt_elapsed, is_inside, mode_chip, new_output_dir, normalize_backend,
                          parse_gui_args, state_text, stats_text, validate_config, viewer_url)
from recorder import app_picker, session_form

_TESTS = os.path.dirname(os.path.abspath(__file__))
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)
import gui_fixtures as fx  # noqa: E402  (tests/gui_fixtures.py: synthetic recordings)

WIDGETS = ("time_lbl", "state_lbl", "dot", "stats", "mode_lbl", "root", "btn_main", "btn_stop",
           "btn_session", "btn_export", "btn_annotator", "btn_export_rec", "btn_folder",
           "result_row", "info", "frame")


class _Env(unittest.TestCase):
    """Temp settings/data dir + English UI; restores global state afterwards."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_env = {k: os.environ.get(k) for k in ("DSTUDIO_SETTINGS", "DSTUDIO_DATA_DIR", "DSTUDIO_LANG")}
        os.environ["DSTUDIO_SETTINGS"] = os.path.join(self.tmp.name, "settings.json")
        os.environ["DSTUDIO_DATA_DIR"] = os.path.join(self.tmp.name, "data")
        os.environ.pop("DSTUDIO_LANG", None)
        self.saved_lang = i18n._lang
        self.saved_listeners = list(i18n._listeners)
        i18n.set_language("en", persist=False)

    def tearDown(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        i18n._lang = self.saved_lang
        i18n._listeners[:] = self.saved_listeners
        self.tmp.cleanup()


def headless_ui(state="idle", snap=None, **cfg):
    ui = object.__new__(OverlayUI)
    ui.gui_args = parse_gui_args(["--session-info", os.path.join(os.environ["DSTUDIO_DATA_DIR"], "none.json")])
    ui.screenshot = False
    ui._init_state()
    ui._persist = False
    ui.cfg.update(cfg)
    for name in WIDGETS:
        setattr(ui, name, Mock())
    ui.state = state
    if snap is not None:
        ui.rec = SimpleNamespace(paused=bool(snap.get("paused")), snapshot=lambda: dict(snap),
                                 toggle_pause=Mock(), stop=Mock())
    return ui


def last_kwargs(mock_widget):
    merged = {}
    for call in mock_widget.configure.call_args_list:
        merged.update(call.kwargs)
    return merged


# ------------------------------------------------------------------ pure logic
class PureLogicTest(_Env):
    def test_timer_format(self):
        self.assertEqual(fmt_elapsed(12345), "00:12.3")
        self.assertEqual(fmt_elapsed(0), "00:00.0")
        self.assertEqual(fmt_elapsed(None), "00:00.0")
        self.assertEqual(fmt_elapsed(-5), "00:00.0")
        self.assertEqual(fmt_elapsed(59 * 60 * 1000 + 59999), "59:59.9")
        self.assertEqual(fmt_elapsed(3 * 3600 * 1000 + 62 * 1000), "3:01:02")  # long recordings
        self.assertEqual(gui._fmt(1000), "00:01.0")

    def test_display_state(self):
        self.assertEqual(display_state("recording", {}), "recording")
        self.assertEqual(display_state("recording", {"paused": True}), "paused")
        self.assertEqual(display_state("paused", {"paused": True, "auto_paused": True}), "auto_paused")
        self.assertEqual(display_state("recording", {"auto_paused": True}), "auto_paused")
        for st in ("stopping", "packing", "exporting", "done", "error", "idle"):
            self.assertEqual(display_state(st, {"auto_paused": True}), st)
        self.assertEqual(display_state("weird"), "idle")

    def test_button_states_per_state(self):
        rec = button_states("recording", autopack=True)
        self.assertTrue(rec["stop"]["enabled"])
        self.assertEqual(rec["stop"]["text"], "■ Stop & package")
        self.assertEqual(rec["main"]["text"], "⏸ Pause")
        self.assertTrue(rec["settings_locked"])
        self.assertFalse(rec["show_results"])
        self.assertEqual(button_states("recording", autopack=False)["stop"]["text"], "■ Stop")
        self.assertEqual(button_states("paused")["main"]["text"], "▶ Resume")
        auto = button_states("auto_paused")
        self.assertEqual(auto["main"]["text"], "⏸ Hold pause")   # F9 semantics: becomes a manual pause
        self.assertTrue(auto["main"]["enabled"])
        self.assertTrue(auto["stop"]["enabled"])
        for busy in ("stopping", "packing", "exporting"):
            b = button_states(busy, has_output=True)
            self.assertFalse(b["main"]["enabled"])
            self.assertFalse(b["stop"]["enabled"])
            self.assertFalse(b["export_rec"]["enabled"])
            self.assertTrue(b["settings_locked"])
        done = button_states("done", has_output=True, markov=True)
        self.assertEqual(done["main"]["text"], "● New recording")
        self.assertFalse(done["stop"]["enabled"])
        for name in ("annotator", "export_rec", "session", "export_cad", "folder"):
            self.assertTrue(done[name]["enabled"], name)
        self.assertEqual(done["export_cad"]["text"], "Verify & export Markov CAD")
        self.assertTrue(done["show_results"])
        idle = button_states("idle")
        self.assertTrue(idle["main"]["enabled"])
        self.assertFalse(idle["export_rec"]["enabled"])
        self.assertFalse(idle["show_results"])
        err = button_states("error", has_output=True)
        self.assertTrue(err["export_rec"]["enabled"])
        self.assertFalse(err["export_cad"]["enabled"])
        self.assertEqual(button_states("done", has_output=True)["export_cad"]["text"],
                         "Verify & export Markov CAD")                       # Markov CAD is the default
        self.assertEqual(button_states("done", has_output=True, markov=False)["export_cad"]["text"],
                         "Verify & export legacy delivery")
        i18n.set_language("zh", persist=False)
        self.assertEqual(button_states("recording")["stop"]["text"], "■ 停止并打包")

    def test_state_text(self):
        self.assertEqual(state_text("auto_paused", app="SOLIDWORKS")[0], "Auto-paused: SOLIDWORKS not in front")
        self.assertEqual(state_text("done", packaged=False)[0], "Stopped")
        self.assertEqual(state_text("packing", closing=True)[0], "Packaging, then closing…")
        i18n.set_language("zh", persist=False)
        self.assertEqual(state_text("auto_paused", app="SOLIDWORKS")[0], "自动暂停：SOLIDWORKS 不在前台")
        self.assertEqual(state_text("idle")[0], "待机")

    def test_record_argv_full_screen(self):
        cfg = {"capture_mode": "desktop", "fps": 30, "quality": 23, "backend": "auto", "draw_cursor": True}
        argv = build_record_argv(cfg, "OUT", session_path="S.json", ffmpeg="C:/ff/ffmpeg.exe",
                                 extra=["--sync-latency-ms", "12"])
        self.assertEqual(argv[0], "start")
        self.assertEqual(argv[argv.index("--out") + 1], "OUT")
        self.assertEqual(argv[argv.index("--source") + 1], "desktop")
        self.assertEqual(argv[argv.index("--fps") + 1], "30")
        self.assertEqual(argv[argv.index("--ffmpeg") + 1], "C:/ff/ffmpeg.exe")
        self.assertEqual(argv[argv.index("--session-info") + 1], "S.json")
        self.assertIn("--draw-mouse-cursor", argv)
        self.assertNotIn("--app-allow", argv)
        self.assertEqual(argv[-2:], ["--sync-latency-ms", "12"])
        no_cursor = build_record_argv(dict(cfg, draw_cursor=False, fps=60), "OUT", ffmpeg="ff")
        self.assertIn("--no-draw-mouse-cursor", no_cursor)
        self.assertEqual(no_cursor[no_cursor.index("--fps") + 1], "60")
        custom = build_record_argv(cfg, "OUT", ffmpeg="ff", source_override="monitor:1")
        self.assertEqual(custom[custom.index("--source") + 1], "monitor:1")

    def test_record_argv_ffmpeg_defaults_to_bundled(self):
        with patch.object(gui.paths, "ffmpeg_path", return_value="X:/bundled/ffmpeg.exe"):
            argv = build_record_argv({"capture_mode": "desktop"}, "OUT")
        self.assertEqual(argv[argv.index("--ffmpeg") + 1], "X:/bundled/ffmpeg.exe")

    def test_record_argv_app_mode(self):
        cfg = {"capture_mode": "app", "app_exe": "SLDWORKS.exe", "fps": 30, "quality": 23,
               "backend": "gdigrab", "draw_cursor": True, "app_allow": ["helper.exe", " "],
               "auto_pause_other_apps": False}
        argv = build_record_argv(cfg, "OUT", ffmpeg="ff")
        self.assertEqual(argv[argv.index("--source") + 1], "app:SLDWORKS.exe")
        self.assertEqual(argv[argv.index("--backend") + 1], "auto")   # gdigrab is invalid in app mode
        i = argv.index("--app-allow")
        self.assertEqual(argv[i + 1], "helper.exe")
        self.assertTrue(argv[i + 2].startswith("--"))
        self.assertIn("--no-auto-pause-other-apps", argv)
        default = build_record_argv(dict(cfg, app_allow=[], auto_pause_other_apps=True), "OUT", ffmpeg="ff")
        self.assertNotIn("--app-allow", default)
        self.assertNotIn("--no-auto-pause-other-apps", default)
        self.assertNotIn("--app-overlay-allow", default)

    def test_record_argv_overlay_allow(self):
        """Finding 25: ⚙ → Allowed overlays reaches the recorder as --app-overlay-allow EXE..."""
        cfg = {"capture_mode": "app", "app_exe": "SLDWORKS.exe", "app_allow": ["helper.exe"],
               "app_overlay_allow": ["dimmer.exe", " ", "wm.exe"]}
        argv = build_record_argv(cfg, "OUT", ffmpeg="ff", extra=["--sync-latency-ms", "5"])
        i = argv.index("--app-overlay-allow")
        self.assertEqual(argv[i + 1:i + 3], ["dimmer.exe", "wm.exe"])
        self.assertTrue(argv[i + 3].startswith("--"))
        self.assertEqual(argv[argv.index("--app-allow") + 1], "helper.exe")
        self.assertEqual(argv[-2:], ["--sync-latency-ms", "5"])
        self.assertNotIn("--app-overlay-allow", build_record_argv(dict(cfg, app_overlay_allow=[]), "OUT", ffmpeg="ff"))
        # Full screen has no masking: the list is not passed
        self.assertNotIn("--app-overlay-allow",
                         build_record_argv(dict(cfg, capture_mode="desktop"), "OUT", ffmpeg="ff"))

    def test_record_argv_accepted_by_recorder(self):
        """Contract section 1: the recorder must accept every flag the bar sends."""
        try:
            from recorder.recorder import parse_args  # noqa: F401
        except Exception as exc:  # Windows-only capture stack not importable here
            self.skipTest(f"recorder not importable: {exc}")
        out = os.path.join(self.tmp.name, "rec")
        for cfg in ({"capture_mode": "desktop", "fps": 30, "quality": 23, "backend": "auto", "draw_cursor": True},
                    {"capture_mode": "desktop", "fps": 60, "quality": 23, "backend": "gdi", "draw_cursor": False},
                    {"capture_mode": "app", "app_exe": "notepad.exe", "fps": 30, "quality": 23, "backend": "dxcam",
                     "draw_cursor": True, "app_allow": ["helper.exe"], "auto_pause_other_apps": False,
                     "app_overlay_allow": ["dimmer.exe"]}):
            args = gui.parse_recorder_args(build_record_argv(cfg, out, ffmpeg="ffmpeg"))
            self.assertEqual(args.fps, cfg["fps"])
            self.assertEqual(bool(args.draw_mouse_cursor), cfg["draw_cursor"])
            self.assertEqual(args.backend, normalize_backend(cfg["capture_mode"], cfg["backend"]))
            if cfg["capture_mode"] == "app":
                self.assertEqual(args.source, "app:notepad.exe")
                self.assertEqual(args.app_allow, ["helper.exe"])
                self.assertEqual(args.app_overlay_allow, ["dimmer.exe"])

    def test_parse_recorder_args_error_is_readable(self):
        try:
            from recorder.recorder import parse_args  # noqa: F401
        except Exception as exc:
            self.skipTest(f"recorder not importable: {exc}")
        with self.assertRaises(ValueError) as ctx:
            gui.parse_recorder_args(["start", "--definitely-not-a-flag"])
        self.assertIn("definitely-not-a-flag", str(ctx.exception))

    def test_validate_config(self):
        ok = {"capture_mode": "desktop", "fps": 30, "quality": 23}
        self.assertEqual(validate_config(ok), "")
        self.assertIn("Frame rate", validate_config(dict(ok, fps=0)))
        self.assertIn("Quality", validate_config(dict(ok, quality=-1)))
        self.assertIn("Choose the app", validate_config(dict(ok, capture_mode="app", app_exe="")))
        self.assertEqual(validate_config(dict(ok, capture_mode="app", app_exe="a.exe")), "")
        i18n.set_language("zh", persist=False)
        self.assertIn("选择要录制的应用", validate_config(dict(ok, capture_mode="app")))

    def test_mode_chip_and_backend(self):
        self.assertEqual(mode_chip({"capture_mode": "desktop"})[0], "Full screen · main display")  # K3
        self.assertEqual(mode_chip({"capture_mode": "app", "app_exe": "SLDWORKS.exe"})[0], "App only: SLDWORKS")
        self.assertEqual(mode_chip({"capture_mode": "app", "app_exe": "x.exe", "app_name": "SOLIDWORKS"})[0],
                         "App only: SOLIDWORKS")
        self.assertEqual(mode_chip({"capture_mode": "app"})[1], gui.WARN)
        self.assertEqual(normalize_backend("app", "gdigrab"), "auto")
        self.assertEqual(normalize_backend("desktop", "gdigrab"), "gdigrab")
        self.assertEqual(normalize_backend("desktop", "bogus"), "auto")
        self.assertEqual(normalize_backend("app", "gdi"), "gdi")

    def test_output_dirs_and_paths(self):
        base = self.tmp.name
        import datetime as dt
        when = dt.datetime(2026, 11, 18, 10, 15, 0)
        first = new_output_dir(base, when)
        self.assertEqual(os.path.basename(first), "rec-20261118-101500")
        os.makedirs(first)
        second = new_output_dir(base, when)
        self.assertEqual(os.path.basename(second), "rec-20261118-101500-2")
        self.assertTrue(is_inside(os.path.join(first, "x"), first))
        self.assertTrue(is_inside(first, first))
        self.assertFalse(is_inside(base, first))
        self.assertEqual(viewer_url(8001, "rec-1"), "http://127.0.0.1:8001/viewer/?lang=en#rec-1")
        self.assertEqual(viewer_url(8000), "http://127.0.0.1:8000/viewer/?lang=en")

    def test_live_fps(self):
        f = LiveFps(window_s=2.0)
        self.assertIsNone(f.add(0.0, 0))
        self.assertIsNone(f.add(0.2, 6))           # too short a window
        self.assertAlmostEqual(f.add(1.0, 30), 30.0)
        self.assertAlmostEqual(f.add(3.0, 150), 60.0, delta=0.1)  # old samples age out
        self.assertIsNone(f.add(3.1, 5))           # counter went backwards -> restart
        f.reset()
        self.assertIsNone(f.value())

    def test_stats_text(self):
        snap = {"frames": 12345, "events": 678, "backend": "dxcam", "encoder": "h264_nvenc", "masked_now": True}
        text, _ = stats_text("recording", snap, cfg={"capture_mode": "app", "app_exe": "a.exe"}, live_fps=29.94)
        self.assertIn("12,345 frames", text)
        self.assertIn("678 events", text)
        self.assertIn("29.9 fps", text)
        self.assertIn("dxcam/nvenc", text)
        self.assertIn("others masked", text)
        text, color = stats_text("recording", dict(snap, last_error="segment restarted"), cfg={})
        self.assertIn("⚠ segment restarted", text)
        self.assertEqual(color, gui.WARN)
        text, color = stats_text("auto_paused", snap, cfg={"capture_mode": "app", "app_exe": "SLDWORKS.exe",
                                                            "app_name": "SOLIDWORKS"})
        self.assertIn("switch back to SOLIDWORKS", text)
        self.assertEqual(color, gui.WARN)
        text, _ = stats_text("done", {}, cfg={}, packaged=True, out_name="rec-1",
                             report={"technical_errors": [], "delivery_missing": ["a", "b"]})
        self.assertIn("2 more", text)
        text, color = stats_text("done", {}, cfg={}, packaged=True, out_name="rec-1",
                                 report={"technical_errors": ["bad"], "delivery_missing": []})
        self.assertEqual(color, gui.WARN)
        text, color = stats_text("idle", {}, cfg={"fps": 60, "draw_cursor": False}, capture_excluded=False)
        self.assertIn("may appear", text)
        self.assertEqual(color, gui.WARN)
        text, color = stats_text("error", {}, cfg={}, error="x" * 500)
        self.assertLessEqual(len(text), 150)
        self.assertEqual(color, gui.RED)

    def test_msg_text_handles_bilingual_dicts(self):
        self.assertEqual(gui.msg_text({"zh": "中", "en": "EN"}), "EN")
        i18n.set_language("zh", persist=False)
        self.assertEqual(gui.msg_text({"zh": "中", "en": "EN"}), "中")
        self.assertEqual(gui.msg_text("plain"), "plain")

    def test_gui_args_pass_unknown_flags_to_recorder(self):
        a = parse_gui_args(["--lang", "zh", "--markov-delivery", "--backend", "gdigrab", "--ffmpeg", "F",
                            "--sync-latency-ms", "12", "--audio-device", "Stereo Mix"])
        self.assertEqual(a.lang, "zh")
        self.assertTrue(a.markov_delivery)
        self.assertEqual(a.backend, "gdigrab")
        self.assertEqual(a.extra, ["--sync-latency-ms", "12", "--audio-device", "Stereo Mix"])
        s = parse_gui_args(["--screenshot", "o.png", "--expand", "--state", "recording-demo"])
        self.assertEqual((s.screenshot, s.expand, s.state), ("o.png", True, "recording-demo"))


# ------------------------------------------------------------------ headless UI
class OverlayStateTest(_Env):
    def test_done_timer_remains_at_final_duration(self):
        ui = headless_ui("done", snap={"elapsed_ms": 12345})
        ui._tick()
        ui._tick()
        texts = [c.kwargs["text"] for c in ui.time_lbl.configure.call_args_list]
        self.assertEqual(texts[:2], ["00:12.3", "00:12.3"])

    def test_stop_button_enabled_only_while_recording(self):
        ui = headless_ui("done", snap={"elapsed_ms": 1})
        ui._sync_buttons("done")
        self.assertEqual(last_kwargs(ui.btn_stop)["state"], "disabled")
        ui.state = "recording"
        ui._refresh()
        kw = last_kwargs(ui.btn_stop)
        self.assertEqual(kw["state"], "normal")
        self.assertEqual(kw["text"], "■ Stop & package")
        self.assertEqual(last_kwargs(ui.btn_main)["text"], "⏸ Pause")

    def test_paused_follows_recorder(self):
        ui = headless_ui("recording", snap={"paused": True, "frames": 10})
        ui._refresh()
        self.assertEqual(ui.state, "paused")
        self.assertEqual(last_kwargs(ui.btn_main)["text"], "▶ Resume")

    def test_auto_paused_shows_app_and_offers_hold(self):
        ui = headless_ui("recording", snap={"paused": True, "auto_paused": True, "frames": 10,
                                            "capture_mode": "app", "target_exe": "SLDWORKS.exe"})
        ui._rec_cfg = dict(ui.cfg, capture_mode="app", app_exe="SLDWORKS.exe", app_name="SOLIDWORKS")
        ui._refresh()
        self.assertEqual(last_kwargs(ui.state_lbl)["text"], "Auto-paused: SOLIDWORKS not in front")
        self.assertEqual(last_kwargs(ui.btn_main)["text"], "⏸ Hold pause")
        self.assertEqual(last_kwargs(ui.btn_stop)["state"], "normal")
        self.assertEqual(last_kwargs(ui.mode_lbl)["text"], "App only: SOLIDWORKS")
        ui.on_main()
        ui._toggle_thread.join(2)
        ui.rec.toggle_pause.assert_called_once()

    def test_main_button_toggles_pause_while_recording(self):
        ui = headless_ui("recording", snap={"frames": 1})
        ui.on_main()
        ui._toggle_thread.join(2)
        ui.rec.toggle_pause.assert_called_once()

    def test_slow_recorder_lock_never_blocks_the_bar(self):
        release = threading.Event()

        def slow_snapshot():
            release.wait(3)
            return {"frames": 99, "elapsed_ms": 1000}

        ui = headless_ui("recording", snap={"frames": 1, "elapsed_ms": 500})
        ui._refresh()                                   # fast path: fresh value
        self.assertEqual(ui._last_snap.get("frames"), 1)
        ui.rec.snapshot = slow_snapshot                 # recorder busy switching segments
        started = time.perf_counter()
        ui._refresh()
        ui._refresh()
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(ui._last_snap.get("frames"), 1)  # last known value is shown meanwhile
        release.set()
        ui._snap_thread.join(2)
        ui._refresh()
        self.assertEqual(ui._last_snap.get("frames"), 99)

    def test_close_while_packaging_waits_instead_of_killing_worker(self):
        ui = headless_ui("packing")
        ui.on_close()
        self.assertTrue(ui._closing)
        ui.root.destroy.assert_not_called()
        ui._tick()
        ui.root.destroy.assert_not_called()
        ui.state = "done"
        ui._tick()
        ui.root.destroy.assert_called_once()

    def test_close_while_recording_finishes_gracefully(self):
        entered, release = threading.Event(), threading.Event()

        def slow_stop():
            entered.set()
            release.wait(2)

        ui = headless_ui("recording", snap={"frames": 1}, autopack=False)
        ui.rec.stop = Mock(side_effect=slow_stop)
        ui.thread = None
        ui.on_close()
        self.assertTrue(ui._closing)
        self.assertTrue(entered.wait(2))
        self.assertEqual(ui.state, "stopping")
        ui._tick()                                  # still finishing: must not close
        ui.root.destroy.assert_not_called()
        release.set()
        for _ in range(50):
            if ui.state == "done":
                break
            time.sleep(0.02)
        self.assertEqual(ui.state, "done")
        ui._tick()
        ui.root.destroy.assert_called_once()

    def test_result_row_appears_after_recording(self):
        ui = headless_ui("done", snap={"elapsed_ms": 5})
        ui.last_out = os.path.join(self.tmp.name, "rec-x")
        os.makedirs(ui.last_out)
        ui._refresh()
        ui.result_row.pack.assert_called_once()
        self.assertTrue(last_kwargs(ui.btn_export_rec)["state"] == "normal")
        ui.state = "recording"
        ui._refresh()
        ui.result_row.pack_forget.assert_called_once()

    def test_error_text_follows_language(self):
        ui = headless_ui()
        ui._set_error("坏了：{e}", "Broken: {e}", e="x")
        self.assertEqual(ui.error, "Broken: x")
        i18n.set_language("zh", persist=False)
        self.assertEqual(ui.error, "坏了：x")
        ui.error = "plain"
        self.assertEqual(ui.error, "plain")

    def test_static_texts_rerender_on_language_change(self):
        ui = headless_ui()
        widget = Mock()
        ui._tr(widget, "开始", "Start")
        self.assertEqual(widget.configure.call_args.kwargs["text"], "Start")
        i18n.set_language("zh", persist=False)
        for apply, _fn in ui._i18n_items:
            apply()
        self.assertEqual(widget.configure.call_args.kwargs["text"], "开始")
        ui.state = "idle"
        ui._refresh()
        self.assertEqual(last_kwargs(ui.state_lbl)["text"], "待机")

    def test_config_error_clears_when_settings_change(self):
        ui = headless_ui(capture_mode="app", app_exe="")
        ui._render_settings_texts = Mock()
        ui._fit = Mock()
        ui.start_recording()
        self.assertEqual(ui.state, "error")
        self.assertIn("Choose the app", ui.error)
        ui._settings_changed()
        self.assertEqual(ui.state, "idle")
        self.assertEqual(ui.error, "")

    def test_start_failure_cleans_empty_output_folder(self):
        ui = headless_ui(capture_mode="desktop")
        with patch.object(gui, "ffmpeg_available", return_value=True), \
                patch.object(gui, "parse_recorder_args", side_effect=ValueError("bad flag")):
            ui.start_recording()
        self.assertEqual(ui.state, "error")
        self.assertIn("bad flag", ui.error)
        data = os.environ["DSTUDIO_DATA_DIR"]
        self.assertEqual([d for d in os.listdir(data) if d.startswith("rec-")], [])

    def test_review_flags_reset_for_each_recording(self):
        ui = headless_ui(capture_mode="desktop")
        ui._session_info = {"engineer_id": "E1", "task_completed": True, "sync_verified": True,
                            "quality_reviewed": True, "originality_confirmed": True}
        seen = {}

        def fake_parse(argv):
            path = argv[argv.index("--session-info") + 1]
            import json
            with open(path, encoding="utf-8") as fh:
                seen.update(json.load(fh))
            raise ValueError("stop here")

        with patch.object(gui, "ffmpeg_available", return_value=True), \
                patch.object(gui, "parse_recorder_args", side_effect=fake_parse):
            ui.start_recording()
        self.assertEqual(seen["engineer_id"], "E1")
        self.assertTrue(seen["originality_confirmed"])
        for key in ("task_completed", "sync_verified", "quality_reviewed"):
            self.assertIs(seen[key], False, key)

    def test_missing_ffmpeg_is_reported_before_creating_anything(self):
        ui = headless_ui(capture_mode="desktop")
        with patch.object(gui, "ffmpeg_available", return_value=False):
            ui.start_recording()
        self.assertEqual(ui.state, "error")
        self.assertIn("FFmpeg", ui.error)


# ------------------------------------------------------------------ v0.2.1 review fixes
def wait_for_queue(ui, timeout=3.0):
    """Run what worker threads posted to the Tk queue (headless _tick)."""
    deadline = time.monotonic() + timeout
    while not ui._ui_queue and time.monotonic() < deadline:
        time.sleep(0.01)
    ui._drain_ui_queue()


HEADER_NOTE = i18n.both("本次核验视频头部帧数；正式导出另做全片解码检查",
                        "This check read the frame count from the video header; exports also decode the full video")
FPS_NOTE = i18n.both("帧率为 60 fps；Markov 样例为 30 fps，交付前请与客户确认",
                     "Frame rate is 60 fps; Markov samples are 30 fps - confirm with the customer before delivery")
SYNC_NOTE = i18n.both("同步字段校验不等于物理同步精度认证，仍需点击画面与日志实测",
                      "Sync field checks are not a physical sync-accuracy certification; still test clicks against the video")


def real_report(root, *, fps=30, policy_mask_in_video=False, decode=False):
    """``validate_dataset()`` of a synthetic one-clip desktop recording (ffprobe faked)."""
    from packager.validate import validate_dataset

    extra = []
    if policy_mask_in_video:
        # an older recorder only *marked* the password manager; the video kept running
        s0 = fx.BASE + 1000
        policy = dict(fx.MASK, trigger="policy_blocked_app", maskTrigger="policy_blocked_app",
                      maskDetail="keepass.exe", appName="KeePass", exeName="KeePass.exe")
        extra = [fx.ev("privacy_mask_start", s0 + 600, **policy), fx.key(s0 + 700, True), fx.key(s0 + 740, False),
                 fx.ev("privacy_mask_end", s0 + 1200, **policy)]
    _s0, n = fx.simple_session(root, fps=fps, extra_events=extra)
    with patch("subprocess.run", return_value=fx.fake_probe(1920, 1080, fps, n)), redirect_stdout(io.StringIO()):
        fx.package(root)
        return validate_dataset(root, decode=decode)


class WindowCloseTest(_Env):
    """Finding 7: WM_CLOSE / sign-out never bypass the stop + package path."""

    def test_close_requests_routed_through_on_close(self):
        ui = headless_ui()
        ui._install_window_handlers()
        protocols = {c.args[0]: c.args[1] for c in ui.root.protocol.call_args_list}
        self.assertEqual(protocols["WM_DELETE_WINDOW"], ui.on_close)
        self.assertEqual(protocols["WM_SAVE_YOURSELF"], ui.on_session_end)
        bound = [c.args[0] for c in ui.root.bind.call_args_list]
        self.assertIn("<Activate>", bound)
        self.assertIn("<FocusIn>", bound)

    def test_wm_close_while_recording_stops_and_packages_first(self):
        ui = headless_ui("recording", snap={"frames": 1}, autopack=False)
        ui.thread = None
        ui._install_window_handlers()
        handler = {c.args[0]: c.args[1] for c in ui.root.protocol.call_args_list}["WM_DELETE_WINDOW"]
        handler()                                   # what Alt+F4 / taskkill (no /F) do
        ui.root.destroy.assert_not_called()
        self.assertTrue(ui._closing)
        for _ in range(100):
            if ui.state == "done":
                break
            time.sleep(0.02)
        ui.rec.stop.assert_called()
        ui._tick()
        ui.root.destroy.assert_called_once()

    def test_session_end_stops_recorder_synchronously(self):
        stopped = threading.Event()

        def slow_stop():
            time.sleep(0.2)
            stopped.set()

        ui = headless_ui("recording", snap={"frames": 1})
        ui.rec.stop = Mock(side_effect=slow_stop)
        ui.on_session_end()
        self.assertTrue(stopped.is_set())          # returned only after the recorder finalized
        self.assertEqual(ui.state, "stopping")
        self.assertTrue(ui._closing)               # Restart Manager: exit once packaged
        ui.root.destroy.assert_not_called()        # ... but not before

    def test_session_end_closes_the_bar_after_packaging(self):
        # Restart Manager (ENDSESSION_CLOSEAPP) never sends WM_CLOSE: the bar must
        # still exit by itself once the normal stop -> package path is done
        ui = headless_ui("recording", snap={"frames": 1}, autopack=False)
        ui.thread = None
        ui.on_session_end()
        ui.rec.stop.assert_called_once()
        ui._tick()
        ui.root.destroy.assert_not_called()        # still "stopping"
        self.assertEqual(last_kwargs(ui.state_lbl)["text"], "Finishing, then closing…")
        ui._after_stopped()                        # what _run_recorder does when run() returns
        self.assertEqual(ui.state, "done")
        ui._tick()
        ui.root.destroy.assert_called_once()

    def test_session_end_gives_up_after_timeout_and_ignores_idle(self):
        release = threading.Event()
        ui = headless_ui("recording", snap={"frames": 1})
        ui.rec.stop = Mock(side_effect=lambda: release.wait(5))
        ui.SESSION_END_STOP_S = 0.1
        started = time.monotonic()
        ui.on_session_end()
        self.assertLess(time.monotonic() - started, 2)
        release.set()
        self.assertTrue(ui._closing)
        idle = headless_ui()
        idle.on_session_end()                      # nothing to stop: no error, no state change
        self.assertEqual(idle.state, "idle")
        self.assertFalse(idle._closing)
        self.assertFalse(idle._session_ending)

    def test_session_end_while_packaging_or_exporting_closes_when_done(self):
        # WM_SAVE_YOURSELF a moment after F10: nothing to stop, but the bar must still exit
        for busy in ("packing", "exporting"):
            ui = headless_ui(busy)
            ui.on_session_end()
            self.assertTrue(ui._closing, busy)
            self.assertTrue(ui._session_ending, busy)
            ui._tick()
            ui.root.destroy.assert_not_called()    # the worker is still writing
            ui.state = "done"
            ui._tick()
            ui.root.destroy.assert_called_once()

    def test_session_end_close_never_shows_a_dialog(self):
        server = Mock()
        for final in ("done", "error"):
            ui = headless_ui("recording", snap={"frames": 1}, autopack=False)
            ui.thread = None
            ui._server = server                    # the annotator is running
            with patch.object(gui, "messagebox") as box, patch.object(gui, "_load_serve_module",
                                                                      side_effect=ImportError("no serve")):
                ui.on_session_end()
                if final == "done":
                    ui._after_stopped()
                else:
                    ui._fail("Packaging failed: disk full")
                ui._tick()
            self.assertEqual(box.mock_calls, [], final)          # nobody answers dialogs at sign-out
            ui.root.destroy.assert_called_once()
            self.assertIsNone(ui._server)                         # the annotator was stopped
        server.shutdown.assert_called()

    def test_normal_close_still_asks_about_the_annotator(self):
        ui = headless_ui("done")
        ui._server = Mock()
        with patch.object(gui, "messagebox") as box:
            box.askyesno.return_value = False
            ui.on_close()
        box.askyesno.assert_called_once()
        ui.root.destroy.assert_not_called()
        self.assertIsNotNone(ui._server)


class MarkovDeliveryDefaultTest(_Env):
    """Finding 8: Markov CAD export is the default everywhere; legacy is a ⚙ option."""

    def test_default_flag_and_settings(self):
        ui = headless_ui("done")
        self.assertTrue(ui._markov())
        gui.update_settings(markov_delivery=False)
        self.assertFalse(headless_ui()._markov())
        ui = object.__new__(OverlayUI)
        ui.gui_args = parse_gui_args(["--markov-delivery"])
        ui.screenshot = False
        ui._init_state()
        self.assertTrue(ui._markov())              # flag overrides the saved setting for this run
        ui.gui_args = parse_gui_args(["--legacy-delivery"])
        gui.update_settings(markov_delivery=True)
        ui._init_state()
        self.assertFalse(ui._markov())
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parse_gui_args(["--markov-delivery", "--legacy-delivery"])

    def test_button_and_legacy_toggle(self):
        ui = headless_ui("done", snap={"elapsed_ms": 1})
        ui.last_out = os.path.join(self.tmp.name, "rec-1")
        os.makedirs(ui.last_out)
        ui._refresh()
        self.assertEqual(last_kwargs(ui.btn_export)["text"], "Verify & export Markov CAD")
        ui._persist = True
        ui._on_legacy(True)
        self.assertFalse(ui._markov())
        self.assertFalse(gui.load_settings()["markov_delivery"])
        self.assertEqual(last_kwargs(ui.btn_export)["text"], "Verify & export legacy delivery")

    def test_packaging_export_and_form_use_the_same_format(self):
        ui = headless_ui("done")
        ui.last_out = os.path.join(self.tmp.name, "rec-2")
        os.makedirs(os.path.join(ui.last_out, "raw"))
        ui._post = lambda fn: None
        ui._run_task = lambda _busy, fn, _title: fn()
        ui._ask_export_parent = lambda _title: os.path.join(self.tmp.name, "out")
        for markov in (True, False):
            ui.cfg["markov_delivery"] = markov
            with patch("packager.build.build") as build:
                self.assertTrue(ui._package_now())
            self.assertIs(build.call_args.kwargs["markov"], markov)
            with patch("packager.export_cad.export_cad", return_value="X") as export:
                ui.export_delivery()
            self.assertIs(export.call_args.kwargs["markov"], markov)
            with patch("recorder.session_form.edit_session") as form:
                ui.edit_session()
            self.assertIs(form.call_args.kwargs["markov"], markov)

    def test_session_form_hint_per_format(self):
        self.assertIn("task_files/", session_form.hint_text())
        legacy = session_form.hint_text(False)
        self.assertIn("Legacy", legacy)
        self.assertIn("gold.zip", legacy)
        self.assertNotIn("task_files/", legacy)


class FullScreenTextTest(_Env):
    """Finding 9 / K3: Full screen = the main display, for every backend."""

    def test_full_screen_texts_say_main_display(self):
        ui = headless_ui()
        self.assertIn("main display", ui._mode_tooltip())
        self.assertNotIn("whole screen", ui._mode_tooltip())
        ui.source_override = "monitor:1"
        self.assertIn("monitor:1", ui._mode_tooltip())
        i18n.set_language("zh", persist=False)
        self.assertEqual(mode_chip({"capture_mode": "desktop"})[0], "全屏 · 主显示器")
        ui.source_override = ""
        self.assertIn("主显示器", ui._mode_tooltip())


class ValidationWarningsTest(_Env):
    """Finding 22: warnings (60 fps, multi-monitor, no cursor, cuts) reach the user."""

    def test_report_warnings_drop_the_check_scope_note(self):
        report = {"warnings": [HEADER_NOTE, FPS_NOTE, "plain note"]}
        self.assertEqual(gui.report_warnings(report), [FPS_NOTE, "plain note"])
        self.assertEqual(gui.report_warnings({"warnings": [HEADER_NOTE["en"]]}), [])
        self.assertEqual(gui.report_warnings(None), [])
        self.assertEqual(gui.report_warnings({"warnings": "single"}), ["single"])

    def test_done_line_counts_notes(self):
        report = {"technical_errors": [], "delivery_missing": [], "warnings": [HEADER_NOTE, FPS_NOTE]}
        text, color = stats_text("done", {}, cfg={}, packaged=True, out_name="rec-1", report=report)
        self.assertTrue(text.endswith("⚠ 1 note(s)"), text)
        self.assertEqual(color, gui.OK)
        text, _ = stats_text("done", {}, cfg={}, packaged=True, out_name="rec-1",
                             report=dict(report, delivery_missing=["a"]))
        self.assertIn("1 more item(s) · ⚠ 1 note(s)", text)
        text, _ = stats_text("done", {}, cfg={}, packaged=True, out_name="rec-1",
                             report={"warnings": [HEADER_NOTE]})
        self.assertNotIn("note", text)
        i18n.set_language("zh", persist=False)
        text, _ = stats_text("done", {}, cfg={}, packaged=True, out_name="rec-1", report=report)
        self.assertIn("1 条提示", text)

    def test_sync_disclaimer_is_not_a_note(self):
        # validate.py adds it to every report: it must not make every recording show "1 note"
        report = {"technical_errors": [], "delivery_missing": [], "warnings": [HEADER_NOTE, SYNC_NOTE]}
        self.assertEqual(gui.report_warnings(report), [])
        self.assertEqual(gui.report_warnings({"warnings": [SYNC_NOTE["zh"], FPS_NOTE]}), [FPS_NOTE])
        self.assertEqual(gui.report_reminders(report), [SYNC_NOTE])
        text, color = stats_text("done", {}, cfg={}, packaged=True, out_name="rec-1", report=report)
        self.assertNotIn("note", text)
        self.assertEqual(color, gui.OK)
        ui = headless_ui("done")
        ui._packaged = True
        ui.last_out = os.path.join(self.tmp.name, "rec-1")
        ui._delivery_report = report
        self.assertNotIn("Notes", ui._stats_tooltip())
        self.assertNotIn("Sync field", ui._stats_tooltip())

    def test_export_dialog_shows_the_sync_reminder_uncounted(self):
        text = gui.export_done_text("Exported to:", "D:/out", {"warnings": [SYNC_NOTE]})
        self.assertNotIn("Notes", text)
        self.assertIn("\n\nSync field checks are not a physical sync-accuracy certification", text)
        self.assertTrue(text.endswith("Open the folder now?"))
        text = gui.export_done_text("Exported to:", "D:/out", {"warnings": [FPS_NOTE, SYNC_NOTE]})
        self.assertIn("Notes (check before delivery):\n• Frame rate is 60 fps", text)
        self.assertNotIn("• Sync field", text)
        self.assertLess(text.index("Frame rate"), text.index("Sync field"))
        i18n.set_language("zh", persist=False)
        self.assertIn("同步字段校验不等于物理同步精度认证", gui.export_done_text("导出到：", "D:/out", {"warnings": [SYNC_NOTE]}))

    def test_real_validation_report_of_a_clean_recording_has_no_note_count(self):
        for decode in (False, True):
            root = os.path.join(self.tmp.name, f"clean-{decode}")
            os.makedirs(root)
            report = real_report(root, decode=decode)
            self.assertTrue(report["technical_ok"], report["technical_errors"])
            self.assertTrue(report["warnings"], "validate.py no longer adds its standing notes?")
            self.assertEqual(gui.report_warnings(report), [])
            for lang, word in (("en", "note"), ("zh", "条提示")):
                i18n.set_language(lang, persist=False)
                text, _ = stats_text("done", {}, cfg={}, packaged=True, out_name="rec-1", report=report)
                self.assertNotIn(word, text)
                self.assertNotIn("⚠", text)
            i18n.set_language("en", persist=False)
        root = os.path.join(self.tmp.name, "clean-60")
        os.makedirs(root)
        report = real_report(root, fps=60)
        self.assertEqual(len(gui.report_warnings(report)), 1, report["warnings"])   # the 60 fps note
        text, _ = stats_text("done", {}, cfg={}, packaged=True, out_name="rec-1", report=report)
        self.assertIn("⚠ 1 note(s)", text)

    def test_privacy_interval_still_in_the_video_is_not_called_exportable(self):
        root = os.path.join(self.tmp.name, "rec-old")
        os.makedirs(root)
        report = real_report(root, policy_mask_in_video=True)
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertEqual(report["uncut_privacy_masks"], 1)
        self.assertEqual(gui.uncut_privacy_masks(report), 1)
        text, color = stats_text("done", {}, cfg={}, packaged=True, out_name="rec-old", report=report)
        self.assertNotIn("exportable", text)
        self.assertIn("1 privacy interval(s) still in the video, cannot export", text)
        self.assertEqual(color, gui.WARN)
        i18n.set_language("zh", persist=False)
        text, _ = stats_text("done", {}, cfg={}, packaged=True, out_name="rec-old", report=report)
        self.assertNotIn("可导出", text)
        self.assertIn("1 段隐私时段仍在视频中，不能导出", text)
        # the hover text says that *both* exports are refused, under its own heading
        i18n.set_language("en", persist=False)
        ui = headless_ui("done")
        ui._packaged = True
        ui.last_out = root
        ui._delivery_report = report
        tip = ui._stats_tooltip()
        details = tip.split("\n\n", 1)[1]
        self.assertTrue(details.startswith("Cannot export:\n• 1 privacy-marked interval(s)"), details)
        self.assertIn("Export recording and the CAD export are refused", details)
        self.assertEqual(details.count("privacy-marked interval"), 1, details)   # not repeated under CAD
        i18n.set_language("zh", persist=False)
        details = ui._stats_tooltip().split("\n\n", 1)[1]
        self.assertTrue(details.startswith("不能导出：\n• 1 段隐私标记时段"), details)
        self.assertIn("“导出录制”和 CAD 导出都会被拒绝", details)
        self.assertEqual(details.count("隐私标记时段"), 1, details)
        i18n.set_language("en", persist=False)
        self.assertNotIn("Cannot export", gui.report_details({"delivery_missing": ["a"], "uncut_privacy_masks": 0}))
        clean = {"technical_errors": [], "delivery_missing": ["a"], "uncut_privacy_masks": 0}
        self.assertIn("recording exportable", stats_text("done", {}, cfg={}, packaged=True, out_name="r",
                                                         report=clean)[0])
        for bad in (None, "x", -2, {"uncut_privacy_masks": "x"}):
            self.assertEqual(gui.uncut_privacy_masks(bad if isinstance(bad, dict) else {"uncut_privacy_masks": bad}), 0)
        self.assertEqual(gui.uncut_privacy_masks(None), 0)

    def test_tooltip_lists_bilingual_messages_in_current_language(self):
        ui = headless_ui("done")
        ui._packaged = True
        ui.last_out = os.path.join(self.tmp.name, "rec-3")
        ui._delivery_report = {"technical_errors": [],
                               "delivery_missing": [i18n.both("缺少任务集", "Task set missing")],
                               "warnings": [FPS_NOTE, HEADER_NOTE]}
        tip = ui._stats_tooltip()
        self.assertIn("Still needed for the CAD delivery:\n• Task set missing", tip)
        self.assertIn("Notes (check before delivery):\n• Frame rate is 60 fps", tip)
        self.assertNotIn("video header", tip)
        i18n.set_language("zh", persist=False)
        tip = ui._stats_tooltip()
        self.assertIn("• 缺少任务集", tip)
        self.assertIn("• 帧率为 60 fps", tip)

    def test_export_done_dialog_lists_notes(self):
        text = gui.export_done_text("Exported to:", "D:/out", {"warnings": [FPS_NOTE]})
        self.assertIn("D:/out", text)
        self.assertIn("• Frame rate is 60 fps", text)
        self.assertTrue(text.endswith("Open the folder now?"))
        self.assertNotIn("Notes", gui.export_done_text("Exported to:", "D:/out", {"warnings": [HEADER_NOTE]}))
        ui = headless_ui("done")
        with patch.object(gui, "messagebox") as box, patch.object(gui, "open_path") as opener:
            box.askyesno.return_value = True
            ui._export_done("D:/out", "Exported to:", {"warnings": [FPS_NOTE]})
        self.assertIn("Frame rate is 60 fps", box.askyesno.call_args.args[1])
        opener.assert_called_once_with("D:/out")


class AnnotatorLanguageTest(_Env):
    """Finding 32 / K7: the annotator opens in the bar's language."""

    def test_viewer_url_carries_lang_before_fragment(self):
        self.assertEqual(viewer_url(8000, "rec 1"), "http://127.0.0.1:8000/viewer/?lang=en#rec%201")
        i18n.set_language("zh", persist=False)
        self.assertEqual(viewer_url(8001, "rec-1"), "http://127.0.0.1:8001/viewer/?lang=zh#rec-1")
        self.assertEqual(viewer_url(8001, lang="en"), "http://127.0.0.1:8001/viewer/?lang=en")
        self.assertEqual(viewer_url(8001, lang="fr"), "http://127.0.0.1:8001/viewer/?lang=en")

    def test_open_annotator_uses_language_url(self):
        ui = headless_ui("done")
        ui.last_out = os.path.join(self.tmp.name, "rec-4")
        os.makedirs(ui.last_out)
        ui._packaged = True
        ui._ensure_viewer = lambda dataset: 8123
        i18n.set_language("zh", persist=False)
        with patch.object(gui.webbrowser, "open") as browser:
            ui.open_annotator()
            wait_for_queue(ui)
        browser.assert_called_once_with("http://127.0.0.1:8123/viewer/?lang=zh#rec-4")


class DeliverySummaryRefreshTest(_Env):
    """Finding 33: narration.json saved from the annotator refreshes the done line."""

    def make_done(self):
        ui = headless_ui("done")
        ui.last_out = os.path.join(self.tmp.name, "rec-5")
        os.makedirs(ui.last_out)
        ui._packaged = True
        narration = os.path.join(ui.last_out, "narration.json")
        ui._delivery_report = {"technical_errors": [], "delivery_missing": ["narration missing"]}
        ui._report_sig, ui._report_markov = gui.narration_signature(ui.last_out), True
        return ui, narration

    def test_unchanged_narration_is_not_revalidated(self):
        ui, _ = self.make_done()
        with patch("packager.validate.validate_dataset") as validate:
            self.assertTrue(ui.refresh_report())
            wait_for_queue(ui)
        validate.assert_not_called()
        self.assertFalse(ui._report_busy)
        self.assertEqual(ui._delivery_report["delivery_missing"], ["narration missing"])

    def test_new_narration_revalidates_on_a_worker(self):
        ui, narration = self.make_done()
        with open(narration, "w", encoding="utf-8") as fh:
            fh.write('{"summary": "s", "annotations": []}')
        fresh = {"technical_errors": [], "delivery_missing": [], "warnings": []}
        with patch("packager.validate.validate_dataset", return_value=fresh) as validate:
            self.assertTrue(ui.refresh_report())
            self.assertFalse(ui.refresh_report())  # one check at a time
            wait_for_queue(ui)
        self.assertEqual(validate.call_args.kwargs, {"decode": False, "task_set": None, "markov": True})
        self.assertIs(ui._delivery_report, fresh)
        self.assertEqual(ui._report_sig, gui.narration_signature(ui.last_out))
        ui._refresh()
        self.assertIn("ready to review", last_kwargs(ui.stats)["text"])

    def test_focus_and_tick_trigger_the_check_only_when_done(self):
        ui, narration = self.make_done()
        with patch.object(ui, "refresh_report") as refresh:
            ui._on_bar_focus()
            refresh.assert_called_once()
            ui.REPORT_CHECK_TICKS = 2
            ui._tick()
            ui._tick()
            self.assertEqual(refresh.call_count, 2)
        ui.state = "recording"
        self.assertFalse(ui.refresh_report())

    def test_stale_result_is_dropped_after_a_newer_report(self):
        ui, narration = self.make_done()
        with open(narration, "w", encoding="utf-8") as fh:
            fh.write("{}")
        release = threading.Event()

        def slow_validate(*_a, **_k):
            release.wait(3)
            return {"delivery_missing": ["stale"]}

        with patch("packager.validate.validate_dataset", side_effect=slow_validate):
            ui.refresh_report()
            newer = {"delivery_missing": [], "technical_errors": []}
            with open(os.path.join(ui.last_out, "validation.json"), "w", encoding="utf-8") as fh:
                import json
                json.dump(newer, fh)
            ui._load_report(markov=True)          # an export finished meanwhile
            release.set()
            wait_for_queue(ui)
        self.assertEqual(ui._delivery_report, newer)
        self.assertFalse(ui._report_busy)

    def test_recording_only_report_is_rechecked_in_the_chosen_format(self):
        ui, _ = self.make_done()
        with open(os.path.join(ui.last_out, "validation.json"), "w", encoding="utf-8") as fh:
            fh.write('{"delivery_missing": ["Task set is missing gold.zip"], "technical_errors": []}')
        ui._load_report(markov=False)              # what "Export recording" leaves behind
        self.assertIs(ui._report_markov, False)
        with patch("packager.validate.validate_dataset", return_value={"delivery_missing": []}) as validate:
            ui._tick()                              # first done tick re-checks right away
            wait_for_queue(ui)
        self.assertIs(validate.call_args.kwargs["markov"], True)
        self.assertEqual(ui._delivery_report, {"delivery_missing": []})

    def test_format_switch_rechecks(self):
        ui, _ = self.make_done()
        with patch("packager.validate.validate_dataset", return_value={"delivery_missing": []}) as validate:
            ui._on_legacy(True)
            wait_for_queue(ui)
        self.assertIs(validate.call_args.kwargs["markov"], False)
        self.assertIs(ui._report_markov, False)


class PrivacyPauseTest(_Env):
    """K4: a password manager in front pauses Full screen recordings too."""

    def test_privacy_pause_texts(self):
        self.assertTrue(gui.is_privacy_pause({"auto_paused": True, "capture_mode": "desktop"}))
        self.assertFalse(gui.is_privacy_pause({"auto_paused": True, "capture_mode": "app"}))
        self.assertFalse(gui.is_privacy_pause({"auto_paused": False, "capture_mode": "desktop"}))
        ui = headless_ui("recording", snap={"paused": True, "auto_paused": True, "frames": 3,
                                            "capture_mode": "desktop"})
        ui._refresh()
        self.assertEqual(last_kwargs(ui.state_lbl)["text"], "Privacy pause: password manager in front")
        self.assertIn("password manager leaves the front", last_kwargs(ui.stats)["text"])
        self.assertEqual(last_kwargs(ui.btn_main)["text"], "⏸ Hold pause")
        # every non-app source is the blocklist; capture_mode comes from the snapshot first
        for mode in ("desktop", "monitor", "region", "window"):
            self.assertTrue(gui.is_privacy_pause({"auto_paused": True, "capture_mode": mode}, {"capture_mode": "app"}))
        self.assertTrue(gui.is_privacy_pause({"auto_paused": True}, {"capture_mode": "desktop"}))
        i18n.set_language("zh", persist=False)
        ui._refresh()
        self.assertEqual(last_kwargs(ui.state_lbl)["text"], "隐私暂停：密码管理器在前台")
        self.assertIn("密码管理器离开前台后自动继续", last_kwargs(ui.stats)["text"])

    def test_app_mode_auto_pause_keeps_the_app_text(self):
        ui = headless_ui("recording", snap={"paused": True, "auto_paused": True, "capture_mode": "app",
                                            "auto_pause_reason": "app"})
        ui._rec_cfg = dict(ui.cfg, capture_mode="app", app_exe="SLDWORKS.exe", app_name="SOLIDWORKS")
        ui._refresh()
        self.assertEqual(last_kwargs(ui.state_lbl)["text"], "Auto-paused: SOLIDWORKS not in front")
        self.assertIn("switch back to SOLIDWORKS to resume", last_kwargs(ui.stats)["text"])
        i18n.set_language("zh", persist=False)
        ui._refresh()
        self.assertEqual(last_kwargs(ui.state_lbl)["text"], "自动暂停：SOLIDWORKS 不在前台")
        self.assertIn("切回 SOLIDWORKS 即自动继续", last_kwargs(ui.stats)["text"])

    def test_recorder_reason_decides_in_any_mode(self):
        # K4: a password manager in front is a privacy pause in App-only mode too
        app = {"capture_mode": "app"}
        self.assertTrue(gui.is_privacy_pause({"auto_paused": True, "capture_mode": "app",
                                              "auto_pause_reason": "policy"}, app))
        self.assertFalse(gui.is_privacy_pause({"auto_paused": True, "capture_mode": "app",
                                               "auto_pause_reason": "app"}, app))
        self.assertTrue(gui.is_privacy_pause({"auto_paused": True, "capture_mode": "desktop",
                                              "auto_pause_reason": "policy"}))
        self.assertFalse(gui.is_privacy_pause({"auto_paused": False, "auto_pause_reason": "policy"}))
        # older recorders send no reason: the capture mode decides
        self.assertFalse(gui.is_privacy_pause({"auto_paused": True, "auto_pause_reason": None}, app))
        self.assertTrue(gui.is_privacy_pause({"auto_paused": True, "auto_pause_reason": None}, {}))
        ui = headless_ui("recording", snap={"paused": True, "auto_paused": True, "frames": 3, "capture_mode": "app",
                                            "auto_pause_reason": "policy", "target_exe": "SLDWORKS.exe"})
        ui._rec_cfg = dict(ui.cfg, capture_mode="app", app_exe="SLDWORKS.exe", app_name="SOLIDWORKS")
        ui._refresh()
        self.assertEqual(last_kwargs(ui.state_lbl)["text"], "Privacy pause: password manager in front")
        stats = last_kwargs(ui.stats)["text"]
        self.assertIn("resumes when the password manager leaves the front", stats)
        self.assertNotIn("switch back to", stats)
        self.assertEqual(last_kwargs(ui.mode_lbl)["text"], "App only: SOLIDWORKS")   # still App-only
        i18n.set_language("zh", persist=False)
        ui._refresh()
        self.assertEqual(last_kwargs(ui.state_lbl)["text"], "隐私暂停：密码管理器在前台")
        stats = last_kwargs(ui.stats)["text"]
        self.assertIn("密码管理器离开前台后自动继续", stats)
        self.assertNotIn("切回", stats)


class RecorderExplanationTest(_Env):
    """snapshot['last_error'] (e.g. a resume refused because the display changed) stays visible."""

    DISPLAY = i18n.both("录制的显示器已断开或分辨率/位置已改变（(0, 0, 1920, 1080) -> None），为保证所有片段画面一致暂不继续录制",
                        "The recorded display was disconnected or changed resolution/position ((0, 0, 1920, 1080) -> "
                        "None); recording does not continue so every clip keeps the same picture")

    def test_shown_in_every_active_state(self):
        app = {"capture_mode": "app", "app_exe": "SLDWORKS.exe", "app_name": "SOLIDWORKS"}
        cases = (("recording", {}, {}), ("paused", {"paused": True}, {}),
                 ("auto_paused", {"paused": True, "auto_paused": True, "capture_mode": "desktop"}, {}),
                 ("auto_paused", {"paused": True, "auto_paused": True, "capture_mode": "app"}, app))
        for key, extra, cfg in cases:
            snap = dict(extra, frames=10, events=3, last_error=self.DISPLAY)
            text, color = stats_text(key, snap, cfg=cfg)
            self.assertIn("⚠ The recorded display was disconnected", text, key)
            self.assertNotIn("F10", text, key)          # the explanation replaces the hotkey hint
            self.assertEqual(color, gui.WARN, key)
            text, _ = stats_text(key, dict(snap, last_error=""), cfg=cfg)
            self.assertNotIn("⚠", text, key)
        i18n.set_language("zh", persist=False)
        text, _ = stats_text("auto_paused", {"auto_paused": True, "capture_mode": "desktop", "last_error": self.DISPLAY},
                             cfg={})
        self.assertIn("⚠ 录制的显示器已断开", text)
        self.assertIn("密码管理器离开前台后自动继续", text)

    # what recorder._check_overlays sets while an unknown overlay blacks out the video
    OVERLAY = {"en": "An unknown full-screen see-through overlay (dimmer.exe wm.exe) covers the screen, so the "
                     "whole video is blacked out for privacy; if it shows nothing private, allow it with "
                     "--app-overlay-allow dimmer.exe wm.exe",
               "zh": "检测到未知的全屏透明覆盖层（dimmer.exe wm.exe），为保护隐私整个画面被遮黑；如确认它不显示隐私内容，"
                     "可用 --app-overlay-allow dimmer.exe wm.exe 放行"}

    def test_overlay_explanation_is_short_first_and_points_to_settings(self):
        self.assertEqual(gui.overlay_problem(self.OVERLAY["en"]), ["dimmer.exe", "wm.exe"])
        self.assertEqual(gui.overlay_problem(self.OVERLAY["zh"]), ["dimmer.exe", "wm.exe"])
        self.assertIsNone(gui.overlay_problem("… --app-overlay-allow"))       # program not identified
        self.assertIsNone(gui.overlay_problem(self.DISPLAY))
        self.assertIsNone(gui.overlay_problem(""))
        snap = {"frames": 12345, "events": 678, "backend": "dxcam", "encoder": "libx264", "masked_now": True,
                "capture_mode": "app", "last_error": self.OVERLAY["en"]}
        text, color = stats_text("recording", snap, cfg={"capture_mode": "app"}, live_fps=29.9)
        # first, so the narrow bar never cuts it off; counters follow
        self.assertTrue(text.startswith("⚠ Overlay dimmer.exe, wm.exe blacks out (part of) the video · allow it in ⚙"), text)
        self.assertIn("12,345 frames", text)
        self.assertNotIn("--app-overlay-allow", text)
        self.assertEqual(color, gui.WARN)
        ui = headless_ui("recording", snap=snap)
        ui._refresh()
        self.assertTrue(last_kwargs(ui.stats)["text"].startswith("⚠ Overlay dimmer.exe"))
        tip = ui._stats_tooltip()
        self.assertTrue(tip.startswith(self.OVERLAY["en"]))
        self.assertIn("enter dimmer.exe, wm.exe under ⚙ → Allowed overlays", tip)
        i18n.set_language("zh", persist=False)
        text, _ = stats_text("recording", dict(snap, last_error=self.OVERLAY["zh"]), cfg={"capture_mode": "app"})
        self.assertTrue(text.startswith("⚠ 覆盖层 dimmer.exe, wm.exe 使（部分）画面变黑 · 可在 ⚙ 中放行"), text)
        self.assertIn("“放行覆盖层”中填入 dimmer.exe, wm.exe", gui.problem_details(self.OVERLAY["zh"]))
        # program not identified: show the recorder's own text, no ⚙ advice that cannot work
        self.assertEqual(gui.problem_text("x --app-overlay-allow"), "⚠ x --app-overlay-allow")
        self.assertNotIn("⚙", gui.problem_details("x --app-overlay-allow"))

    def test_tooltip_and_refresh_show_the_full_text(self):
        message = self.DISPLAY["en"]
        ui = headless_ui("recording", snap={"paused": True, "auto_paused": True, "capture_mode": "desktop",
                                            "frames": 5, "last_error": message})
        ui._refresh()
        self.assertIn("⚠ The recorded display", last_kwargs(ui.stats)["text"])
        self.assertEqual(ui._stats_tooltip(), message)
        ui = headless_ui("recording", snap={"frames": 5, "last_error": self.DISPLAY})
        ui._refresh()
        self.assertIn("⚠ The recorded display", last_kwargs(ui.stats)["text"])
        self.assertEqual(ui._stats_tooltip(), message)


class OpenDataTest(_Env):
    """K8: ``--open-data`` (Start-menu Recordings shortcut) opens the folder and exits."""

    def test_open_data_opens_folder_and_exits(self):
        with patch.object(gui, "open_path") as opener, patch.object(i18n, "safe_stdio") as safe:
            self.assertEqual(gui.main(["--open-data"]), 0)
        safe.assert_called_once_with()             # leaves the test runner's streams alone
        opener.assert_called_once_with(os.environ["DSTUDIO_DATA_DIR"])
        self.assertTrue(os.path.isdir(os.environ["DSTUDIO_DATA_DIR"]))


class OverlayAllowSettingTest(_Env):
    """Finding 25: ⚙ → App only → Allowed overlays (settings key app_overlay_allow)."""

    def test_field_cleans_saves_and_reaches_the_recorder(self):
        ui = headless_ui(capture_mode="app", app_exe="SLDWORKS.exe")
        ui._persist = True
        jobs = []
        ui.root.after = Mock(side_effect=lambda _ms, fn: jobs.append(fn) or f"job{len(jobs)}")
        ui.e_overlay = Mock()
        ui.e_overlay.get.return_value = " Dimmer.exe; wm.exe, Dimmer.exe ,, "
        ui._on_overlay_allow()
        self.assertEqual(ui.cfg["app_overlay_allow"], ["Dimmer.exe", "wm.exe"])
        ui._on_overlay_allow()                      # unchanged text: no second save scheduled
        self.assertEqual(len(jobs), 1)
        jobs[-1]()                                  # the debounced save
        self.assertEqual(gui.load_settings()["app_overlay_allow"], ui.cfg["app_overlay_allow"])
        ui.e_overlay.get.return_value = "dimmer.exe"
        ui._on_overlay_allow()
        ui.destroy()                                # closed before the debounce fired: still saved
        self.assertEqual(gui.load_settings()["app_overlay_allow"], ["dimmer.exe"])
        seen = {}

        def fake_parse(argv):
            seen["argv"] = argv
            raise ValueError("stop here")

        with patch.object(gui, "ffmpeg_available", return_value=True), \
                patch.object(gui, "parse_recorder_args", side_effect=fake_parse):
            ui.start_recording()
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--app-overlay-allow") + 1], "dimmer.exe")
        self.assertEqual(headless_ui().cfg["app_overlay_allow"], ["dimmer.exe"])   # next start

    def test_field_belongs_to_app_only_mode(self):
        ui = headless_ui(capture_mode="desktop")
        ui.overlay_lbl, ui.overlay_cell = Mock(), Mock()
        ui._show_overlay_row(False)
        ui.overlay_cell.grid_remove.assert_called_once()
        ui._show_overlay_row(True)
        ui.overlay_lbl.grid.assert_called_once()


def make_recording(data, name, *, status="recording", segments=True, metadata=False, capture=True,
                   frames=30, validation=None):
    """A minimal rec-* folder for the crash-recovery scan."""
    folder = os.path.join(data, name)
    raw = os.path.join(folder, "raw")
    os.makedirs(raw, exist_ok=True)
    if capture:
        with open(os.path.join(raw, "capture.json"), "w", encoding="utf-8") as fh:
            fh.write('{"status": "%s"}' % status)
    if segments:
        with open(os.path.join(raw, "segments.json"), "w", encoding="utf-8") as fh:
            fh.write('[{"index": 0, "file": "seg_000.mp4", "frame_count": %d}]' % frames if frames else "[]")
    if metadata:
        with open(os.path.join(folder, "metadata.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
    if metadata if validation is None else validation:     # packager.build writes validation.json last
        with open(os.path.join(folder, "validation.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
    return folder


def fake_package(fail=()):
    """``gui.package_recording`` stand-in: writes what the packager leaves behind."""
    import json

    def package(out, *, title="", session_info=None, markov=True):
        if os.path.basename(out) in fail:
            raise SystemExit("segments.json contains no video frames; nothing to package")
        with open(os.path.join(out, "metadata.json"), "w", encoding="utf-8") as fh:
            json.dump({"total_duration_ms": 65400, "total_frames": 1962, "event_count": 321}, fh)
        with open(os.path.join(out, "validation.json"), "w", encoding="utf-8") as fh:
            json.dump({"technical_ok": True, "technical_errors": [], "delivery_missing": [], "warnings": []}, fh)
    return Mock(side_effect=package)


class CrashRecoveryTest(_Env):
    """K11: recordings that never finished are offered for packaging at start-up."""

    def setUp(self):
        super().setUp()
        self.data = os.environ["DSTUDIO_DATA_DIR"]
        os.makedirs(self.data)

    def test_scan_lists_only_unfinished_recordings_nobody_records_into(self):
        crashed = make_recording(self.data, "rec-20261101-090000")
        unpacked = make_recording(self.data, "rec-20261101-100000", status="completed")    # packaging never ran
        no_capture = make_recording(self.data, "rec-20261101-110000", capture=False)
        make_recording(self.data, "rec-20261101-120000", metadata=True)                    # already recovered
        make_recording(self.data, "rec-20261101-130000", status="completed", segments=False)
        make_recording(self.data, "rec-20261101-133000", status="completed", frames=0)        # no video at all
        make_recording(self.data, "rec-20261101-134000", frames=0)        # crashed before any frame: nothing to do
        make_recording(self.data, "rec-20261101-135000", segments=False)  # crashed before any segment
        interrupted = make_recording(self.data, "rec-20261101-136000", status="completed",
                                     metadata=True, validation=False)        # packaging killed half-way
        live = make_recording(self.data, "rec-20261101-140000")
        make_recording(self.data, "rec-20261101-150000")                                    # answered "No"
        make_recording(self.data, "other-20261101")
        os.makedirs(os.path.join(self.data, "rec-20261101-160000"))                         # no raw/
        open(os.path.join(self.data, "rec-file"), "w").close()
        lock = reclock.acquire(os.path.join(live, "raw"))
        self.assertIsNotNone(lock)
        try:
            self.assertTrue(reclock.is_live(os.path.join(live, "raw")))
            found = gui.unfinished_recordings(self.data, dismissed=["rec-20261101-150000"])
            self.assertEqual(found, [crashed, unpacked, no_capture, interrupted])
            self.assertFalse(gui.recording_unfinished(live))
            # never packaged while someone records into it
            with patch.object(gui, "package_recording") as package:
                folder, ok, why = gui.recover_recording(live)
            package.assert_not_called()
            self.assertFalse(ok)
            self.assertIn("still being recorded", why)
        finally:
            lock.release()
        self.assertIn(live, gui.unfinished_recordings(self.data))                # crashed: lock is free again
        self.assertEqual(gui.unfinished_recordings(os.path.join(self.data, "missing")), [])

    def test_no_is_remembered_and_never_asked_again(self):
        make_recording(self.data, "rec-20261101-090000")
        make_recording(self.data, "rec-20261101-100000")
        ui = headless_ui()
        ui._persist = True
        with patch.object(gui, "messagebox") as box, patch.object(gui, "package_recording") as package:
            box.askyesno.return_value = False
            self.assertTrue(ui._start_recovery_scan())
            self.assertFalse(ui._start_recovery_scan())          # once per bar
            wait_for_queue(ui)
            question = box.askyesno.call_args.args[1]
            self.assertIn("Found 2 recording(s) that did not finish (e.g. after a crash or power cut). "
                          "Recover them now?", question)
            self.assertIn("• rec-20261101-090000", question)
            package.assert_not_called()
            self.assertEqual(gui.load_settings()["recovery_dismissed"],
                             ["rec-20261101-090000", "rec-20261101-100000"])
            again = headless_ui()
            self.assertTrue(again._start_recovery_scan())
            again._recovery_thread.join(5)
            self.assertEqual(len(again._ui_queue), 0)        # nothing left to offer
        box.askyesno.assert_called_once()
        for name in ("rec-20261101-090000", "rec-20261101-100000"):       # nothing deleted
            self.assertTrue(os.path.isfile(os.path.join(self.data, name, "raw", "segments.json")))
        i18n.set_language("zh", persist=False)
        self.assertIn("发现 1 条未正常结束的录制（例如因崩溃或断电）。现在恢复吗？", gui.recovery_question(["rec-1"]))

    def test_screenshot_mode_never_scans(self):
        make_recording(self.data, "rec-20261101-090000")
        ui = headless_ui()
        ui.screenshot = True
        with patch.object(gui, "unfinished_recordings") as scan:
            self.assertFalse(ui._start_recovery_scan())
        scan.assert_not_called()
        self.assertEqual(len(ui._ui_queue), 0)

    def test_yes_packages_each_and_the_newest_becomes_the_done_recording(self):
        old = make_recording(self.data, "rec-20261101-090000")
        broken = make_recording(self.data, "rec-20261102-090000")
        newest = make_recording(self.data, "rec-20261103-090000")
        with open(os.path.join(newest, "session_info.json"), "w", encoding="utf-8") as fh:
            fh.write('{"engineer_id": "E7", "task_set": "T"}')
        ui = headless_ui(markov_delivery=False)                  # legacy format chosen in ⚙
        package = fake_package(fail={"rec-20261102-090000"})
        with patch.object(gui, "messagebox") as box, patch.object(gui, "package_recording", package), \
                patch.object(gui, "open_path") as opener:
            box.askyesno.side_effect = [True, True]               # recover? yes; open the folder? yes
            ui._offer_recovery([old, broken, newest])
            self.assertEqual(ui.state, "packing")
            ui._refresh()
            self.assertEqual(last_kwargs(ui.btn_main)["state"], "disabled")   # no new recording meanwhile
            wait_for_queue(ui)
        self.assertEqual([c.args[0] for c in package.call_args_list], [old, broken, newest])
        self.assertTrue(all(c.kwargs["markov"] is False for c in package.call_args_list))
        self.assertEqual(package.call_args.kwargs["session_info"], {"engineer_id": "E7", "task_set": "T"})
        summary = box.askyesno.call_args.args[1]
        self.assertIn("Recovered 2 recording(s):\n• rec-20261101-090000\n• rec-20261103-090000", summary)
        self.assertIn("Could not recover 1 recording(s)", summary)
        self.assertIn("• rec-20261102-090000: segments.json contains no video frames", summary)
        self.assertTrue(summary.endswith("Open the folder now?"))
        opener.assert_called_once_with(self.data)
        # the bar now offers Open annotator / Export for the newest recovered recording
        self.assertEqual(ui.state, "done")
        self.assertEqual(ui.last_out, newest)
        self.assertTrue(ui._packaged)
        self.assertEqual(ui._session_info["task_set"], "T")
        self.assertTrue(ui._delivery_report["technical_ok"])
        ui._snapshot()
        ui._snap_thread.join(2)
        ui._refresh()
        self.assertEqual(last_kwargs(ui.time_lbl)["text"], "01:05.4")
        self.assertEqual(last_kwargs(ui.btn_export_rec)["state"], "normal")
        self.assertEqual(last_kwargs(ui.btn_annotator)["state"], "normal")
        self.assertIn("rec-20261103-090000", last_kwargs(ui.stats)["text"])
        self.assertTrue(os.path.isfile(os.path.join(broken, "raw", "segments.json")))   # nothing deleted
        i18n.set_language("zh", persist=False)
        text = gui.recovery_summary([old], [(broken, "坏了")])
        self.assertIn("已恢复 1 条录制：", text)
        self.assertIn("• rec-20261102-090000：坏了", text)

    def test_waits_until_the_bar_is_not_recording(self):
        folder = make_recording(self.data, "rec-20261101-090000")
        ui = headless_ui("recording", snap={"frames": 1})
        with patch.object(gui, "messagebox") as box:
            box.askyesno.return_value = False
            ui._offer_recovery([folder])
            ui._tick()
            box.askyesno.assert_not_called()        # a dialog would be recorded into the video
            ui.state = "done"
            ui._tick()
        box.askyesno.assert_called_once()
        self.assertIsNone(ui._recovery_pending)

    def test_keeps_the_bars_own_recording(self):
        folder = make_recording(self.data, "rec-20261101-090000")
        ui = headless_ui("done")
        # this session's own recording (Stop without autopack): looks unfinished, is never offered
        ui.last_out = make_recording(self.data, "rec-20261118-101500", status="completed")
        package = fake_package()
        with patch.object(gui, "messagebox") as box, patch.object(gui, "package_recording", package):
            box.askyesno.side_effect = [True, False]
            ui._offer_recovery([folder, ui.last_out])
            wait_for_queue(ui)
        self.assertIn("Found 1 recording(s)", box.askyesno.call_args_list[0].args[1])
        self.assertEqual([c.args[0] for c in package.call_args_list], [folder])
        self.assertEqual(ui.state, "done")                     # the bar keeps its own recording
        self.assertTrue(ui.last_out.endswith("rec-20261118-101500"))
        self.assertFalse(ui._packaged)

    def test_real_packager_recovers_a_crashed_session(self):
        root = os.path.join(self.data, "rec-20261101-090000")
        _s0, n = fx.simple_session(root, capture={"status": "recording"})   # the recorder died
        self.assertEqual(gui.unfinished_recordings(self.data), [root])
        ui = headless_ui()
        with patch("subprocess.run", return_value=fx.fake_probe(1920, 1080, 30, n)), \
                redirect_stdout(io.StringIO()), patch.object(gui, "messagebox") as box:
            box.askyesno.return_value = False
            self.assertTrue(ui.recover_recordings([root]))
            wait_for_queue(ui, timeout=20)
        self.assertTrue(os.path.isfile(os.path.join(root, "metadata.json")))
        self.assertTrue(os.path.isfile(os.path.join(root, "raw", "segments.json")))
        self.assertEqual(ui.state, "done")
        self.assertEqual(ui.last_out, root)
        self.assertIn("technical_ok", ui._delivery_report)
        self.assertIn("Recovered 1 recording(s)", box.askyesno.call_args.args[1])
        self.assertEqual(gui.unfinished_recordings(self.data), [])            # not offered again


# ------------------------------------------------------------------ helpers
class ScreenshotPngTest(unittest.TestCase):
    def test_png_encoder_round_trip(self):
        bgra = bytes([0, 0, 255, 255,   0, 255, 0, 255,   255, 0, 0, 255,   10, 20, 30, 255])
        rgb = gui.bgra_to_rgb(bgra, 2, 2)
        self.assertEqual(rgb, bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 30, 20, 10]))
        png = gui.png_bytes(2, 2, rgb)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        length, tag = struct.unpack(">I4s", png[8:16])
        self.assertEqual(tag, b"IHDR")
        width, height, depth, ctype = struct.unpack(">IIBB", png[16:26])
        self.assertEqual((width, height, depth, ctype), (2, 2, 8, 2))
        idat = png.index(b"IDAT")
        size = struct.unpack(">I", png[idat - 4:idat])[0]
        raw = zlib.decompress(png[idat + 4: idat + 4 + size])
        self.assertEqual(raw, b"\x00" + rgb[:6] + b"\x00" + rgb[6:])
        self.assertTrue(png.endswith(b"IEND\xaeB`\x82"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a", "shot.png")
            gui.write_png(path, 2, 2, rgb)
            self.assertGreater(os.path.getsize(path), 50)
        self.assertTrue(gui._uniform(b"\x01\x02\x03" * 4))
        self.assertFalse(gui._uniform(rgb))


class AppPickerLogicTest(_Env):
    def test_normalize_filter_sort(self):
        raw = [{"exe": "SLDWORKS.exe", "name": "SOLIDWORKS", "pids": [1], "titles": ["Part1 - SOLIDWORKS"],
                "hwnds": [5]},
               {"exe": "C:\\x\\notepad.exe", "titles": ["a.txt - Notepad", "b.txt"], "pids": 3},
               {"exe": ""}, "garbage"]
        apps = [a for a in map(app_picker.normalize_app, raw) if a]
        self.assertEqual([a["exe"] for a in apps], ["SLDWORKS.exe", "notepad.exe"])
        self.assertEqual(apps[1]["name"], "notepad")
        self.assertEqual(apps[1]["pids"], [3])
        self.assertEqual(app_picker.first_title(apps[1]), "a.txt - Notepad  (+1)")
        self.assertEqual(app_picker.filter_apps(apps, "part1"), [apps[0]])
        self.assertEqual(app_picker.filter_apps(apps, "NOTE txt"), [apps[1]])
        self.assertEqual(app_picker.filter_apps(apps, ""), apps)
        self.assertEqual(app_picker.sort_apps(apps, "notepad.exe")[0]["exe"], "notepad.exe")
        name, exe, title = app_picker.row_values(dict(apps[0], elevated=True), warn_elevated=True)
        self.assertEqual(name, "SOLIDWORKS (admin)")

    def test_manual_entry(self):
        self.assertEqual(app_picker.manual_app("SLDWORKS")["exe"], "SLDWORKS.exe")
        self.assertEqual(app_picker.manual_app('"C:\\Program Files\\App\\acad.exe"')["exe"], "acad.exe")
        self.assertEqual(app_picker.manual_app("acad.exe")["name"], "acad")
        for bad in ("", "   ", "a*b.exe", "x/"):
            self.assertIsNone(app_picker.manual_app(bad), bad)

    def test_load_apps_never_raises(self):
        with patch("recorder.app_mask.list_apps", side_effect=RuntimeError("boom")):
            apps, err = app_picker.load_apps()
        self.assertEqual(apps, [])
        self.assertIn("boom", err)
        self.assertIsNone(app_picker.process_elevated("not a pid"))


class SessionFormLogicTest(_Env):
    def test_save_merges_and_keeps_unknown_keys(self):
        path = os.path.join(self.tmp.name, "rec-20261118-1", "session_info.json")
        result = session_form.save_info(path, {"custom": 1, "engineer_id": "old"},
                                        {"engineer_id": "new", "task_completed": True})
        self.assertEqual(result, {"custom": 1, "engineer_id": "new", "task_completed": True})
        self.assertEqual(session_form.load_info(path), result)
        self.assertIn("rec-20261118-1", session_form.scope_text(path))
        self.assertIn("Defaults", session_form.scope_text(os.path.join(self.tmp.name, "cad-session.json")))
        self.assertIn("task.pdf", session_form.hint_text(True))
        i18n.set_language("zh", persist=False)
        self.assertIn("任务集目录", session_form.hint_text(True))
        self.assertEqual(session_form.field_label(session_form.FIELDS[0]), "工程师编号")


# ------------------------------------------------------------------ real Tk
class RealTkSmokeTest(_Env):
    """Builds the real bar (needs a display; skipped on headless CI)."""

    def test_build_toggle_language_and_states(self):
        if os.environ.get("DSTUDIO_SKIP_TK_TESTS") == "1" or gui.tk is None:
            self.skipTest("Tk tests disabled")
        args = parse_gui_args(["--screenshot", os.path.join(self.tmp.name, "x.png"), "--lang", "en"])
        try:
            ui = OverlayUI(gui_args=args)
        except gui.tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        try:
            self.assertFalse(ui._recovery_scanned)                        # CI screenshots never scan / ask
            ui.show_settings(True)
            ui.root.update()
            self.assertEqual(ui.btn_main.cget("text"), "● Start recording")
            ui.cfg.update(capture_mode="app", app_exe="SLDWORKS.exe")
            ui._render_settings_texts()
            ui.root.update()
            self.assertTrue(ui.overlay_cell.winfo_ismapped())             # ⚙ → Allowed overlays (App only)
            ui.cfg["capture_mode"] = "desktop"
            ui._render_settings_texts()
            ui.root.update()
            self.assertFalse(ui.overlay_cell.winfo_ismapped())
            ui.btn_lang.buttons["zh"].invoke()
            ui.root.update()
            self.assertEqual(ui.btn_main.cget("text"), "● 开始录制")
            self.assertEqual(ui.btn_choose_app.cget("text"), "选择应用…")
            for state in ("recording-demo", "autopaused-demo", "done-demo"):
                ui.apply_demo_state(state)
                ui.root.update()
            self.assertTrue(ui.result_row.winfo_ismapped())
            done = ui.stats.cget("text")                                  # zh: 2 missing items + 1 note
            self.assertIn("还缺 2 项", done)
            self.assertIn("1 条提示", done)
            self.assertTrue(ui.root.protocol("WM_DELETE_WINDOW"))         # Alt+F4 goes through on_close
            self.assertFalse(ui.chk_legacy.get())                         # Markov CAD by default
            self.assertFalse(os.path.exists(os.environ["DSTUDIO_SETTINGS"]))  # screenshot mode never saves
        finally:
            ui.destroy()


if __name__ == "__main__":
    unittest.main()
