"""export_recording(): the Markov clip set without a task set or narration.

The real-FFmpeg check at the bottom skips when FFmpeg/FFprobe cannot run
(e.g. a broken Homebrew build); it runs on the Windows CI image.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from packager import export_cad as export_module  # noqa: E402
from packager.build import build  # noqa: E402
from packager.export_cad import EMPTY_NARRATION, export_recording  # noqa: E402
from recorder import reclock  # noqa: E402
from recorder.i18n import set_language  # noqa: E402
from recorder.paths import ffmpeg_path, ffprobe_path  # noqa: E402
from test_pipeline import (BASE, MASK, click, en, ev, fake_probe, jsonl, key, move, package,  # noqa: E402
                           screen, snapshot, write_session)

CLIP_SET = {"clip.mp4", "events.json", "frame_events.json", "metadata.json", "narration.json"}
MARKOV_KEYS = {"category", "platform", "total_duration_ms", "total_frames", "event_count", "fps"}


def make_dataset(root, fps=30, status="completed", origin=(0, 0), mask=MASK):
    """Two clips around an app-mode auto-pause, packaged with a placeholder video.

    ``origin`` places the captured monitor in the virtual screen; the input
    positions below are given relative to it.
    """
    ox, oy = origin
    screens = [screen(0, 0, 0, 1920, 1080, True)]
    if origin != (0, 0):
        screens.append(screen(1, ox, oy, 1920, 1080))
    s0, s1 = BASE + 1000, BASE + 1000 + 1000 + 3000
    events = snapshot(s0 - 300, ox + 10, oy + 10, screens) + [
        move(s0 + 100, ox + 200, oy + 200), click(s0 + 400, ox + 200, oy + 200, True),
        click(s0 + 450, ox + 200, oy + 200, False),
        ev("scroll", s0 + 500, x=ox + 210, y=oy + 220, deltaX=0, deltaY=-1, channel="wheel"),
        ev("drag", s0 + 600, x=ox + 230, y=oy + 240, button="left", pressure=1,
           buttons={"left": True, "right": False, "middle": False, "other_3": False, "other_4": False}),
        ev("window_moved", s0 + 650, x=ox + 5, y=oy + 6, width=800, height=600, appName="SOLIDWORKS"),
        ev("privacy_mask_start", s0 + 1003, **mask),
        *snapshot(s1 - 120, ox + 640, oy + 360, screens), ev("privacy_mask_end", s1 - 110, **mask),
        key(s1 + 300, True), key(s1 + 350, False)]
    n = fps  # one second per clip
    write_session(root, [(s0, n), (s1, n)], events, fps=fps, video=True,
                  capture={"capture_mode": "app", "source": "app:SLDWORKS.exe", "screens": screens,
                           "capture_rect": {"x": ox, "y": oy, "width": 1920, "height": 1080},
                           "cursor_in_video": True, "status": status})
    probe = fake_probe(1920, 1080, fps, 2 * n)
    with patch("subprocess.run", return_value=probe):
        package(root)
    return probe


class ExportRecordingTest(unittest.TestCase):
    def setUp(self):
        set_language("en", persist=False)
        self.tmp = Path(tempfile.mkdtemp(prefix="destudio-export-"))
        self.dataset = self.tmp / "rec-20261118-101500"
        self.dataset.mkdir()
        probe = make_dataset(str(self.dataset))
        self.patcher = patch("subprocess.run", return_value=probe)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def export(self, name="export"):
        return Path(export_recording(self.dataset, self.tmp / name))

    def test_writes_exactly_the_markov_clip_set(self):
        target = self.export()
        self.assertEqual(target, (self.tmp / "export").resolve())
        self.assertEqual({p.name for p in target.iterdir()}, CLIP_SET)
        self.assertEqual((target / "clip.mp4").read_bytes(), (self.dataset / "clip.mp4").read_bytes())
        meta = json.loads((target / "metadata.json").read_text(encoding="utf-8"))
        source_meta = json.loads((self.dataset / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(set(meta), MARKOV_KEYS)
        self.assertEqual(meta["category"], "cad")
        for k in MARKOV_KEYS - {"category"}:
            self.assertEqual(meta[k], source_meta[k])
        self.assertEqual(json.loads((target / "narration.json").read_text(encoding="utf-8")), EMPTY_NARRATION)
        self.assertFalse(list(self.tmp.glob(".rec-export-*")), "temporary folder left behind")

    def test_event_logs_are_jsonl_without_local_fields(self):
        target = self.export()
        for name in ("events.json", "frame_events.json"):
            text = (target / name).read_text(encoding="utf-8")
            self.assertTrue(text.startswith("{"), f"{name} must be JSONL, not a JSON array")
            self.assertNotIn("\r\n", text)
        events = jsonl(target / "events.json")
        frames = jsonl(target / "frame_events.json")
        meta = json.loads((target / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(len(events), meta["event_count"])
        self.assertEqual(len(frames), meta["total_frames"])
        for e in events:
            self.assertNotIn("seq", e)
            self.assertNotIn("osTime", e)
        for i, fr in enumerate(frames):
            self.assertEqual((fr["frame"], fr["video_t_ms"]), (i, i * 1000 / 30))
            for e in fr["events"]:
                self.assertNotIn("seq", e)
                self.assertNotIn("osTime", e)
        # privacy masks survive the export in the right frames
        self.assertEqual(frames[29]["events"][-1]["type"], "privacy_mask_start")
        self.assertIn("privacy_mask_end", [e["type"] for e in frames[30]["events"]])
        # the dataset keeps seq/osTime for tracing
        self.assertIn("seq", jsonl(self.dataset / "events.json")[0])

    def test_same_per_file_transformation_as_markov_cad_export(self):
        task = self.tmp / "task"
        task.mkdir()
        for name in ("task.pdf", "task_desc.json", "rubrics.json", "references.zip", "gold.zip"):
            (task / name).write_bytes(b"fixture")
        cad = self.tmp / "cad"
        cad.mkdir()
        export_module._write_markov(self.dataset.resolve(), task, cad)
        target = self.export()
        for name in CLIP_SET:
            self.assertEqual((target / name).read_bytes(), (cad / name).read_bytes(), name)

    def test_existing_narration_is_copied(self):
        narration = {"summary": "Model a bracket", "annotations": [
            {"start_s": 0, "end_s": 2, "action": "Sketch", "intent": "Base", "app": "SOLIDWORKS", "details": ""}]}
        (self.dataset / "narration.json").write_text(json.dumps(narration), encoding="utf-8")
        target = self.export()
        self.assertEqual(json.loads((target / "narration.json").read_text(encoding="utf-8")), narration)

    def test_missing_narration_gets_the_empty_template(self):
        (self.dataset / "narration.json").unlink()
        target = self.export()
        self.assertEqual(json.loads((target / "narration.json").read_text(encoding="utf-8")), EMPTY_NARRATION)

    def test_delivery_items_are_not_required(self):
        self.export()
        report = json.loads((self.dataset / "validation.json").read_text(encoding="utf-8"))
        self.assertTrue(report["technical_ok"])
        self.assertFalse(report["ready_for_delivery"])   # no task set / session info / narration
        self.assertTrue(report["decode_verified"])

    def test_refuses_to_overwrite(self):
        existing = self.tmp / "existing"
        existing.mkdir()
        (existing / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError) as failure:
            export_recording(self.dataset, existing)
        self.assertIn("already exists", str(failure.exception))
        self.assertEqual([p.name for p in existing.iterdir()], ["keep.txt"])
        for bad in (self.dataset, self.tmp):
            with self.assertRaises(ValueError):
                export_recording(self.dataset, bad)
        self.export("first")
        with self.assertRaises(ValueError):
            self.export("first")

    def set_status(self, status):
        capture = json.loads((self.dataset / "capture.json").read_text(encoding="utf-8"))
        capture["status"] = status
        (self.dataset / "capture.json").write_text(json.dumps(capture), encoding="utf-8")

    def live(self):
        """The recorder is still writing raw/ (K11): status "recording", lock held."""
        self.set_status("recording")
        lock = reclock.acquire(str(self.dataset / "raw"))
        self.assertIsNotNone(lock, "could not take the recording lock")
        return lock

    def test_requires_technical_checks(self):
        with self.live():
            with self.assertRaises(ValueError) as failure:
                self.export()
        self.assertIn("Technical checks failed", str(failure.exception))
        self.assertIn("still in progress", str(failure.exception))
        self.assertFalse((self.tmp / "export").exists())
        self.assertFalse(list(self.tmp.glob(".rec-export-*")))

    def test_recording_recovered_after_a_crash_is_exported_with_a_warning(self):
        # K11: status never reached "completed" and nobody holds the lock (crash / killed recorder)
        for status in ("failed", "recording"):
            with self.subTest(status=status):
                self.set_status(status)
                target = self.export("recovered-" + status)
                self.assertEqual({p.name for p in target.iterdir()}, CLIP_SET)
                report = json.loads((self.dataset / "validation.json").read_text(encoding="utf-8"))
                self.assertTrue(report["technical_ok"], report["technical_errors"])
                self.assertTrue(any("recovered after an abnormal stop" in w for w in en(report["warnings"])))

    def test_frame_count_mismatch_blocks_export(self):
        self.patcher.stop()
        self.patcher = patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 59))
        self.patcher.start()
        with self.assertRaises(ValueError):
            self.export()

    def test_error_message_follows_ui_language(self):
        (self.tmp / "taken").mkdir()
        set_language("zh", persist=False)
        with self.assertRaises(ValueError) as failure:
            export_recording(self.dataset, self.tmp / "taken")
        self.assertIn("已存在", str(failure.exception))

    def test_positions_are_video_pixels_in_the_delivered_clip_set(self):
        """App mode on a monitor left of / below the primary: origin (-1920, 200)."""
        shutil.rmtree(self.dataset)
        self.dataset.mkdir()
        make_dataset(str(self.dataset), origin=(-1920, 200))
        target = self.export()
        local = {e["seq"]: e for e in jsonl(self.dataset / "events.json")}
        exported = jsonl(target / "events.json")
        self.assertEqual(len(exported), len(local))
        by_type = {}
        for raw, out in zip(local.values(), exported):
            self.assertEqual(raw["type"], out["type"])
            by_type.setdefault(out["type"], []).append((raw, out))
        for kind, expected in (("click", (200, 200)), ("scroll", (210, 220)), ("drag", (230, 240)),
                               ("window_moved", (5, 6))):
            raw, out = by_type[kind][0]
            self.assertEqual((out["x"], out["y"]), expected, kind)
            self.assertEqual((raw["x"], raw["y"]), (expected[0] - 1920, expected[1] + 200), kind)
        self.assertEqual(by_type["window_moved"][0][1]["width"], 800)
        moves = [out for _, out in by_type["mouse_move"]]
        self.assertTrue(all(0 <= m["x"] < 1920 and 0 <= m["y"] < 1080 for m in moves), moves)
        # screen_config keeps the real monitor layout
        screens = by_type["screen_config"][0][1]["screens"]
        self.assertEqual(screens[1]["originX"], -1920)
        frames = jsonl(target / "frame_events.json")
        local_frames = jsonl(self.dataset / "frame_events.json")
        for fr, lf in zip(frames, local_frames):
            synthetic = fr["events"][0]
            self.assertEqual((synthetic["x"], synthetic["y"]),
                             (lf["events"][0]["x"] + 1920, lf["events"][0]["y"] - 200))
            for e in fr["events"]:
                if e["type"] in ("click", "scroll", "drag", "mouse_move"):
                    self.assertTrue(0 <= e["x"] < 1920 and 0 <= e["y"] < 1080, e)
        self.assertEqual((frames[0]["events"][0]["x"], frames[0]["events"][0]["y"]), (10, 10))
        self.assertEqual((frames[30]["events"][0]["x"], frames[30]["events"][0]["y"]), (640, 360))
        drag = next(e for fr in frames for e in fr["events"] if e.get("source") == "raw_drag_frame_state")
        self.assertEqual((drag["x"], drag["y"]), (230, 240))
        # the local dataset and the annotator keep virtual-screen pixels
        self.assertEqual(local_frames[0]["events"][0]["x"], -1910)
        report = json.loads((self.dataset / "validation.json").read_text(encoding="utf-8"))
        self.assertEqual(report["offscreen_cursor_frames"], 0)

    def test_cursor_on_another_monitor_is_exported_as_is_with_a_warning(self):
        """App mode on the left monitor (-1920, 200); the cursor visits the primary."""
        shutil.rmtree(self.dataset)
        self.dataset.mkdir()
        screens = [screen(0, 0, 0, 2560, 1440, True), screen(1, -1920, 200, 1920, 1080)]
        s0 = BASE + 1000
        events = snapshot(s0 - 300, -1900, 220, screens) + [
            move(s0 + 300, 500, 500), click(s0 + 400, 500, 500, True), click(s0 + 450, 500, 500, False),
            move(s0 + 600, -1000, 700), key(s0 + 700, True), key(s0 + 750, False)]
        write_session(str(self.dataset), [(s0, 30)], events, video=True,
                      capture={"capture_mode": "app", "source": "app:SLDWORKS.exe", "screens": screens,
                               "capture_rect": {"x": -1920, "y": 200, "width": 1920, "height": 1080},
                               "cursor_in_video": True})
        self.patcher.stop()
        self.patcher = patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 30))
        self.patcher.start()
        package(str(self.dataset))
        target = self.export()
        report = json.loads((self.dataset / "validation.json").read_text(encoding="utf-8"))
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        frames = jsonl(target / "frame_events.json")
        away = [fr for fr in frames if fr["events"][0]["x"] >= 1920]
        self.assertEqual(report["offscreen_cursor_frames"], len(away))
        self.assertEqual(len(away), 9)                     # 300 ms .. 600 ms
        self.assertTrue(any("outside the video in 9 frame(s)" in w for w in en(report["warnings"])))
        # kept, not clamped, and no extra field in the delivered events
        self.assertEqual((away[0]["events"][0]["x"], away[0]["events"][0]["y"]), (2420, 300))
        click_out = next(e for e in jsonl(target / "events.json") if e["type"] == "click")
        self.assertEqual((click_out["x"], click_out["y"]), (2420, 300))
        for fr in frames:
            for e in fr["events"]:
                self.assertFalse({"offscreen", "outside"} & set(e), e)

    def test_video_scaled_from_the_capture_area_scales_positions(self):
        from packager.export_cad import _to_video_px, video_transform
        geometry = {"originX": 100, "originY": 50, "width": 2560, "height": 1440,
                    "videoWidth": 1280, "videoHeight": 720}
        (self.dataset / "view" / "geometry.json").write_text(json.dumps(geometry), encoding="utf-8")
        transform = video_transform(self.dataset)
        self.assertEqual(transform, (100.0, 50.0, 0.5, 0.5))
        self.assertEqual(_to_video_px({"type": "click", "x": 2660, "y": 1490}, transform),
                         {"type": "click", "x": 1280, "y": 720})
        self.assertEqual(_to_video_px({"type": "key", "x": 5}, transform), {"type": "key", "x": 5})
        self.assertEqual(_to_video_px({"type": "mouse_delta", "dx": 4, "dy": 2}, transform),
                         {"type": "mouse_delta", "dx": 4, "dy": 2})

    def test_policy_mask_cut_from_the_video_is_exported(self):
        policy = dict(MASK, trigger="policy_blocked_app", maskTrigger="policy_blocked_app",
                      maskDetail="1password.exe", appName="1Password", exeName="1Password.exe")
        shutil.rmtree(self.dataset)
        self.dataset.mkdir()
        make_dataset(str(self.dataset), mask=policy)
        target = self.export()
        types = [e["type"] for e in jsonl(target / "events.json")]
        self.assertEqual(types.count("privacy_mask_start"), 1)

    def test_privacy_mask_still_in_the_video_blocks_export(self):
        # an older recorder only *marked* the password manager: video kept running
        policy = dict(MASK, trigger="policy_blocked_app", maskTrigger="policy_blocked_app")
        root = self.tmp / "rec-old"
        root.mkdir()
        s0 = BASE + 1000
        events = snapshot(s0 - 300, 10, 10, [screen(0, 0, 0, 1920, 1080, True)]) + [
            move(s0 + 50, 20, 20), click(s0 + 100, 20, 20, True), click(s0 + 120, 20, 20, False),
            ev("privacy_mask_start", s0 + 200, **policy), key(s0 + 300, True), key(s0 + 340, False),
            ev("privacy_mask_end", s0 + 800, **policy)]
        write_session(str(root), [(s0, 30)], events, video=True,
                      capture={"capture_rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}})
        self.patcher.stop()
        self.patcher = patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 30))
        self.patcher.start()
        package(str(root))
        with self.assertRaises(ValueError) as failure:
            export_recording(root, self.tmp / "old-export")
        self.assertIn("privacy", str(failure.exception))
        self.assertFalse((self.tmp / "old-export").exists())
        set_language("zh", persist=False)
        with self.assertRaises(ValueError) as failure:
            export_recording(root, self.tmp / "old-export")
        self.assertIn("隐私", str(failure.exception))

    def test_technical_errors_are_rendered_in_the_ui_language(self):
        set_language("zh", persist=False)
        with self.live():
            with self.assertRaises(ValueError) as failure:
                self.export()
        self.assertIn("录制仍在进行中", str(failure.exception))
        self.assertNotIn("{'zh'", str(failure.exception))

    def test_keys_logged_during_a_cut_privacy_pause_block_export(self):
        """v0.1: F9 cut the video but kept logging keys, typed into a password manager."""
        policy = dict(MASK, trigger="policy_blocked_app", maskTrigger="policy_blocked_app",
                      maskDetail="keepass.exe", appName="KeePass", exeName="KeePass.exe")
        root = self.tmp / "rec-v01"
        root.mkdir()
        screens = [screen(0, 0, 0, 1920, 1080, True)]
        s0, s1 = BASE + 1000, BASE + 1000 + 1000 + 5000
        events = snapshot(s0 - 300, 10, 10, screens) + [
            move(s0 + 100, 200, 200), click(s0 + 400, 200, 200, True), click(s0 + 450, 200, 200, False),
            ev("privacy_mask_start", s0 + 2000, **policy),
            ev("key", s0 + 2500, keyCode=80, characters="p", isDown=True, modifiers=[]),
            ev("key", s0 + 2550, keyCode=80, characters="p", isDown=False, modifiers=[]),
            ev("privacy_mask_end", s0 + 4000, **policy),
            *snapshot(s1 - 100, 300, 300, screens), key(s1 + 300, True), key(s1 + 350, False)]
        write_session(str(root), [(s0, 30), (s1, 30)], events, video=True,
                      capture={"capture_rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}})
        self.patcher.stop()
        self.patcher = patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 60))
        self.patcher.start()
        package(str(root))
        with self.assertRaises(ValueError) as failure:
            export_recording(root, self.tmp / "v01-export")
        self.assertIn("privacy", str(failure.exception))
        self.assertFalse((self.tmp / "v01-export").exists())
        report = json.loads((root / "validation.json").read_text(encoding="utf-8"))
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertEqual(report["uncut_privacy_masks"], 1)

    def test_cli_recording_only(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = export_module.main(["--recording-only", "--dataset", str(self.dataset),
                                       "--out", str(self.tmp / "cli")])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(out.getvalue().strip(), str((self.tmp / "cli").resolve()))
        with redirect_stdout(io.StringIO()):
            again = export_module.main(["--recording-only", "--dataset", str(self.dataset),
                                        "--out", str(self.tmp / "cli")])
        self.assertEqual(again, 2)


def _tool_runs(path):
    try:
        return subprocess.run([path, "-hide_banner", "-version"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


FFMPEG, FFPROBE = ffmpeg_path(), ffprobe_path()
MEDIA_OK = _tool_runs(FFMPEG) and _tool_runs(FFPROBE)


FRAGMENTED = ["-movflags", "+frag_keyframe+empty_moov+default_base_moof"]   # v0.2.1 raw segments (K10)
FASTSTART = ["-movflags", "+faststart"]                                     # v0.2 raw segments


@unittest.skipUnless(MEDIA_OK, f"FFmpeg/FFprobe not runnable here ({FFMPEG}, {FFPROBE})")
class RealMediaExportTest(unittest.TestCase):
    """Real libx264 segments -> package -> validate -> export (skips without FFmpeg)."""

    W, H, FPS = 320, 240, 60

    def setUp(self):
        set_language("en", persist=False)
        self.tmp = Path(tempfile.mkdtemp(prefix="destudio-media-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def encode(self, path, frames, hue, fps=None, size=None, mux=FRAGMENTED):
        fps = fps or self.FPS
        w, h = size or (self.W, self.H)
        r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", f"testsrc2=size={w}x{h}:rate={fps}",
                            "-frames:v", str(frames), "-vf", f"hue=h={hue}",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                            "-g", str(2 * fps), *mux, "-r", str(fps), str(path)], capture_output=True)
        if r.returncode != 0:
            self.skipTest("this FFmpeg build has no libx264 (Markov clips are libx264)")

    def session(self, name, counts, fps=30, sizes=None, mux=FRAGMENTED, edit=None, capture=None):
        """Recording with one click per clip; returns (root, raw)."""
        root = self.tmp / name
        root.mkdir()
        screens = [screen(0, 0, 0, self.W, self.H, True)]
        starts, t = [], BASE + 1000
        events = []
        for n in counts:
            starts.append(t)
            events += snapshot(t - 100, 10, 10, screens) + [click(t + 20, 30, 40, True),
                                                            click(t + 25, 30, 40, False)]
            t += n * 1000.0 / fps + 1500
        events.append(key(starts[0] + 10, True))
        cap = {"capture_mode": "desktop", "screens": screens, "ffmpeg": FFMPEG, "cursor_in_video": True,
               "capture_rect": {"x": 0, "y": 0, "width": self.W, "height": self.H}}
        cap.update(capture or {})
        raw = write_session(str(root), list(zip(starts, counts)), events, fps=fps, capture=cap)
        for i, n in enumerate(counts):
            self.encode(Path(raw, "segments", f"seg_{i:03d}.mp4"), n, 40 * i, fps=fps,
                        size=(sizes or {}).get(i), mux=mux)
        if edit:
            segs = json.loads(Path(raw, "segments.json").read_text(encoding="utf-8"))
            for i, change in edit.items():
                segs[i].update(change)
            Path(raw, "segments.json").write_text(json.dumps(segs), encoding="utf-8")
        return root, raw

    def probe(self, path):
        out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-count_frames",
                              "-show_entries", "stream=codec_name,width,height,avg_frame_rate,nb_read_frames"
                              ":format=duration", "-of", "json", str(path)],
                             capture_output=True, text=True, check=True)
        data = json.loads(out.stdout)
        return data["streams"][0], float(data["format"]["duration"])

    def test_package_validate_and_export(self):
        root = self.tmp / "rec-media"
        root.mkdir()
        screens = [screen(0, 0, 0, 1920, 1080, True), screen(1, -self.W, 0, self.W, self.H)]
        s0, s1 = BASE + 1000, BASE + 1000 + 1000 + 2000
        events = snapshot(s0 - 200, -100, 100, screens) + [
            click(s0 + 300, -100, 100, True), click(s0 + 340, -100, 100, False),
            ev("privacy_mask_start", s0 + 1002, **MASK),
            *snapshot(s1 - 100, -50, 50, screens), ev("privacy_mask_end", s1 - 90, **MASK),
            key(s1 + 200, True), key(s1 + 260, False)]
        raw = write_session(str(root), [(s0, self.FPS), (s1, self.FPS)], events, fps=self.FPS,
                            capture={"capture_mode": "app", "source": "app:SLDWORKS.exe", "screens": screens,
                                     "capture_rect": {"x": -self.W, "y": 0, "width": self.W, "height": self.H},
                                     "cursor_in_video": True, "ffmpeg": FFMPEG})
        for i, hue in enumerate((0, 90)):
            self.encode(Path(raw, "segments", f"seg_{i:03d}.mp4"), self.FPS, hue)
        build(raw, str(root), dataset_id="rec-media", category="cad")
        report = json.loads((root / "validation.json").read_text(encoding="utf-8"))
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertTrue(report["full_screen"])
        self.assertTrue(any("60 fps" in w for w in en(report["warnings"])))
        self.assertFalse([w for w in en(report["warnings"]) if "encoder" in w])
        target = Path(export_recording(root, self.tmp / "delivery"))
        self.assertEqual({p.name for p in target.iterdir()}, CLIP_SET)
        stream, _ = self.probe(target / "clip.mp4")
        self.assertEqual(int(stream["nb_read_frames"]), 2 * self.FPS)
        self.assertEqual((stream["width"], stream["height"]), (self.W, self.H))
        self.assertEqual(stream["avg_frame_rate"], f"{self.FPS}/1")
        self.assertEqual(len(jsonl(target / "frame_events.json")), 2 * self.FPS)
        # the click at virtual x=-100 is 220 px into the 320 px wide video
        clicks = [e for e in jsonl(target / "events.json") if e["type"] == "click"]
        self.assertEqual({(e["x"], e["y"]) for e in clicks}, {(self.W - 100, 100)})

    def test_odd_segment_lengths_keep_an_exact_frame_rate(self):
        """Finding 13: 37/41-frame segments used to concat at 29.99 fps."""
        for mux, name in ((FASTSTART, "v02"), (FRAGMENTED, "v021")):
            root, raw = self.session("rec-odd-" + name, [37, 41, 37, 37, 41, 37], mux=mux)
            build(raw, str(root), dataset_id="rec-odd", category="cad")
            report = json.loads((root / "validation.json").read_text(encoding="utf-8"))
            self.assertTrue(report["technical_ok"], (name, report["technical_errors"]))
            stream, duration = self.probe(root / "clip.mp4")
            self.assertEqual(stream["avg_frame_rate"], "30/1", name)
            self.assertEqual(int(stream["nb_read_frames"]), 230, name)
            self.assertAlmostEqual(duration, 230 / 30, delta=0.002)

    def test_single_fragmented_segment_becomes_a_faststart_mp4(self):
        root, raw = self.session("rec-single", [45])
        self.assertIn(b"moof", Path(raw, "segments", "seg_000.mp4").read_bytes())
        build(raw, str(root), dataset_id="rec-single", category="cad")
        data = (root / "clip.mp4").read_bytes()
        self.assertNotIn(b"moof", data)
        self.assertLess(data.index(b"moov"), data.index(b"mdat"))
        report = json.loads((root / "validation.json").read_text(encoding="utf-8"))
        self.assertTrue(report["technical_ok"], report["technical_errors"])

    def test_mixed_resolutions_are_refused(self):
        root, raw = self.session("rec-mixed", [30, 30], sizes={1: (2 * self.W, self.H)})
        with self.assertRaises(ValueError) as failure:
            build(raw, str(root), dataset_id="rec-mixed", category="cad")
        self.assertIn(f"{2 * self.W}x{self.H}", str(failure.exception))
        self.assertFalse((root / "clip.mp4").exists())

    def test_crash_cut_segment_is_packaged_from_its_decodable_frames(self):
        # a crashed recorder never writes status "completed" (K2)
        root, raw = self.session("rec-crash", [60, 150], capture={"status": "recording"},
                                 edit={1: {"in_progress": True, "finalized": False, "returncode": None}})
        seg = Path(raw, "segments", "seg_001.mp4")
        data = seg.read_bytes()
        seg.write_bytes(data[:int(len(data) * 0.8)])            # power lost while writing
        build(raw, str(root), dataset_id="rec-crash", category="cad")
        meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
        kept = meta["clips"][1]["frame_count"]
        self.assertTrue(0 < kept < 150, kept)
        stream, _ = self.probe(root / "clip.mp4")
        self.assertEqual(int(stream["nb_read_frames"]), 60 + kept)
        self.assertEqual(stream["avg_frame_rate"], "30/1")
        report = json.loads((root / "validation.json").read_text(encoding="utf-8"))
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertTrue(any("decodable" in w for w in en(report["warnings"])))
        self.assertTrue(any("recovered after an abnormal stop" in w for w in en(report["warnings"])))
        export_recording(root, self.tmp / "crash-export")       # full decode check passes too

    def test_stale_sidecar_of_a_crashed_recorder_is_extended(self):
        """K2: the recorder died, FFmpeg finished the file; the sidecar lags 20 frames."""
        root, raw = self.session("rec-stale", [60, 90], edit={1: {"in_progress": True}})
        segs = json.loads(Path(raw, "segments.json").read_text(encoding="utf-8"))
        segs[1]["frames"] = segs[1]["frames"][:70]
        Path(raw, "segments.json").write_text(json.dumps(segs), encoding="utf-8")
        build(raw, str(root), dataset_id="rec-stale", category="cad")
        meta = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["total_frames"], 150)
        report = json.loads((root / "validation.json").read_text(encoding="utf-8"))
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertTrue(any("nominal frame interval" in w for w in en(report["warnings"])))

    def test_broken_segment_is_left_out(self):
        root, raw = self.session("rec-broken", [30, 30, 30],
                                 edit={1: {"broken": True, "frames": [], "finalized": False, "returncode": 1}})
        Path(raw, "segments", "seg_001.mp4").write_bytes(b"\0" * 64)      # unreadable leftovers
        build(raw, str(root), dataset_id="rec-broken", category="cad")
        stream, _ = self.probe(root / "clip.mp4")
        self.assertEqual(int(stream["nb_read_frames"]), 60)
        report = json.loads((root / "validation.json").read_text(encoding="utf-8"))
        self.assertTrue(report["technical_ok"], report["technical_errors"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
