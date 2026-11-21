"""E2E 4: manual pause / resume with F9 and stop with F10 (global hotkeys).

  record ~3 s of input -> F9 (pause) -> 3 s of input that must NOT be logged
  -> F9 (resume) -> ~3 s of input -> F10 (stop)

Checks: exactly 2 clips; no mouse/keyboard event with a timestamp inside the
pause (with a small margin for the hotkey itself); the keys typed during the
pause (q x z 7 8 9) never appear; keys before/after the pause do; frame count
matches ffprobe; capture.json paused time covers the gap.

Default backend (auto, 30 fps): real captures (raw/segments.json
capture.captures) cover >= 80 % of the CFR frames; desktop mode records the
primary monitor only (capture.json capture_rect == primary monitor rect, the
video has that size) and the segment after the resume reproduces exactly the
same capture_rect (single display, contract K3).
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_lib import (Report, copy_small_dataset, ffprobe, input_events_between, keys_with_codes,  # noqa: E402
                     load_json, load_segments, package, read_jsonl, real_capture_ok, rect_tuple, scenario_main,
                     spawn_helper, start_recorder, summarize_segments, validate, video_file, wait_exit,
                     wait_recording, win)

SECRET = "qxz789"
SECRET_VKS = [ord(c.upper()) for c in SECRET]
MARGIN_MS = 400.0


def body(rep: Report, args, procs) -> None:
    w32 = win()
    pw, ph = w32.primary_size()
    pad_w, pad_h = min(640, pw - 160), min(420, ph - 180)
    pad = spawn_helper(procs, rep.out_dir, "pad", color="#2E7D32", x=(pw - pad_w) // 2, y=(ph - pad_h) // 2,
                       w=pad_w, h=pad_h, title="D Studio E2E pause pad", text=True, label="pause / resume")
    x0, y0, x1, y1 = pad.rect
    text_pt = ((x0 + x1) // 2, y0 + int((y1 - y0) * 0.78))
    w32.force_foreground(pad.hwnd, click_at=text_pt)

    out = os.path.join(rep.out_dir, "pause_resume")
    p, log = start_recorder(out, ["--source", "desktop", "--backend", "auto", "--fps", "30",
                                  "--max-seconds", "90"], procs, "pause_resume")
    if not rep.check("recorder started", wait_recording(p, log, out), log):
        wait_exit(p, 10)
        return

    # segment 1
    w32.move_to(*text_pt, duration=0.3)
    w32.click()
    before = w32.type_text("dstudio")
    w32.move_to(text_pt[0] - 100, text_pt[1] - 60, duration=0.4)
    time.sleep(1.0)

    # pause
    t_pause = time.time() * 1000.0
    w32.tap(w32.VK_F9)
    time.sleep(1.2)
    w32.move_to(*text_pt, duration=0.4)
    w32.click()
    w32.type_text(SECRET)
    w32.wheel(-1)
    w32.move_to(text_pt[0] + 120, text_pt[1] - 80, duration=0.5)
    time.sleep(0.8)
    t_resume = time.time() * 1000.0
    w32.tap(w32.VK_F9)

    # segment 2
    time.sleep(1.5)
    w32.move_to(*text_pt, duration=0.3)
    w32.click()
    after = w32.type_text("markov")
    time.sleep(1.0)
    t_stop = time.time() * 1000.0
    w32.tap(w32.VK_F10)
    rc = wait_exit(p, 60)
    rep.note("timeline_ms", {"pause": t_pause, "resume": t_resume, "stop": t_stop})
    rep.check("recorder stopped by F10 (exit code 0)", rc == 0, f"exit={rc}")

    raw = read_jsonl(os.path.join(out, "raw", "events.jsonl"))
    leaked = input_events_between(raw, t_pause + MARGIN_MS, t_resume - 100.0)
    rep.check("raw: no input events while paused", not leaked,
              [{k: e.get(k) for k in ("type", "timestamp", "keyCode", "x", "y")} for e in leaked[:8]])
    rep.check("raw: keys typed while paused never logged", not keys_with_codes(raw, SECRET_VKS))
    rep.check("raw: keys before the pause logged", bool(keys_with_codes(raw, before)))
    rep.check("raw: keys after the resume logged", bool(keys_with_codes(raw, after)))

    pk = package(out, rep.out_dir, "pause_resume")
    if not rep.check("packager exit code 0", pk.returncode == 0, f"exit={pk.returncode}"):
        return
    meta = load_json(os.path.join(out, "metadata.json"))
    clips = meta.get("clips") or []
    rep.note("clips", clips)
    rep.check("exactly 2 clips", len(clips) == 2, len(clips))
    events = read_jsonl(os.path.join(out, "events.json"))
    rep.check("events.json: no input events while paused",
              not input_events_between(events, t_pause + MARGIN_MS, t_resume - 100.0))
    rep.check("events.json: keys typed while paused never logged", not keys_with_codes(events, SECRET_VKS))
    probe = ffprobe(video_file(out))
    rep.note("ffprobe", probe)
    rep.check("ffprobe decoded frames == metadata.total_frames",
              probe["nb_read_frames"] == int(meta.get("total_frames") or -1),
              f"{probe['nb_read_frames']} vs {meta.get('total_frames')}")
    cap_path = os.path.join(out, "raw", "capture.json")
    cap = load_json(cap_path) if os.path.isfile(cap_path) else {}
    gap = t_resume - t_pause
    rep.check("capture.json paused_ms covers the pause", float(cap.get("paused_ms") or 0) >= gap * 0.7,
              f"paused_ms={cap.get('paused_ms')} gap={gap:.0f}", soft=True)
    if len(clips) == 2:
        rep.check("clip 2 gap_before_ms ~ pause length", float(clips[1].get("gap_before_ms") or 0) >= gap * 0.6,
                  clips[1].get("gap_before_ms"), soft=True)
    val = validate(out, rep.out_dir, "pause_resume")
    rep.check("validation technical_ok", bool(val.get("technical_ok")), val.get("technical_errors"))

    # real captures (not only CFR slots) on the default backend
    stats = summarize_segments(load_segments(out))
    rep.note("segment_stats", {k: stats[k] for k in ("segments", "frames", "captures", "real_ratio", "capture_fps",
                                                     "backends", "capture_rects", "cursor_frames")})
    rep.check("auto backend really captured >= 80 % of the 30 fps frames", bool(real_capture_ok(stats)),
              {k: stats[k] for k in ("frames", "captures", "real_ratio", "backends")})
    # single display (K3): the primary monitor, and the resumed segment is identical
    primary = w32.primary_monitor_rect()
    rect = rect_tuple(cap.get("capture_rect"))
    rep.check("desktop mode records the primary monitor (capture.json capture_rect)", rect == primary,
              {"capture_rect": rect, "primary": primary, "monitors": w32.monitor_count()})
    rects = stats["capture_rects"]
    rep.check("every segment has the first segment's capture_rect (resume keeps the display)",
              len(rects) >= 2 and all(r is not None and r == rect for r in rects), rects)
    if rect:
        rep.check("video size == capture_rect size",
                  abs(int(probe["width"] or 0) - rect[2]) <= 1 and abs(int(probe["height"] or 0) - rect[3]) <= 1,
                  f"{probe['width']}x{probe['height']} vs {rect[2]}x{rect[3]}")
    rep.note("dataset", copy_small_dataset(out, rep.out_dir))


if __name__ == "__main__":
    raise SystemExit(scenario_main("pause_resume", body))
