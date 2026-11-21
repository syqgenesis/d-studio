"""Generate a synthetic dataset so the whole pipeline can be exercised without
recording (and so CI/UI work has something to look at).

    python tools/make_demo.py --out data/demo
    python tools/make_demo.py --out data/demo-app --app-mode --fps 60

Produces raw/ (events.jsonl, segments.json, capture.json, segments/*.mp4) with
ffmpeg test patterns, then runs the packager to produce the deliverable.

* default: full-desktop capture with a manual pause during which input was
  still logged (how v0.1 recordings look; the packager drops those events);
* ``--app-mode``: v0.2 app-only capture of one monitor on a two-monitor
  desktop, where the pause is an auto-pause (another app came to the front):
  ``privacy_mask_start`` before the gap, nothing logged while paused, and a
  resume snapshot + ``privacy_mask_end`` before the next clip.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from packager.build import build  # noqa: E402
from recorder.i18n import safe_stdio  # noqa: E402
from recorder.paths import ffmpeg_path  # noqa: E402

BASE_MS = 1_793_283_736_969
FPS = 30
W, H = 1280, 720
SEG_SECONDS = (9, 5)
SECOND_SCREEN = {"index": 1, "originX": W, "originY": 0, "width": 1920, "height": 1080,
                 "visibleWidth": 1920, "visibleHeight": 1040, "scaleFactor": 1.25,
                 "clipIndex": 0, "deviceName": "\\\\.\\DISPLAY2", "primary": False}


def ease(t: float) -> float:
    return t * t * (3 - 2 * t)


class Builder:
    def __init__(self) -> None:
        self.events = []
        self.seq = 0

    def emit(self, etype: str, ts: float, **fields) -> None:
        self.seq += 1
        rec = {"type": etype}
        rec.update(fields)
        rec["timestamp"] = round(ts, 3)
        rec["seq"] = self.seq
        rec["osTime"] = int(ts - BASE_MS)
        self.events.append(rec)

    def move_path(self, t0: float, duration: float, points, hz: float = 90.0, step: float = 0.02) -> None:
        n = max(2, int(duration * hz))
        prev = None
        for i in range(n + 1):
            p = i / n
            seg = min(int(p * (len(points) - 1)), len(points) - 2)
            local = (p * (len(points) - 1)) - seg
            a, b = points[seg], points[seg + 1]
            x = a[0] + (b[0] - a[0]) * ease(local)
            y = a[1] + (b[1] - a[1]) * ease(local)
            ts = t0 + p * duration * 1000.0
            self.emit("mouse_move", ts, x=round(x), y=round(y), appName=self.app,
                      bundleId=self.bundle, screen=0, sourcePid=0)
            if prev is not None:
                dx, dy = round(x - prev[0]), round(y - prev[1])
                if dx or dy:
                    self.emit("mouse_delta", ts, dx=dx, dy=dy, packets=1,
                              appName=self.app, bundleId=self.bundle, screen=0, sourcePid=0)
            prev = (x, y)

    app, bundle = "Windows Explorer", "explorer.exe"


def primary_screen() -> dict:
    return {"index": 0, "originX": 0, "originY": 0, "width": W, "height": H,
            "visibleWidth": W, "visibleHeight": H - 32, "scaleFactor": 1,
            "clipIndex": 0, "deviceName": "\\\\.\\DISPLAY1", "primary": True}


def screens_for(app_mode: bool) -> list:
    return [primary_screen(), dict(SECOND_SCREEN)] if app_mode else [primary_screen()]


def build_events(app_mode: bool = False) -> list:
    b = Builder()
    screens = screens_for(app_mode)
    b.emit("screen_config", BASE_MS - 1200, screenCount=len(screens), screens=screens, reason="open")
    b.emit("active_app", BASE_MS - 1200, name="Windows Explorer", bundleId="explorer.exe",
           windowTitle="Home", pid=1000, isFullScreen=False, hwnd=100, windowClass="CabinetWClass")
    b.emit("appearance_change", BASE_MS - 1100, appearance="dark")
    b.emit("battery_status", BASE_MS - 1100, isCharging=True, batteryLevel=97)
    b.emit("power_source_change", BASE_MS - 1100, isOnAC=True, batteryLevel=97)
    b.emit("memory_pressure", BASE_MS - 1100, pressureLevel="normal")
    for i in range(8):
        b.emit("mouse_move", BASE_MS - 1000 + i * 12, x=120 + i * 3, y=180 + i,
               appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0)

    # ---- segment 0: file explorer, opening a sky footage folder -------------
    seg0 = BASE_MS + 1000
    b.move_path(seg0 + 120, 1.6, [(120, 180), (420, 300), (560, 260)])
    b.emit("click", seg0 + 1750, x=560, y=260, button="left", isDown=True, clickCount=1,
           pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
    b.emit("click", seg0 + 1830, x=560, y=260, button="left", isDown=False, clickCount=1,
           pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
    b.move_path(seg0 + 1900, 1.2, [(560, 260), (680, 420), (700, 450)])

    # double click to open a folder
    for k in (0, 90):
        b.emit("click", seg0 + 3200 + k, x=700, y=450, button="left", isDown=True, clickCount=1 + k // 90,
               pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
        b.emit("click", seg0 + 3260 + k, x=700, y=450, button="left", isDown=False, clickCount=1 + k // 90,
               pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
    b.emit("window_title_changed", seg0 + 3400, title="Sky Footage - File Explorer",
           appName=b.app, bundleId=b.bundle)
    b.emit("window_resized", seg0 + 3600, x=0, y=0, width=1000, height=640,
           appName=b.app, bundleId=b.bundle, windowTitle="Sky Footage")

    # scroll through the asset list
    for i in range(14):
        b.emit("scroll", seg0 + 3900 + i * 55, x=700, y=450, deltaX=0, deltaY=-1,
               phase=0, momentumPhase=0, isPrecise=False, channel="wheel",
               appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
    b.move_path(seg0 + 4300, 0.6, [(700, 450), (760, 500)])

    # switch to DaVinci Resolve
    b.emit("active_app", seg0 + 5000, name="DaVinci Resolve", bundleId="Resolve.exe",
           windowTitle="Untitled Project - DaVinci Resolve", pid=2000, isFullScreen=False,
           hwnd=200, windowClass="Qt662QWindowIcon")
    b.app, b.bundle = "DaVinci Resolve", "Resolve.exe"
    b.move_path(seg0 + 5100, 1.0, [(760, 500), (900, 200), (960, 180)])
    b.emit("click", seg0 + 6200, x=960, y=180, button="left", isDown=True, clickCount=1,
           pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
    b.emit("click", seg0 + 6280, x=960, y=180, button="left", isDown=False, clickCount=1,
           pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)

    # a drag (scrubbing a slider)
    drag_start = seg0 + 6500
    b.emit("click", drag_start, x=500, y=600, button="left", isDown=True, clickCount=1,
           pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
    n = 40
    for i in range(n + 1):
        p = i / n
        x = 500 + 480 * p
        y = 600 - 40 * math.sin(p * math.pi)
        ts = drag_start + 80 + p * 900
        buttons = {"left": True, "right": False, "middle": False, "other_3": False, "other_4": False}
        b.emit("drag", ts, x=round(x), y=round(y), button="left", pressure=1,
               buttons=buttons, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
    b.emit("click", drag_start + 1050, x=980, y=600, button="left", isDown=False, clickCount=1,
           pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)

    # keyboard: ctrl+z, then typing a project name
    b.emit("modifier_change", seg0 + 7700, modifiers=2, sourcePid=0)
    b.emit("key", seg0 + 7750, keyCode=90, characters="\u001a", isDown=True, modifiers=2,
           appName=b.app, bundleId=b.bundle, isRepeat=False, sourcePid=0, scanCode=44,
           extended=False, hkl="40090409", dead=False)
    b.emit("key", seg0 + 7860, keyCode=90, characters="", isDown=False, modifiers=2,
           appName=b.app, bundleId=b.bundle, isRepeat=False, sourcePid=0, scanCode=44,
           extended=False, hkl="40090409", dead=False)
    b.emit("modifier_change", seg0 + 7900, modifiers=0, sourcePid=0)
    for ch in "Sky":
        vk = ord(ch.upper())
        b.emit("key", seg0 + 8000, keyCode=vk, characters=ch, isDown=True, modifiers=0,
               appName=b.app, bundleId=b.bundle, isRepeat=False, sourcePid=0,
               scanCode=vk, extended=False, hkl="40090409", dead=False)
        b.emit("key", seg0 + 8040, keyCode=vk, characters="", isDown=False, modifiers=0,
               appName=b.app, bundleId=b.bundle, isRepeat=False, sourcePid=0,
               scanCode=vk, extended=False, hkl="40090409", dead=False)
    b.emit("memory_pressure", seg0 + 8400, pressureLevel="warning")

    gap0 = BASE_MS + 1000 + SEG_SECONDS[0] * 1000
    seg1 = gap0 + 4000
    if app_mode:
        # ---- auto-pause: a chat app came to the front; nothing is logged ----
        mask = dict(trigger="non_target_app", maskTrigger="non_target_app", maskDetail="wechat.exe",
                    appName="WeChat", exeName="WeChat.exe", windowTitle="", maskedDisplay=0)
        b.emit("privacy_mask_start", gap0 + 5, **mask)
        # resume snapshot (Markov opens every segment like this), then mask end
        b.emit("screen_config", seg1 - 180, screenCount=len(screens), screens=screens, reason="open")
        b.emit("active_app", seg1 - 180, name="DaVinci Resolve", bundleId="Resolve.exe",
               windowTitle="Untitled Project - DaVinci Resolve", pid=2000, isFullScreen=False,
               hwnd=200, windowClass="Qt662QWindowIcon")
        b.emit("appearance_change", seg1 - 179, appearance="dark")
        b.emit("battery_status", seg1 - 179, isCharging=True, batteryLevel=96)
        b.emit("power_source_change", seg1 - 179, isOnAC=True, batteryLevel=96)
        b.emit("memory_pressure", seg1 - 179, pressureLevel="normal")
        b.emit("mouse_move", seg1 - 178, x=1040, y=620, appName=b.app, bundleId=b.bundle,
               screen=0, sourcePid=0)
        b.emit("privacy_mask_end", seg1 - 170, **mask)
    else:
        # ---- the pause: input keeps flowing, but no frames are captured ----
        b.emit("mouse_move", gap0 + 600, x=980, y=600, appName=b.app, bundleId=b.bundle,
               screen=0, sourcePid=0)
        b.emit("click", gap0 + 900, x=980, y=600, button="left", isDown=True, clickCount=1,
               pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
        b.emit("click", gap0 + 980, x=980, y=600, button="left", isDown=False, clickCount=1,
               pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
        b.emit("mouse_move", gap0 + 1400, x=1040, y=620, appName=b.app, bundleId=b.bundle,
               screen=0, sourcePid=0)

    # ---- segment 1: after a pause, grading ---------------------------------
    b.emit("active_app", seg1 + 60, name="DaVinci Resolve", bundleId="Resolve.exe",
           windowTitle="Sky Composite - DaVinci Resolve", pid=2000, isFullScreen=False,
           hwnd=200, windowClass="Qt662QWindowIcon")
    b.move_path(seg1 + 200, 1.5, [(980, 600), (400, 320), (300, 300)])
    b.emit("click", seg1 + 1800, x=300, y=300, button="left", isDown=True, clickCount=1,
           pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
    b.emit("click", seg1 + 1880, x=300, y=300, button="left", isDown=False, clickCount=1,
           pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
    b.move_path(seg1 + 1900, 2.2, [(300, 300), (520, 380), (420, 560), (540, 600)])
    for i in range(6):
        b.emit("scroll", seg1 + 4200 + i * 70, x=540, y=600, deltaX=0, deltaY=1,
               phase=0, momentumPhase=0, isPrecise=True, channel="wheel",
               appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
    b.emit("click", seg1 + 4800, x=1100, y=140, button="right", isDown=True, clickCount=1,
           pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
    b.emit("click", seg1 + 4880, x=1100, y=140, button="right", isDown=False, clickCount=1,
           pressure=1, appName=b.app, bundleId=b.bundle, screen=0, sourcePid=0, modifiers=0)
    b.events.sort(key=lambda r: (r["timestamp"], r["seq"]))
    return b.events


def make_video(path: str, pattern: str, seconds: int = 5, hue: int = 0, fps: int = FPS,
               ffmpeg: str = "ffmpeg") -> None:
    vf = f"hue=h={hue}," if hue else ""
    # Same container as the v0.2.1 recorder: fragmented MP4 with a keyframe
    # every 2 s (crash-safe); the packager remuxes it into a +faststart clip.mp4.
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"{pattern}=size={W}x{H}:rate={fps}",
           "-frames:v", str(int(seconds * fps)), "-vf", f"{vf}format=yuv420p",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", str(2 * fps),
           "-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-r", str(fps), path]
    subprocess.run(cmd, check=True)


def main(argv=None) -> int:
    safe_stdio()   # the packager prints bilingual messages
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/demo")
    ap.add_argument("--id", default=None)
    ap.add_argument("--no-video", action="store_true")
    ap.add_argument("--fps", type=int, default=FPS, help="frame rate (Markov samples: 30)")
    ap.add_argument("--app-mode", action="store_true",
                    help="v0.2 app-only capture of one monitor with an auto-pause")
    args = ap.parse_args(argv)
    fps = max(1, min(120, args.fps))
    ffmpeg = ffmpeg_path()

    out = os.path.abspath(args.out)
    raw = os.path.join(out, "raw")
    seg_dir = os.path.join(raw, "segments")
    os.makedirs(seg_dir, exist_ok=True)
    dataset_id = args.id or os.path.basename(out)

    events = build_events(args.app_mode)
    with open(os.path.join(raw, "events.jsonl"), "w", encoding="utf-8") as fh:
        for rec in events:
            fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")

    seg0 = BASE_MS + 1000
    seg1 = seg0 + SEG_SECONDS[0] * 1000 + 4000
    lengths = [seconds * fps for seconds in SEG_SECONDS]
    segments = []
    for idx, (start, length) in enumerate(zip((seg0, seg1), lengths)):
        frames = [[i, round(start + i * 1000.0 / fps, 3)] for i in range(length)]
        segments.append({
            "index": idx, "file": f"seg_{idx:03d}.mp4",
            "start_wall_ms": start, "end_wall_ms": round(start + length * 1000.0 / fps, 3),
            "fps": fps, "frame_count": len(frames), "frames": frames,
            "encoder": "libx264", "cmd": "synthetic", "finalized": True, "returncode": 0,
        })
        path = os.path.join(seg_dir, f"seg_{idx:03d}.mp4")
        if not args.no_video:
            make_video(path, "testsrc2", seconds=SEG_SECONDS[idx], hue=idx * 90, fps=fps, ffmpeg=ffmpeg)
        else:
            open(path, "wb").close()
    with open(os.path.join(raw, "segments.json"), "w", encoding="utf-8") as fh:
        json.dump(segments, fh)

    with open(os.path.join(raw, "capture.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "version": 1, "status": "completed", "host": "demo", "platform": "windows",
            "os_version": "synthetic", "fps": fps,
            "source": "app:Resolve.exe" if args.app_mode else "desktop", "codec": "libx264",
            "quality": 23, "segments": 2, "paused_ms": 4000,
            "started_at_ms": BASE_MS - 1200,
            "duration_ms": sum(lengths) * 1000.0 / fps + 4000,
            "screens": screens_for(args.app_mode),
            "capture_mode": "app" if args.app_mode else "desktop",
            "capture_rect": {"x": 0, "y": 0, "width": W, "height": H},
            "cursor_in_video": False,  # ffmpeg test pattern: no OS cursor drawn
            "app_target": ({"exe": "Resolve.exe", "allow": ["textinputhost.exe", "ctfmon.exe"],
                            "auto_pause": True} if args.app_mode else None),
            "mask_stats": ({"masked_frames": 0, "auto_pauses": 1, "auto_paused_ms": 4000}
                           if args.app_mode else {"masked_frames": 0, "auto_pauses": 0, "auto_paused_ms": 0}),
            "sync_latency_ms": 0, "privacy_blocklist": [], "ffmpeg": ffmpeg,
        }, fh, indent=2)

    meta = build(raw, out, dataset_id=dataset_id, title="Demo · Sky Replacement",
                 description="Synthetic demo session: open sky footage in Explorer, then grade in Resolve.",
                 category="video-editing", max_gap_ms=30000)
    print(f"[demo] ready: {out}  ({meta['total_frames']} frames, {meta['event_count']} events)")
    print("[demo] open the annotator:  python tools/serve.py --open")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
