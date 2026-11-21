"""End-to-end pipeline checks on a synthetic dataset (no recording required).

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from packager.build import build, split_gap, view_geometry  # noqa: E402
from packager.validate import message_text, validate_dataset  # noqa: E402
from recorder import reclock  # noqa: E402
from recorder.i18n import set_language  # noqa: E402

BASE = 1_800_000_000_000.0
MASK = {"trigger": "non_target_app", "maskTrigger": "non_target_app", "maskDetail": "wechat.exe",
        "appName": "WeChat", "exeName": "WeChat.exe", "windowTitle": "", "maskedDisplay": 1}


def jsonl(path):
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def screen(index, x, y, w, h, primary=False):
    return {"index": index, "originX": x, "originY": y, "width": w, "height": h,
            "visibleWidth": w, "visibleHeight": h - 40, "scaleFactor": 1.0, "clipIndex": 0,
            "deviceName": f"\\\\.\\DISPLAY{index + 1}", "primary": primary}


def write_session(root, segments, events, *, fps=30, capture=None, video=False):
    """Synthetic raw/ capture: ``segments`` = [(first_frame_wall_ms, frame_count)].

    ``events`` get seq/osTime in timestamp order.  ``video=True`` drops a
    placeholder clip.mp4 in the package so validation probes it (tests patch
    subprocess.run with :func:`fake_probe`).
    """
    raw = os.path.join(root, "raw")
    os.makedirs(os.path.join(raw, "segments"), exist_ok=True)
    ordered = sorted(events, key=lambda e: e["timestamp"])
    with open(os.path.join(raw, "events.jsonl"), "w", encoding="utf-8") as fh:
        for i, ev in enumerate(ordered, 1):
            rec = dict(ev)
            rec.setdefault("seq", i)
            rec.setdefault("osTime", int(rec["timestamp"] - BASE) + 100_000)
            fh.write(json.dumps(rec) + "\n")
    segs = []
    for i, (start, count) in enumerate(segments):
        segs.append({"index": i, "file": f"seg_{i:03d}.mp4", "fps": fps, "frame_count": count,
                     "start_wall_ms": start, "end_wall_ms": start + count * 1000.0 / fps,
                     "frames": [[k, start + k * 1000.0 / fps] for k in range(count)]})
        open(os.path.join(raw, "segments", f"seg_{i:03d}.mp4"), "wb").close()
    with open(os.path.join(raw, "segments.json"), "w", encoding="utf-8") as fh:
        json.dump(segs, fh)
    cap = {"version": 1, "status": "completed", "platform": "windows", "fps": fps,
           "source": "desktop", "ffmpeg": "ffmpeg", "screens": [screen(0, 0, 0, 1920, 1080, True)],
           "input_health": {"raw_mouse_available": True}}
    cap.update(capture or {})
    with open(os.path.join(raw, "capture.json"), "w", encoding="utf-8") as fh:
        json.dump(cap, fh)
    if video:
        with open(os.path.join(root, "clip.mp4"), "wb") as fh:
            fh.write(b"placeholder video for tests")
    return raw


def fake_probe(width, height, fps, frames, codec="h264", encoder="Lavc60.3.100 libx264"):
    """subprocess.run stand-in answering every ffprobe call for a clip."""
    payload = json.dumps({
        "streams": [{"codec_name": codec, "width": width, "height": height, "avg_frame_rate": f"{fps}/1",
                     "nb_frames": str(frames), "nb_read_frames": str(frames), "tags": {"encoder": encoder}}],
        "format": {"duration": str(frames / fps)}})
    return SimpleNamespace(stdout=payload, stderr="", returncode=0)


def en(messages):
    """English text of report messages (validation.json stores both languages)."""
    return [message_text(m, "en") for m in messages]


def package(root, **kwargs):
    kwargs.setdefault("dataset_id", "test")
    kwargs.setdefault("category", "cad")
    return build(os.path.join(root, "raw"), root, skip_video=True, **kwargs)


def ev(etype, ts, **fields):
    rec = {"type": etype, "timestamp": float(ts)}
    rec.update(fields)
    return rec


def click(ts, x, y, down, button="left"):
    return ev("click", ts, x=x, y=y, button=button, isDown=down, clickCount=1, pressure=1,
              appName="SOLIDWORKS", bundleId="SLDWORKS.exe", screen=0, sourcePid=0, modifiers=0)


def key(ts, down, code=83):
    return ev("key", ts, keyCode=code, characters="s" if down else "", isDown=down, modifiers=0,
              isRepeat=False, scanCode=31, extended=False, hkl="04090409", dead=False,
              appName="SOLIDWORKS", bundleId="SLDWORKS.exe", sourcePid=0)


def move(ts, x, y):
    return ev("mouse_move", ts, x=x, y=y, appName="SOLIDWORKS", bundleId="SLDWORKS.exe",
              screen=0, sourcePid=0)


def snapshot(ts, x, y, screens):
    """What a v0.2 recorder writes when a segment opens (start or resume)."""
    return [ev("screen_config", ts, screenCount=len(screens), screens=screens, reason="open"),
            ev("active_app", ts, name="SOLIDWORKS", bundleId="SLDWORKS.exe", windowTitle="Part1",
               pid=4242, isFullScreen=False, hwnd=77, windowClass="Afx"),
            ev("appearance_change", ts + 0.2, appearance="light"),
            ev("memory_pressure", ts + 0.3, pressureLevel="normal"),
            move(ts + 0.4, x, y)]


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="destudio-test-")
        cls.out = os.path.join(cls.tmp, "demo")
        subprocess.run([sys.executable, os.path.join(ROOT, "tools", "make_demo.py"),
                        "--out", cls.out, "--no-video"], check=True, cwd=ROOT,
                       stdout=subprocess.DEVNULL)
        with open(os.path.join(cls.out, "metadata.json"), encoding="utf-8") as fh:
            cls.meta = json.load(fh)
        cls.frames = jsonl(os.path.join(cls.out, "frame_events.json"))
        cls.events = jsonl(os.path.join(cls.out, "events.json"))
        cls.mouse = jsonl(os.path.join(cls.out, "view", "mouse.jsonl"))
        with open(os.path.join(cls.out, "view", "geometry.json"), encoding="utf-8") as fh:
            cls.geo = json.load(fh)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ layout
    def test_deliverable_files_exist(self):
        for name in ("metadata.json", "events.json", "frame_events.json", "narration.json",
                     os.path.join("view", "mouse.jsonl"), os.path.join("view", "events.jsonl"),
                     os.path.join("view", "geometry.json")):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)

    def test_clips_are_gapless_and_match_frame_count(self):
        clips = self.meta["clips"]
        self.assertEqual(len(clips), 2, "demo has one pause -> two clips")
        expected = 0
        for c in clips:
            self.assertEqual(c["frame_start"], expected)
            expected += c["frame_count"]
        self.assertEqual(self.meta["total_frames"], expected)
        self.assertEqual(self.meta["total_duration_ms"],
                         round(expected * 1000 / self.meta["fps"]))
        self.assertEqual(clips[1]["gap_before_ms"], 4000)

    # --------------------------------------------------------------- frame data
    def test_frame_events_shape(self):
        fps = self.meta["fps"]
        self.assertEqual(len(self.frames), self.meta["total_frames"])
        for i, fr in enumerate(self.frames):
            self.assertEqual(fr["frame"], i)
            self.assertAlmostEqual(fr["video_t_ms"], i * 1000.0 / fps, places=6)
            first = fr["events"][0]
            self.assertEqual(first["type"], "mouse_move")
            self.assertEqual(first["source"], "frame_timestamps_sidecar")
            self.assertTrue(first["synthetic"])
            self.assertEqual(first["t_in_frame_ms"], 0)
            for ev in fr["events"]:
                self.assertIn("video_t_ms", ev)
                self.assertGreaterEqual(ev["t_in_frame_ms"], -1e-9)
                self.assertLessEqual(ev["t_in_frame_ms"], 1000.0 / fps + 1e-6)

    def test_mouse_index_matches_frames(self):
        self.assertEqual(len(self.mouse), self.meta["total_frames"])
        for i, m in enumerate(self.mouse):
            self.assertEqual(m["f"], i)
            self.assertGreaterEqual(m["x"], 0)
            self.assertGreaterEqual(m["y"], 0)

    def test_geometry_is_pixel_space(self):
        self.assertEqual(self.geo["width"], 1280)
        self.assertEqual(self.geo["height"], 720)
        self.assertEqual(self.geo["originX"], 0)

    # ------------------------------------------------------------ event mapping
    def test_event_count_matches_metadata(self):
        self.assertEqual(len(self.events), self.meta["event_count"])

    def test_events_sorted_and_unique_seq(self):
        prev_ts = -1.0
        seqs = set()
        for ev in self.events:
            self.assertGreaterEqual(ev["timestamp"], prev_ts - 1e-6)
            prev_ts = ev["timestamp"]
            self.assertNotIn(ev["seq"], seqs)
            seqs.add(ev["seq"])

    def test_wallclock_maps_onto_video_timeline(self):
        """Recompute the documented mapping from metadata.clips and compare."""
        fps = self.meta["fps"]
        clips = self.meta["clips"]
        by_seq = {}
        for fr in self.frames:
            for ev in fr["events"]:
                if "seq" in ev:
                    by_seq[ev["seq"]] = (fr["frame"], ev)
        self.assertTrue(by_seq)
        checked = 0
        for ev in self.events:
            hit = by_seq.get(ev["seq"])
            if hit is None:
                continue  # dropped inside a removed gap
            frame, packaged = hit
            clip = next(c for c in clips
                        if c["frame_start"] <= frame < c["frame_start"] + c["frame_count"])
            predicted = clip["frame_start"] * 1000.0 / fps + (ev["timestamp"] - clip["source_start_ms"])
            if predicted < 0:
                # pre-roll event: clamped into frame 0 by design
                self.assertEqual(frame, 0)
                self.assertEqual(packaged["video_t_ms"], 0.0)
                continue
            self.assertAlmostEqual(packaged["video_t_ms"], predicted, delta=1.0,
                                   msg=f"seq {ev['seq']} {ev['type']}")
            self.assertEqual(frame, int(packaged["video_t_ms"] * fps / 1000.0))
            checked += 1
        self.assertGreater(checked, 20)

    def test_gap_events_are_dropped_but_state_carries_over(self):
        with open(os.path.join(self.out, "capture.json"), encoding="utf-8") as fh:
            capture = json.load(fh)
        self.assertGreater(capture["packager"]["dropped_events_in_gaps"], 0)
        # cursor state at the first frame of clip 2 comes from the dropped gap events
        clip2_start = self.meta["clips"][1]["frame_start"]
        self.assertEqual(self.frames[clip2_start]["events"][0]["x"], 1040)
        self.assertEqual(self.frames[clip2_start]["events"][0]["y"], 620)

    def test_preroll_events_land_on_frame_zero(self):
        types = {e["type"] for e in self.frames[0]["events"]}
        self.assertIn("screen_config", types)
        self.assertIn("active_app", types)
        self.assertTrue(all(e["t_in_frame_ms"] == 0 for e in self.frames[0]["events"]
                            if e["type"] in ("screen_config", "active_app", "appearance_change")))

    def test_drag_frames_carry_button_state(self):
        with_drag = [fr for fr in self.frames if any(e["type"] == "drag" for e in fr["events"])]
        self.assertTrue(with_drag, "demo contains a drag")
        for fr in with_drag[:5]:
            move = fr["events"][0]
            drag = next(e for e in fr["events"] if e["type"] == "drag")
            self.assertTrue(drag["aggregated_count"] >= 1)
            self.assertIn("left", move["buttons"])
            self.assertEqual(drag["source"], "raw_drag_frame_state")

    def test_repack_is_idempotent(self):
        with open(os.path.join(self.out, "frame_events.json"), "rb") as fh:
            before = fh.read()
        build(os.path.join(self.out, "raw"), self.out, dataset_id=self.meta["workflow_id"],
              title=self.meta["title"], skip_video=True)
        with open(os.path.join(self.out, "frame_events.json"), "rb") as fh:
            after = fh.read()
        self.assertEqual(before, after)


    def test_metadata_records_capture_mode(self):
        self.assertEqual(self.meta["capture_mode"], "desktop")
        self.assertIs(self.meta["cursor_in_video"], False)


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        set_language("en", persist=False)
        self.tmp = tempfile.mkdtemp(prefix="destudio-session-")
        self.root = os.path.join(self.tmp, "rec-test")
        os.makedirs(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load(self):
        with open(os.path.join(self.root, "metadata.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(os.path.join(self.root, "validation.json"), encoding="utf-8") as fh:
            report = json.load(fh)
        return meta, jsonl(os.path.join(self.root, "frame_events.json")), report

    def assert_each_discrete_event_once(self, frames):
        seen = {}
        for fr in frames:
            for e in fr["events"]:
                if "seq" in e:
                    self.assertNotIn(e["seq"], seen, f"seq {e['seq']} in frames {seen.get(e['seq'])} and {fr['frame']}")
                    seen[e["seq"]] = fr["frame"]
        return seen


class AppModeTest(SessionTestCase):
    SCREENS = [screen(0, 0, 0, 2560, 1440, primary=True), screen(1, -1920, 200, 1920, 1080)]

    def make(self, video_size=(1920, 1080), **capture):
        cap = {"capture_mode": "app", "source": "app:SLDWORKS.exe", "screens": self.SCREENS,
               "capture_rect": {"x": -1920, "y": 200, "width": 1920, "height": 1080},
               "cursor_in_video": True,
               "app_target": {"exe": "SLDWORKS.exe", "allow": ["textinputhost.exe"], "auto_pause": True},
               "mask_stats": {"masked_frames": 12, "auto_pauses": 0, "auto_paused_ms": 0}}
        cap.update(capture)
        start = BASE + 1000
        events = snapshot(start - 300, -1000, 700, self.SCREENS) + [
            move(start + 100, -900, 650), click(start + 500, -900, 650, True),
            click(start + 560, -900, 650, False), key(start + 700, True), key(start + 760, False)]
        write_session(self.root, [(start, 30)], events, capture=cap, video=True)
        with patch("subprocess.run", return_value=fake_probe(*video_size, 30, 30)):
            package(self.root)
        return self.load()

    def test_geometry_maps_from_the_captured_monitor_with_negative_origin(self):
        meta, frames, report = self.make()
        with open(os.path.join(self.root, "view", "geometry.json"), encoding="utf-8") as fh:
            geo = json.load(fh)
        self.assertEqual((geo["originX"], geo["originY"], geo["width"], geo["height"]), (-1920, 200, 1920, 1080))
        self.assertEqual((geo["videoWidth"], geo["videoHeight"]), (1920, 1080))
        self.assertEqual(geo["captureMode"], "app")
        self.assertEqual(meta["capture_mode"], "app")
        self.assertIs(meta["cursor_in_video"], True)
        self.assertEqual(frames[0]["events"][0]["x"], -1000)
        self.assertEqual(frames[4]["events"][0]["x"], -900)

    def test_app_mode_with_matching_video_counts_as_full_screen(self):
        _, _, report = self.make()
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertTrue(report["full_screen"])
        self.assertFalse([m for m in en(report["delivery_missing"]) if "full-screen" in m])
        self.assertFalse([w for w in en(report["warnings"]) if "cursor" in w])

    def test_app_mode_compares_against_monitor_not_desktop_union(self):
        # the union of both monitors would be 4480x1440; the video shows one monitor
        _, _, report = self.make(video_size=(4480, 1440))
        self.assertFalse(report["technical_ok"])
        self.assertTrue(any("1920x1080" in e for e in en(report["technical_errors"])), report["technical_errors"])

    def test_odd_monitor_cropped_to_even_is_accepted_with_warning(self):
        for video in ((1364, 768), (1366, 768)):          # v0.2.1 crops; older recorders padded
            with self.subTest(video=video):
                _, _, report = self.make(video_size=video,
                                         capture_rect={"x": 0, "y": 0, "width": 1365, "height": 768})
                self.assertTrue(report["technical_ok"], report["technical_errors"])
                note = next(m for m in report["warnings"] if "rounded to an even size" in m["en"])
                self.assertIn("取整到偶数", note["zh"])
                self.assertNotIn("补齐", note["zh"])

    def test_app_mode_without_capture_rect_uses_primary_monitor(self):
        geo = view_geometry({"capture_mode": "app", "screens": self.SCREENS}, {})
        self.assertEqual((geo["originX"], geo["width"], geo["videoWidth"]), (0, 2560, 2560))


class CaptureModeTest(SessionTestCase):
    def run_mode(self, capture, video_size=(800, 600)):
        start = BASE + 1000
        write_session(self.root, [(start, 30)], [move(start + 10, 5, 5), click(start + 20, 5, 5, True)],
                      capture=capture, video=True)
        with patch("subprocess.run", return_value=fake_probe(*video_size, 30, 30)):
            package(self.root)
        return self.load()

    def test_region_capture_is_a_full_screen_gap(self):
        meta, _, report = self.run_mode({"source": "region:10,20,800,600"})
        self.assertEqual(meta["capture_mode"], "region")
        self.assertFalse(report["full_screen"])
        self.assertTrue(any("full-screen" in m for m in en(report["delivery_missing"])))
        with open(os.path.join(self.root, "view", "geometry.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["originX"], 10)

    def test_window_capture_is_a_full_screen_gap(self):
        meta, _, report = self.run_mode({"source": "window:Notepad", "capture_mode": "window"})
        self.assertEqual(meta["capture_mode"], "window")
        self.assertTrue(any("full-screen" in m for m in en(report["delivery_missing"])))

    def test_legacy_capture_json_without_new_fields(self):
        screens = [screen(0, 0, 0, 1920, 1080, True), screen(1, 1920, 0, 800, 600)]
        meta, frames, report = self.run_mode({"source": "monitor:1", "screens": screens})
        self.assertEqual(meta["capture_mode"], "monitor")
        self.assertIs(meta["cursor_in_video"], False)
        with open(os.path.join(self.root, "view", "geometry.json"), encoding="utf-8") as fh:
            geo = json.load(fh)
        self.assertEqual((geo["originX"], geo["width"], geo["height"]), (1920, 800, 600))
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertTrue(report["full_screen"])
        self.assertTrue(any("cursor" in w for w in en(report["warnings"])))
        with open(os.path.join(self.root, "capture.json"), encoding="utf-8") as fh:
            capture = json.load(fh)
        self.assertEqual((capture["capture_mode"], capture["cursor_in_video"]), ("monitor", False))

    def test_desktop_resolution_mismatch_is_still_an_error(self):
        _, _, report = self.run_mode({}, video_size=(1280, 720))
        self.assertFalse(report["technical_ok"])

    def test_messages_carry_both_languages(self):
        set_language("zh", persist=False)
        _, _, report = self.run_mode({"source": "region:0,0,800,600"})
        self.assertEqual(report["language"], "zh")
        for key in ("technical_errors", "delivery_missing", "warnings"):
            for message in report[key]:
                self.assertEqual(set(message), {"zh", "en"}, message)
        gap = next(m for m in report["delivery_missing"] if "全屏" in m["zh"])
        self.assertIn("full-screen", gap["en"])
        # switching the UI language later renders the stored report in English
        set_language("en", persist=False)
        self.assertIn("full-screen", message_text(gap))
        self.assertIn("全屏", message_text(gap, "zh"))

    def test_codec_must_be_h264_and_encoder_libx264(self):
        start = BASE + 1000
        write_session(self.root, [(start, 30)], [move(start + 10, 5, 5), click(start + 20, 5, 5, True)],
                      capture={}, video=True)
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 30, codec="hevc")):
            package(self.root)
        report = self.load()[2]
        self.assertFalse(report["technical_ok"])
        self.assertTrue(any("H.264" in e for e in en(report["technical_errors"])), report["technical_errors"])
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 30, encoder="Lavc h264_nvenc")):
            report = validate_dataset(self.root)
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertTrue(any("h264_nvenc" in w and "libx264" in w for w in en(report["warnings"])))
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 30)):
            report = validate_dataset(self.root)
        self.assertFalse([w for w in en(report["warnings"]) if "encoder" in w])

    def test_notes_the_gui_filters_keep_their_text(self):
        # recorder/gui.py picks these two notes out of the warnings by substring
        _, _, report = self.run_mode({}, video_size=(1920, 1080))
        notes = report["warnings"]
        self.assertTrue(any("frame count from the video header" in m["en"] and "视频头部帧数" in m["zh"]
                            for m in notes), notes)
        self.assertTrue(any(m["en"].startswith("Sync field checks are not a physical sync-accuracy certification")
                            and m["zh"].startswith("同步字段校验不等于物理同步精度认证") for m in notes), notes)


class OffscreenCursorTest(SessionTestCase):
    """K3: desktop mode records the primary monitor only; the cursor can leave it."""
    SCREENS = [screen(0, 0, 0, 1920, 1080, True), screen(1, 1920, 0, 1920, 1080)]

    def make(self, away_x):
        s0 = BASE + 1000
        events = snapshot(s0 - 300, 100, 100, self.SCREENS) + [
            move(s0 + 500, away_x, 300), click(s0 + 600, away_x, 300, True), click(s0 + 650, away_x, 300, False),
            move(s0 + 800, 500, 500), key(s0 + 900, True)]
        write_session(self.root, [(s0, 30)], events, video=True,
                      capture={"capture_mode": "desktop", "source": "desktop", "screens": self.SCREENS,
                               "capture_rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}})
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 30)):
            package(self.root)
        return self.load()

    def test_frames_with_the_cursor_on_another_monitor_are_counted(self):
        _, frames, report = self.make(2500)
        away = [f["frame"] for f in frames if f["events"][0]["x"] >= 1920]
        self.assertEqual(len(away), 9, away)
        self.assertEqual(report["offscreen_cursor_frames"], 9)
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        warning = next(m for m in report["warnings"] if "outside the video" in m["en"])
        self.assertIn("9 frame(s) (30.0% of the clip)", warning["en"])
        self.assertIn("9 帧（占 30.0%）", warning["zh"])
        # kept as they are, no extra fields
        synthetic = frames[away[0]]["events"][0]
        self.assertEqual(synthetic["x"], 2500)
        self.assertNotIn("offscreen", synthetic)

    def test_cursor_on_the_recorded_monitor_gives_no_warning(self):
        _, _, report = self.make(1919)
        self.assertEqual(report["offscreen_cursor_frames"], 0)
        self.assertFalse([w for w in en(report["warnings"]) if "outside the video" in w])


class SixtyFpsTest(SessionTestCase):
    def test_frame_math_at_60_fps(self):
        fps = 60
        s0, s1 = BASE + 1000, BASE + 1000 + 1000 + 2000   # 60 frames, 2 s manual pause
        events = snapshot(s0 - 200, 10, 10, [screen(0, 0, 0, 1920, 1080, True)]) + [
            click(s0 + 508.25, 100, 100, True), click(s0 + 540, 100, 100, False),
            *snapshot(s1 - 90, 50, 60, [screen(0, 0, 0, 1920, 1080, True)]),
            key(s1 + 250.5, True), key(s1 + 290, False)]
        write_session(self.root, [(s0, 60), (s1, 30)], events, fps=fps, video=True)
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, fps, 90)):
            package(self.root)
        meta, frames, report = self.load()
        self.assertEqual(meta["fps"], 60)
        self.assertEqual(meta["total_frames"], 90)
        self.assertEqual(meta["total_duration_ms"], 1500)
        self.assertEqual([c["frame_start"] for c in meta["clips"]], [0, 60])
        self.assertEqual(meta["clips"][1]["gap_before_ms"], 2000)
        dt = 1000.0 / fps
        for i, fr in enumerate(frames):
            self.assertEqual(fr["frame"], i)
            self.assertEqual(fr["video_t_ms"], i * 1000 / 60)
            for e in fr["events"]:
                self.assertLess(e["t_in_frame_ms"], dt)
                self.assertAlmostEqual(e["video_t_ms"], fr["video_t_ms"] + e["t_in_frame_ms"], places=6)
        where = self.assert_each_discrete_event_once(frames)
        by_type = {}
        for fr in frames:
            for e in fr["events"]:
                if "seq" in e:
                    by_type.setdefault(e["type"], []).append((fr["frame"], e))
        frame, down = by_type["click"][0]
        self.assertEqual(frame, int(508.25 // dt))                      # frame 30
        self.assertAlmostEqual(down["video_t_ms"], 508.25, places=3)
        frame, kdown = by_type["key"][0]
        self.assertEqual(frame, 60 + int(250.5 // dt))                  # frame 75
        self.assertAlmostEqual(kdown["video_t_ms"], 1000 + 250.5, places=3)
        # resume snapshot opens clip 1 (frame 60), cursor taken from it
        self.assertEqual(frames[60]["events"][0]["x"], 50)
        self.assertIn("screen_config", [e["type"] for e in frames[60]["events"]])
        self.assertEqual(len(where), sum(1 for e in jsonl(os.path.join(self.root, "events.json"))
                                         if e["type"] != "mouse_move"))
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertTrue(any("60 fps" in w for w in en(report["warnings"])))


class PrivacyMaskGapTest(SessionTestCase):
    SCREENS = [screen(0, 0, 0, 1920, 1080, True)]

    def make(self, trigger_mask=MASK):
        s0, s1 = BASE + 1000, BASE + 1000 + 1000 + 4000          # 30 frames each
        events = snapshot(s0 - 300, 10, 10, self.SCREENS) + [
            move(s0 + 100, 200, 200), click(s0 + 400, 200, 200, True), click(s0 + 450, 200, 200, False),
            ev("privacy_mask_start", s0 + 1000 + 8, **trigger_mask),     # just after clip 0 ends
            # paused: nothing logged; resume snapshot before the first new frame
            *snapshot(s1 - 150, 777, 333, self.SCREENS),
            ev("privacy_mask_end", s1 - 140, **trigger_mask),
            key(s1 + 300, True), key(s1 + 350, False),
            ev("privacy_mask_start", s1 + 1000 + 20, **trigger_mask),    # auto-pause, then stop
        ]
        self.events = events
        write_session(self.root, [(s0, 30), (s1, 30)], events, video=True,
                      capture={"capture_mode": "app", "source": "app:SLDWORKS.exe",
                               "capture_rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                               "cursor_in_video": True,
                               "mask_stats": {"masked_frames": 4, "auto_pauses": 2, "auto_paused_ms": 4000}})
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 60)):
            package(self.root)
        return self.load()

    @staticmethod
    def types(frame):
        return [e["type"] for e in frame["events"] if "seq" in e]

    def test_mask_start_closes_the_clip_before_the_gap(self):
        _, frames, _ = self.make()
        self.assertEqual(self.types(frames[29])[-1], "privacy_mask_start")
        start = next(e for e in frames[29]["events"] if e["type"] == "privacy_mask_start")
        self.assertAlmostEqual(start["t_in_frame_ms"], 1000 / 30 - 0.001, places=6)
        self.assertEqual(start["maskTrigger"], "non_target_app")

    def test_resume_snapshot_and_mask_end_open_the_next_clip(self):
        _, frames, _ = self.make()
        first = frames[30]
        self.assertEqual(self.types(first), ["screen_config", "active_app", "appearance_change",
                                             "memory_pressure", "privacy_mask_end"])
        for e in first["events"]:
            self.assertEqual(e["t_in_frame_ms"], 0)
            self.assertEqual(e["video_t_ms"], 1000.0)
        self.assertEqual((first["events"][0]["x"], first["events"][0]["y"]), (777, 333))

    def test_mask_start_after_the_last_clip_lands_on_the_last_frame(self):
        _, frames, _ = self.make()
        self.assertEqual(self.types(frames[-1])[-1], "privacy_mask_start")

    def test_every_event_is_logged_and_discrete_ones_mapped_once(self):
        meta, frames, report = self.make()
        self.assertEqual(meta["event_count"], len(self.events))
        raw = jsonl(os.path.join(self.root, "events.json"))
        self.assertEqual(sum(e["type"].startswith("privacy_mask") for e in raw), 3)
        where = self.assert_each_discrete_event_once(frames)
        self.assertEqual(len(where), sum(1 for e in raw if e["type"] != "mouse_move"))
        with open(os.path.join(self.root, "capture.json"), encoding="utf-8") as fh:
            stats = json.load(fh)["packager"]
        self.assertEqual(stats["dropped_events_in_gaps"], 0)
        self.assertTrue(report["technical_ok"], report["technical_errors"])

    def test_auto_pause_is_not_a_manual_privacy_item(self):
        _, _, report = self.make()
        self.assertFalse([m for m in en(report["delivery_missing"]) if "privacy" in m])
        self.assertTrue(any("auto-paused 2" in w for w in en(report["warnings"])))
        self.assertEqual((report["privacy_masks"], report["uncut_privacy_masks"]), (0, 0))

    def test_cut_policy_mask_needs_no_manual_handling(self):
        # v0.2.1: a password manager in front pauses the recording like app mode
        policy = dict(MASK, trigger="policy_blocked_app", maskTrigger="policy_blocked_app",
                      maskDetail="keepass.exe", appName="KeePass", exeName="KeePass.exe")
        _, _, report = self.make(trigger_mask=policy)
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertEqual((report["privacy_masks"], report["uncut_privacy_masks"]), (2, 0))
        self.assertFalse([m for m in en(report["delivery_missing"]) if "privacy" in m])
        self.assertTrue(any("blocklisted" in w for w in en(report["warnings"])))

    def test_uncut_policy_mask_from_an_old_recording_needs_manual_handling(self):
        policy = dict(MASK, trigger="policy_blocked_app", maskTrigger="policy_blocked_app")
        s0 = BASE + 1000
        events = snapshot(s0 - 300, 10, 10, self.SCREENS) + [
            move(s0 + 100, 200, 200),
            ev("privacy_mask_start", s0 + 200, **policy),            # only marked: video keeps running
            key(s0 + 300, True), key(s0 + 350, False),
            ev("privacy_mask_end", s0 + 700, **policy)]
        write_session(self.root, [(s0, 30)], events, video=True)
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 30)):
            package(self.root)
        report = self.load()[2]
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertEqual((report["privacy_masks"], report["uncut_privacy_masks"]), (1, 1))
        self.assertTrue(any("privacy" in m for m in en(report["delivery_missing"])))
        self.assertFalse(report["ready_for_delivery"])

    POLICY = dict(MASK, trigger="policy_blocked_app", maskTrigger="policy_blocked_app",
                  maskDetail="keepass.exe", appName="KeePass", exeName="KeePass.exe")

    def legacy_pause(self, typed, version=None):
        """v0.1: F9 cut the video but input kept being logged; KeePass was in front meanwhile."""
        s0, s1 = BASE + 1000, BASE + 1000 + 1000 + 5000
        events = snapshot(s0 - 300, 10, 10, self.SCREENS) + [
            move(s0 + 100, 200, 200), click(s0 + 400, 200, 200, True), click(s0 + 450, 200, 200, False),
            ev("privacy_mask_start", s0 + 2000, **self.POLICY), *typed(s0 + 2500),
            ev("privacy_mask_end", s0 + 4000, **self.POLICY),
            *snapshot(s1 - 100, 300, 300, self.SCREENS), key(s1 + 300, True), key(s1 + 350, False)]
        capture = {"capture_rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}}
        if version is not None:
            capture["version"] = version
        write_session(self.root, [(s0, 30), (s1, 30)], events, video=True, capture=capture)
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 60)):
            package(self.root)
        return self.load()[2]

    @staticmethod
    def into(app, *events):
        """The same events, attributed to another foreground app."""
        for e in events:
            e.update(appName=app.split(".")[0], bundleId=app)
        return list(events)

    def test_keys_logged_inside_a_cut_mask_count_as_uncut(self):
        # no frame lies inside the mask, but events.json still holds the password keystrokes
        report = self.legacy_pause(lambda t: self.into("KeePass.exe", key(t, True, code=80),
                                                       key(t + 50, False, code=80)))
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertEqual((report["privacy_masks"], report["uncut_privacy_masks"]), (1, 1))
        self.assertTrue(any("input log" in m for m in en(report["delivery_missing"])), report["delivery_missing"])
        self.assertFalse(report["ready_for_delivery"])
        self.assertFalse([w for w in en(report["warnings"]) if "was cut from the video" in w])

    def test_a_click_inside_a_cut_mask_counts_as_uncut(self):
        report = self.legacy_pause(lambda t: self.into("KeePass.exe", click(t, 900, 500, True),
                                                       click(t + 40, 900, 500, False)))
        self.assertEqual((report["privacy_masks"], report["uncut_privacy_masks"]), (1, 1))

    def test_unattributed_input_counts_only_for_pre_v02_recorders(self):
        typed = lambda t: self.into("", key(t, True, code=80), key(t + 50, False, code=80))  # noqa: E731
        self.assertEqual(self.legacy_pause(typed)["uncut_privacy_masks"], 1)      # no capture version = v0.1

    def test_input_into_the_target_while_the_resumed_clip_starts_is_not_a_leak(self):
        # v0.2.1 re-enables input right after the new segment starts; privacy_mask_end is stamped just
        # before its first frame, so a click into SOLIDWORKS in that start-up window lies inside the mask
        report = self.legacy_pause(lambda t: [click(t, 900, 500, True), click(t + 40, 900, 500, False),
                                              key(t + 80, True, code=80), key(t + 120, False, code=80)],
                                   version=2)
        self.assertEqual((report["privacy_masks"], report["uncut_privacy_masks"]), (1, 0))
        self.assertEqual(report["privacy_input_events"], 0)

    def test_v01_keys_typed_into_a_password_manager_during_a_plain_pause_block_export(self):
        # v0.1: F9 (no privacy marks at all), KeePass in front, master password typed, F9 again
        s0, s1 = BASE + 1000, BASE + 1000 + 1000 + 5000
        events = snapshot(s0 - 300, 10, 10, self.SCREENS) + [
            move(s0 + 100, 200, 200),
            *self.into("KeePass.exe", key(s0 + 2500, True, code=72), key(s0 + 2550, False, code=72)),
            *snapshot(s1 - 100, 300, 300, self.SCREENS), key(s1 + 300, True), key(s1 + 350, False)]
        write_session(self.root, [(s0, 30), (s1, 30)], events, video=True,
                      capture={"capture_rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}})
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 60)):
            package(self.root)
        report = self.load()[2]
        self.assertEqual(report["privacy_masks"], 0)
        self.assertEqual(report["privacy_input_events"], 2)
        self.assertGreater(report["uncut_privacy_masks"], 0)          # export_recording refuses
        self.assertTrue(any("password manager" in m for m in en(report["delivery_missing"])))

    def test_v01_chat_typed_while_paused_blocks_export_but_v02_does_not(self):
        s0, s1 = BASE + 1000, BASE + 1000 + 1000 + 5000
        for version, expected in ((None, 2), (2, 0)):
            with self.subTest(version=version):
                shutil.rmtree(self.root, ignore_errors=True)
                os.makedirs(self.root)
                events = snapshot(s0 - 300, 10, 10, self.SCREENS) + [
                    move(s0 + 100, 200, 200),
                    *self.into("WeChat.exe", key(s0 + 2500, True, code=72), key(s0 + 2550, False, code=72)),
                    *snapshot(s1 - 100, 300, 300, self.SCREENS)]
                capture = {"capture_rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}}
                if version:
                    capture["version"] = version
                write_session(self.root, [(s0, 30), (s1, 30)], events, video=True, capture=capture)
                with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 60)):
                    package(self.root)
                self.assertEqual(self.load()[2]["privacy_input_events"], expected)

    def test_modifier_keys_and_moves_inside_a_cut_mask_are_not_typed_input(self):
        report = self.legacy_pause(lambda t: [key(t, True, code=0xA0), key(t + 50, False, code=0xA0),
                                              ev("key", t + 60, keyCode=0x11, isDown=True),
                                              move(t + 100, 50, 60)])
        self.assertEqual((report["privacy_masks"], report["uncut_privacy_masks"]), (1, 0))
        self.assertTrue(any("was cut from the video" in w for w in en(report["warnings"])))

    def test_unusable_clip_table_is_an_error_and_its_masks_count_as_uncut(self):
        self.make(trigger_mask=self.POLICY)              # both masks cut (see the test above)
        path = os.path.join(self.root, "metadata.json")
        with open(path, encoding="utf-8") as fh:
            meta = json.load(fh)
        for clips in ([], None, [dict(meta["clips"][0], frame_count=29), meta["clips"][1]]):
            with self.subTest(clips=clips):
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(dict(meta, clips=clips), fh)
                with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 60)):
                    report = validate_dataset(self.root)
                self.assertIn("metadata clips table is malformed", en(report["technical_errors"]))
                self.assertEqual((report["privacy_masks"], report["uncut_privacy_masks"]), (2, 2))

    def test_stair_stepped_duplicate_walls_do_not_hide_an_uncut_mask(self):
        # v0.2 DXGI: a duplicate-padded slot carried the PREVIOUS capture's wall.
        # KeePass rendered at slot 10, the watcher marked it 27 ms later, then the
        # screen stayed static: slots 11..59 all carry slot 10's wall, i.e. every
        # frame shown during the mask has a sidecar wall before its start.
        policy = dict(MASK, trigger="policy_blocked_app", maskTrigger="policy_blocked_app",
                      maskDetail="keepass.exe")
        s0, dt = BASE + 1000, 1000 / 30
        events = snapshot(s0 - 300, 10, 10, self.SCREENS) + [
            move(s0 + 50, 20, 20), click(s0 + 100, 20, 20, True), click(s0 + 120, 20, 20, False),
            ev("privacy_mask_start", s0 + 10 * dt + 27, **policy),
            ev("privacy_mask_end", s0 + 1900, **policy)]
        raw = write_session(self.root, [(s0, 75)], events, video=True,
                            capture={"capture_rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}})
        path = os.path.join(raw, "segments.json")
        with open(path, encoding="utf-8") as fh:
            segs = json.load(fh)
        segs[0]["frames"] = [[k, round(s0 + (10 if 10 < k < 60 else k) * dt, 3)] for k in range(75)]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(segs, fh)
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 75)):
            package(self.root)
        _, frames, report = self.load()
        start = s0 + 10 * dt + 27
        self.assertFalse([f for f in frames if f["events"][0]["source_wall_ms"] > start + 1
                          and f["events"][0]["source_wall_ms"] < s0 + 1900])   # no sidecar wall inside
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertEqual((report["privacy_masks"], report["uncut_privacy_masks"]), (1, 1))
        self.assertTrue(any("privacy" in m for m in en(report["delivery_missing"])))
        self.assertFalse([w for w in en(report["warnings"]) if "was cut from the video" in w])


class GapSplitTest(unittest.TestCase):
    def test_legacy_pause_input_is_dropped(self):
        gap = [move(1, 1, 1), click(2, 1, 1, True), click(3, 1, 1, False)]
        self.assertEqual(split_gap(gap), ([], [], gap))

    def test_zero_frame_resume_in_the_middle_keeps_the_last_snapshot(self):
        first = snapshot(10, 1, 1, [])
        second = snapshot(30, 2, 2, [])
        gap = [ev("privacy_mask_start", 1, **MASK), *first, ev("privacy_mask_end", 11, **MASK),
               ev("privacy_mask_start", 20, **MASK), *second]
        tail, head, dropped = split_gap(gap)
        self.assertEqual([e["timestamp"] for e in tail], [1, 20])
        self.assertEqual(head, second)
        self.assertEqual(len(dropped), len(first) + 1)

    def test_change_screen_config_is_not_a_resume_marker(self):
        gap = [ev("screen_config", 5, reason="change", screens=[], screenCount=0), move(6, 1, 1)]
        self.assertEqual(split_gap(gap)[1], [])


class RobustnessTest(SessionTestCase):
    def test_torn_raw_line_is_skipped_and_flagged(self):
        start = BASE + 1000
        raw = write_session(self.root, [(start, 10)], [move(start + 5, 1, 1), click(start + 10, 1, 1, True)])
        with open(os.path.join(raw, "events.jsonl"), "a", encoding="utf-8") as fh:
            fh.write('{"type":"mouse_move","timestamp":18000000')   # power loss mid-write
        meta = package(self.root)
        self.assertEqual(meta["event_count"], 2)
        _, _, report = self.load()
        self.assertTrue(any("unreadable" in e for e in en(report["technical_errors"])))

    def test_torn_log_tail_after_power_loss_is_a_warning_for_a_recovered_session(self):
        start = BASE + 1000
        for where, recovered_ok in (("tail", True), ("middle", False)):
            with self.subTest(where=where):
                shutil.rmtree(self.root, ignore_errors=True)
                os.makedirs(self.root)
                events = [move(start + 5, 1, 1), click(start + 10, 1, 1, True)]
                raw = write_session(self.root, [(start, 10)], events, video=True,
                                    capture={"status": "recording",
                                             "capture_rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}})
                path = os.path.join(raw, "events.jsonl")
                with open(path, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
                torn = '{"type":"mouse_move","timestamp":1800' + "\x00" * 40
                lines = lines + [torn] if where == "tail" else [lines[0], torn] + lines[1:]
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 10)):
                    package(self.root)
                report = self.load()[2]
                self.assertEqual(report["technical_ok"], recovered_ok, report["technical_errors"])
                if recovered_ok:
                    self.assertTrue(any("cut off the last 1 line" in w for w in en(report["warnings"])))

    def test_missing_events_log_still_packages(self):
        start = BASE + 1000
        raw = write_session(self.root, [(start, 10)], [])
        os.remove(os.path.join(raw, "events.jsonl"))
        meta = package(self.root)
        self.assertEqual((meta["total_frames"], meta["event_count"]), (10, 0))

    def test_no_frames_is_a_clear_error(self):
        write_session(self.root, [], [])
        with self.assertRaises(SystemExit) as failure:
            package(self.root)
        self.assertIn("no video frames", str(failure.exception))


    def test_non_finite_timestamps_are_reported_not_raised(self):
        policy = dict(MASK, trigger="policy_blocked_app", maskTrigger="policy_blocked_app")
        s0 = BASE + 1000
        events = snapshot(s0 - 300, 10, 10, [screen(0, 0, 0, 1920, 1080, True)]) + [
            move(s0 + 100, 200, 200), click(s0 + 400, 200, 200, True), click(s0 + 450, 200, 200, False),
            ev("privacy_mask_start", s0 + 500, **policy), ev("privacy_mask_end", s0 + 600, **policy)]
        write_session(self.root, [(s0, 30)], events, video=True)
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 30)):
            package(self.root)
        path = os.path.join(self.root, "events.json")
        rows = jsonl(path)
        bad = {"privacy_mask_start": float("nan"), "click": float("inf")}
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                if row["type"] in bad:
                    row["timestamp"] = bad.pop(row["type"])
                fh.write(json.dumps(row) + "\n")            # NaN / Infinity, which Python's json reads
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 30)):
            report = validate_dataset(self.root)
        self.assertFalse(report["technical_ok"])
        self.assertIn("Raw event timestamps are invalid or out of order", en(report["technical_errors"]))
        self.assertEqual(report["checked_events"], len(rows))


class CliTest(SessionTestCase):
    def test_validate_prints_its_chinese_report_to_a_cp1252_pipe(self):
        # the installed app runs python -E: on an English Windows a pipe is cp1252
        from packager import validate as validate_module
        set_language("zh", persist=False)
        s0 = BASE + 1000
        write_session(self.root, [(s0, 30)], [move(s0 + 10, 5, 5), click(s0 + 20, 5, 5, True)], video=True)
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 30)):
            package(self.root)
        out, err = io.BytesIO(), io.BytesIO()
        stdout, stderr = io.TextIOWrapper(out, encoding="cp1252"), io.TextIOWrapper(err, encoding="cp1252")
        with patch("subprocess.run", return_value=fake_probe(1920, 1080, 30, 30)), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = validate_module.main(["--dataset", self.root])
            stdout.flush()
        self.assertIn(code, (0, 2))
        text = out.getvalue().decode("utf-8")
        self.assertIn("同步字段校验不等于物理同步精度认证", text)
        self.assertEqual(json.loads(text)["checked_frames"], 30)

    def test_every_cli_main_makes_stdio_safe_first(self):
        from packager import build as build_module, export_cad, validate as validate_module
        modules = [build_module, validate_module, export_cad]
        for name in ("make_demo", "burn_overlay"):
            spec = importlib.util.spec_from_file_location("_dstudio_" + name, os.path.join(ROOT, "tools", name + ".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules.append(module)
        for module in modules:
            with self.subTest(module=module.__name__), patch.object(module, "safe_stdio") as safe, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    module.main(["--help"])
                safe.assert_called_once_with()


# ----------------------------------------------------------------------------
# v0.2.1: segment finalization (K1/K2), concat (K10, exact durations), sizes
# ----------------------------------------------------------------------------
TB = 512          # packet tick per frame at time_base 1/15360 (30 fps)


def gop_packets(n, keyint=60):
    """Decode-order packets of an x264-like stream with 2 B-frames per P-frame.

    Like a fragmented MP4: pts starts two frames late, dts counts packets.
    """
    order, k = [], 0
    while k < n:
        if k % keyint == 0:
            order.append(k)
            k += 1
            continue
        group = list(range(k, min(k + 3, n, (k // keyint + 1) * keyint)))
        order.extend([group[-1]] + group[:-1])
        k = group[-1] + 1
    return [{"pts": (f + 2) * TB, "dts": i * TB, "flags": "K_" if f % keyint == 0 else "__"}
            for i, f in enumerate(order)]


class FakeMedia:
    """subprocess.run stand-in for ffprobe/ffmpeg over a raw segment folder.

    ``sizes``/``packets``/``damaged`` are keyed by segment file name; clip.mp4
    answers like :func:`fake_probe` with ``clip_frames``.  Every ffmpeg call
    records its concat list and writes a placeholder output.
    """

    def __init__(self, clip_frames, fps=30, sizes=None, packets=None, damaged=(), unreadable=()):
        self.clip_frames, self.fps = clip_frames, fps
        self.sizes, self.packets = sizes or {}, packets or {}
        self.damaged, self.unreadable = set(damaged), set(unreadable)
        self.ffmpeg_calls, self.concat_lists, self.probed = [], [], []

    def __call__(self, cmd, **kwargs):
        exe = os.path.basename(str(cmd[0])).lower()
        if "ffprobe" in exe:
            path = str(cmd[-1])
            name = os.path.basename(path)
            if name == "clip.mp4":
                return fake_probe(*self.sizes.get(name, (1920, 1080)), self.fps, self.clip_frames)
            self.probed.append((name, any("packet=" in str(c) for c in cmd)))
            if name in self.unreadable:
                return SimpleNamespace(stdout="{}", stderr="moov atom not found", returncode=1)
            w, h = self.sizes.get(name, (1920, 1080))
            out = {"streams": [{"codec_name": "h264", "width": w, "height": h, "time_base": "1/15360"}]}
            if any("packet=" in str(c) for c in cmd):
                out["packets"] = self.packets.get(name, gop_packets(30))
            return SimpleNamespace(stdout=json.dumps(out), returncode=0,
                                   stderr="partial file" if name in self.damaged else "")
        self.ffmpeg_calls.append(list(cmd))
        listing = cmd[cmd.index("-i") + 1]
        with open(listing, encoding="utf-8") as fh:
            self.concat_lists.append(fh.read())
        with open(cmd[-1], "wb") as fh:
            fh.write(b"remuxed fixture")
        return SimpleNamespace(stdout="", stderr="", returncode=0)


class SegmentRepairTest(SessionTestCase):
    SCREENS = [screen(0, 0, 0, 1920, 1080, True)]

    def make(self, segments, events, edit=None, media=None, total=None, capture=None):
        raw = write_session(self.root, segments, events, capture=capture)
        path = os.path.join(raw, "segments.json")
        with open(path, encoding="utf-8") as fh:
            segs = json.load(fh)
        for seg in segs:
            seg.update({"finalized": True, "returncode": 0})
            with open(os.path.join(raw, "segments", seg["file"]), "wb") as fh:
                fh.write(b"segment fixture")
        for i, change in (edit or {}).items():
            segs[i].update(change)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(segs, fh)
        self.media = media or FakeMedia(total or sum(n for _, n in segments))
        with patch("subprocess.run", side_effect=self.media):
            build(raw, self.root, dataset_id="test", category="cad")
        return self.load()

    @staticmethod
    def files_in(listing):
        return [os.path.basename(line[6:-1]) for line in listing.splitlines() if line.startswith("file ")]

    def repairs(self):
        with open(os.path.join(self.root, "capture.json"), encoding="utf-8") as fh:
            return json.load(fh)["packager"]["segment_repairs"]

    # ---- finding 13 + K10: exact concat durations, always remuxed ----------
    def test_concat_list_carries_exact_frame_durations(self):
        s0 = BASE + 1000
        segs = [(s0, 37), (s0 + 3000, 41), (s0 + 6000, 37)]
        meta, frames, report = self.make(segs, [move(s0 + 5, 1, 1)])
        listing = self.media.concat_lists[0].splitlines()
        self.assertEqual([l for l in listing if l.startswith("duration")],
                         ["duration 1.233333", "duration 1.366667", "duration 1.233333"])
        self.assertEqual(self.files_in(self.media.concat_lists[0]), ["seg_000.mp4", "seg_001.mp4", "seg_002.mp4"])
        cmd = self.media.ffmpeg_calls[0]
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertIn("+faststart", cmd)
        self.assertEqual(meta["total_frames"], 115)
        self.assertTrue(report["technical_ok"], report["technical_errors"])

    def test_durations_do_not_accumulate_rounding_error(self):
        s0 = BASE + 1000
        segs = [(s0 + i * 2000, 1) for i in range(30)]      # 30 one-frame clips
        self.make(segs, [move(s0 + 5, 1, 1)])
        total_us = sum(round(float(l.split()[1]) * 1e6) for l in self.media.concat_lists[0].splitlines()
                       if l.startswith("duration"))
        self.assertEqual(total_us, 1_000_000)

    def test_single_segment_is_remuxed_with_faststart(self):
        s0 = BASE + 1000
        self.make([(s0, 30)], [move(s0 + 5, 1, 1)])
        self.assertEqual(len(self.media.ffmpeg_calls), 1)
        cmd = self.media.ffmpeg_calls[0]
        self.assertEqual((cmd[cmd.index("-c") + 1], cmd[cmd.index("-movflags") + 1]), ("copy", "+faststart"))
        with open(os.path.join(self.root, "clip.mp4"), "rb") as fh:
            self.assertEqual(fh.read(), b"remuxed fixture")   # not a byte copy of the fragmented segment
        self.assertEqual(self.files_in(self.media.concat_lists[0]), ["seg_000.mp4"])

    def test_copy_failure_falls_back_to_libx264_crf23(self):
        class FailingCopy(FakeMedia):
            def __call__(self, cmd, **kwargs):
                if "ffprobe" not in os.path.basename(str(cmd[0])).lower() and "copy" in cmd:
                    self.ffmpeg_calls.append(list(cmd))
                    return SimpleNamespace(stdout="", stderr="codec parameters differ", returncode=1)
                return super().__call__(cmd, **kwargs)

        s0 = BASE + 1000
        media = FailingCopy(60)
        self.make([(s0, 30), (s0 + 3000, 30)], [move(s0 + 5, 1, 1)], media=media)
        encode = media.ffmpeg_calls[-1]
        self.assertEqual(encode[encode.index("-c:v") + 1], "libx264")
        self.assertEqual(encode[encode.index("-crf") + 1], "23")
        self.assertIn("+faststart", encode)

    # ---- finding 17: one resolution per clip --------------------------------
    def test_mixed_segment_resolutions_are_refused(self):
        s0 = BASE + 1000
        media = FakeMedia(60, sizes={"seg_000.mp4": (1920, 1080), "seg_001.mp4": (3840, 1080)})
        with self.assertRaises(ValueError) as failure:
            self.make([(s0, 30), (s0 + 3000, 30)], [move(s0 + 5, 1, 1)], media=media)
        self.assertIn("different resolutions", str(failure.exception))
        self.assertIn("3840x1080", str(failure.exception))
        self.assertEqual(media.ffmpeg_calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "clip.mp4")))

    def test_finalized_segments_get_a_cheap_header_probe_only(self):
        s0 = BASE + 1000
        self.make([(s0, 30), (s0 + 3000, 30)], [move(s0 + 5, 1, 1)])
        self.assertEqual(sorted(self.media.probed), [("seg_000.mp4", False), ("seg_001.mp4", False)])

    # ---- K1: broken segments are skipped, their time is a gap ---------------
    def test_broken_segment_is_skipped_and_its_input_dropped(self):
        s0, s1, s2 = BASE + 1000, BASE + 3000, BASE + 6000
        events = snapshot(s0 - 200, 10, 10, self.SCREENS) + [click(s0 + 400, 10, 10, True)] + \
            snapshot(s1 - 100, 500, 500, self.SCREENS) + [click(s1 + 400, 500, 500, True),
                                                           key(s1 + 500, True)] + \
            snapshot(s2 - 100, 700, 300, self.SCREENS) + [key(s2 + 300, True)]
        meta, frames, report = self.make([(s0, 30), (s1, 30), (s2, 30)], events,
                                         edit={1: {"broken": True, "frames": [], "finalized": False,
                                                   "returncode": 1}}, total=60)
        self.assertEqual(self.files_in(self.media.concat_lists[0]), ["seg_000.mp4", "seg_002.mp4"])
        self.assertEqual([c["segment_index"] for c in meta["clips"]], [0, 2])
        self.assertEqual(meta["total_frames"], 60)
        self.assertEqual(meta["clips"][1]["gap_before_ms"], 4000)
        # the restart snapshot (not the broken clip's) opens clip 1
        self.assertEqual((frames[30]["events"][0]["x"], frames[30]["events"][0]["y"]), (700, 300))
        mapped = {e["timestamp"] for fr in frames for e in fr["events"] if "seq" in e}
        self.assertNotIn(s1 + 400, mapped)
        self.assertNotIn(s1 + 500, mapped)
        self.assertIn(s2 + 300, mapped)
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertEqual(self.repairs()[0]["reason"], "broken")
        self.assertTrue(any("damaged" in w for w in en(report["warnings"])))

    def test_broken_first_segment_makes_the_preroll_a_gap(self):
        s0, s1 = BASE + 1000, BASE + 4000
        events = snapshot(s0 - 200, 10, 10, self.SCREENS) + [click(s0 + 400, 10, 10, True),
                                                               click(s0 + 450, 10, 10, False)] + \
            snapshot(s1 - 100, 640, 360, self.SCREENS) + [key(s1 + 300, True)]
        meta, frames, report = self.make([(s0, 30), (s1, 30)], events,
                                         edit={0: {"broken": True, "frames": []}}, total=30)
        self.assertEqual(meta["total_frames"], 30)
        types = [e["type"] for e in frames[0]["events"] if "seq" in e]
        self.assertEqual(types, ["screen_config", "active_app", "appearance_change", "memory_pressure"])
        self.assertEqual((frames[0]["events"][0]["x"], frames[0]["events"][0]["y"]), (640, 360))
        self.assertTrue(report["technical_ok"], report["technical_errors"])

    # ---- K2: in_progress / unfinalized segments are fitted to the file ------
    def test_in_progress_segment_is_truncated_to_decodable_frames(self):
        s0, s1 = BASE + 1000, BASE + 3000
        media = FakeMedia(50, packets={"seg_001.mp4": gop_packets(20)})
        meta, frames, report = self.make([(s0, 30), (s1, 30)], [move(s0 + 5, 1, 1), key(s1 + 900, True)],
                                         edit={1: {"in_progress": True, "finalized": False,
                                                   "returncode": None}}, media=media)
        self.assertEqual(meta["total_frames"], 50)
        self.assertEqual(meta["clips"][1]["frame_count"], 20)
        self.assertIn("duration 0.666667", media.concat_lists[0])
        self.assertNotIn("outpoint", media.concat_lists[0])
        self.assertEqual(self.repairs()[0]["reason"], "truncated")
        self.assertTrue(report["technical_ok"], report["technical_errors"])

    def test_sidecar_saved_before_the_crash_is_extended_to_the_file(self):
        s0 = BASE + 1000
        media = FakeMedia(26, packets={"seg_000.mp4": gop_packets(26)})
        meta, frames, report = self.make([(s0, 20)], [move(s0 + 5, 1, 1), click(s0 + 800, 1, 1, True)],
                                         edit={0: {"in_progress": True}}, media=media)
        self.assertEqual(meta["total_frames"], 26)
        self.assertEqual(frames[25]["events"][0]["source_wall_ms"], round(s0 + 25 * 1000 / 30))
        self.assertIn("click", [e["type"] for e in frames[24]["events"]])   # 800 ms -> frame 24
        self.assertEqual(self.repairs()[0]["reason"], "extended")
        self.assertTrue(report["technical_ok"], report["technical_errors"])

    def test_file_cut_mid_write_is_trimmed_to_a_gapless_prefix(self):
        s0 = BASE + 1000
        cut = gop_packets(30)[:9]           # decode order ends in a P-frame whose B-frames are lost
        media = FakeMedia(7, packets={"seg_000.mp4": cut})
        meta, _, report = self.make([(s0, 30)], [move(s0 + 5, 1, 1)],
                                    edit={0: {"finalized": False, "returncode": 1}}, media=media)
        self.assertEqual(meta["total_frames"], 7)
        listing = media.concat_lists[0]
        self.assertIn("duration 0.233333", listing)
        outpoint = float(next(l for l in listing.splitlines() if l.startswith("outpoint")).split()[1])
        # drops the first packet that is not part of the gapless prefix (dts tick 7)
        self.assertAlmostEqual(outpoint, (7 * TB) / 15360 - 0.5 / 30, places=5)
        self.assertEqual(self.repairs()[0]["reason"], "truncated")

    def test_unreadable_unfinalized_segment_is_dropped(self):
        s0, s1 = BASE + 1000, BASE + 3000
        media = FakeMedia(30, unreadable={"seg_001.mp4"})
        meta, _, report = self.make([(s0, 30), (s1, 30)], [move(s0 + 5, 1, 1)],
                                    edit={1: {"finalized": False, "returncode": 3221225477}}, media=media)
        self.assertEqual(meta["total_frames"], 30)
        self.assertEqual(self.files_in(media.concat_lists[0]), ["seg_000.mp4"])
        self.assertEqual(self.repairs()[0]["reason"], "unreadable")

    # ---- K2: a crashed recorder never wrote status "completed" ---------------
    CRASHED = {"status": "recording"}

    def test_crash_recovered_session_is_technically_ok_with_a_warning(self):
        s0 = BASE + 1000
        media = FakeMedia(26, packets={"seg_000.mp4": gop_packets(26)})
        _, _, report = self.make([(s0, 20)], [move(s0 + 5, 1, 1), click(s0 + 800, 1, 1, True)],
                                 edit={0: {"in_progress": True}}, media=media, capture=self.CRASHED)
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        recovered = [m for m in report["warnings"] if "recovered after an abnormal stop" in m["en"]]
        self.assertEqual(len(recovered), 1, report["warnings"])
        self.assertIn("录制异常结束后已恢复", recovered[0]["zh"])

    def test_in_progress_segment_matching_its_file_still_marks_the_recovery(self):
        s0 = BASE + 1000
        media = FakeMedia(20, packets={"seg_000.mp4": gop_packets(20)})
        _, _, report = self.make([(s0, 20)], [move(s0 + 5, 1, 1)], edit={0: {"in_progress": True}},
                                 media=media, capture=self.CRASHED)
        self.assertEqual([(r["reason"], r["frames_kept"]) for r in self.repairs()], [("complete", 20)])
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertTrue(any("recovered after an abnormal stop" in w for w in en(report["warnings"])))
        self.assertFalse([w for w in en(report["warnings"]) if "Segment " in w])   # nothing was cut

    def test_crash_between_segments_is_recovered_without_segment_repairs(self):
        # K11: every segment was finalized (a crash during a pause or between two
        # segments), so there is nothing to repair; the lock was freed by the OS
        s0 = BASE + 1000
        for status in ("recording", "failed"):
            with self.subTest(status=status):
                _, _, report = self.make([(s0, 30), (s0 + 5000, 30)], [move(s0 + 5, 1, 1)],
                                         capture={"status": status})
                self.assertEqual(self.repairs(), [])
                self.assertTrue(report["technical_ok"], report["technical_errors"])
                recovered = [m for m in report["warnings"] if "recovered after an abnormal stop" in m["en"]]
                self.assertEqual(len(recovered), 1, report["warnings"])
                self.assertIn("every segment was saved normally", recovered[0]["en"])
                self.assertIn("录制异常结束后已恢复", recovered[0]["zh"])

    def live(self):
        """Hold raw/.recording.lock like a running recorder (K11)."""
        lock = reclock.acquire(os.path.join(self.root, "raw"))
        self.assertIsNotNone(lock, "could not take the recording lock")
        return lock

    def test_crash_with_a_released_recording_lock_is_recovered(self):
        reclock.acquire(os.path.join(self.root, "raw")).release()   # the OS frees it when the recorder dies
        s0 = BASE + 1000
        _, _, report = self.make([(s0, 30)], [move(s0 + 5, 1, 1)], capture=self.CRASHED)
        self.assertTrue(os.path.isfile(reclock.lock_path(os.path.join(self.root, "raw"))))
        self.assertTrue(report["technical_ok"], report["technical_errors"])
        self.assertTrue(any("recovered after an abnormal stop" in w for w in en(report["warnings"])))

    def test_live_recording_is_not_packaged(self):
        s0 = BASE + 1000
        raw = write_session(self.root, [(s0, 30)], [move(s0 + 5, 1, 1)], capture=self.CRASHED)
        media = FakeMedia(30)
        with self.live(), patch("subprocess.run", side_effect=media):
            with self.assertRaises(SystemExit) as failure:
                build(raw, self.root, dataset_id="test", category="cad")
            self.assertIn("still in progress", str(failure.exception))
            set_language("zh", persist=False)
            with self.assertRaises(SystemExit) as failure:
                build(raw, self.root, dataset_id="test", category="cad")
            self.assertIn("录制仍在进行中", str(failure.exception))
        self.assertEqual((media.ffmpeg_calls, media.probed), ([], []))
        self.assertFalse(os.path.exists(os.path.join(self.root, "metadata.json")))

    def test_validating_a_live_recording_is_a_technical_error(self):
        s0 = BASE + 1000
        self.make([(s0, 30)], [move(s0 + 5, 1, 1)], capture=self.CRASHED)
        with self.live(), patch("subprocess.run", side_effect=FakeMedia(30)):
            report = validate_dataset(self.root)
        self.assertFalse(report["technical_ok"])
        self.assertTrue(any("still in progress" in e for e in en(report["technical_errors"])), report)
        self.assertFalse([w for w in en(report["warnings"]) if "recovered" in w])
        with patch("subprocess.run", side_effect=FakeMedia(30)):
            self.assertTrue(validate_dataset(self.root)["technical_ok"])      # lock released: recovered

    def test_recovered_session_whose_clip_does_not_match_is_an_error(self):
        s0 = BASE + 1000
        media = FakeMedia(25, packets={"seg_000.mp4": gop_packets(26)})     # clip.mp4 reports 25 of 26
        _, _, report = self.make([(s0, 20)], [move(s0 + 5, 1, 1)], edit={0: {"in_progress": True}},
                                 media=media, capture=self.CRASHED)
        self.assertFalse(report["technical_ok"])
        self.assertIn("The recording did not finish normally", en(report["technical_errors"]))
        self.assertFalse([w for w in en(report["warnings"]) if "recovered" in w])


class UsablePrefixTest(unittest.TestCase):
    def test_complete_file_needs_no_cut(self):
        from packager.build import usable_prefix
        self.assertEqual(usable_prefix(gop_packets(20)), (20, None))

    def test_damaged_file_drops_the_partial_last_packet(self):
        from packager.build import usable_prefix
        packets = gop_packets(20)
        frames, cut = usable_prefix(packets, damaged=True)
        self.assertLess(frames, 20)
        self.assertEqual(cut, packets[frames]["dts"])

    def test_decode_order_hole_is_not_kept(self):
        from packager.build import usable_prefix
        # I0 P3 B1 B2 P6 : frames 4 and 5 never arrived
        self.assertEqual(usable_prefix(gop_packets(30)[:5]), (4, 4 * TB))

    def test_nothing_decodable(self):
        from packager.build import usable_prefix
        self.assertEqual(usable_prefix([]), (0, None))
        self.assertEqual(usable_prefix([{"pts": 0, "dts": 0}], damaged=True), (0, None))


class PrivacyCutTest(unittest.TestCase):
    def test_intervals_pair_by_trigger_and_open_ones_run_to_the_end(self):
        from packager.validate import privacy_intervals
        marks = [(10, "privacy_mask_start", "policy_blocked_app"), (12, "privacy_mask_start", "non_target_app"),
                 (20, "privacy_mask_end", "non_target_app"), (30, "privacy_mask_end", "policy_blocked_app"),
                 (40, "privacy_mask_end", "policy_blocked_app"), (50, "privacy_mask_start", "policy_blocked_app")]
        self.assertEqual(privacy_intervals(marks), [(10, 30, "policy_blocked_app"), (12, 20, "non_target_app"),
                                                    (50, float("inf"), "policy_blocked_app")])

    DT = 1000 / 30
    TIMELINE = [(0.0, 30), (5000.0, 60)]       # (nominal start ms, frames): frame k plays at start + k*dt

    def test_clip_timeline_reads_the_precise_clip_starts(self):
        from packager.validate import clip_timeline
        clips = [{"source_start_ms": 1000, "source_start_precise_ms": 999.6, "frame_count": 30},
                 {"source_start_ms": 5000, "frame_count": 60}]
        self.assertEqual(clip_timeline(clips, self.DT), [(999.6, 30), (5000.0, 60)])
        self.assertIsNone(clip_timeline([{"frame_count": 3}], self.DT))
        self.assertEqual(clip_timeline(None, self.DT), [])
        # with metadata's total_frames: the counts must add up (an empty table for a non-empty video is unusable)
        self.assertEqual(clip_timeline(clips, self.DT, total=90), [(999.6, 30), (5000.0, 60)])
        self.assertIsNone(clip_timeline(clips, self.DT, total=91))
        self.assertIsNone(clip_timeline([], self.DT, total=90))
        self.assertIsNone(clip_timeline(None, self.DT, total=90))

    def test_input_inside_is_strict(self):
        from packager.validate import input_inside
        times = [100.0, 200.0, 300.0]
        self.assertTrue(input_inside(150, 250, times))
        self.assertTrue(input_inside(250, float("inf"), times))
        self.assertFalse(input_inside(100, 200, times))                 # boundaries are not inside
        self.assertFalse(input_inside(300, float("inf"), times))
        self.assertFalse(input_inside(0, 100, times))
        self.assertFalse(input_inside(0, 50, []))

    def test_strict_rule_with_one_frame_of_slack_per_boundary(self):
        from packager.validate import mask_was_cut
        dt, tl, inf = self.DT, self.TIMELINE, float("inf")
        last = 29 * dt                                                   # stopped clip's last frame
        # v0.2.1 stamping: start after the last frame, end before the first resumed one
        self.assertTrue(mask_was_cut(last + 0.001, 4999.999, tl, dt))
        # one frame per boundary: the stopped clip's last frame and the resumed clip's first frame
        self.assertTrue(mask_was_cut(last - 10, 5000.002, tl, dt))
        self.assertTrue(mask_was_cut(last - 10, 4999.999, tl, dt))
        self.assertTrue(mask_was_cut(last + 0.001, 5000.002, tl, dt))
        # a second frame on either side is too much
        self.assertFalse(mask_was_cut(28 * dt - 1, 4999.999, tl, dt))
        self.assertFalse(mask_was_cut(last + 0.001, 5000 + dt + 1, tl, dt))
        # an old recording: the mask lies inside one continuous clip, however short
        self.assertFalse(mask_was_cut(100, 600, tl, dt))
        self.assertFalse(mask_was_cut(110, 140, tl, dt))                 # one frame (133 ms) of a running clip
        self.assertFalse(mask_was_cut(-1, 2000, [(0.0, 2)], dt))         # a whole 2-frame clip
        # mask open at stop: only the stopped clip's last frame may follow its start
        self.assertTrue(mask_was_cut(5000 + 59 * dt - 5, inf, tl, dt))
        self.assertFalse(mask_was_cut(5000 + 58 * dt - 5, inf, tl, dt))
        # nothing inside: before, between and after the clips
        for start, end in ((-500, -1), (1000, 4990), (7000, inf)):
            self.assertTrue(mask_was_cut(start, end, tl, dt), (start, end))


class LastSnapshotTest(unittest.TestCase):
    def test_gap_head_starts_at_the_last_resume_snapshot(self):
        first = snapshot(10, 1, 1, [])
        second = snapshot(30, 2, 2, [])
        lost = [click(20, 5, 5, True), click(21, 5, 5, False)]     # logged in a clip that is not in the video
        tail, head, dropped = split_gap([*first, *lost, *second])
        self.assertEqual((tail, head), ([], second))
        self.assertEqual(dropped, first + lost)

    def test_auto_resume_still_opens_with_its_snapshot(self):
        start = ev("privacy_mask_start", 1, **MASK)
        resume = snapshot(10, 1, 1, []) + [ev("privacy_mask_end", 11, **MASK)]
        self.assertEqual(split_gap([start, *resume]), ([start], resume, []))

    def test_v02_input_while_the_resumed_clip_starts_joins_its_head(self):
        start = ev("privacy_mask_start", 1, **MASK)
        startup = [key(8, True), click(9, 5, 5, True)]          # gate re-opened, first frame not yet there
        resume = snapshot(10, 1, 1, []) + [ev("privacy_mask_end", 11, **MASK)]
        tail, head, dropped = split_gap([start, move(5, 1, 1), *startup, *resume], input_gated=True)
        self.assertEqual(tail, [start])
        self.assertEqual(head, resume + startup)                 # the clip still opens with its snapshot
        self.assertEqual([r["type"] for r in dropped], ["mouse_move"])
        # v0.1 kept logging during a pause: the same input is not in the video
        tail, head, dropped = split_gap([start, *startup, *resume])
        self.assertEqual(head, resume)
        self.assertEqual(dropped, startup)


if __name__ == "__main__":
    unittest.main(verbosity=2)
