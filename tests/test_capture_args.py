"""Recorder CLI (contract section 1) and capture backend selection (runs anywhere).

Recorders are built on fake monitors / window queries (:func:`fake_os`): the
results never depend on the displays or apps of the machine running the tests.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recorder import capture  # noqa: E402
from recorder.capture import create_segment, fallback_backend  # noqa: E402
from recorder.gdi_capture import GdiSegment  # noqa: E402
from recorder.i18n import get_language, set_language  # noqa: E402
from recorder.recorder import Recorder, app_target_of, capture_mode_of, parse_args  # noqa: E402
from recorder.videocap import VideoSegment  # noqa: E402


def quiet_parse(argv):
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        return parse_args(argv)


class ParseArgsTest(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, {"DSTUDIO_DATA_DIR": "/tmp/dstudio-data-x", "DSTUDIO_FFMPEG": ""}):
            a = parse_args(["start"])
        self.assertEqual(a.fps, 30)
        self.assertEqual(a.source, "desktop")
        self.assertEqual(a.backend, "auto")
        self.assertTrue(a.draw_mouse_cursor)            # Markov clips show the cursor
        self.assertTrue(a.auto_pause_other_apps)
        self.assertEqual(a.app_allow, [])
        self.assertIsNone(a.app_monitor)
        self.assertTrue(a.ffmpeg)                         # paths.ffmpeg_path(), never empty
        self.assertTrue(os.path.basename(a.out).startswith("rec-"))
        self.assertEqual(os.path.dirname(a.out), os.path.abspath("/tmp/dstudio-data-x"))

    def test_no_subcommand_is_backwards_compatible(self):
        a = parse_args(["--out", "x", "--fps", "60"])
        self.assertEqual((a.start, a.out, a.fps), ("start", "x", 60))

    def test_boolean_optional_flags(self):
        a = parse_args(["start", "--no-draw-mouse-cursor", "--no-auto-pause-other-apps"])
        self.assertFalse(a.draw_mouse_cursor)
        self.assertFalse(a.auto_pause_other_apps)
        a = parse_args(["start", "--draw-mouse-cursor"])  # old spelling still accepted
        self.assertTrue(a.draw_mouse_cursor)

    def test_app_mode_flags(self):
        a = parse_args(["start", "--source", "app:SLDWORKS.exe", "--app-allow", "a.exe", "B.EXE",
                        "--app-monitor", "1", "--backend", "gdi", "--fps", "60"])
        self.assertEqual(capture_mode_of(a.source), "app")
        self.assertEqual(app_target_of(a.source), "SLDWORKS.exe")
        self.assertEqual(a.app_allow, ["a.exe", "B.EXE"])
        self.assertEqual((a.app_monitor, a.backend, a.fps), (1, "gdi", 60))

    def test_fps_range(self):
        for ok in ("1", "30", "60", "120"):
            self.assertEqual(parse_args(["start", "--fps", ok]).fps, int(ok))
        for bad in ("0", "121", "-5", "abc", "29.97"):
            with self.assertRaises(SystemExit, msg=bad):
                quiet_parse(["start", "--fps", bad])

    def test_source_validation(self):
        for good in ("desktop", "monitor:1", "region:0,0,1280,720", "window:Notepad", "app:acad.exe"):
            self.assertEqual(parse_args(["start", "--source", good]).source, good)
        for bad in ("monitor:x", "region:1,2,3", "region:0,0,0,10", "app:", "app:  ", "screen", "window:"):
            with self.assertRaises(SystemExit, msg=bad):
                quiet_parse(["start", "--source", bad])

    def test_backend_choices(self):
        for b in ("auto", "dxcam", "gdi", "gdigrab"):
            self.assertEqual(parse_args(["start", "--backend", b]).backend, b)
        with self.assertRaises(SystemExit):
            quiet_parse(["start", "--backend", "obs"])

    def test_capture_mode_of(self):
        self.assertEqual([capture_mode_of(s) for s in ("desktop", "monitor:0", "region:1,2,3,4",
                                                       "window:x", "app:y.exe")],
                         ["desktop", "monitor", "region", "window", "app"])


class BackendSelectionTest(unittest.TestCase):
    def kwargs(self, source="desktop", **extra):
        base = dict(index=0, path=os.path.join(tempfile.gettempdir(), "seg_000.mp4"), fps=60,
                    source=source, encoder="libx264", ffmpeg="ffmpeg", masker=None,
                    capture_rect=(0, 0, 1920, 1080), monitor_device=None, output_idx=0)
        base.update(extra)
        return base

    def test_app_mode_rejects_gdigrab_bilingually(self):
        old = get_language()
        try:
            for lang, word in (("zh", "仅录制应用"), ("en", "App-only")):
                set_language(lang, persist=False)
                with self.assertRaises(ValueError) as err:
                    create_segment("gdigrab", **self.kwargs("app:SLDWORKS.exe"))
                self.assertIn(word, str(err.exception))
        finally:
            set_language(old, persist=False)

    def test_app_mode_auto_never_picks_gdigrab(self):
        with patch.object(capture, "backend_available", return_value=False):
            seg = create_segment("auto", **self.kwargs("app:SLDWORKS.exe"))
        self.assertIsInstance(seg, GdiSegment)
        self.assertEqual(fallback_backend("app:SLDWORKS.exe"), "gdi")
        self.assertEqual(fallback_backend("desktop"), "gdigrab")

    def test_desktop_auto_without_dxgi_uses_gdigrab(self):
        with patch.object(capture, "backend_available", return_value=False):
            seg = create_segment("auto", **self.kwargs())
        self.assertIsInstance(seg, VideoSegment)

    def test_window_source_needs_gdigrab(self):
        self.assertIsInstance(create_segment("auto", **self.kwargs("window:Notepad")), VideoSegment)
        for b in ("dxcam", "gdi"):
            with self.assertRaises(ValueError):
                create_segment(b, **self.kwargs("window:Notepad"))

    def test_unknown_backend(self):
        with self.assertRaises(ValueError):
            create_segment("obs", **self.kwargs())

    def test_gdi_segment_is_fps_generic(self):
        seg = create_segment("gdi", **self.kwargs(fps=120))
        self.assertEqual(seg.fps, 120)
        self.assertEqual(seg.max_backlog_frames, 600)
        cmd = seg._build_cmd(1920, 1080)
        self.assertEqual(cmd[cmd.index("-r") + 1], "120")
        self.assertEqual(cmd[cmd.index("-g") + 1], "240")
        self.assertEqual(cmd[cmd.index("-s") + 1], "1920x1080")
        odd = seg._build_cmd(1366, 767)
        self.assertIn("crop=trunc(iw/2)*2:trunc(ih/2)*2", odd)


class GdigrabCommandTest(unittest.TestCase):
    def setUp(self):
        p = patch("recorder.videocap.primary_monitor_rect", lambda: None)   # never the real displays
        p.start()
        self.addCleanup(p.stop)

    def make(self, fps, **kw):
        return VideoSegment(0, os.path.join(tempfile.gettempdir(), "seg.mp4"), fps,
                            encoder="libx264", ffmpeg="ffmpeg", **kw)

    def test_fps_generic_and_cursor_on_by_default(self):
        for fps in (30, 60):
            cmd = self.make(fps)._build_cmd()
            self.assertEqual(cmd[cmd.index("-framerate") + 1], str(fps))
            self.assertEqual(cmd[cmd.index("-draw_mouse") + 1], "1")
            self.assertIn(f"fps={fps},showinfo", cmd[cmd.index("-filter_complex") + 1])
            self.assertEqual(cmd[cmd.index("-g") + 1], str(fps * 2))

    def test_cursor_can_be_turned_off(self):
        cmd = self.make(30, draw_mouse=False)._build_cmd()
        self.assertEqual(cmd[cmd.index("-draw_mouse") + 1], "0")

    def test_showinfo_pts_maps_to_wall_clock_at_any_fps(self):
        for fps in (30, 60):
            seg = self.make(fps)
            seg._clock_offset_ms = 0.0
            seg._proc = type("P", (), {"stderr": iter([
                "[Parsed_showinfo_2 @ 0x1] n:   0 pts:  %d pts_time:1 \n" % (fps * 1000),
                "[Parsed_showinfo_2 @ 0x1] n:   1 pts:  %d pts_time:1 \n" % (fps * 1000 + 1),
            ])})()
            seg._read_stderr()
            self.assertEqual(seg.frames[0], (0, 1_000_000.0))
            self.assertAlmostEqual(seg.frames[1][1] - seg.frames[0][1], 1000.0 / fps)


# ------------------------------------------------- backends against fake Win32 APIs
_SINK = ("import sys\n"
         "data = sys.stdin.buffer.read()\n"
         "open(sys.argv[1], 'wb').write(data)\n")

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


class FakeGdi:
    """Just enough of recorder.cursor_draw._GdiApi to drive GdiSegment off Windows."""

    def __init__(self):
        from types import SimpleNamespace
        import ctypes
        self.ctypes = ctypes
        self.buffers, self.selected, self.deleted, self.patblts, self.icons = {}, {}, [], [], []
        self.released_dcs = []
        self._next = 100
        self.winmm = None
        self.user32 = SimpleNamespace(GetDC=lambda h: 1, ReleaseDC=lambda h, dc: self.released_dcs.append(dc) or 1,
                                      DrawIconEx=lambda *a: self.icons.append(a) or 1)
        self.gdi32 = SimpleNamespace(CreateCompatibleDC=self._new_dc, DeleteDC=lambda dc: 1,
                                     SelectObject=self._select, DeleteObject=self.deleted.append,
                                     BitBlt=self._bitblt, PatBlt=self._patblt, GdiFlush=lambda: 1)

    def _new_dc(self, ref):
        self._next += 1
        return self._next

    def create_dib(self, hdc, w, h):
        buf = (self.ctypes.c_ubyte * (w * h * 4))()
        self._next += 1
        self.buffers[self._next] = (buf, w, h)
        return self._next, self.ctypes.addressof(buf)

    def _select(self, dc, obj):
        prev = self.selected.get(dc, 0)
        self.selected[dc] = obj
        return prev

    fail_every = 0
    blits = 0

    def _bitblt(self, dst, x, y, w, h, src, sx, sy, rop):
        self.blits += 1
        if self.fail_every and self.blits > 2 and self.blits % self.fail_every == 0:
            return 0
        buf, bw, bh = self.buffers[self.selected[dst]]
        self.ctypes.memset(buf, 0xC8, bw * bh * 4)
        return 1

    def _patblt(self, dc, x, y, w, h, rop):
        buf, bw, bh = self.buffers[self.selected[dc]]
        self.patblts.append((x, y, w, h))
        for row in range(y, y + h):
            self.ctypes.memset(self.ctypes.addressof(buf) + (row * bw + x) * 4, 0, w * 4)
        return 1

    # cursor API (CursorTracker)
    def cursor(self):
        return (0xABC, 30, 20)

    def icon_meta(self, hcur):
        return (4, 2, 16, 16)


def _app_masker(capture_rect, target_rect):
    from recorder.app_mask import AppMasker, WinInfo
    return AppMasker("SLDWORKS.exe", own_pid=1, capture_rect=capture_rect,
                     enumerate_fn=lambda b: [WinInfo(5, 9, "SLDWORKS.exe", target_rect)],
                     foreground_fn=lambda: {"hwnd": 5, "pid": 9, "exe": "SLDWORKS.exe"})


def _sink_cmd(seg, out):
    seg._build_cmd = lambda w, h: [sys.executable, "-c", _SINK, out]


class FakeGdiBackendTest(unittest.TestCase):
    def test_gdi_segment_masks_draws_cursor_and_frees_everything(self):
        import time
        from recorder import cursor_draw, gdi_capture
        fake = FakeGdi()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(gdi_capture, "IS_WINDOWS", True), \
                patch.object(gdi_capture, "gdi", lambda: fake), \
                patch.object(cursor_draw, "gdi", lambda: fake):
            w, h = 32, 16
            masker = _app_masker((100, 50, w, h), (100, 50, 116, 58))   # target = left half, top half
            seg = gdi_capture.GdiSegment(0, os.path.join(tmp, "seg.mp4"), 60, encoder="libx264",
                                         ffmpeg="ffmpeg", capture_rect=(100, 50, w, h), masker=masker)
            out = os.path.join(tmp, "raw.bgra")
            _sink_cmd(seg, out)
            seg.start()
            time.sleep(0.3)
            seg.stop()
            data = open(out, "rb").read()
        frame_bytes = w * h * 4
        self.assertEqual(len(data), seg.frame_count * frame_bytes)
        self.assertGreater(seg.frame_count, 5)
        first = data[:frame_bytes]
        px = lambda x, y: first[(y * w + x) * 4]
        self.assertEqual(px(2, 2), 0xC8)          # target pixel kept
        self.assertEqual(px(20, 2), 0)            # right of the target: black
        self.assertEqual(px(2, 12), 0)            # below the target: black
        self.assertEqual(seg.masked_frames, seg.captures)
        self.assertTrue(seg.cursor_in_video)
        # cursor at screen (30,20), hotspot (4,2), capture origin (100,50) -> DC (-74,-32)
        self.assertEqual(fake.icons[0][1:3], (30 - 4 - 100, 20 - 2 - 50))
        # every pooled DIB (queue + 2) and the validation tile were deleted; DCs released
        self.assertEqual(sorted(fake.deleted), sorted(fake.buffers))
        self.assertEqual(len(fake.released_dcs), 2)
        info = seg.capture_info()
        self.assertEqual(info["capture_rect"], {"x": 100, "y": 50, "width": w, "height": h})
        self.assertTrue(info["captureblt"])

    def test_failed_blits_keep_the_dc_clean(self):
        import time
        from recorder import cursor_draw, gdi_capture
        fake = FakeGdi()
        fake.fail_every = 3
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(gdi_capture, "IS_WINDOWS", True), \
                patch.object(gdi_capture, "gdi", lambda: fake), \
                patch.object(cursor_draw, "gdi", lambda: fake):
            seg = gdi_capture.GdiSegment(0, os.path.join(tmp, "seg.mp4"), 60, encoder="libx264",
                                         ffmpeg="ffmpeg", capture_rect=(0, 0, 8, 4), draw_mouse=False)
            out = os.path.join(tmp, "raw.bgra")
            _sink_cmd(seg, out)
            seg.start()
            mem_dc = seg._mem_dc
            time.sleep(0.3)
            seg.stop()
            size = os.path.getsize(out)
        self.assertGreater(seg.blt_failures, 0)
        self.assertEqual(fake.selected[mem_dc], 0)              # stock bitmap back in the DC
        self.assertEqual(sorted(fake.deleted), sorted(fake.buffers))
        self.assertEqual([n for n, _ in seg.frames], list(range(seg.frame_count)))
        self.assertEqual(size, seg.frame_count * 8 * 4 * 4)
        self.assertFalse(seg.cursor_in_video)

    @unittest.skipIf(sys.platform == "win32", "on Windows GDI capture is real")
    def test_gdi_refuses_off_windows(self):
        with self.assertRaises(RuntimeError):
            GdiSegment(0, os.path.join(tempfile.gettempdir(), "x.mp4"), 30, encoder="libx264").start()


@unittest.skipIf(np is None, "numpy not installed")
class FakeDxgiBackendTest(unittest.TestCase):
    def fake_module(self, created):
        from types import ModuleType, SimpleNamespace

        def output(name, x, y, w, h):
            dc = SimpleNamespace(left=x, top=y, right=x + w, bottom=y + h)
            return SimpleNamespace(devicename=name, desc=SimpleNamespace(DeviceName=name, DesktopCoordinates=dc))

        class Camera:
            def __init__(self, region, w, h):
                self.region, self.w, self.h = region, w, h

            def start(self, target_fps, video_mode):
                self.fps = target_fps

            def get_latest_frame(self):
                import time
                time.sleep(1 / 120)
                return np.full((self.h, self.w, 4), 200, np.uint8)

            def stop(self):
                pass

            def release(self):
                pass

        mod = ModuleType("bettercam")
        mod._factory = SimpleNamespace(devices=[object()], outputs=[[output(r"\\.\DISPLAY1", 0, 0, 64, 32),
                                                                       output(r"\\.\DISPLAY2", 64, -8, 32, 16)]])

        def create(output_idx=0, region=None, output_color="RGB", device_idx=0):
            created.append({"output_idx": output_idx, "region": region, "color": output_color})
            if region:
                return Camera(region, region[2] - region[0], region[3] - region[1])
            return Camera(None, *((64, 32) if output_idx == 0 else (32, 16)))
        mod.create = create
        return mod

    def test_dxcam_maps_monitor_to_output_and_masks(self):
        import time
        from recorder import dxcam_capture
        created = []
        mod = self.fake_module(created)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(dxcam_capture, "load_backend", lambda: (mod, "bettercam")):
            masker = _app_masker((64, -8, 32, 16), (64, -8, 80, 0))
            seg = dxcam_capture.DxcamSegment(0, os.path.join(tmp, "seg.mp4"), 60, encoder="libx264",
                                             ffmpeg="ffmpeg", capture_rect=(64, -8, 32, 16),
                                             monitor_device=r"\\.\DISPLAY2", masker=masker,
                                             draw_mouse=False)
            out = os.path.join(tmp, "raw.bgra")
            _sink_cmd(seg, out)
            seg.start()
            time.sleep(0.25)
            seg.stop()
            data = open(out, "rb").read()
        self.assertEqual(created[0], {"output_idx": 1, "region": None, "color": "BGRA"})
        fb = 32 * 16 * 4
        self.assertEqual(len(data), seg.frame_count * fb)
        frame = np.frombuffer(data[:fb], np.uint8).reshape(16, 32, 4)
        self.assertTrue((frame[0:8, 0:16] == 200).all())
        self.assertTrue((frame[8:, :] == 0).all())
        self.assertTrue((frame[:, 16:] == 0).all())
        self.assertEqual(seg.capture_info()["output_idx"], 1)

    def test_region_is_relative_to_its_output(self):
        from recorder import dxcam_capture
        created = []
        mod = self.fake_module(created)
        outs = dxcam_capture.dxgi_outputs(mod)
        self.assertEqual([o["rect"] for o in outs], [(0, 0, 64, 32), (64, -8, 32, 16)])
        chosen = dxcam_capture.select_output(outs, (70, -4, 10, 6))
        self.assertEqual(chosen["output_idx"], 1)
        self.assertIsNone(dxcam_capture.select_output(outs, (60, 0, 10, 6)))   # spans two outputs
        with patch.object(dxcam_capture, "load_backend", lambda: (mod, "bettercam")):
            seg = dxcam_capture.DxcamSegment(0, os.path.join(tempfile.gettempdir(), "r.mp4"), 30,
                                             encoder="libx264", source="region:70,-4,10,6",
                                             capture_rect=(70, -4, 10, 6), draw_mouse=False)
            self.assertEqual(seg._open(), (10, 6))
            seg._close()
        self.assertEqual(created[-1]["region"], (6, 4, 16, 10))


class FakeDesktop:
    screens = [{"index": 0, "originX": 0, "originY": 0, "width": 1920, "height": 1080,
                "deviceName": r"\\.\DISPLAY1", "primary": True},
               {"index": 1, "originX": 1920, "originY": 0, "width": 2560, "height": 1440,
                "deviceName": r"\\.\DISPLAY2", "primary": False}]

    def __init__(self):
        self.screens = [dict(s) for s in FakeDesktop.screens]


def _fake_masker(*args, **kw):
    """A real AppMasker on fake window queries: no app of this machine is visible."""
    from recorder.app_mask import AppMasker
    kw.setdefault("enumerate_fn", lambda bounds: [])
    kw.setdefault("foreground_fn", lambda: {"hwnd": 0, "pid": 0, "exe": "", "name": ""})
    return AppMasker(*args, **kw)


@contextlib.contextmanager
def fake_os(desktop=None):
    """Patch every machine-dependent query a Recorder makes while it is built."""
    desktop = desktop or FakeDesktop
    with patch("recorder.recorder.Desktop", desktop), \
            patch("recorder.recorder.list_monitors", lambda: [dict(s) for s in FakeDesktop.screens]), \
            patch("recorder.recorder.enable_dpi_awareness", lambda: "n/a"), \
            patch("recorder.recorder.virtual_screen_rect", lambda: None), \
            patch("recorder.recorder.AppMasker", _fake_masker):
        yield


class RecorderSetupTest(unittest.TestCase):
    def make(self, *argv):
        tmp = tempfile.mkdtemp(prefix="destudio-args-")
        args = parse_args(["start", "--out", tmp, "--codec", "libx264", "--ffmpeg", "ffmpeg", *argv])
        with fake_os(), contextlib.redirect_stdout(io.StringIO()):
            rec = Recorder(args)
        self.addCleanup(rec.log.close)
        return rec

    def test_app_mode_rejects_gdigrab_before_touching_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "rec")
            args = parse_args(["start", "--out", out, "--source", "app:SLDWORKS.exe", "--backend", "gdigrab"])
            with fake_os(), self.assertRaises(ValueError):
                Recorder(args)
            self.assertFalse(os.path.exists(out))

    def test_missing_monitor_index_is_a_clear_error_before_touching_disk(self):
        # never a silent fallback to the whole virtual desktop (K3: one display)
        old = get_language()
        try:
            for lang, word in (("zh", "找不到显示器 5"), ("en", "Monitor 5 was not found")):
                set_language(lang, persist=False)
                with tempfile.TemporaryDirectory() as tmp:
                    out = os.path.join(tmp, "rec")
                    args = parse_args(["start", "--out", out, "--source", "monitor:5"])
                    with fake_os(), self.assertRaises(ValueError) as err:
                        Recorder(args)
                    self.assertIn(word, str(err.exception))
                    self.assertIn("0, 1", str(err.exception))          # the monitors that do exist
                    self.assertFalse(os.path.exists(out))
        finally:
            set_language(old, persist=False)

    def test_app_mode_monitor_choice_and_allow_list(self):
        rec = self.make("--source", "app:SLDWORKS.exe", "--app-monitor", "1", "--app-allow", "Foo")
        self.assertEqual(rec.capture_rect, (1920, 0, 2560, 1440))
        self.assertEqual(rec.masker.capture_rect, (1920, 0, 2560, 1440))
        self.assertIn("foo.exe", rec.allow_exes)
        self.assertIn("ctfmon.exe", rec.allow_exes)
        self.assertTrue(rec.auto_pause_enabled)
        # no target window visible (fake window list) -> primary monitor
        rec2 = self.make("--source", "app:SLDWORKS.exe", "--no-auto-pause-other-apps")
        self.assertEqual(rec2.capture_rect, (0, 0, 1920, 1080))
        self.assertFalse(rec2.auto_pause_enabled)

    def test_bad_app_monitor_is_a_clear_error(self):
        tmp = tempfile.mkdtemp(prefix="destudio-args-")
        args = parse_args(["start", "--out", tmp, "--source", "app:x.exe", "--app-monitor", "7"])
        with fake_os(), self.assertRaises(ValueError):
            Recorder(args)

    def test_static_capture_rects(self):
        self.assertEqual(self.make("--source", "monitor:1").capture_rect, (1920, 0, 2560, 1440))
        self.assertEqual(self.make("--source", "region:10,20,640,480").capture_rect, (10, 20, 640, 480))
        # desktop = ONE display: the primary monitor (Markov clips are single-display), never
        # the virtual desktop (a second monitor's chats/mail must not end up in the clip)
        rec = self.make()
        self.assertEqual(rec.capture_rect, (0, 0, 1920, 1080))
        self.assertEqual(rec.capture_monitor["deviceName"], r"\\.\DISPLAY1")

        class SecondIsPrimary(FakeDesktop):
            def __init__(self):
                super().__init__()
                self.screens[0]["primary"], self.screens[1]["primary"] = False, True

        tmp = tempfile.mkdtemp(prefix="destudio-args-")
        args = parse_args(["start", "--out", tmp, "--codec", "libx264", "--ffmpeg", "ffmpeg"])
        with fake_os(SecondIsPrimary), contextlib.redirect_stdout(io.StringIO()):
            rec = Recorder(args)
        self.addCleanup(rec.log.close)
        self.assertEqual(rec.capture_rect, (1920, 0, 2560, 1440))

        class NoScreens(FakeDesktop):
            def __init__(self):
                self.screens = []

        # no monitor list at all (query failed): virtual desktop as the last resort
        with fake_os(NoScreens), \
                patch("recorder.recorder.virtual_screen_rect", return_value=(0, 0, 800, 600)), \
                contextlib.redirect_stdout(io.StringIO()):
            rec = Recorder(parse_args(["start", "--out", tempfile.mkdtemp(prefix="destudio-args-"),
                                       "--codec", "libx264", "--ffmpeg", "ffmpeg"]))
        self.addCleanup(rec.log.close)
        self.assertEqual(rec.capture_rect, (0, 0, 800, 600))


class _GeoSeg:
    """Segment fake for start_segment geometry tests (records its kwargs)."""

    encoder = "libx264"
    crashed = False
    error_tail = ""

    def __init__(self, backend, fail=False, report_rect=None, output_size=None, **kw):
        self.backend = backend
        self.kw = kw
        self.output_size = output_size
        self.index = kw["index"]
        self.path = kw["path"]
        self.fps = kw["fps"]
        self.fail = fail
        self.capture_rect = report_rect or kw.get("capture_rect")
        self.frames = []
        self.stopped = False

    def start(self):
        if self.fail:
            raise RuntimeError(f"{self.backend} unavailable")
        from recorder.clock import wall_ms
        self.frames = [(0, wall_ms())]
        open(self.path, "wb").close()
        return self

    def stop(self):
        self.stopped = True
        return self

    @property
    def frame_count(self):
        return len(self.frames)

    def to_dict(self):
        return {"index": self.index, "file": os.path.basename(self.path), "fps": self.fps,
                "frames": [list(f) for f in self.frames], "finalized": self.stopped, "returncode": 0}


class DesktopGeometryTest(unittest.TestCase):
    """K3: desktop = primary monitor for every backend; later clips keep the first clip's rect."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="destudio-geo-")
        args = parse_args(["start", "--out", tmp, "--codec", "libx264", "--ffmpeg", "ffmpeg"])
        with fake_os(), contextlib.redirect_stdout(io.StringIO()):
            self.rec = Recorder(args)
        self.addCleanup(self.rec.log.close)
        self.made = []
        self.plan = []          # per create_segment call: dict(fail=..., report_rect=...)

        def fake_create(backend, **kw):
            opts = self.plan.pop(0) if self.plan else {}
            seg = _GeoSeg(backend, **opts, **kw)
            self.made.append(seg)
            return seg
        for target, value in (("recorder.recorder.create_segment", fake_create),
                              ("recorder.recorder.list_monitors", lambda: [dict(s) for s in FakeDesktop.screens])):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        out = patch("sys.stdout", new_callable=io.StringIO)
        out.start()
        self.addCleanup(out.stop)

    def test_desktop_records_the_primary_monitor_with_every_backend(self):
        self.rec.start_segment()
        kw = self.made[0].kw
        self.assertEqual((kw["offset"], kw["video_size"], kw["capture_rect"], kw["monitor_device"]),
                         ((0, 0), (1920, 1080), (0, 0, 1920, 1080), r"\\.\DISPLAY1"))
        self.assertEqual(self.rec._pinned_rect, (0, 0, 1920, 1080))

    def test_fallback_on_resume_reproduces_the_pinned_rect(self):
        self.rec.start_segment()                                   # dxcam / auto: first clip
        self.rec.stop_segment()
        self.plan = [{"fail": True}, {}]                           # auto pick fails -> gdigrab
        self.rec.start_segment()
        first_try, fallback = self.made[1], self.made[2]
        self.assertEqual(fallback.backend, "gdigrab")
        for kw in (first_try.kw, fallback.kw):
            self.assertEqual((kw["offset"], kw["video_size"], kw["capture_rect"]),
                             ((0, 0), (1920, 1080), (0, 0, 1920, 1080)))
        self.assertIs(self.rec.segment, fallback)

    def test_clip_with_other_geometry_is_discarded_and_retried(self):
        self.rec.start_segment()
        self.rec.stop_segment()
        self.plan = [{"report_rect": (0, 0, 3840, 1080)}]          # e.g. a grabber ignoring the rect
        with self.assertRaises(RuntimeError):
            self.rec.start_segment()
        bad = self.made[1]
        self.assertTrue(bad.stopped)
        self.assertFalse(os.path.exists(bad.path))
        self.assertEqual(len(self.rec.segments), 1)
        self.assertIsNone(self.rec.segment)

    def test_clip_with_another_encoded_size_is_discarded(self):
        self.plan = [{"output_size": (1920, 1080)}]
        self.rec.start_segment()
        self.assertEqual(self.rec._pinned_size, (1920, 1080))
        self.rec.stop_segment()
        self.plan = [{"output_size": (1920, 1078)}]               # same area, other even-size rounding
        with self.assertRaisesRegex(RuntimeError, "1920x1078"):
            self.rec.start_segment()
        self.assertTrue(self.made[1].stopped)
        self.assertEqual(len(self.rec.segments), 1)
        self.plan = [{"output_size": (1920, 1080)}]
        self.rec.start_segment()                                   # the retry matches: kept
        self.assertEqual(len(self.rec.segments), 2)

    def test_renumbered_display_with_the_same_rect_keeps_recording(self):
        self.rec.start_segment()
        self.rec.stop_segment()
        renamed = [dict(s) for s in FakeDesktop.screens]
        renamed[0]["deviceName"], renamed[1]["deviceName"] = r"\\.\DISPLAY3", r"\\.\DISPLAY1"   # swapped
        with patch("recorder.recorder.list_monitors", lambda: renamed):
            self.rec.start_segment()
        kw = self.made[1].kw
        self.assertEqual((kw["capture_rect"], kw["monitor_device"]), ((0, 0, 1920, 1080), r"\\.\DISPLAY3"))
        self.assertEqual(self.rec._pinned_device, r"\\.\DISPLAY3")

    def test_changed_display_is_refused_instead_of_recording_another_area(self):
        self.rec.start_segment()
        self.rec.stop_segment()
        smaller = [dict(s) for s in FakeDesktop.screens]
        smaller[0].update(width=1280, height=720)
        with patch("recorder.recorder.list_monitors", lambda: smaller), self.assertRaises(RuntimeError):
            self.rec.start_segment()
        self.assertEqual(len(self.made), 1)                        # nothing was even created


if __name__ == "__main__":
    unittest.main(verbosity=2)
