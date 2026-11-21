"""Regression checks for FFmpeg outside PATH (including paths with spaces)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from packager.build import build, resolve_tools  # noqa: E402

try:  # Windows-only (ctypes.WinDLL); the packaging checks below run everywhere
    from recorder.recorder import Recorder, parse_args
    from recorder.videocap import VideoSegment
    WINDOWS_IMPORT_ERROR = None
except (ImportError, AttributeError, OSError) as exc:  # pragma: no cover - non-Windows
    Recorder = parse_args = VideoSegment = None
    WINDOWS_IMPORT_ERROR = exc

needs_windows = unittest.skipIf(Recorder is None, f"recorder needs Windows: {WINDOWS_IMPORT_ERROR}")


class MediaPathTest(unittest.TestCase):
    @needs_windows
    def test_recording_uses_configured_executable_outside_path(self):
        with tempfile.TemporaryDirectory(prefix="destudio paths ") as tmp:
            executable = str(Path(tmp, "media tools", "ffmpeg.exe"))
            rec = object.__new__(Recorder)
            rec.args = parse_args(["start", "--backend", "gdigrab", "--codec", "libx264",
                                   "--ffmpeg", executable])
            rec.stopped = threading.Event()
            rec.segments = []
            rec.seg_dir = tmp
            # Exercise real backend selection, without recording the test machine.
            with patch.object(VideoSegment, "start", autospec=True):
                rec.start_segment()
            self.assertEqual(rec.segment.ffmpeg, executable)
            self.assertEqual(rec.active_backend, "gdigrab")

    def test_packaging_uses_saved_tools_without_path(self):
        with tempfile.TemporaryDirectory(prefix="destudio paths ") as tmp:
            out = Path(tmp)
            raw = out / "raw"
            raw.mkdir()
            tool_dir = out / "media tools"
            tool_dir.mkdir()
            ffmpeg = str(tool_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
            ffprobe = str(tool_dir / ("ffprobe.exe" if os.name == "nt" else "ffprobe"))
            Path(ffmpeg).touch()
            Path(ffprobe).touch()
            (raw / "capture.json").write_text(json.dumps({"fps": 30, "ffmpeg": ffmpeg}), encoding="utf-8")
            (raw / "segments.json").write_text(json.dumps([{
                "index": 0, "file": "seg_000.mp4", "fps": 30, "frame_count": 2,
                "start_wall_ms": 1000, "end_wall_ms": 1067,
                "frames": [[0, 1000], [1, 1033.333]],
            }]), encoding="utf-8")
            (raw / "events.jsonl").write_text("", encoding="utf-8")

            def write_video(segments, path, ffmpeg="ffmpeg", raw_dir=None, ffprobe=None, fps=None):
                self.assertEqual(ffmpeg, str(tool_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")))
                self.assertEqual(ffprobe, str(tool_dir / ("ffprobe.exe" if os.name == "nt" else "ffprobe")))
                self.assertEqual(fps, 30)
                Path(path).write_bytes(b"test fixture")
                return True

            def probe(command, **kwargs):
                self.assertEqual(command[0], ffprobe)
                return SimpleNamespace(stdout=json.dumps({
                    "streams": [{"codec_name": "h264", "width": 640, "height": 480, "avg_frame_rate": "30/1"}],
                    "format": {"duration": "0.066667"},
                }), returncode=0)

            with patch.dict(os.environ, {"PATH": ""}), \
                    patch("packager.build.build_video", side_effect=write_video), \
                    patch("packager.build.subprocess.run", side_effect=probe):
                result = build(str(raw), str(out))
            self.assertEqual(result["total_frames"], 2)
            geometry = json.loads((out / "view" / "geometry.json").read_text(encoding="utf-8"))
            self.assertEqual((geometry["videoWidth"], geometry["videoHeight"]), (640, 480))

    def test_missing_saved_tools_fall_back_to_bundled_paths(self):
        with tempfile.TemporaryDirectory(prefix="destudio paths ") as tmp:
            bundled = Path(tmp, "bundled ffmpeg")
            bundled.mkdir()
            exe = ".exe" if os.name == "nt" else ""
            ffmpeg, ffprobe = bundled / ("ffmpeg" + exe), bundled / ("ffprobe" + exe)
            ffmpeg.touch()
            ffprobe.touch()
            moved = str(Path(tmp, "old install", "ffmpeg" + exe))  # recorded on another PC
            with patch.dict(os.environ, {"PATH": "", "DSTUDIO_FFMPEG": str(ffmpeg)}):
                self.assertEqual(resolve_tools({"ffmpeg": moved}), (str(ffmpeg), str(ffprobe)))
                # saved ffmpeg exists but its ffprobe is gone -> bundled ffprobe
                Path(moved).parent.mkdir()
                Path(moved).touch()
                self.assertEqual(resolve_tools({"ffmpeg": moved}), (moved, str(ffprobe)))
                self.assertEqual(resolve_tools({}), (str(ffmpeg), str(ffprobe)))

    @needs_windows
    def test_missing_executable_reports_the_configured_path(self):
        with tempfile.TemporaryDirectory(prefix="destudio paths ") as tmp:
            missing = str(Path(tmp, "missing ffmpeg.exe"))
            segment = VideoSegment(0, str(Path(tmp, "out.mp4")), 30,
                                   encoder="libx264", ffmpeg=missing)
            with self.assertRaises(FileNotFoundError) as error:
                segment.start()
            self.assertIn(missing, str(error.exception))


if __name__ == "__main__":
    unittest.main()
