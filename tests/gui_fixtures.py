"""Synthetic recordings for the recorder-bar tests (tests/test_overlay_state.py).

A raw/ capture in the recorder's format, packaged with the real packager and
checked with the real ``packager.validate.validate_dataset``; FFprobe is faked
with :func:`fake_probe`.  Owned by the GUI tests (kept apart from
tests/test_pipeline.py so a helper change there cannot break them).
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

BASE = 1_800_000_000_000.0
MASK = {"trigger": "non_target_app", "maskTrigger": "non_target_app", "maskDetail": "wechat.exe",
        "appName": "WeChat", "exeName": "WeChat.exe", "windowTitle": "", "maskedDisplay": 1}


def screen(index, x, y, w, h, primary=False):
    return {"index": index, "originX": x, "originY": y, "width": w, "height": h,
            "visibleWidth": w, "visibleHeight": h - 40, "scaleFactor": 1.0, "clipIndex": 0,
            "deviceName": f"\\\\.\\DISPLAY{index + 1}", "primary": primary}


def write_session(root, segments, events, *, fps=30, capture=None, video=False):
    """Synthetic raw/ capture: ``segments`` = [(first_frame_wall_ms, frame_count)].

    ``events`` get seq/osTime in timestamp order.  ``video=True`` drops a
    placeholder clip.mp4 in the package so validation probes it (tests patch
    subprocess.run with :func:`fake_probe`).  ``capture`` overrides fields of
    raw/capture.json (e.g. ``{"status": "recording"}`` for a crashed session).
    """
    raw = os.path.join(root, "raw")
    os.makedirs(os.path.join(raw, "segments"), exist_ok=True)
    ordered = sorted(events, key=lambda e: e["timestamp"])
    with open(os.path.join(raw, "events.jsonl"), "w", encoding="utf-8") as fh:
        for i, ev_ in enumerate(ordered, 1):
            rec = dict(ev_)
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


def package(root, **kwargs):
    """The real packager on ``root/raw`` (video reused: tests drop a placeholder clip)."""
    from packager.build import build

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


def simple_session(root, *, fps=30, capture=None, extra_events=()):
    """One 2-second desktop clip with a click (what real_report and the recovery tests use)."""
    s0, n = BASE + 1000, 2 * fps
    screens = [screen(0, 0, 0, 1920, 1080, True)]
    events = snapshot(s0 - 300, 10, 10, screens) + [move(s0 + 100, 200, 200), click(s0 + 400, 200, 200, True),
                                                    click(s0 + 450, 200, 200, False)] + list(extra_events)
    cap = {"capture_rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}, "cursor_in_video": True}
    cap.update(capture or {})
    write_session(root, [(s0, n)], events, fps=fps, video=True, capture=cap)
    return s0, n
