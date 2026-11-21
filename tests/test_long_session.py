"""v0.2.2 hardening for long sessions on real workstations.

* full disk: the recorder warns below 3 GB, stops cleanly below 1 GB, and the
  bar refuses to start below 3 GB; a full disk while packaging fails fast
  instead of falling through to a long re-encode;
* repeated clip crashes surface in the bar instead of a silent running timer;
* DXGI: a small ring buffer, and a first frame that cannot block forever.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from packager import build  # noqa: E402
from recorder import dxcam_capture, gui  # noqa: E402
from recorder import recorder as recmod  # noqa: E402

Usage = namedtuple("Usage", "total used free")
GB = 1024 ** 3


def usage(free_gb: float) -> Usage:
    return Usage(500 * GB, 0, int(free_gb * GB))


def make_recorder(out: str):
    rec = object.__new__(recmod.Recorder)
    rec.stopped = threading.Event()
    rec.out = out
    rec.last_error = ""
    rec.stop = Mock(side_effect=rec.stopped.set)
    return rec


class DiskGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def check(self, rec, free_gb):
        rec._disk_due = 0.0
        with patch.object(recmod.shutil, "disk_usage", return_value=usage(free_gb)):
            return rec._check_disk()

    def test_plenty_of_space_is_silent(self):
        rec = make_recorder(self.tmp)
        self.assertFalse(self.check(rec, 50))
        self.assertEqual(rec.last_error, "")
        rec.stop.assert_not_called()

    def test_low_space_warns_then_clears(self):
        rec = make_recorder(self.tmp)
        self.assertFalse(self.check(rec, 2.5))
        self.assertIn("2.5", rec.last_error)
        rec.stop.assert_not_called()
        self.assertFalse(self.check(rec, 20))           # space freed
        self.assertEqual(rec.last_error, "")

    def test_warning_does_not_replace_another_problem(self):
        rec = make_recorder(self.tmp)
        rec.last_error = "overlay problem"
        self.check(rec, 2.0)
        self.assertEqual(rec.last_error, "overlay problem")

    def test_nearly_full_stops_cleanly(self):
        rec = make_recorder(self.tmp)
        rec.last_error = "overlay problem"
        self.assertTrue(self.check(rec, 0.4))
        rec.stop.assert_called_once()
        self.assertIn("0.4", rec.last_error)            # the stop reason wins
        self.assertFalse(self.check(rec, 0.1))          # already stopped: nothing more
        rec.stop.assert_called_once()

    def test_checked_only_every_few_seconds(self):
        rec = make_recorder(self.tmp)
        rec._disk_due = time.monotonic() + 60
        with patch.object(recmod.shutil, "disk_usage", side_effect=AssertionError("too often")):
            self.assertFalse(rec._check_disk())

    def test_unreadable_drive_is_ignored(self):
        rec = make_recorder(self.tmp)
        rec._disk_due = 0.0
        with patch.object(recmod.shutil, "disk_usage", side_effect=OSError("gone")):
            self.assertFalse(rec._check_disk())
        self.assertEqual(rec.last_error, "")


class CrashLoopTest(unittest.TestCase):
    def test_three_crashes_in_a_minute_reach_the_bar(self):
        rec = make_recorder("unused")
        rec._note_crash("x\nffmpeg: Conversion failed!\n")
        rec._note_crash("y")
        self.assertEqual(rec.last_error, "")
        rec._note_crash("frame=1\nav_interleaved_write_frame(): No space left on device\n\n")
        self.assertIn("No space left on device", rec.last_error)

    def test_old_crashes_do_not_count_and_warning_clears(self):
        rec = make_recorder("unused")
        rec._crash_times = [time.monotonic() - 3600, time.monotonic() - 3500]
        rec._note_crash("z")
        self.assertEqual(rec.last_error, "")
        for _ in range(3):
            rec._note_crash("boom")
        self.assertTrue(rec.last_error)
        rec._clear_crash_warning()                      # too recent: kept
        self.assertTrue(rec.last_error)
        rec._crash_times = [time.monotonic() - 10 * recmod.CRASH_LOOP_WINDOW_S]
        rec._clear_crash_warning()
        self.assertEqual(rec.last_error, "")

    def test_does_not_replace_another_problem(self):
        rec = make_recorder("unused")
        rec.last_error = "disk"
        for _ in range(5):
            rec._note_crash("boom")
        self.assertEqual(rec.last_error, "disk")


class StartPreflightTest(unittest.TestCase):
    def test_disk_free_bytes(self):
        self.assertGreater(gui.disk_free_bytes(tempfile.gettempdir()), 0)
        self.assertIsNone(gui.disk_free_bytes(os.path.join(tempfile.gettempdir(), "no", "such", "dir")))

    def test_start_threshold_matches_recorder_warning(self):
        self.assertEqual(gui.MIN_FREE_START_BYTES, recmod.DISK_WARN_BYTES)
        self.assertGreater(recmod.DISK_WARN_BYTES, recmod.DISK_STOP_BYTES)


class PackagerDiskFullTest(unittest.TestCase):
    def test_messages(self):
        self.assertTrue(build.is_disk_full("av_interleaved_write_frame(): No space left on device"))
        self.assertTrue(build.is_disk_full("Error writing trailer: Error number -28 occurred"))
        self.assertTrue(build.is_disk_full("There is not enough space on the disk."))
        self.assertFalse(build.is_disk_full("Invalid data found when processing input"))
        self.assertFalse(build.is_disk_full(""))

    def _segments(self, tmp):
        raw = os.path.join(tmp, "raw")
        os.makedirs(os.path.join(raw, "segments"))
        with open(os.path.join(raw, "segments", "seg_000.mp4"), "wb") as fh:
            fh.write(b"\0" * 64)
        return [{"file": "seg_000.mp4", "frames": [[0, 0.0]], "frame_count": 1, "fps": 30}], raw

    def test_disk_full_skips_the_reencode(self):
        tmp = tempfile.mkdtemp()
        segs, raw = self._segments(tmp)
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            return SimpleNamespace(returncode=1, stderr="No space left on device", stdout="")

        with patch.object(build, "check_segment_sizes"), patch.object(build, "_video_frame_count", return_value=1), \
                patch.object(build.subprocess, "run", side_effect=run):
            with self.assertRaises(RuntimeError) as ctx:
                build.build_video(segs, os.path.join(tmp, "clip.mp4"), raw_dir=raw)
        self.assertEqual(len(calls), 1)                  # copy only, no re-encode
        self.assertIn("-c", calls[0][0])
        self.assertEqual(calls[0][1]["timeout"], build.CONCAT_COPY_TIMEOUT_S)
        self.assertTrue("磁盘" in str(ctx.exception) or "disk" in str(ctx.exception).lower())

    def test_copy_timeout_falls_back_to_reencode_without_limit(self):
        tmp = tempfile.mkdtemp()
        segs, raw = self._segments(tmp)
        calls = []

        def run(cmd, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise build.subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return SimpleNamespace(returncode=1, stderr="other failure", stdout="")

        with patch.object(build, "check_segment_sizes"), patch.object(build, "_video_frame_count", return_value=1), \
                patch.object(build.subprocess, "run", side_effect=run):
            with self.assertRaises(RuntimeError):
                build.build_video(segs, os.path.join(tmp, "clip.mp4"), raw_dir=raw)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[1]["timeout"])


class DxgiTest(unittest.TestCase):
    def test_first_frame_returns_the_frame(self):
        cam = Mock(spec=["get_latest_frame"], get_latest_frame=Mock(return_value="frame"))
        self.assertEqual(dxcam_capture.first_frame(cam, 1.0), "frame")

    def test_first_frame_times_out_instead_of_hanging(self):
        release = threading.Event()
        cam = Mock(spec=["get_latest_frame"], get_latest_frame=Mock(side_effect=lambda: release.wait(10) and None))
        t0 = time.monotonic()
        self.assertIsNone(dxcam_capture.first_frame(cam, 0.2))
        self.assertLess(time.monotonic() - t0, 2.0)
        release.set()

    def test_first_frame_reraises_library_errors(self):
        cam = Mock(spec=["get_latest_frame"], get_latest_frame=Mock(side_effect=OSError("access lost")))
        with self.assertRaises(OSError):
            dxcam_capture.first_frame(cam, 1.0)

    def test_latest_frame_copies_under_the_library_lock(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")

        class BetterCam:                         # same private layout as bettercam 1.0.0
            def __init__(self):
                self.max_buffer_len = 4
                self.__lock = threading.Lock()
                self.__frame_available = threading.Event()
                self.__frame_buffer = np.zeros((4, 2, 2, 4), dtype=np.uint8)
                self.__head = 0

            def push(self, value):
                with self.__lock:
                    self.__frame_buffer[self.__head] = value
                    self.__head = (self.__head + 1) % self.max_buffer_len
                    self.__frame_available.set()

            def locked(self):
                return self.__lock.locked()

            def get_latest_frame(self):
                raise AssertionError("must not use the unlocked copy")

        cam = BetterCam()
        cam.push(7)
        cam.push(9)
        seen = {}
        real_array = np.array

        def spy(obj, *a, **kw):
            seen["locked"] = cam.locked()
            return real_array(obj, *a, **kw)

        with patch.object(np, "array", side_effect=spy):
            frame = dxcam_capture.latest_frame(cam)
        self.assertTrue(seen["locked"])
        self.assertEqual(int(frame[0, 0, 0]), 9)                  # newest slot
        cam.push(11)
        self.assertEqual(int(frame[0, 0, 0]), 9)                  # a private copy
        self.assertEqual(int(dxcam_capture.first_frame(cam, 1.0)[0, 0, 0]), 11)

    def test_latest_frame_falls_back_without_library_internals(self):
        cam = Mock(spec=["get_latest_frame"], get_latest_frame=Mock(return_value="frame"))
        self.assertEqual(dxcam_capture.latest_frame(cam), "frame")

    def test_small_ring_buffer(self):
        self.assertLessEqual(dxcam_capture.RING_FRAMES, 8)
        src = open(dxcam_capture.__file__, encoding="utf-8").read()
        self.assertIn("max_buffer_len=RING_FRAMES", src)
        self.assertNotIn("self._camera.get_latest_frame()", src)


if __name__ == "__main__":
    unittest.main()
