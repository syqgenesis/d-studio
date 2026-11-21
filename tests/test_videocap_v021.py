"""v0.2.1 video contracts (K1, K3, K5, K6, K10) and review findings 5/7/12/15/16/18/28.

Runs anywhere: ffmpeg is replaced by small Python processes, Win32 by fakes.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recorder import videocap  # noqa: E402
from recorder.clock import wall_ms  # noqa: E402
from recorder.videocap import (EVEN_CROP, Frame, FramePacer, KillOnCloseJob, PipedSegment,  # noqa: E402
                               VideoSegment, pick_encoder, segment_output_args, wait_for_finalize)

PY = sys.executable

# stand-in for ffmpeg reading rawvideo on stdin: counts bytes, then (optionally)
# keeps "finalizing" by appending to the output file, then exits with a code
_SINK = ("import sys, time\n"
         "count_path, out, grow_s, rc, hang = sys.argv[1], sys.argv[2], float(sys.argv[3]), "
         "int(sys.argv[4]), sys.argv[5] == '1'\n"
         "n = 0\n"
         "while True:\n"
         "    b = sys.stdin.buffer.read(65536)\n"
         "    if not b: break\n"
         "    n += len(b)\n"
         "open(count_path, 'w').write(str(n))\n"
         "end = time.time() + grow_s\n"
         "while time.time() < end:\n"
         "    with open(out, 'ab') as fh: fh.write(b'x' * 64)\n"
         "    time.sleep(0.05)\n"
         "while hang: time.sleep(1)\n"
         "sys.exit(rc)\n")


class SyntheticSource(PipedSegment):
    """Unpaced source (like bettercam) whose capture walls come from a list."""

    backend = "synthetic"
    paced = False

    def __init__(self, *args, walls_rel=(), w=2, h=2, grow_s=0.0, rc=0, hang=False, **kw):
        super().__init__(*args, encoder="libx264", ffmpeg=PY, queue_frames=64, **kw)
        self.walls_rel = list(walls_rel)
        self.w, self.h = w, h
        self.base = None
        self.i = 0
        self.released = 0
        self.lock = threading.Lock()
        self.count_path = self.path + ".count"
        self.sink_args = (grow_s, rc, hang)

    def _open(self):
        return self.w, self.h

    def _grab(self):
        if self.i >= len(self.walls_rel):
            return None
        if self.base is None:
            self.base = wall_ms()
        wall = self.base + self.walls_rel[self.i]
        self.i += 1
        buf = bytearray([self.i % 256]) * (self.w * self.h * 4)

        def release():
            with self.lock:
                self.released += 1
        return Frame(memoryview(buf), wall, release=release)

    def _build_cmd(self, w, h):
        grow_s, rc, hang = self.sink_args
        return [PY, "-c", _SINK, self.count_path, self.path, str(grow_s), str(rc), "1" if hang else "0"]

    def exhausted(self):
        return self.i >= len(self.walls_rel)

    def bytes_received(self):
        with open(self.count_path) as fh:
            return int(fh.read())


class SegTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="destudio-v021-")

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name="seg_000.mp4"):
        return os.path.join(self.tmp.name, name)

    def run_synthetic(self, fps, walls_rel, **kw):
        seg = SyntheticSource(0, self.path(), fps, walls_rel=walls_rel, **kw)
        seg.start()
        deadline = time.monotonic() + 30
        while not seg.exhausted() and time.monotonic() < deadline:
            time.sleep(0.02)
        # let the capture thread hand the last frame to the writer
        time.sleep(0.1)
        seg.stop()
        return seg


# ----------------------------------------------------------------------- K6
class PickEncoderTest(unittest.TestCase):
    def pick(self, listed, working, preferred="auto"):
        with patch.object(videocap, "available_encoders", lambda ffmpeg="ffmpeg": set(listed)), \
                patch.object(videocap, "probe_encoder", lambda name, ffmpeg="ffmpeg": name in working):
            return pick_encoder(preferred, "ffmpeg")

    def test_auto_prefers_libx264_over_hardware(self):
        all_hw = {"h264_nvenc", "hevc_nvenc", "h264_amf", "hevc_amf", "h264_qsv", "libx264"}
        self.assertEqual(self.pick(all_hw, all_hw), "libx264")

    def test_hardware_h264_only_when_libx264_does_not_work(self):
        listed = {"libx264", "hevc_nvenc", "h264_qsv"}
        self.assertEqual(self.pick(listed, {"hevc_nvenc", "h264_qsv"}), "h264_qsv")
        self.assertEqual(self.pick({"h264_nvenc", "h264_amf"}, {"h264_amf"}), "h264_amf")

    def test_auto_never_picks_hevc(self):
        listed = {"libx264", "hevc_nvenc", "hevc_amf", "hevc_qsv", "libx265"}
        self.assertEqual(self.pick(listed, {"hevc_nvenc", "hevc_amf", "hevc_qsv", "libx265"}), "libx264")

    def test_explicit_choice_is_trusted(self):
        self.assertEqual(self.pick(set(), set(), "hevc_nvenc"), "hevc_nvenc")
        self.assertEqual(self.pick(set(), set(), "h264_nvenc"), "h264_nvenc")


# ---------------------------------------------------------------------- K10
class SegmentCommandTest(unittest.TestCase):
    def check_fragmented(self, cmd, fps):
        joined = " ".join(cmd)
        self.assertNotIn("faststart", joined)
        self.assertEqual(cmd[cmd.index("-movflags") + 1], "+frag_keyframe+empty_moov+default_base_moof")
        self.assertLessEqual(int(cmd[cmd.index("-g") + 1]), 2 * fps)      # keyframe at least every 2 s
        self.assertLess(cmd.index("-movflags"), cmd.index("-y"))

    def test_gdigrab_segment_is_fragmented(self):
        for fps in (30, 60):
            seg = VideoSegment(0, "seg.mp4", fps, encoder="libx264", ffmpeg="ffmpeg")
            self.check_fragmented(seg._build_cmd(), fps)

    def test_piped_segment_is_fragmented(self):
        for fps in (30, 60, 120):
            seg = PipedSegment(0, "seg.mp4", fps, encoder="libx264", ffmpeg="ffmpeg")
            self.check_fragmented(seg._build_cmd(1920, 1080), fps)
        self.assertEqual(segment_output_args(30)[:2], ["-g", "60"])

    def test_gdigrab_uses_capture_rect_or_primary_monitor(self):
        seg = VideoSegment(0, "seg.mp4", 30, encoder="libx264", ffmpeg="ffmpeg",
                           capture_rect=(0, 0, 2560, 1440))
        cmd = seg._build_cmd()
        self.assertEqual((cmd[cmd.index("-offset_x") + 1], cmd[cmd.index("-offset_y") + 1]), ("0", "0"))
        self.assertEqual(cmd[cmd.index("-video_size") + 1], "2560x1440")
        with patch.object(videocap, "primary_monitor_rect", lambda: (0, 0, 1920, 1080)):
            seg = VideoSegment(0, "seg.mp4", 30, encoder="libx264", ffmpeg="ffmpeg")
            cmd = seg._build_cmd()
        self.assertEqual(cmd[cmd.index("-video_size") + 1], "1920x1080")
        self.assertEqual(seg.capture_rect, (0, 0, 1920, 1080))
        # explicit offset/size (monitor:N, region) and single windows are unchanged
        seg = VideoSegment(0, "seg.mp4", 30, encoder="libx264", ffmpeg="ffmpeg", source="monitor:1",
                           offset=(1920, 0), video_size=(1280, 1024), capture_rect=(1920, 0, 1280, 1024))
        cmd = seg._build_cmd()
        self.assertEqual(cmd[cmd.index("-offset_x") + 1], "1920")
        with patch.object(videocap, "primary_monitor_rect", lambda: (0, 0, 1920, 1080)):
            cmd = VideoSegment(0, "seg.mp4", 30, encoder="libx264", ffmpeg="ffmpeg",
                               source="window:Notepad")._build_cmd()
        self.assertNotIn("-video_size", cmd)
        self.assertIn("title=Notepad", cmd)

    def test_gdi_backend_desktop_defaults_to_primary_monitor(self):
        from recorder import gdi_capture
        seg = gdi_capture.GdiSegment(0, "seg.mp4", 30, encoder="libx264", ffmpeg="ffmpeg")
        with patch.object(gdi_capture, "primary_monitor_rect", lambda: (0, 0, 1920, 1080)):
            self.assertEqual(seg._resolve_rect(), (0, 0, 1920, 1080))

    def test_piped_audio_ends_with_the_video(self):
        # a live dshow input never ends: -shortest lets ffmpeg finalize on stdin EOF
        seg = PipedSegment(0, "seg.mp4", 30, encoder="libx264", ffmpeg="ffmpeg", audio_device="Mic (USB)")
        cmd = seg._build_cmd(1920, 1080)
        self.assertIn("audio=Mic (USB)", cmd)
        self.assertIn("-shortest", cmd)
        self.assertLess(cmd.index("-shortest"), cmd.index("-y"))
        # apad: a microphone lost mid-recording must not end the video through -shortest
        af = cmd.index("-af")
        self.assertEqual(cmd[af + 1], "apad")
        self.assertLess(cmd.index("1:a?"), af)
        self.assertLess(af, cmd.index("-c:a"))
        self.assertLess(af, cmd.index("-shortest"))
        no_audio = PipedSegment(0, "seg.mp4", 30, encoder="libx264", ffmpeg="ffmpeg")._build_cmd(1920, 1080)
        self.assertNotIn("-shortest", no_audio)
        self.assertNotIn("-af", no_audio)



def _working_ffmpeg():
    """The shipped / configured ffmpeg if it can run libx264 and lavfi, else None."""
    from recorder import paths
    ff = paths.ffmpeg_path()
    try:
        out = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20)
        filters = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode or "libx264" not in out.stdout or " sine " not in filters.stdout:
        return None
    return ff


class AudioPadRealTest(unittest.TestCase):
    """The piped audio command with a REAL ffmpeg (windows-latest CI uses the shipped 9.0.2).

    The microphone (here a lavfi sine that ends after 0.5 s, like an unplugged
    USB mic) must not end the video through -shortest, and stdin EOF must still
    finalize the file quickly.
    """

    FPS, FRAMES, W, H = 30, 45, 64, 48

    def run_cmd(self, mic_seconds):
        ff = _working_ffmpeg()
        if ff is None:
            self.skipTest("no working ffmpeg with libx264 + lavfi")
        tmp = tempfile.mkdtemp(prefix="dstudio-apad-")
        out = os.path.join(tmp, "seg.mp4")
        cmd = PipedSegment(0, out, self.FPS, encoder="libx264", ffmpeg=ff, audio_device="Mic")._build_cmd(self.W, self.H)
        i = cmd.index("audio=Mic")
        mic = "sine=frequency=440:sample_rate=48000" + (f":duration={mic_seconds}" if mic_seconds else "")
        cmd[i - 3:i + 1] = ["-f", "lavfi", "-i", mic]          # stand-in for "-f dshow -i audio=Mic"
        cmd[cmd.index("-loglevel") + 1] = "error"
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        frame = bytes([40, 80, 160, 255]) * (self.W * self.H)
        written = 0
        try:
            for _ in range(self.FRAMES):
                proc.stdin.write(frame)
                written += 1
                time.sleep(1.0 / self.FPS)
        except (BrokenPipeError, OSError):
            pass
        t0 = time.monotonic()
        try:
            _, err = proc.communicate(timeout=30)      # closes stdin = the recorder's EOF
        except subprocess.TimeoutExpired:
            proc.kill()
            self.fail("ffmpeg did not finalize within 30 s of stdin EOF")
        probe_exe = os.path.join(os.path.dirname(ff), os.path.basename(ff).replace("ffmpeg", "ffprobe"))
        probe = subprocess.run([probe_exe if os.path.isfile(probe_exe) else "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                                "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", out],
                               capture_output=True, text=True, timeout=60)
        return proc.returncode, written, probe.stdout.strip(), time.monotonic() - t0, err

    def test_lost_microphone_keeps_the_video_and_stdin_eof_finalizes(self):
        rc, written, frames, took, err = self.run_cmd(0.5)
        self.assertEqual(written, self.FRAMES, "the pipe broke when the mic ended (video cut by -shortest)")
        self.assertEqual(rc, 0, err)
        self.assertEqual(frames, str(self.FRAMES))
        self.assertLess(took, 15)

    def test_live_microphone_still_finalizes_on_stdin_eof(self):
        rc, written, frames, took, err = self.run_cmd(None)
        self.assertEqual((rc, written), (0, self.FRAMES), err)
        self.assertEqual(frames, str(self.FRAMES))
        self.assertLess(took, 15)

# ----------------------------------------------------------------------- K3
class EvenSizeTest(unittest.TestCase):
    """Every backend encodes the same size for the same capture rect (no pad vs crop)."""

    def gdigrab(self, rect, **kw):
        seg = VideoSegment(0, "seg.mp4", 30, encoder="libx264", ffmpeg="ffmpeg", capture_rect=rect, **kw)
        return seg, seg._build_cmd()

    def piped(self, w, h):
        seg = PipedSegment(0, "seg.mp4", 30, encoder="libx264", ffmpeg="ffmpeg", capture_rect=(0, 0, w, h))
        seg._size = (w, h)                                  # what start() records from _open()
        return seg, seg._build_cmd(w, h)

    def test_odd_rect_is_cropped_the_same_way_by_every_backend(self):
        g, gcmd = self.gdigrab((0, 0, 1001, 701))
        graph = gcmd[gcmd.index("-filter_complex") + 1]
        self.assertNotIn("pad=", graph)
        self.assertIn(EVEN_CROP, graph)
        self.assertLess(graph.index(EVEN_CROP), graph.index("showinfo"))   # showinfo sees the encoded size
        p, pcmd = self.piped(1001, 701)
        self.assertEqual(pcmd[pcmd.index("-vf") + 1], EVEN_CROP)
        self.assertEqual(g.output_size, (1000, 700))
        self.assertEqual(p.output_size, g.output_size)
        self.assertEqual(g.to_dict()["output_size"], [1000, 700])
        self.assertEqual(p.to_dict()["output_size"], [1000, 700])

    def test_even_rect_keeps_its_size(self):
        g, _ = self.gdigrab((1920, 0, 2560, 1440))
        p, pcmd = self.piped(2560, 1440)
        self.assertNotIn("-vf", pcmd)
        self.assertEqual((g.output_size, p.output_size), ((2560, 1440), (2560, 1440)))

    def test_unknown_size_is_none_until_showinfo_reports_it(self):
        seg = VideoSegment(0, "seg.mp4", 30, encoder="libx264", ffmpeg="ffmpeg", source="window:Notepad")
        self.assertIsNone(PipedSegment(0, "seg.mp4", 30, encoder="libx264", ffmpeg="ffmpeg").to_dict()["output_size"])
        seg._build_cmd()
        self.assertIsNone(seg.output_size)
        self.assertIsNone(seg.to_dict()["output_size"])
        line = ("[Parsed_showinfo_2 @ 000001d0] n:   0 pts:52012345678 pts_time:1.73374e+09 duration:1 "
                "pos:-1 fmt:bgra sar:0/1 s:1278x720 i:P iskey:1 type:I checksum:0BADF00D\n")
        seg._proc = type("P", (), {"stderr": iter([line]), "poll": lambda self: None})()
        seg._read_stderr()
        self.assertEqual(seg.frame_count, 1)
        self.assertEqual(seg.output_size, (1278, 720))
        self.assertEqual(seg.to_dict()["output_size"], [1278, 720])


class FinalizeWaitTest(SegTestBase):
    def spawn(self, code):
        return subprocess.Popen([PY, "-c", code])

    def test_never_gives_up_while_the_output_grows(self):
        out = self.path("grow.mp4")
        code = ("import time\n"
                "for _ in range(50):\n"
                "    open(%r, 'ab').write(b'x'); time.sleep(0.05)\n" % out)
        proc = self.spawn(code)
        try:
            # interpreter start-up (Defender, a loaded runner) is not part of the test
            deadline = time.monotonic() + 30
            while not os.path.exists(out) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(os.path.exists(out), "the child never started writing")
            idle_s = 1.0
            t = time.monotonic()
            self.assertTrue(wait_for_finalize(proc, out, idle_s=idle_s, poll_s=0.05))
            self.assertGreater(time.monotonic() - t, idle_s)  # outlived idle_s: growth kept it alive
            self.assertEqual(proc.returncode, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_gives_up_when_stuck(self):
        proc = self.spawn("import time; time.sleep(30)")
        try:
            t = time.monotonic()
            self.assertFalse(wait_for_finalize(proc, self.path("none.mp4"), idle_s=0.3, poll_s=0.05))
            self.assertLess(time.monotonic() - t, 5)
            self.assertFalse(wait_for_finalize(proc, self.path("none.mp4"), idle_s=30, poll_s=0.05,
                                               on_poll=lambda: False))
        finally:
            proc.kill()
            proc.wait()


# ------------------------------------------------------------- K1 + finding 5/18
class FinalizedFlagTest(SegTestBase):
    def test_clean_eof_is_finalized_and_in_progress_while_recording(self):
        seg = SyntheticSource(0, self.path(), 30, walls_rel=[i * 1000.0 / 30 for i in range(10)])
        seg.start()
        live = seg.to_dict()
        self.assertTrue(live["in_progress"])
        self.assertFalse(live["finalized"])
        self.assertIsNone(live["returncode"])
        while not seg.exhausted():
            time.sleep(0.01)
        time.sleep(0.05)
        seg.stop()
        d = seg.to_dict()
        self.assertEqual((d["finalized"], d["returncode"], d["in_progress"]), (True, 0, False))
        self.assertEqual(d["frame_count"], 10)

    def test_nonzero_exit_is_not_finalized(self):
        seg = self.run_synthetic(30, [i * 1000.0 / 30 for i in range(5)], rc=3)
        d = seg.to_dict()
        self.assertEqual((d["finalized"], d["returncode"]), (False, 3))

    def test_finalizing_ffmpeg_is_not_killed_while_the_file_grows(self):
        # "finalizing" takes 2.5x the stuck threshold but keeps writing: never killed
        seg = SyntheticSource(0, self.path(), 30, walls_rel=[i * 1000.0 / 30 for i in range(5)], grow_s=2.0)
        seg.finalize_idle_s = 0.8
        seg.start()
        while not seg.exhausted():
            time.sleep(0.01)
        time.sleep(0.05)
        t = time.monotonic()
        seg.stop()
        self.assertGreater(time.monotonic() - t, 1.0)
        self.assertTrue(seg.finalized, seg.error_tail)
        self.assertEqual(seg.to_dict()["returncode"], 0)

    def test_stuck_ffmpeg_is_killed_and_not_finalized(self):
        seg = SyntheticSource(0, self.path(), 30, walls_rel=[0.0, 40.0], hang=True)
        seg.finalize_idle_s = 0.3
        seg.start()
        time.sleep(0.1)
        t = time.monotonic()
        seg.stop()
        self.assertLess(time.monotonic() - t, 10)
        d = seg.to_dict()
        self.assertFalse(d["finalized"])
        self.assertIsNotNone(d["returncode"])
        self.assertNotEqual(d["returncode"], 0)

    def test_gdigrab_segment_reports_finalize_state(self):
        seg = VideoSegment(0, self.path(), 30, encoder="libx264", ffmpeg="ffmpeg")
        d = seg.to_dict()
        for key in ("finalized", "returncode", "in_progress"):
            self.assertIn(key, d)
        self.assertEqual((d["finalized"], d["returncode"], d["in_progress"]), (False, None, False))

    def _gdigrab_with(self, code):
        """A fake gdigrab ffmpeg; it creates the output file once it is up and running."""
        seg = VideoSegment(0, self.path(), 30, encoder="libx264", ffmpeg="ffmpeg")
        seg._proc = subprocess.Popen([PY, "-c", "open(%r, 'ab').write(b'm')\n" % seg.path + code],
                                     stdin=subprocess.PIPE, text=True)
        seg.start_wall_ms = wall_ms()
        deadline = time.monotonic() + 20
        while not os.path.exists(seg.path) and time.monotonic() < deadline:
            time.sleep(0.01)
        return seg

    def test_gdigrab_q_then_slow_finalize_is_waited_for(self):
        out = self.path()
        code = ("import sys, time\n"
                "sys.stdin.read(1)\n"
                "for _ in range(30):\n"
                "    open(%r, 'ab').write(b'x'); time.sleep(0.05)\n" % out)
        seg = self._gdigrab_with(code)
        seg.stop(timeout=0.6)                   # finalize (~1.5 s) outlasts the idle limit
        d = seg.to_dict()
        self.assertEqual((d["finalized"], d["returncode"], d["in_progress"]), (True, 0, False))

    def test_gdigrab_ignoring_q_while_capturing_is_terminated(self):
        out = self.path()
        code = ("import time\n"
                "while True:\n"
                "    open(%r, 'ab').write(b'x'); time.sleep(0.02)\n" % out)
        seg = self._gdigrab_with(code)
        stop_feed = threading.Event()

        def feed():                         # showinfo keeps reporting new frames
            n = 0
            while not stop_feed.is_set():
                with seg._lock:
                    seg.frames.append((n, wall_ms()))
                n += 1
                time.sleep(1 / 60)
        th = threading.Thread(target=feed, daemon=True)
        th.start()
        try:
            t = time.monotonic()
            seg.stop(timeout=0.5)
            self.assertLess(time.monotonic() - t, 10)
        finally:
            stop_feed.set()
            th.join()
        d = seg.to_dict()
        self.assertFalse(d["finalized"])
        self.assertNotEqual(d["returncode"], 0)


# ------------------------------------------------------------ finding 12 (pacer)
class EarlyCaptureTest(SegTestBase):
    def check_on_wall_time(self, seg, fps, period):
        dt = 1000.0 / fps
        slots = [n for n, _ in seg.frames]
        self.assertEqual(slots, list(range(len(slots))))
        walls = [t for _, t in seg.frames]
        self.assertTrue(all(a < b for a, b in zip(walls, walls[1:])))
        t0 = seg.start_wall_ms
        for n, wall in seg.frames:                        # every frame inside its own slot
            self.assertGreaterEqual(wall, t0 + n * dt - 1e-6)
            self.assertLess(wall, t0 + (n + 1) * dt)
        span = walls[-1] - t0 + dt                        # wall-clock span the clip covers
        self.assertLessEqual(abs(seg.frame_count * dt - span), 2 * dt)
        self.assertEqual(seg.duplicated_slots, 0)
        self.assertGreater(seg.early_captures, 0)
        self.assertEqual(seg.released, seg.captures + seg.early_captures)
        self.assertEqual(seg.bytes_received(), seg.frame_count * seg.w * seg.h * 4)
        self.assertEqual(seg.capture_info()["early_captures"], seg.early_captures)

    def test_bettercam_60fps_timer_does_not_stretch_the_video(self):
        period = 1000 // 60                               # 16 ms -> 62.5 Hz
        walls = [i * period for i in range(int(60_000 / period))]   # one simulated minute
        seg = self.run_synthetic(60, walls)
        self.check_on_wall_time(seg, 60, period)
        self.assertAlmostEqual(seg.frame_count, 3600, delta=1)       # not 3750 (+2.5 s)

    def test_bettercam_30fps_timer_does_not_stretch_the_video(self):
        period = 1000 // 30                               # 33 ms -> 30.3 Hz
        walls = [i * period for i in range(int(60_000 / period))]
        seg = self.run_synthetic(30, walls)
        self.check_on_wall_time(seg, 30, period)
        self.assertAlmostEqual(seg.frame_count, 1800, delta=1)


# -------------------------------------------------------------- K5 / finding 15
class NominalDuplicateTest(SegTestBase):
    def test_padded_slots_get_nominal_slot_time(self):
        dt = 1000.0 / 30
        walls = [0.0, 1.2 * dt, 46.3 * dt, 47.1 * dt]     # 44 slots of encoder/capture stall
        seg = self.run_synthetic(30, walls)
        self.assertEqual([n for n, _ in seg.frames], list(range(48)))
        t0 = seg.start_wall_ms
        self.assertAlmostEqual(seg.frames[1][1], t0 + 1.2 * dt, places=3)
        for slot in range(2, 46):
            self.assertAlmostEqual(seg.frames[slot][1], t0 + slot * dt, places=6)
        self.assertAlmostEqual(seg.frames[46][1], t0 + 46.3 * dt, places=3)
        walls_out = [t for _, t in seg.frames]
        self.assertTrue(all(a < b for a, b in zip(walls_out, walls_out[1:])), "no wall-time plateau")
        d = seg.to_dict()
        self.assertEqual(len(d["duplicate_runs"]), 1)
        first, last, content = d["duplicate_runs"][0]
        self.assertEqual((first, last), (2, 45))
        self.assertAlmostEqual(content, t0 + 1.2 * dt, places=2)  # the picture shown in those slots
        self.assertEqual(seg.duplicated_slots, 44)
        self.assertTrue(all(len(item) == 2 for item in d["frames"]))   # [n, wall] format unchanged


# ------------------------------------------------------------------- finding 28
class SyncLatencyTest(SegTestBase):
    def test_latency_shifts_sidecar_walls_not_the_slot_grid(self):
        dt = 1000.0 / 30
        walls = [0.0, 1.1 * dt, 2.2 * dt, 5.5 * dt, 6.4 * dt]
        plain = self.run_synthetic(30, walls)
        late = SyntheticSource(0, self.path("seg_001.mp4"), 30, walls_rel=walls, sync_latency_ms=40.0)
        late.start()
        while not late.exhausted():
            time.sleep(0.01)
        time.sleep(0.05)
        late.stop()
        self.assertEqual([n for n, _ in late.frames], [n for n, _ in plain.frames])
        raw_t0 = late.base
        self.assertAlmostEqual(late.start_wall_ms, raw_t0 - 40.0, places=6)
        self.assertAlmostEqual(late.frames[0][1], raw_t0 - 40.0, places=3)
        self.assertAlmostEqual(late.frames[1][1], raw_t0 + 1.1 * dt - 40.0, places=3)
        self.assertAlmostEqual(late.frames[3][1], raw_t0 + 3 * dt - 40.0, places=6)   # padded slot
        self.assertEqual(late.capture_info()["sync_latency_ms"], 40.0)
        self.assertEqual(late.first_frame_wall, late.frames[0][1])

    def test_paced_backend_with_latency_keeps_pacing(self):
        class Paced(SyntheticSource):
            paced = True

            def _grab(self):
                buf = bytearray(16)
                return Frame(memoryview(buf), wall_ms(), release=lambda: None)
        seg = Paced(0, self.path(), 60, sync_latency_ms=50.0)
        seg.start()
        time.sleep(0.5)
        before = wall_ms()
        seg.stop()
        self.assertEqual(seg.early_captures, 0)
        self.assertEqual([n for n, _ in seg.frames], list(range(seg.frame_count)))
        self.assertGreater(seg.frame_count, 15)
        self.assertLess(seg.frames[-1][1], before - 50.0 + 1.0)   # walls are latency-corrected


# -------------------------------------------------------------------- finding 7
class FakeKernel32:
    def __init__(self, assign_ok=True):
        self.calls = []
        self.assign_ok = assign_ok

    def CreateJobObjectW(self, attrs, name):
        self.calls.append(("create", attrs, name))
        return 0x4321

    def SetInformationJobObject(self, job, cls, info_ref, size):
        info = info_ref._obj
        self.calls.append(("set", job, cls, info.BasicLimitInformation.LimitFlags, size))
        return 1

    def AssignProcessToJobObject(self, job, handle):
        self.calls.append(("assign", job, handle))
        return 1 if self.assign_ok else 0

    def CloseHandle(self, h):
        self.calls.append(("close", h))
        return 1


class KillOnCloseJobTest(unittest.TestCase):
    def test_structure_layout_matches_win32(self):
        size = ctypes.sizeof(videocap._JOBOBJECT_EXTENDED_LIMIT_INFORMATION)
        self.assertEqual(size, 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112)

    def test_job_is_created_once_with_kill_on_close(self):
        k32 = FakeKernel32()
        job = KillOnCloseJob(kernel32=k32)
        proc = type("P", (), {"_handle": 1234})()
        self.assertTrue(job.assign(proc))
        self.assertTrue(job.assign(type("P", (), {"_handle": 99})()))
        kinds = [c[0] for c in k32.calls]
        self.assertEqual(kinds, ["create", "set", "assign", "assign"])
        _, jh, cls, flags, size = k32.calls[1]
        self.assertEqual((jh, cls), (0x4321, 9))
        self.assertTrue(flags & 0x2000)                   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self.assertEqual(size, ctypes.sizeof(videocap._JOBOBJECT_EXTENDED_LIMIT_INFORMATION))
        self.assertEqual(k32.calls[2], ("assign", 0x4321, 1234))
        self.assertEqual(job.assigned, 2)

    def test_failures_never_raise(self):
        job = KillOnCloseJob(kernel32=FakeKernel32(assign_ok=False))
        self.assertFalse(job.assign(type("P", (), {"_handle": 5})()))
        self.assertTrue(job.error)
        self.assertFalse(KillOnCloseJob(kernel32=FakeKernel32()).assign(object()))   # no handle

    def test_noop_off_windows(self):
        if sys.platform == "win32":
            self.skipTest("Windows: covered by the real job test")
        self.assertFalse(videocap.assign_to_kill_job(type("P", (), {"_handle": 1})()))

    @unittest.skipUnless(sys.platform == "win32", "Windows Job Objects")
    def test_real_job_kills_child_when_handle_closes(self):
        job = KillOnCloseJob()
        child = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
        try:
            self.assertTrue(job.assign(child), job.error)
            job._kernel32().CloseHandle(job.handle)       # = the recorder process exiting
            child.wait(timeout=10)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                child.kill()

    @staticmethod
    def fake_gdigrab(tmp):
        """A VideoSegment whose "ffmpeg" exits 0 on 'q' (no real capture)."""
        seg = VideoSegment(0, os.path.join(tmp, "seg.mp4"), 30, encoder="libx264", ffmpeg=PY)
        seg._build_cmd = lambda: [PY, "-c", "import sys; sys.stdin.read(1)"]
        return seg

    def assigned_by(self, seg):
        calls = []
        with patch.object(videocap, "assign_to_kill_job", lambda proc, log=None: calls.append(proc) or True):
            seg.start()
            try:
                return [p is seg._proc for p in calls]
            finally:
                seg.stop(timeout=10) if isinstance(seg, VideoSegment) else seg.stop()

    def test_only_gdigrab_goes_in_the_job(self):
        # a piped ffmpeg finalizes by itself on stdin EOF when we die; killing it
        # in the job would lose the last fragment (K10)
        with tempfile.TemporaryDirectory() as tmp:
            gdigrab = self.fake_gdigrab(tmp)
            self.assertEqual(self.assigned_by(gdigrab), [True])
            self.assertTrue(gdigrab.finalized, gdigrab.error_tail)
            piped = SyntheticSource(0, os.path.join(tmp, "seg_001.mp4"), 30, walls_rel=[0.0, 40.0])
            self.assertEqual(self.assigned_by(piped), [])
            self.assertTrue(piped.finalized, piped.error_tail)

    @unittest.skipUnless(sys.platform == "win32", "Windows Job Objects")
    def test_gdigrab_ffmpeg_is_in_the_real_job_and_piped_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = videocap._KILL_JOB.assigned if videocap._KILL_JOB else 0
            seg = self.fake_gdigrab(tmp)
            seg.start()
            seg.stop(timeout=10)
            self.assertIsNotNone(videocap._KILL_JOB)
            self.assertEqual(videocap._KILL_JOB.assigned, before + 1, videocap._KILL_JOB.error)
            piped = SyntheticSource(0, os.path.join(tmp, "seg_001.mp4"), 30, walls_rel=[0.0, 40.0])
            piped.start()
            piped.stop()
            self.assertEqual(videocap._KILL_JOB.assigned, before + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
