"""CFR pacer + the shared capture -> queue -> ffmpeg-pipe machinery.

No GPU / no recording needed: the pipe tests use a fake frame source and a
tiny Python process standing in for ffmpeg (it counts the bytes on stdin).
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recorder.clock import wall_ms  # noqa: E402
from recorder.dxcam_capture import FramePacer  # noqa: E402
from recorder.videocap import Frame, PipedSegment  # noqa: E402


class PacerTest(unittest.TestCase):
    def test_one_capture_per_slot_when_healthy(self):
        p = FramePacer(30, t0=1_000.0)
        spans = []
        for i in range(6):
            spans.append(p.advance(1_000.0 + i * 1000 / 30))
        self.assertEqual(spans, [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])
        self.assertEqual(p.next_slot, 6)

    def test_stall_duplicates_previous_frame_to_keep_cfr(self):
        p = FramePacer(30, t0=0.0)
        self.assertEqual(p.advance(0.0), (0, 0))
        self.assertEqual(p.advance(33.0), (1, 1))
        # capture thread stalls for ~1s: every elapsed slot is filled by duplicates
        first, last = p.advance(1_000.0)
        self.assertEqual(first, 2)
        self.assertEqual(last, 30)
        self.assertEqual(last - first + 1, 29)   # 28 duplicates + the real frame
        self.assertEqual(p.next_slot, 31)

    def test_slot_never_goes_backwards_on_early_capture(self):
        p = FramePacer(30, t0=1_000.0)
        p.advance(1_000.0)
        p.advance(1_010.0)     # early capture maps into the current slot
        first, last = p.advance(1_005.0)  # out-of-order / jittery timestamp
        self.assertGreaterEqual(first, last)
        self.assertEqual(p.next_slot, last + 1)

    def test_early_capture_is_detected_so_it_can_be_dropped(self):
        p = FramePacer(30, t0=0.0)
        self.assertEqual(p.advance(0.0), (0, 0))
        self.assertTrue(p.is_early(33.0))          # slot 0 is already filled
        self.assertFalse(p.is_early(1000.0 / 30))  # exactly the start of slot 1
        self.assertEqual(p.slot_of(1000.0 / 30), 1)
        self.assertEqual(p.advance(40.0), (1, 1))
        self.assertTrue(p.is_early(66.0))
        self.assertFalse(p.is_early(67.0))

    def test_fast_timer_with_drops_stays_on_wall_time(self):
        # bettercam's timer period is 1000 // fps ms: 33 ms at 30 fps, 16 ms at 60 fps
        for fps in (30, 60):
            p = FramePacer(fps, t0=0.0)
            period = 1000 // fps
            frames = dups = early = 0
            t = 0.0
            while t <= 300_000.0:
                if p.is_early(t):
                    early += 1
                else:
                    first, last = p.advance(t)
                    frames += last - first + 1
                    dups += last - first
                t += period
            expected = int(300_000.0 * fps / 1000.0) + 1
            self.assertLessEqual(abs(frames - expected), 1, fps)   # was +3 s / +12.5 s of video
            self.assertEqual(dups, 0, fps)
            self.assertGreater(early, 0, fps)

    def test_backlog_is_detectable(self):
        p = FramePacer(30, t0=0.0)
        p.advance(0.0)
        first, last = p.advance(5_000.0)
        self.assertGreaterEqual(last - first + 1, 5 * 30)

    def test_60_and_120_fps_grids(self):
        for fps in (60, 120):
            p = FramePacer(fps, t0=500.0)
            spans = [p.advance(500.0 + i * 1000.0 / fps) for i in range(fps * 2)]
            self.assertEqual(spans, [(i, i) for i in range(fps * 2)], fps)
            self.assertAlmostEqual(p.slot_time(fps), 1500.0)
            # one second of stall at 60/120 fps -> fps-1 duplicates
            first, last = p.advance(500.0 + 3000.0)
            self.assertEqual(last, fps * 3)
            self.assertEqual(last - first, fps * 3 - fps * 2)

    def test_jitter_within_a_slot_keeps_one_frame_per_slot(self):
        p = FramePacer(60, t0=0.0)
        dt = 1000.0 / 60
        prev = -1
        for i in range(600):
            jitter = (i * 37 % 7) - 3          # +-3 ms scheduling noise
            first, last = p.advance(i * dt + dt * 0.5 + jitter)
            self.assertEqual(first, prev + 1)
            prev = last
        self.assertEqual(p.next_slot, 600)


# ------------------------------------------------------------------ pipe tests
_SINK = ("import sys, time\n"
         "out, delay = sys.argv[1], float(sys.argv[2])\n"
         "n = 0\n"
         "while True:\n"
         "    b = sys.stdin.buffer.read(65536)\n"
         "    if not b: break\n"
         "    n += len(b)\n"
         "    if delay: time.sleep(delay)\n"
         "open(out, 'w').write(str(n))\n")


class FakeSource(PipedSegment):
    """Paced synthetic source: optional stall once, optional frame failures."""

    backend = "fake"
    paced = True

    def __init__(self, *args, w=16, h=8, stall_after=None, stall_ms=0.0, sink_delay=0.0, **kw):
        super().__init__(*args, encoder="libx264", ffmpeg=sys.executable, **kw)
        self.w, self.h = w, h
        self.stall_after, self.stall_ms_once = stall_after, stall_ms
        self.sink_delay = sink_delay
        self.grabs = 0
        self.released = 0
        self.lock = threading.Lock()
        self.count_path = self.path + ".count"

    def _open(self):
        return self.w, self.h

    def _grab(self):
        self.grabs += 1
        if self.stall_after is not None and self.grabs == self.stall_after:
            time.sleep(self.stall_ms_once / 1000.0)
        buf = bytearray([self.grabs % 256]) * (self.w * self.h * 4)

        def release():
            with self.lock:
                self.released += 1
        return Frame(memoryview(buf), wall_ms(), masked=self.grabs % 2 == 0, release=release)

    def _build_cmd(self, w, h):
        return [sys.executable, "-c", _SINK, self.count_path, str(self.sink_delay)]

    def bytes_received(self):
        with open(self.count_path) as fh:
            return int(fh.read())


class PipedSegmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="destudio-pipe-")

    def tearDown(self):
        self.tmp.cleanup()

    def run_segment(self, seconds, fps, **kw):
        seg = FakeSource(0, os.path.join(self.tmp.name, "seg_000.mp4"), fps, **kw)
        seg.start()
        self.assertGreater(seg.start_wall_ms, 0)
        time.sleep(seconds)
        seg.stop()
        return seg

    def check_invariants(self, seg):
        slots = [n for n, _ in seg.frames]
        self.assertEqual(slots, list(range(len(slots))), "slots must be contiguous")
        walls = [t for _, t in seg.frames]
        self.assertEqual(walls, sorted(walls), "frame wall clock must not go backwards")
        self.assertEqual(seg.bytes_received(), seg.frame_count * seg.w * seg.h * 4)
        self.assertEqual(seg.frame_count, seg.captures + seg.duplicated_slots)
        self.assertEqual(seg.released, seg.captures + seg.early_captures, "every pooled buffer is handed back")
        d = seg.to_dict()
        for key in ("index", "file", "start_wall_ms", "end_wall_ms", "fps", "frame_count", "frames",
                    "encoder", "backend", "capture", "finalized", "returncode"):
            self.assertIn(key, d)
        self.assertEqual(d["frame_count"], len(d["frames"]))
        self.assertTrue(d["finalized"], "the sink exited 0 after EOF")
        self.assertEqual(d["returncode"], 0)
        self.assertFalse(d["in_progress"])
        self.assertFalse(seg.crashed)

    def test_60fps_cfr_frame_count(self):
        seg = self.run_segment(0.8, 60)
        self.check_invariants(seg)
        expected = (seg.end_wall_ms - seg.start_wall_ms) * 60 / 1000.0
        self.assertAlmostEqual(seg.frame_count, expected, delta=2)
        self.assertGreater(seg.frame_count, 0.8 * 60 * 0.75)
        self.assertGreater(seg.masked_frames, 0)

    def test_30fps_stall_becomes_duplicates(self):
        # K5: a duplicate-padded slot carries its nominal wall t0 + slot*dt, with
        # --sync-latency-ms already subtracted (like every other sidecar wall)
        latency = 20.0
        seg = self.run_segment(0.9, 30, stall_after=5, stall_ms=300, sync_latency_ms=latency)
        self.check_invariants(seg)
        self.assertGreaterEqual(seg.duplicated_slots, 6)       # ~300 ms at 30 fps
        walls = [t for _, t in seg.frames]
        self.assertTrue(all(a < b for a, b in zip(walls, walls[1:])),
                        "duplicate slots carry their nominal time, never a plateau")
        dt = 1000.0 / 30
        raw_t0 = seg._t0                                      # capture wall of slot 0
        self.assertAlmostEqual(seg.start_wall_ms, raw_t0 - latency, places=6)
        self.assertAlmostEqual(seg.frames[0][1], raw_t0 - latency, places=3)
        runs = seg.to_dict()["duplicate_runs"]
        self.assertTrue(runs)
        dup_slots = set()
        for first, last, content_wall in runs:
            for slot in range(int(first), int(last) + 1):
                dup_slots.add(slot)
                self.assertAlmostEqual(seg.frames[slot][1], raw_t0 + slot * dt - latency, places=6)
                self.assertAlmostEqual(seg.frames[slot][1], seg.start_wall_ms + slot * dt, places=6)
            self.assertLess(content_wall, seg.frames[int(first)][1])
        self.assertEqual(len(dup_slots), seg.duplicated_slots)
        for slot, wall in seg.frames:                         # real captures stay inside their slot
            if slot not in dup_slots:
                self.assertGreaterEqual(wall, seg.start_wall_ms + slot * dt - 1e-6)
                self.assertLess(wall, seg.start_wall_ms + (slot + 1) * dt)

    def test_slow_encoder_shows_up_as_duplicates_not_drift(self):
        # 256 KB frames, a reader managing ~20 frames/s: the queue fills, grabs are skipped
        seg = self.run_segment(0.7, 60, w=256, h=256, sink_delay=0.012, queue_frames=2)
        self.check_invariants(seg)
        self.assertGreater(seg.encoder_drops, 0)
        last_slot, last_wall = seg.frames[-1]
        # the CFR grid never runs ahead of real time
        self.assertLessEqual(seg.start_wall_ms + last_slot * 1000.0 / 60, last_wall + 1000.0 / 60 + 1)

    def test_start_failure_leaves_nothing_running(self):
        class Broken(FakeSource):
            def _grab(self):
                return None
        seg = Broken(0, os.path.join(self.tmp.name, "seg_001.mp4"), 30, stall_ms=200)
        seg._grab_first = lambda: (_ for _ in ()).throw(RuntimeError("no frames"))
        with self.assertRaises(RuntimeError):
            seg.start()
        self.assertIsNotNone(seg._proc)
        self.assertIsNotNone(seg._proc.poll(), "the encoder process must not be left running")


if __name__ == "__main__":
    unittest.main(verbosity=2)
