"""E2E 1: full-desktop recording, 30 fps, gdigrab backend, ~6 s with real input.

While recording, SendInput moves the mouse, clicks, types and scrolls over a
helper window. Then the packager runs and we check:

* recorder + packager exit 0, validation.json technical_ok
* ffprobe decoded frame count == metadata.total_frames == frame_events lines
* ffprobe avg_frame_rate == 30 (CFR slots; FFmpeg's gdigrab reports no real-capture
  count - the real-capture rate of the default backend is checked in
  e2e_pause_resume.py / e2e_export.py / e2e_fps60.py)
* events.json contains click, key, mouse_move and scroll
* (soft) the OS cursor is drawn into the video where we parked it
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_lib import (Report, copy_small_dataset, cursor_pixel_check, event_types, ffprobe,  # noqa: E402
                     frame_rgb, load_json, package, read_jsonl, scenario_main, spawn_helper, start_recorder,
                     validate, video_file, wait_exit, wait_recording, win)

FPS = 30
SECONDS = 6
PAD_RGB = (0x33, 0x66, 0xCC)


def body(rep: Report, args, procs) -> None:
    w32 = win()
    vx, vy, vw, vh = w32.virtual_screen()
    pw, ph = w32.primary_size()
    rep.note("screen", {"virtual": [vx, vy, vw, vh], "primary": [pw, ph], "cursor": w32.cursor_info()})
    pad_w, pad_h = min(700, pw - 120), min(460, ph - 160)
    pad_x, pad_y = (pw - pad_w) // 2, (ph - pad_h) // 2
    pad = spawn_helper(procs, rep.out_dir, "pad", color="#3366CC", x=pad_x, y=pad_y, w=pad_w, h=pad_h,
                       title="D Studio E2E pad", text=True, label="D Studio E2E")
    x0, y0, x1, y1 = pad.rect
    canvas = ((x0 + x1) // 2, y0 + int((y1 - y0) * 0.30))       # uniform-colour area
    text_pt = ((x0 + x1) // 2, y0 + int((y1 - y0) * 0.78))      # inside the Text widget
    rep.check("pad window in front", w32.force_foreground(pad.hwnd, click_at=canvas), soft=True)

    out = os.path.join(rep.out_dir, "desktop30")
    p, log = start_recorder(out, ["--source", "desktop", "--backend", "gdigrab", "--fps", str(FPS),
                                  "--max-seconds", str(SECONDS)], procs, "desktop30")
    if not rep.check("recorder started", wait_recording(p, log, out), log):
        wait_exit(p, 10)
        return
    started = time.time()
    # ---- input while recording (~3.5 s)
    w32.move_to(canvas[0] - 150, canvas[1], duration=0.3)
    w32.move_to(canvas[0] + 150, canvas[1] + 20, duration=0.4)
    w32.click()                                   # click on the pad
    w32.move_to(*text_pt, duration=0.25)
    w32.click()                                   # focus the Text widget
    typed = w32.type_text("markov dstudio 123")
    for _ in range(3):
        w32.wheel(-1)
    w32.move_to(*canvas, duration=0.3)            # park the cursor on the uniform area
    parked_ms = time.time() * 1000.0
    rep.note("input_seconds", round(time.time() - started, 2))
    rep.note("typed_vks", typed)

    rc = wait_exit(p, SECONDS + 40)
    rep.check("recorder exit code 0", rc == 0, f"exit={rc}")
    pk = package(out, rep.out_dir, "desktop30")
    if not rep.check("packager exit code 0", pk.returncode == 0, f"exit={pk.returncode}"):
        return
    meta = load_json(os.path.join(out, "metadata.json"))
    video = video_file(out)
    probe = ffprobe(video)
    rep.note("ffprobe", probe)
    rep.note("metadata", {k: meta.get(k) for k in ("total_frames", "fps", "total_duration_ms", "event_count",
                                                     "capture_mode", "cursor_in_video")})
    total = int(meta.get("total_frames") or 0)
    rep.check("ffprobe decoded frames == metadata.total_frames", probe["nb_read_frames"] == total,
              f"{probe['nb_read_frames']} vs {total}")
    rep.check("avg_frame_rate is 30", abs(probe["avg_fps"] - FPS) < 0.01, probe["avg_frame_rate"])
    rep.check("metadata fps is 30", int(meta.get("fps") or 0) == FPS, meta.get("fps"))
    rep.check("enough frames for ~6 s", total >= int(0.6 * FPS * SECONDS), f"{total} frames")
    fe = read_jsonl(os.path.join(out, "frame_events.json"))
    rep.check("frame_events.json has one line per frame", len(fe) == total, f"{len(fe)} lines")
    events = read_jsonl(os.path.join(out, "events.json"))
    types = event_types(events)
    rep.note("event_types", types)
    for t in ("mouse_move", "click", "key", "scroll"):
        rep.check(f"events.json contains {t}", types.get(t, 0) > 0, types.get(t, 0))
    val = validate(out, rep.out_dir, "desktop30")
    rep.check("validation technical_ok", bool(val.get("technical_ok")), val.get("technical_errors"))

    # ---- cursor drawn into the video by FFmpeg's draw_mouse (soft here: the default
    # backend's Python-drawn cursor is a hard check in e2e_export.py)
    try:
        cur = cursor_pixel_check(out, video, canvas, parked_ms, PAD_RGB,
                                 os.path.join(rep.out_dir, "frames", "desktop30-cursor.png"))
        cur["cursor_info"] = w32.cursor_info()
        rep.note("cursor_pixels", cur)
        rep.check("cursor drawn into the video", cur["ok"], f"{cur['at_cursor']} px differ", soft=True)
        mid = (probe["duration_s"] or SECONDS) / 2
        frame_rgb(video, mid, os.path.join(rep.out_dir, "frames", "desktop30-mid.png"))
    except Exception as exc:
        rep.check("cursor pixel check ran", False, repr(exc), soft=True)
    rep.note("dataset", copy_small_dataset(out, rep.out_dir))


if __name__ == "__main__":
    raise SystemExit(scenario_main("desktop30", body))
