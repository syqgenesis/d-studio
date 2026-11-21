"""E2E 7: crash safety (contracts K2 crash-safe sidecar + K10 fragmented segments).

Starts a full-desktop recording with the default backend, lets it run (at
least ~6 s, and until raw/segments.json lists >= 2.5 s of frames of the
running segment), then kills the recorder process hard with TerminateProcess
(no finally, no atexit) followed by its FFmpeg child - like a power cut or
"End process tree". Then the packager runs on raw/ and must still produce a
decodable clip:

* raw/segments.json exists although stop() never ran (written while
  recording; the running segment is marked in_progress)
* packager exit 0; clip.mp4 decodes with >= 2 s of frames; ffprobe's frame
  count == metadata.total_frames == frame_events.json lines
* clip.mp4 is a normal MP4 (moov before mdat, no fragments)
* (soft) the raw segment mp4 is fragmented (moof boxes); validation technical_ok

The recording shows the runner desktop: its video files are deleted at the end
(only reports and JSON sidecars are kept as CI artifacts).
"""
from __future__ import annotations

import glob
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_lib import (Report, ffprobe, is_faststart_mp4, load_json, load_segments, mp4_top_level_boxes,  # noqa: E402
                     package, prune_videos, read_jsonl, scenario_main, spawn_helper, start_recorder,
                     summarize_segments, validate, video_file, wait_recording, win)

FPS = 30
MIN_RUN_S = 6.0                 # kill no earlier than this
MAX_RUN_S = 25.0                # ...and no later (segments.json is rewritten about every 10 s)
NEED_LISTED = int(2.5 * FPS)    # frames of the running segment listed in segments.json before the kill
MIN_FRAMES = 2 * FPS            # >= 2 s must survive


def wiggle(stop: threading.Event, box) -> None:
    w32 = win()
    x0, y0, x1, y1 = box
    i = 0
    while not stop.is_set():
        i += 1
        try:
            w32.move_abs(x0 + (i * 29) % max(1, x1 - x0), y0 + (i * 17) % max(1, y1 - y0))
        except OSError:
            pass
        time.sleep(0.03)


def body(rep: Report, args, procs) -> None:
    w32 = win()
    pw, ph = w32.primary_size()
    pad = spawn_helper(procs, rep.out_dir, "pad", color="#00695C", x=pw // 5, y=ph // 5, w=(pw * 3) // 5,
                       h=(ph * 3) // 5, title="D Studio E2E crash pad", label="crash safety")
    w32.force_foreground(pad.hwnd, click_at=pad.center())
    x0, y0, x1, y1 = pad.rect
    out = os.path.join(rep.out_dir, "crash")
    seg_json = os.path.join(out, "raw", "segments.json")
    try:
        p, log = start_recorder(out, ["--source", "desktop", "--backend", "auto", "--fps", str(FPS),
                                      "--max-seconds", "120"], procs, "crash")
        if not rep.check("recorder started", wait_recording(p, log, out), log):
            return
        stop = threading.Event()
        th = threading.Thread(target=wiggle, args=(stop, (x0 + 40, y0 + 60, x1 - 40, y1 - 40)), daemon=True)
        th.start()
        t0 = time.time()
        listed = 0
        while time.time() - t0 < MAX_RUN_S and p.poll() is None:
            if time.time() - t0 >= MIN_RUN_S:
                listed = sum(len(s.get("frames") or []) for s in load_segments(out))
                if listed >= NEED_LISTED:
                    break
            time.sleep(0.25)
        stop.set()
        th.join(2)
        ran = round(time.time() - t0, 1)
        rep.check("recorder still running when it is killed", p.poll() is None, f"exit={p.poll()} after {ran}s")
        rep.check(f"raw/segments.json listed >= {NEED_LISTED} frames while recording (K2)", listed >= NEED_LISTED,
                  f"{listed} frames after {ran}s")

        # ------------------------------------------------------------ the crash
        children = w32.descendants(p.pid) if p.poll() is None else []
        killed = w32.terminate(p.pid)
        for pid, _exe in children:
            w32.terminate(pid)
        try:
            p.wait(15)
        except Exception:
            pass
        rep.note("killed", {"after_s": ran, "recorder_pid": p.pid, "children": children})
        rep.check("recorder killed with TerminateProcess", killed)
        time.sleep(1.0)

        # ------------------------------------------------------------ what survived
        rep.check("raw/segments.json exists after the crash", os.path.isfile(seg_json))
        segs = load_segments(out)
        stats = summarize_segments(segs)
        rep.note("segments_after_crash", {k: stats[k] for k in ("segments", "frames", "in_progress", "finalized",
                                                                "broken", "backends")})
        rep.check("the running segment is marked in_progress", any(stats["in_progress"]), stats["in_progress"],
                  soft=True)
        seg_files = sorted(glob.glob(os.path.join(out, "raw", "segments", "*.mp4")))
        if seg_files:
            boxes = mp4_top_level_boxes(seg_files[-1])
            rep.note("segment_boxes", boxes[:12])
            rep.check("segment mp4 is fragmented (decodable without a final moov, K10)", "moof" in boxes, boxes[:12],
                      soft=True)

        pk = package(out, rep.out_dir, "crash")
        if not rep.check("packager exit code 0 on the crashed recording", pk.returncode == 0,
                         f"exit={pk.returncode}"):
            return
        meta = load_json(os.path.join(out, "metadata.json"))
        video = video_file(out)
        probe = ffprobe(video)
        total = int(meta.get("total_frames") or 0)
        rep.note("ffprobe", probe)
        rep.check(f"clip.mp4 decodes with >= {MIN_FRAMES} frames (2 s)", (probe["nb_read_frames"] or 0) >= MIN_FRAMES,
                  probe["nb_read_frames"])
        rep.check("ffprobe decoded frames == metadata.total_frames", probe["nb_read_frames"] == total,
                  f"{probe['nb_read_frames']} vs {total}")
        fe = read_jsonl(os.path.join(out, "frame_events.json"))
        rep.check("frame_events.json has one line per frame", len(fe) == total, f"{len(fe)} vs {total}")
        boxes = mp4_top_level_boxes(video)
        rep.check("clip.mp4 is a normal MP4 (moov before mdat, not fragmented)", is_faststart_mp4(boxes), boxes[:12])
        val = validate(out, rep.out_dir, "crash")
        rep.check("validation technical_ok", bool(val.get("technical_ok")), val.get("technical_errors"), soft=True)
    finally:
        rep.note("videos_removed", prune_videos(out) if os.path.isdir(out) else [])


if __name__ == "__main__":
    raise SystemExit(scenario_main("crash_safety", body))
