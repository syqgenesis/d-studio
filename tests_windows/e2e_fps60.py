"""E2E 2: 60 fps recording with every capture backend that works here.

Records ~5 s at --fps 60 with continuous mouse motion (so DXGI sees new
frames) for each backend: auto, dxcam, gdi, gdigrab. For each one we record
whether it worked, the encoder, ffprobe's avg_frame_rate, frame counts and the
recorder's own real-capture statistics (raw/segments.json capture.captures /
capture_fps / duplicated_slots).

A CFR stream always *looks* like 60 fps: missed slots are padded with the
previous frame, so ffprobe's rate and frame count match whatever was really
captured. "Reaches 60 fps" therefore also needs real captures: captures /
frames >= 0.8 (backends without that statistic, i.e. FFmpeg's gdigrab, can
only show CFR and never count as reaching 60 fps).

Hard checks: the default backend (auto) records a 60 fps CFR video whose
decoded frame count matches metadata AND whose real captures are >= 80 % of the
frames; when artifacts/env (env_report.py) shows that DXGI duplication works
here, auto must really use dxcam (no fallback) and the dxcam backend is hard
too. gdi / gdigrab and dxcam without working DXGI stay soft.
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_lib import (Report, copy_small_dataset, env_dxgi_works, ffprobe, load_env_report, load_json,  # noqa: E402
                     load_segments, package, real_capture_ok, scenario_main, start_recorder, summarize_segments,
                     video_file, wait_exit, wait_recording, win)

FPS = 60
SECONDS = 5
BACKENDS = ("auto", "dxcam", "gdi", "gdigrab")
MIN_REAL = 0.8


def wiggle(stop: threading.Event, box) -> None:
    w32 = win()
    x0, y0, x1, y1 = box
    i = 0
    while not stop.is_set():
        i += 1
        x = x0 + (i * 37) % max(1, x1 - x0)
        y = y0 + (i * 23) % max(1, y1 - y0)
        try:
            w32.move_abs(x, y)
        except OSError:
            pass
        time.sleep(1 / 120)


def body(rep: Report, args, procs) -> None:
    pw, ph = win().primary_size()
    box = (pw // 4, ph // 4, pw * 3 // 4, ph * 3 // 4)
    results = {}
    for backend in BACKENDS:
        tag = f"fps60-{backend}"
        out = os.path.join(rep.out_dir, tag)
        res = {"backend_requested": backend}
        results[backend] = res
        p, log = start_recorder(out, ["--source", "desktop", "--backend", backend, "--fps", str(FPS),
                                      "--max-seconds", str(SECONDS)], procs, tag)
        if not wait_recording(p, log, out, timeout=30):
            res["error"] = "did not start"
            res["exit"] = wait_exit(p, 15, stop_with_f10=False)
            continue
        stop = threading.Event()
        th = threading.Thread(target=wiggle, args=(stop, box), daemon=True)
        th.start()
        rc = wait_exit(p, SECONDS + 40)
        stop.set()
        th.join(2)
        res["exit"] = rc
        if rc != 0:
            res["error"] = f"recorder exit {rc}"
            continue
        pk = package(out, rep.out_dir, tag)
        if pk.returncode != 0:
            res["error"] = f"packager exit {pk.returncode}"
            continue
        try:
            meta = load_json(os.path.join(out, "metadata.json"))
            probe = ffprobe(video_file(out))
            cap = load_json(os.path.join(out, "capture.json")) if os.path.isfile(os.path.join(out, "capture.json")) \
                else load_json(os.path.join(out, "raw", "capture.json"))
            seg_list = load_segments(out)
            first = seg_list[0] if seg_list else {}
            stats = summarize_segments(seg_list)
            res.update({
                "backend_used": cap.get("backend") or first.get("backend"),
                "backend_fallbacks": cap.get("backend_fallbacks"),
                "segment_backends": stats["backends"],
                "backend_detail": cap.get("backend_detail") or first.get("capture"),
                "encoder": first.get("encoder") or cap.get("codec"),
                "avg_frame_rate": probe["avg_frame_rate"], "avg_fps": probe["avg_fps"],
                "nb_read_frames": probe["nb_read_frames"], "total_frames": meta.get("total_frames"),
                "duration_s": probe["duration_s"], "size": [probe["width"], probe["height"]],
                "captures": stats["captures"], "real_ratio": stats["real_ratio"],
                "capture_fps": stats["capture_fps"],
            })
            total = int(meta.get("total_frames") or 0)
            res["cfr_ok"] = (abs(probe["avg_fps"] - FPS) < 0.01 and probe["nb_read_frames"] == total
                             and total >= int(0.5 * FPS * SECONDS))
            res["real_ok"] = real_capture_ok(stats, MIN_REAL)    # None: backend cannot tell (gdigrab)
            res["ok"] = bool(res["cfr_ok"] and res["real_ok"])
        except Exception as exc:
            res["error"] = repr(exc)
        copy_small_dataset(out, rep.out_dir)
    rep.note("backends", results)
    env = load_env_report()
    dxgi = env_dxgi_works(env)
    rep.note("env_dxgi_duplication_works", dxgi if env else "no artifacts/env/env.json")
    working = [b for b, r in results.items() if r.get("ok")]
    rep.note("backends_reaching_60fps", working)

    def detail(r):
        return {k: r.get(k) for k in ("backend_used", "backend_fallbacks", "avg_frame_rate", "nb_read_frames",
                                      "total_frames", "captures", "real_ratio", "capture_fps", "error")}

    auto = results.get("auto", {})
    rep.check("auto backend records 60 fps CFR with matching frame count", bool(auto.get("cfr_ok")), detail(auto))
    rep.check(f"auto backend really captures >= {int(MIN_REAL * 100)} % of the 60 fps frames",
              bool(auto.get("real_ok")), detail(auto))
    if dxgi:
        rep.check("auto uses DXGI (dxcam) without fallback where DXGI duplication works",
                  auto.get("backend_used") == "dxcam" and not auto.get("backend_fallbacks"), detail(auto))
    else:
        rep.check("auto backend used", bool(auto.get("backend_used")), detail(auto), soft=True)
    rep.check("at least one backend reaches 60 fps with real captures", bool(working), working)
    for b in BACKENDS[1:]:
        r = results[b]
        hard = b == "dxcam" and dxgi
        if r.get("real_ok") is None and not r.get("error"):
            note = f"{r.get('avg_frame_rate')} / {r.get('nb_read_frames')} frames (CFR only: no capture count)"
        else:
            note = r.get("error") or (f"{r.get('avg_frame_rate')} / {r.get('nb_read_frames')} frames, "
                                      f"real {r.get('captures')} captures ({r.get('real_ratio')})")
        rep.check(f"backend {b} at 60 fps", bool(r.get("ok")), note, soft=not hard)


if __name__ == "__main__":
    raise SystemExit(scenario_main("fps60", body))
