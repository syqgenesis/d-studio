"""E2E 8: single display (contract K3) and the default encoder (K6).

For ``--backend gdi`` and ``--backend gdigrab``: a ~3 s ``--source desktop``
recording at 30 fps with the default ``--codec auto``. Hard checks per backend:

* recorder / packager exit 0
* capture.json capture_mode == desktop and capture_rect == the PRIMARY monitor
  (Markov is single-display; never the whole virtual desktop), every segment
  has that rect, and the video has that size
* capture.json codec and every segment's encoder == libx264 (Markov = libx264
  CRF 23; auto must not pick a hardware encoder or HEVC when libx264 works)
* clip.mp4 is H.264, carries libx264's SEI ("x264 - core") and is a normal
  faststart MP4

(The default backend's capture_rect, including after a resume, is checked in
e2e_pause_resume.py.) The recordings show the runner desktop: their video files
are deleted at the end.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_lib import (CREATE_NO_WINDOW, IS_WIN, Report, ffprobe, ffprobe_path, has_x264_sei,  # noqa: E402
                     is_faststart_mp4, load_json, load_segments, mp4_top_level_boxes, package, prune_videos,
                     rect_tuple, scenario_main, spawn_helper, start_recorder, summarize_segments, video_file,
                     wait_exit, wait_recording, win)

FPS = 30
SECONDS = 3
BACKENDS = ("gdi", "gdigrab")


def stream_encoder_tag(video: str) -> str:
    proc = subprocess.run([ffprobe_path(), "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream_tags=encoder:format_tags=encoder", "-of", "json", video],
                          capture_output=True, timeout=120, creationflags=CREATE_NO_WINDOW if IS_WIN else 0)
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return ""
    st = ((data.get("streams") or [{}])[0].get("tags") or {}).get("encoder") or ""
    return st or ((data.get("format") or {}).get("tags") or {}).get("encoder") or ""


def wiggle(stop: threading.Event, box) -> None:
    w32 = win()
    x0, y0, x1, y1 = box
    i = 0
    while not stop.is_set():
        i += 1
        try:
            w32.move_abs(x0 + (i * 31) % max(1, x1 - x0), y0 + (i * 19) % max(1, y1 - y0))
        except OSError:
            pass
        time.sleep(0.03)


def check_backend(rep: Report, backend: str, primary, box, procs) -> None:
    tag = f"display-{backend}"
    out = os.path.join(rep.out_dir, tag)
    try:
        p, log = start_recorder(out, ["--source", "desktop", "--backend", backend, "--fps", str(FPS),
                                      "--max-seconds", str(SECONDS)], procs, tag)
        if not rep.check(f"{backend}: recorder started", wait_recording(p, log, out), log):
            wait_exit(p, 10, stop_with_f10=False)
            return
        stop = threading.Event()
        th = threading.Thread(target=wiggle, args=(stop, box), daemon=True)
        th.start()
        rc = wait_exit(p, SECONDS + 40)
        stop.set()
        th.join(2)
        rep.check(f"{backend}: recorder exit code 0", rc == 0, f"exit={rc}")
        pk = package(out, rep.out_dir, tag)
        if not rep.check(f"{backend}: packager exit code 0", pk.returncode == 0, f"exit={pk.returncode}"):
            return
        cap = load_json(os.path.join(out, "raw", "capture.json"))
        stats = summarize_segments(load_segments(out))
        video = video_file(out)
        probe = ffprobe(video, count_frames=False)
        rect = rect_tuple(cap.get("capture_rect"))
        rep.note(f"{backend}_capture", {"capture_mode": cap.get("capture_mode"), "capture_rect": rect,
                                        "segment_rects": stats["capture_rects"], "backend": cap.get("backend"),
                                        "codec": cap.get("codec"), "segment_encoders": stats["encoders"],
                                        "video": [probe["width"], probe["height"], probe["codec_name"]],
                                        "stream_encoder_tag": stream_encoder_tag(video)})
        # ---- K3 single display
        rep.check(f"{backend}: capture_mode desktop", cap.get("capture_mode") == "desktop", cap.get("capture_mode"))
        rep.check(f"{backend}: capture_rect == primary monitor", rect == primary,
                  {"capture_rect": rect, "primary": primary})
        rep.check(f"{backend}: every segment records the same rect", bool(stats["capture_rects"])
                  and all(r == rect for r in stats["capture_rects"]), stats["capture_rects"])
        if rect:
            rep.check(f"{backend}: video size == primary monitor size",
                      abs(int(probe["width"] or 0) - rect[2]) <= 1 and abs(int(probe["height"] or 0) - rect[3]) <= 1,
                      f"{probe['width']}x{probe['height']} vs {rect[2]}x{rect[3]}")
        # ---- K6 encoder
        rep.check(f"{backend}: --codec auto picked libx264 (capture.json codec)", cap.get("codec") == "libx264",
                  cap.get("codec"))
        rep.check(f"{backend}: every segment encoded with libx264", stats["encoders"] == ["libx264"],
                  stats["encoders"])
        rep.check(f"{backend}: clip.mp4 is H.264", probe.get("codec_name") == "h264", probe.get("codec_name"))
        rep.check(f"{backend}: clip.mp4 carries libx264's SEI", has_x264_sei(video))
        boxes = mp4_top_level_boxes(video)
        rep.check(f"{backend}: clip.mp4 is a normal faststart MP4", is_faststart_mp4(boxes), boxes[:12])
    finally:
        rep.note(f"{backend}_videos_removed", prune_videos(out) if os.path.isdir(out) else [])


def body(rep: Report, args, procs) -> None:
    w32 = win()
    pw, ph = w32.primary_size()
    primary = w32.primary_monitor_rect()
    rep.note("displays", {"primary": primary, "monitors": w32.monitor_count(), "virtual": w32.virtual_screen()})
    pad = spawn_helper(procs, rep.out_dir, "pad", color="#455A64", x=pw // 4, y=ph // 4, w=pw // 2, h=ph // 2,
                       title="D Studio E2E display pad", label="single display")
    w32.force_foreground(pad.hwnd, click_at=pad.center())
    x0, y0, x1, y1 = pad.rect
    box = (x0 + 30, y0 + 60, x1 - 30, y1 - 30)
    for backend in BACKENDS:
        check_backend(rep, backend, primary, box, procs)


if __name__ == "__main__":
    raise SystemExit(scenario_main("display_encoder", body))
