"""Unit tests for the pure helpers used by the Windows E2E scripts (portable)."""
from __future__ import annotations

import importlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import e2e_lib  # noqa: E402
import gui_screenshots  # noqa: E402
import run_all  # noqa: E402
import run_unit  # noqa: E402


def frame(w, h, fill=(255, 255, 255)):
    return bytearray(bytes(fill) * (w * h))


def paint(buf, w, box, rgb):
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        for x in range(x0, x1):
            i = (y * w + x) * 3
            buf[i:i + 3] = bytes(rgb)


class Helpers(unittest.TestCase):
    def test_parse_rate(self):
        self.assertEqual(e2e_lib.parse_rate("30/1"), 30.0)
        self.assertAlmostEqual(e2e_lib.parse_rate("30000/1001"), 29.97, places=2)
        self.assertEqual(e2e_lib.parse_rate("0/0"), 0.0)
        self.assertEqual(e2e_lib.parse_rate("60"), 60.0)
        self.assertEqual(e2e_lib.parse_rate(None), 0.0)

    def test_region_stats_black_red_white(self):
        w, h = 100, 60
        buf = frame(w, h)
        paint(buf, w, (0, 0, 40, 30), (0, 0, 0))
        paint(buf, w, (50, 0, 90, 30), (255, 0, 0))
        black = e2e_lib.region_stats(bytes(buf), w, h, (0, 0, 40, 30))
        red = e2e_lib.region_stats(bytes(buf), w, h, (50, 0, 90, 30))
        white = e2e_lib.region_stats(bytes(buf), w, h, (0, 35, 100, 60))
        self.assertEqual((black["dark_frac"], black["red_frac"]), (1.0, 0.0))
        self.assertEqual((red["dark_frac"], red["red_frac"]), (0.0, 1.0))
        self.assertEqual(white["dark_frac"], 0.0)
        self.assertGreater(white["mean"], 250)
        # boxes are clipped to the frame
        self.assertEqual(e2e_lib.region_stats(bytes(buf), w, h, (90, 50, 200, 200))["n"], 10 * 10)
        self.assertEqual(e2e_lib.region_stats(bytes(buf), w, h, (200, 200, 300, 300))["n"], 0)

    def test_count_differing(self):
        w, h = 40, 40
        buf = frame(w, h, (0x33, 0x66, 0xCC))
        paint(buf, w, (10, 10, 14, 20), (255, 255, 255))
        self.assertEqual(e2e_lib.count_differing(bytes(buf), w, h, (0, 0, 40, 40), (0x33, 0x66, 0xCC)), 40)

    def test_screen_to_video(self):
        self.assertEqual(e2e_lib.screen_to_video((100, 50, 300, 150), (-1920, 0)), (2020, 50, 2220, 150))
        self.assertEqual(e2e_lib.screen_to_video((100, 100, 200, 200), (0, 0), (0.5, 0.5)), (50, 50, 100, 100))

    def test_event_filters(self):
        events = [
            {"type": "key", "keyCode": 81, "timestamp": 1000},
            {"type": "key", "keyCode": "77", "timestamp": 2000},
            {"type": "mouse_move", "timestamp": 1500},
            {"type": "privacy_mask_start", "timestamp": 1600},
            {"type": "click", "timestamp": 3000},
        ]
        self.assertEqual(len(e2e_lib.keys_with_codes(events, [81])), 1)
        self.assertEqual(len(e2e_lib.keys_with_codes(events, [77])), 1)
        between = e2e_lib.input_events_between(events, 1200, 2500)
        self.assertEqual(sorted(e["type"] for e in between), ["key", "mouse_move"])
        self.assertEqual(e2e_lib.event_types(events)["key"], 2)

    def test_read_jsonl_rejects_non_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "a.json")
            with open(good, "w", encoding="utf-8") as fh:
                fh.write('{"a": 1}\n\n{"b": 2}\n')
            self.assertEqual(len(e2e_lib.read_jsonl(good)), 2)
            bad = os.path.join(tmp, "b.json")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("[1, 2]\n")
            with self.assertRaises(ValueError):
                e2e_lib.read_jsonl(bad)

    def test_report_ok_logic(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = e2e_lib.Report("t", tmp)
            self.assertFalse(rep.ok)                       # no checks -> not ok
            rep.check("soft one", False, soft=True)
            rep.check("hard one", True)
            self.assertTrue(rep.ok)
            rep.check("hard fail", False)
            self.assertFalse(rep.ok)
            self.assertTrue(os.path.isfile(rep.write()))

    def test_png_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.png")
            raw = b"".join(b"\x00" + b"\xff\x00\x00" * 120 for _ in range(30))
            def chunk(tag, data):
                return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
            png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 120, 30, 8, 2, 0, 0, 0))
                   + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
            with open(path, "wb") as fh:
                fh.write(png)
            self.assertEqual(gui_screenshots.png_size(path), (120, 30))
            with open(path, "wb") as fh:
                fh.write(b"not a png at all, sorry....")
            self.assertIsNone(gui_screenshots.png_size(path))

    def test_windows_only_import_detection(self):
        self.assertTrue(run_unit.is_windows_only_import_error("AttributeError: module 'ctypes' has no attribute 'WinDLL'"))
        self.assertTrue(run_unit.is_windows_only_import_error("ModuleNotFoundError: No module named 'winreg'"))
        self.assertFalse(run_unit.is_windows_only_import_error("SyntaxError: invalid syntax"))
        self.assertFalse(run_unit.is_windows_only_import_error("ModuleNotFoundError: No module named 'numpy'"))


def box(kind: bytes, payload: bytes = b"", size64: bool = False) -> bytes:
    if size64:
        return struct.pack(">I", 1) + kind + struct.pack(">Q", 16 + len(payload)) + payload
    return struct.pack(">I", 8 + len(payload)) + kind + payload


class V021Helpers(unittest.TestCase):
    """Helpers behind the v0.2.1 E2E checks (real captures, K3 rects, K10 MP4 layout, mask leaks)."""

    def test_segments_list_accepts_both_layouts(self):
        segs = [{"index": 0}, {"index": 1}]
        self.assertEqual(e2e_lib.segments_list(segs), segs)
        self.assertEqual(e2e_lib.segments_list({"segments": segs}), segs)
        self.assertEqual(e2e_lib.segments_list({"x": 1}), [])
        self.assertEqual(e2e_lib.segments_list([1, {"index": 2}]), [{"index": 2}])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(e2e_lib.load_segments(tmp), [])
            os.makedirs(os.path.join(tmp, "raw"))
            with open(os.path.join(tmp, "raw", "segments.json"), "w", encoding="utf-8") as fh:
                fh.write('[{"index": 0, "frames": [[0, 1.0]')          # half-written: no crash
            self.assertEqual(e2e_lib.load_segments(tmp), [])

    def test_summarize_segments_real_captures(self):
        rect = {"x": 0, "y": 0, "width": 1024, "height": 768}
        segs = [
            {"backend": "dxcam", "encoder": "libx264", "frames": [[i, i * 33.3] for i in range(90)],
             "capture": {"captures": 30, "capture_fps": 10.0, "cursor_frames": 25, "capture_rect": rect}},
            {"backend": "dxcam", "encoder": "libx264", "frame_count": 60, "in_progress": True, "finalized": False,
             "capture": {"captures": 60, "capture_fps": 30.0, "cursor_frames": 0, "capture_rect": rect}},
        ]
        s = e2e_lib.summarize_segments(segs)
        self.assertEqual((s["frames"], s["captures"], s["cursor_frames"]), (150, 90, 25))
        self.assertAlmostEqual(s["real_ratio"], 0.6)
        self.assertFalse(e2e_lib.real_capture_ok(s))                 # 2 of 3 slots padded in segment 1
        self.assertTrue(e2e_lib.real_capture_ok(s, 0.5))
        self.assertEqual(s["capture_rects"], [(0, 0, 1024, 768)] * 2)
        self.assertEqual((s["in_progress"], s["finalized"]), ([False, True], [None, False]))
        self.assertEqual(s["encoders"], ["libx264"])
        gdigrab = e2e_lib.summarize_segments([{"backend": "gdigrab", "frames": [[0, 1.0]],
                                               "capture": {"capture_rect": rect, "draw_mouse": True}}])
        self.assertIsNone(gdigrab["captures"])
        self.assertIsNone(e2e_lib.real_capture_ok(gdigrab))          # cannot tell: never counts as "real"
        self.assertIsNone(gdigrab["cursor_frames"])

    def test_rect_tuple(self):
        self.assertEqual(e2e_lib.rect_tuple({"x": -1920, "y": 0, "width": 1920, "height": 1080}), (-1920, 0, 1920, 1080))
        self.assertEqual(e2e_lib.rect_tuple([1, 2, 3, 4]), (1, 2, 3, 4))
        self.assertIsNone(e2e_lib.rect_tuple({"x": 1}))
        self.assertIsNone(e2e_lib.rect_tuple(None))

    def test_mp4_boxes_and_faststart(self):
        with tempfile.TemporaryDirectory() as tmp:
            def write(name, data):
                path = os.path.join(tmp, name)
                with open(path, "wb") as fh:
                    fh.write(data)
                return path

            normal = write("a.mp4", box(b"ftyp", b"isom") + box(b"moov", b"x" * 40) + box(b"mdat", b"y" * 100))
            self.assertEqual(e2e_lib.mp4_top_level_boxes(normal), ["ftyp", "moov", "mdat"])
            self.assertTrue(e2e_lib.is_faststart_mp4(e2e_lib.mp4_top_level_boxes(normal)))
            frag = write("b.mp4", box(b"ftyp") + box(b"moov") + box(b"moof", b"m") + box(b"mdat", b"d" * 9)
                         + box(b"moof") + box(b"mdat", b"z", size64=True))
            boxes = e2e_lib.mp4_top_level_boxes(frag)
            self.assertEqual(boxes, ["ftyp", "moov", "moof", "mdat", "moof", "mdat"])
            self.assertFalse(e2e_lib.is_faststart_mp4(boxes))
            tail_moov = write("c.mp4", box(b"ftyp") + box(b"mdat", b"q" * 50) + box(b"moov"))
            self.assertFalse(e2e_lib.is_faststart_mp4(e2e_lib.mp4_top_level_boxes(tail_moov)))
            # size 0 = "to the end of the file"; a truncated file stops cleanly
            open_end = write("d.mp4", box(b"ftyp") + struct.pack(">I", 0) + b"mdat" + b"r" * 30)
            self.assertEqual(e2e_lib.mp4_top_level_boxes(open_end), ["ftyp", "mdat"])
            cut = write("e.mp4", box(b"ftyp") + struct.pack(">I", 500) + b"moov" + b"s" * 10)
            self.assertEqual(e2e_lib.mp4_top_level_boxes(cut), ["ftyp", "moov"])
            sei = write("f.mp4", box(b"ftyp") + box(b"mdat", b"\x00\x00\x01\x06\x05x264 - core 165 r3222"))
            self.assertTrue(e2e_lib.has_x264_sei(sei))
            self.assertFalse(e2e_lib.has_x264_sei(normal))

    def _red_and_bright(self):
        w, h = 20, 10
        buf = frame(w, h, (0, 0, 0))
        paint(buf, w, (0, 0, 5, 4), (250, 10, 20))        # 20 saturated red pixels
        paint(buf, w, (10, 0, 12, 10), (200, 120, 120))   # pink: not "saturated red"
        paint(buf, w, (15, 0, 20, 10), (32, 32, 32))      # dark-mode grey: visible above level 24
        rgb = bytes(buf)
        return (e2e_lib.red_pixel_count(rgb, w, h), round(e2e_lib.bright_fraction(rgb, w, h, 24), 3),
                round(e2e_lib.bright_fraction(rgb, w, h, 64), 3))

    def test_red_pixels_and_visibility(self):
        self.assertEqual(self._red_and_bright(), (20, 0.45, 0.2))
        with mock.patch.dict(sys.modules, {"numpy": None}):           # pure-Python fallback agrees
            self.assertEqual(self._red_and_bright(), (20, 0.45, 0.2))

    def test_env_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "env.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"dxgi_duplication_works": True, "windows": {"cursor": {"showing": True}}}, fh)
            with mock.patch.dict(os.environ, {"DSTUDIO_ENV_REPORT": path}):
                env = e2e_lib.load_env_report()
            self.assertTrue(e2e_lib.env_dxgi_works(env) and e2e_lib.env_cursor_showing(env))
            self.assertEqual(e2e_lib.load_env_report(os.path.join(tmp, "missing.json")), {})
            self.assertFalse(e2e_lib.env_dxgi_works({}) or e2e_lib.env_cursor_showing({}))

    def test_prune_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "raw", "segments"))
            for rel in ("clip.mp4", os.path.join("raw", "segments", "seg_000.mp4"), "metadata.json"):
                open(os.path.join(tmp, rel), "w").close()
            removed = e2e_lib.prune_videos(tmp)
            self.assertEqual(sorted(removed), sorted(["clip.mp4", os.path.join("raw", "segments", "seg_000.mp4")]))
            self.assertEqual(sorted(os.listdir(tmp)), ["metadata.json", "raw"])

    def test_iter_frames_rgb_decodes_every_frame(self):
        ff = shutil.which("ffmpeg")
        if not ff:
            self.skipTest("ffmpeg not installed")
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, "red.mp4")
            gen = subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x48:r=10:d=1",
                                  "-pix_fmt", "yuv420p", "-y", video], capture_output=True, timeout=60)
            probe_ok = shutil.which("ffprobe") and subprocess.run(["ffprobe", "-version"], capture_output=True,
                                                                  timeout=30).returncode == 0
            if gen.returncode != 0 or not probe_ok:
                self.skipTest("ffmpeg/ffprobe cannot encode here")
            with mock.patch.object(e2e_lib, "ffmpeg_path", return_value=ff), \
                    mock.patch.object(e2e_lib, "ffprobe_path", return_value=shutil.which("ffprobe")):
                frames = list(e2e_lib.iter_frames_rgb(video, scale_div=2))
            self.assertEqual(len(frames), 10)
            idx, w, h, rgb = frames[-1]
            self.assertEqual((idx, w, h, len(rgb)), (9, 32, 24, 32 * 24 * 3))
            self.assertGreater(e2e_lib.red_pixel_count(rgb, w, h), 0.9 * w * h)


class Scenarios(unittest.TestCase):
    def test_every_scenario_is_registered_and_importable(self):
        names = [n for n, _s, _t in run_all.SCENARIOS]
        self.assertEqual(len(names), len(set(names)))
        for name in ("desktop30", "fps60", "app_only", "pause_resume", "export",
                     "mask_transition", "crash_safety", "display_encoder"):
            self.assertIn(name, names)
        for name, script, timeout in run_all.SCENARIOS:
            self.assertTrue(os.path.isfile(os.path.join(HERE, script)), script)
            self.assertLessEqual(timeout, 600, name)
            mod = importlib.import_module(script[:-3])
            self.assertTrue(callable(getattr(mod, "body", None)), script)
        on_disk = {f for f in os.listdir(HERE) if f.startswith("e2e_") and f.endswith(".py") and f != "e2e_lib.py"}
        self.assertEqual(on_disk, {s for _n, s, _t in run_all.SCENARIOS})

    @unittest.skipIf(sys.platform == "win32", "on Windows this would drive the real desktop")
    def test_new_scenarios_skip_cleanly_off_windows(self):
        for script in ("e2e_mask_transition", "e2e_crash_safety", "e2e_display_encoder"):
            mod = importlib.import_module(script)
            with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout"):
                name = script[4:]
                self.assertEqual(e2e_lib.scenario_main(name, mod.body, argv=["--out", tmp]), 0)
                with open(os.path.join(tmp, "report.json"), encoding="utf-8") as fh:
                    self.assertTrue(json.load(fh)["info"].get("skipped"))


if __name__ == "__main__":
    unittest.main()
