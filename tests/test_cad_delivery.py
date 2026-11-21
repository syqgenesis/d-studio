"""Lifecycle, frame alignment, and fail-closed delivery regression checks."""
import ctypes
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recorder.i18n import set_language  # noqa: E402
from packager.build import build_clips, write_frame_events  # noqa: E402
from packager.validate import validate_dataset  # noqa: E402
from packager.export_cad import export_cad  # noqa: E402

try:  # Windows-only (ctypes.WinDLL); the packager checks below run everywhere
    from recorder.recorder import Recorder
    from recorder.raw_mouse import HEADER, MOUSE, relative_packet
    WINDOWS_IMPORT_ERROR = None
except (ImportError, AttributeError, OSError) as exc:  # pragma: no cover - non-Windows
    Recorder = HEADER = MOUSE = relative_packet = None
    WINDOWS_IMPORT_ERROR = exc


@unittest.skipIf(Recorder is None, f"recorder needs Windows: {WINDOWS_IMPORT_ERROR}")
class StopTest(unittest.TestCase):
    def make_recorder(self):
        rec = object.__new__(Recorder)
        rec.lock = threading.RLock()
        rec.stopped = threading.Event()
        rec._stop_done = threading.Event()
        rec._stopped_at = rec._stop_error = rec._paused_at = None
        rec.started_at, rec.paused_ms, rec.paused = 1000, 0, False
        rec.segments = [SimpleNamespace(frame_count=90, encoder="libx264")]
        rec.segment = None
        rec.args = SimpleNamespace(fps=30)
        rec._hook = rec._watcher = None
        rec.log, rec.desktop = Mock(count=10), Mock()
        rec._write_capture = Mock()
        rec._write_segments = Mock()
        rec._write_segments_safe = Mock()      # v0.2.1 crash-safe sidecar (K2)
        rec.active_backend, rec.out, rec.raw = "gdigrab", "unused", "unused"
        return rec

    def test_stop_freezes_time_without_blocking_ui_and_runs_once(self):
        rec = self.make_recorder()
        entered, finish = threading.Event(), threading.Event()
        def slow_stop():
            entered.set()
            if not finish.wait(3):
                raise TimeoutError()
        rec.stop_segment = Mock(side_effect=slow_stop)
        with patch("recorder.recorder.wall_ms", return_value=5000):
            worker = threading.Thread(target=rec.stop)
            worker.start()
            self.assertTrue(entered.wait(1))
            with patch("recorder.recorder.wall_ms", return_value=9000):
                before = time.perf_counter()
                self.assertEqual(rec.snapshot()["elapsed_ms"], 4000)
                self.assertLess(time.perf_counter() - before, 0.2)
            finish.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        with patch("recorder.recorder.wall_ms", return_value=999999):
            self.assertEqual(rec.snapshot()["elapsed_ms"], 3000)
            rec.stop()
        rec.stop_segment.assert_called_once()

    def test_stop_during_pause_excludes_remaining_pause(self):
        rec = self.make_recorder()
        rec.paused, rec._paused_at = True, 3000
        rec._stopped_at = 7000
        self.assertEqual(rec.snapshot()["elapsed_ms"], 2000)

    def test_failure_unblocks_other_stop_callers(self):
        rec = self.make_recorder()
        rec.stop_segment = Mock(side_effect=OSError("test failure"))
        with self.assertRaises(OSError):
            rec.stop()
        self.assertTrue(rec._stop_done.is_set())
        with self.assertRaises(OSError):
            rec.stop()


class AlignmentTest(unittest.TestCase):
    def test_irregular_sidecar_maps_event_to_actual_frame(self):
        clips = build_clips([{"frames": [[0, 1000], [1, 1010], [2, 1060]]}], 30, 30000)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)
            event = {"type": "click", "timestamp": 1030, "seq": 1, "x": 10, "y": 20, "isDown": True, "button": "left"}
            write_frame_events(clips, iter([event]), 30, str(path / "frames.json"), str(path / "view"))
            frames = [json.loads(s) for s in (path / "frames.json").read_text().splitlines()]
            self.assertFalse(any(e.get("seq") == 1 for e in frames[0]["events"]))
            click = next(e for e in frames[1]["events"] if e.get("seq") == 1)
            self.assertAlmostEqual(click["video_t_ms"], 46.6666666667)
            mouse = [json.loads(s) for s in (path / "view" / "mouse.jsonl").read_text().splitlines()]
            self.assertEqual(mouse[1]["x"], frames[1]["events"][0]["x"])

    @unittest.skipIf(HEADER is None, f"raw mouse parser needs Windows: {WINDOWS_IMPORT_ERROR}")
    def test_raw_mouse_uses_device_counts_and_excludes_absolute_packets(self):
        header = HEADER(type=0, size=ctypes.sizeof(HEADER) + ctypes.sizeof(MOUSE))
        mouse = MOUSE(dx=-123, dy=89)
        self.assertEqual(relative_packet(bytes(header) + bytes(mouse)), (-123, 89))
        mouse.flags = 1
        self.assertIsNone(relative_packet(bytes(header) + bytes(mouse)))


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        set_language("zh", persist=False)
        self.tmp = tempfile.TemporaryDirectory(prefix="cad-delivery-test-")
        self.root = pathlib.Path(self.tmp.name) / "dataset"
        self.root.mkdir()
        self.task = self.root.parent / "tasks"
        self.task.mkdir()
        self.write("task.pdf", "%PDF-1.7\nfixture", self.task)
        self.write("task_desc.json", {"title": "fixture task"}, self.task)
        self.write("rubrics.json", {"criteria": ["fixture"]}, self.task)
        for name, files in (("references.zip", ["reference.txt"]), ("gold.zip", ["part.sldprt", "part.step"])):
            with zipfile.ZipFile(self.task / name, "w") as z:
                for f in files:
                    z.writestr(f, "test fixture only")
        self.write("license.pdf", "%PDF-fixture")
        self.write("authorization.pdf", "%PDF-fixture")
        meta = {"workflow_id": "test", "total_frames": 2, "fps": 30, "total_duration_ms": 67,
                "clips": [{"clip_index": 0, "segment_index": 0, "frame_start": 0, "frame_count": 2,
                           "source_start_ms": 1000, "source_start_precise_ms": 1000.0,
                           "source_end_ms": 1000 + 2000 / 30, "gap_before_ms": 0, "gap_before_clamped": False}],
                "video_file": "clip.mp4", "event_count": 3, "engineer_id": "fixture", "part_id": "fixture",
                "authorization_reference": "authorization.pdf", "software": {"name": "fixture", "version": "1",
                "interface_language": "en", "license_reference": "license.pdf"},
                "review": {k: True for k in ("sync_verified", "quality_reviewed", "originality_confirmed", "task_completed")}}
        self.write("metadata.json", meta)
        self.write("capture.json", {"status": "completed", "source": "desktop", "ffmpeg": "ffmpeg",
                                     "input_health": {"raw_mouse_available": True}, "session_info": {"task_set": str(self.task)}})
        events = [{"type": k, "seq": i + 1, "timestamp": 1000 + i} for i, k in enumerate(("mouse_move", "click", "key"))]
        self.write("events.json", "\n".join(json.dumps(e) for e in events))
        rows = []
        for f in range(2):
            mapped = [{**e, "video_t_ms": 0, "t_in_frame_ms": 0} for e in events] if f == 0 else []
            rows.append({"frame": f, "video_t_ms": f * 1000 / 30, "events": [
                {"type": "mouse_move", "source": "frame_timestamps_sidecar", "video_t_ms": f * 1000 / 30,
                 "t_in_frame_ms": 0}] + mapped})
        self.write("frame_events.json", "\n".join(json.dumps(r) for r in rows))
        self.write("narration.json", {"summary": "fixture", "annotations": [{"start_s": 0, "end_s": 2/30,
                                                                            "action": "fixture", "intent": "fixture", "app": "fixture", "details": ""}]})
        self.write("clip.mp4", "test fixture only")
        self.probe = patch("packager.validate.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps({
            "streams": [{"codec_name": "h264", "width": 640, "height": 480, "avg_frame_rate": "30/1",
                         "nb_frames": "2", "nb_read_frames": "2", "tags": {"encoder": "Lavc libx264"}}],
            "format": {"duration": "0.066667"}})))
        self.probe.start()

    def tearDown(self):
        self.probe.stop()
        self.tmp.cleanup()

    def write(self, name, data, root=None):
        ((root or self.root) / name).write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")

    def test_complete_fixture_exports_with_exact_customer_names_and_hashes(self):
        target = self.root.parent / "delivery"
        export_cad(self.root, target)
        clip = target / "clip_set" / self.root.name
        for name in ("clip.mp4", "events.json", "frame_events.json", "narration.json", "metadata.json"):
            self.assertTrue((clip / name).is_file())
        meta = json.loads((clip / "metadata.json").read_text(encoding="utf-8"))
        self.assertTrue((clip / meta["software"]["license_reference"]).is_file())
        self.assertTrue((target / "delivery_manifest.json").is_file())

    def make_markov_task(self):
        inputs = ["task_files/diagrams/input.png"]
        outputs = ["output_files/project_files/main.SLDASM", "output_files/project_files/part.SLDPRT",
                   "output_files/project_files/main.step", "output_files/project_files/main.x_t"]
        for name in inputs + outputs:
            path = self.task / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test fixture only")
        for archive in ("references.zip", "gold.zip"):
            (self.task / archive).unlink()
        self.write("task_desc.json", {"task": "Test assembly", "domain": "CAD", "application": "SOLIDWORKS",
                                     "description": "Synthetic test data", "input_files": inputs,
                                     "deliverables": [{"path": name, "type": "native_project_or_dependency",
                                                       "format": pathlib.Path(name).suffix.lower(), "description": "Test fixture"}
                                                      for name in outputs]}, self.task)
        self.write("rubrics.json", {"schema_version": "1.0", "rubrics": [
            {"id": "R1", "requirement": "Synthetic test requirement"}]}, self.task)
        meta = json.loads((self.root / "metadata.json").read_text())
        meta["platform"] = "windows"
        self.write("metadata.json", meta)
        return inputs, outputs

    def test_markov_export_matches_sample_layout_and_zip_members(self):
        inputs, outputs = self.make_markov_task()
        target = self.root.parent / "markov"
        export_cad(self.root, target, markov=True)
        self.assertEqual({p.name for p in target.iterdir()}, {
            "task.pdf", "task_desc.json", "rubrics.json", "references.zip", "gold.zip",
            "clip.mp4", "events.json", "frame_events.json", "narration.json", "metadata.json",
        })
        for archive, paths in (("references.zip", inputs), ("gold.zip", outputs)):
            with zipfile.ZipFile(target / archive) as bundle:
                self.assertCountEqual(bundle.namelist(), paths)
        meta = json.loads((target / "metadata.json").read_text())
        self.assertEqual(meta, {"category": "cad", "platform": "windows", "total_duration_ms": 67,
                                "total_frames": 2, "event_count": 3, "fps": 30})
        events = [json.loads(line) for line in (target / "events.json").read_text().splitlines()]
        self.assertEqual([event["type"] for event in events], ["mouse_move", "click", "key"])
        self.assertNotIn("seq", events[0])
        frame = json.loads((target / "frame_events.json").read_text().splitlines()[0])
        self.assertEqual(frame["events"][-1]["type"], "key")
        self.assertNotIn("seq", frame["events"][-1])
        self.assertIn("seq", json.loads((self.root / "events.json").read_text().splitlines()[0]))

    def test_markov_blocks_missing_and_unlisted_model_files(self):
        _, outputs = self.make_markov_task()
        (self.task / outputs[-1]).unlink()
        (self.task / "output_files" / "project_files" / "extra.step").write_bytes(b"fixture")
        with self.assertRaises(ValueError) as failure:
            export_cad(self.root, self.root.parent / "incomplete", markov=True)
        self.assertIn(outputs[-1], str(failure.exception))
        self.assertIn("extra.step", str(failure.exception))
        self.assertFalse((self.root.parent / "incomplete").exists())

    def test_task_set_issues_are_stored_in_both_languages(self):
        _, outputs = self.make_markov_task()
        (self.task / "output_files" / "project_files" / "extra.step").write_bytes(b"fixture")
        with self.assertRaises(ValueError) as failure:
            export_cad(self.root, self.root.parent / "incomplete", markov=True)
        self.assertIn("\u6e05\u5355\u5916", str(failure.exception))   # zh UI: "not in the manifest"
        report = json.loads((self.root / "validation.json").read_text(encoding="utf-8"))
        unlisted = next(m for m in report["delivery_missing"] if "extra.step" in m["en"])
        self.assertIn("unlisted", unlisted["en"])
        self.assertIn("\u6e05\u5355\u5916", unlisted["zh"])
        for message in report["technical_errors"] + report["delivery_missing"] + report["warnings"]:
            self.assertEqual(set(message), {"zh", "en"})

    def test_bad_frame_mapping_is_detected(self):
        path = self.root / "frame_events.json"
        path.write_text(path.read_text().replace('"video_t_ms": 0,', '"video_t_ms": 1000,'), encoding="utf-8")
        self.assertFalse(validate_dataset(self.root)["technical_ok"])

    def test_missing_narration_blocks_export_without_creating_delivery(self):
        self.write("narration.json", {"summary": "", "annotations": []})
        target = self.root.parent / "bad-delivery"
        with self.assertRaises(ValueError):
            export_cad(self.root, target)
        self.assertFalse(target.exists())

    def test_missing_key_from_frame_log_is_detected(self):
        path = self.root / "frame_events.json"
        rows = [json.loads(s) for s in path.read_text().splitlines()]
        rows[0]["events"] = [event for event in rows[0]["events"] if event["type"] != "key"]
        self.write("frame_events.json", "\n".join(json.dumps(row) for row in rows))
        self.assertFalse(validate_dataset(self.root)["technical_ok"])

    def test_missing_clip_table_blocks_delivery(self):
        meta = json.loads((self.root / "metadata.json").read_text(encoding="utf-8"))
        del meta["clips"]
        self.write("metadata.json", meta)
        report = validate_dataset(self.root)
        self.assertFalse(report["technical_ok"])
        self.assertIn("metadata 的 clip 表格式错误", [m["zh"] for m in report["technical_errors"]])
        with self.assertRaises(ValueError):
            export_cad(self.root, self.root.parent / "no-clips")
        self.assertFalse((self.root.parent / "no-clips").exists())

    def test_missing_segment_and_input_loss_are_not_silently_accepted(self):
        capture = json.loads((self.root / "capture.json").read_text())
        capture["input_health"]["dropped_events"] = 1
        self.write("capture.json", capture)
        self.assertFalse(validate_dataset(self.root)["technical_ok"])


if __name__ == "__main__":
    unittest.main()
