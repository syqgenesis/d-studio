"""E2E 5: "export recording only" produces exactly the Markov clip set.

Records a short desktop clip (or uses --dataset), packages it, then runs
``python -m packager.export_cad --recording-only --dataset D --out O`` and checks:

* O contains exactly clip.mp4, events.json, frame_events.json, metadata.json, narration.json
* events.json / frame_events.json are valid JSONL (one JSON object per line)
* metadata.json is a JSON object with fps / total_frames; narration.json has
  "annotations" (empty list when nothing was annotated yet)
* clip.mp4 decodes and its frame count matches metadata.total_frames
* a second export to the same folder is refused (non-zero exit, folder untouched)

The recording is made with the default backend (auto: dxcam/gdi draw the cursor
in Python, as the recorder bar does), so it also checks goal 1 "cursor drawn
into the video": capture.json cursor_in_video, raw/segments.json
capture.cursor_frames > 0 and the cursor's pixels where it was parked over the
pad's uniform area. Hard when the runner reports a showing cursor
(GetCursorInfo CURSOR_SHOWING), soft otherwise (some VMs have no cursor).
"""
from __future__ import annotations

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_lib import (Report, cursor_pixel_check, env_cursor_showing, ffprobe, load_env_report,  # noqa: E402
                     load_json, load_segments, package, read_jsonl, run, scenario_main, spawn_helper,
                     start_recorder, summarize_segments, video_file, wait_exit, wait_recording, win)

EXPECTED = {"clip.mp4", "events.json", "frame_events.json", "metadata.json", "narration.json"}
PAD_RGB = (0x6A, 0x1B, 0x9A)


def add_args(ap) -> None:
    ap.add_argument("--dataset", help="use an existing packaged recording instead of recording one")


def record_one(rep: Report, procs) -> str:
    w32 = win()
    pw, ph = w32.primary_size()
    pad = spawn_helper(procs, rep.out_dir, "pad", color="#6A1B9A", x=pw // 4, y=ph // 4, w=pw // 2, h=ph // 2,
                       title="D Studio E2E export pad", text=True, label="export")
    x0, y0, x1, y1 = pad.rect
    text_pt = ((x0 + x1) // 2, y0 + int((y1 - y0) * 0.78))
    canvas = ((x0 + x1) // 2, y0 + int((y1 - y0) * 0.30))      # uniform pad colour
    w32.force_foreground(pad.hwnd, click_at=text_pt)
    out = os.path.join(rep.out_dir, "export_src")
    p, log = start_recorder(out, ["--source", "desktop", "--backend", "auto", "--fps", "30", "--max-seconds", "4"],
                            procs, "export_src")
    if not wait_recording(p, log, out):
        wait_exit(p, 10)
        raise RuntimeError("recorder did not start; see " + log)
    w32.move_to(*text_pt, duration=0.3)
    w32.click()
    w32.type_text("export test")
    w32.wheel(-1)
    w32.move_to(*canvas, duration=0.3)            # park the cursor over the uniform area
    parked_ms = time.time() * 1000.0
    cursor_now = w32.cursor_info()
    rc = wait_exit(p, 45)
    rep.check("recorder exit code 0", rc == 0, f"exit={rc}")
    pk = package(out, rep.out_dir, "export_src")
    rep.check("packager exit code 0", pk.returncode == 0, f"exit={pk.returncode}")
    if pk.returncode == 0:
        check_cursor(rep, out, canvas, parked_ms, cursor_now)
    return out


def check_cursor(rep: Report, out: str, canvas, parked_ms: float, cursor_now) -> None:
    """Goal 1 on the default backend: the OS cursor is drawn into the video."""
    env = load_env_report()
    showing = bool(cursor_now.get("showing"))
    rep.note("cursor", {"at_park": cursor_now, "env_report_showing": env_cursor_showing(env) if env else None})
    cap_path = os.path.join(out, "raw", "capture.json")
    cap = load_json(cap_path) if os.path.isfile(cap_path) else {}
    stats = summarize_segments(load_segments(out))
    rep.note("backend", {"capture.json": cap.get("backend"), "segments": stats["backends"],
                         "cursor_frames": stats["cursor_frames"], "frames": stats["frames"]})
    rep.check("capture.json cursor_in_video is true (default backend)", cap.get("cursor_in_video") is True,
              cap.get("cursor_in_video"))
    rep.check("segments report cursor_frames > 0 (cursor drawn by the default backend)",
              bool(stats["cursor_frames"]), stats["cursor_frames"], soft=not showing)
    try:
        cur = cursor_pixel_check(out, video_file(out), canvas, parked_ms, PAD_RGB)
        rep.note("cursor_pixels", cur)
        rep.check("cursor drawn into the video where it was parked (default backend)", cur["ok"],
                  f"{cur['at_cursor']} px differ (reference {cur['reference']})", soft=not showing)
    except Exception as exc:
        rep.check("cursor pixel check ran", False, repr(exc), soft=not showing)


def body(rep: Report, args, procs) -> None:
    dataset = os.path.abspath(args.dataset) if args.dataset else record_one(rep, procs)
    rep.note("dataset", dataset)
    dest = os.path.join(rep.out_dir, "export_out")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    cmd = [sys.executable, "-m", "packager.export_cad", "--recording-only", "--dataset", dataset, "--out", dest]
    first = run(cmd, timeout=600, log=os.path.join(rep.out_dir, "export.log"))
    if not rep.check("export exit code 0", first.returncode == 0, first.text.strip()[-500:]):  # type: ignore[attr-defined]
        return
    files = set(os.listdir(dest)) if os.path.isdir(dest) else set()
    rep.check("exactly the 5 Markov clip files", files == EXPECTED,
              {"extra": sorted(files - EXPECTED), "missing": sorted(EXPECTED - files)})
    lines = {}
    for name in ("events.json", "frame_events.json"):
        try:
            rows = read_jsonl(os.path.join(dest, name))
            lines[name] = len(rows)
            rep.check(f"{name} is valid JSONL", len(rows) > 0, f"{len(rows)} lines")
            rep.check(f"{name} has no internal seq/osTime fields",
                      not any("seq" in r or "osTime" in r for r in rows[:200]), soft=True)
        except Exception as exc:
            rep.check(f"{name} is valid JSONL", False, repr(exc))
    meta = load_json(os.path.join(dest, "metadata.json"))
    rep.note("metadata", meta)
    rep.check("metadata.json has fps and total_frames", isinstance(meta, dict) and "fps" in meta
              and "total_frames" in meta, sorted(meta) if isinstance(meta, dict) else type(meta).__name__)
    narration = load_json(os.path.join(dest, "narration.json"))
    rep.check("narration.json has an annotations list", isinstance(narration, dict)
              and isinstance(narration.get("annotations"), list), narration if len(str(narration)) < 300 else "...")
    if isinstance(meta, dict) and "total_frames" in meta:
        rep.check("frame_events.json has one line per frame", lines.get("frame_events.json") == int(meta["total_frames"]),
                  f"{lines.get('frame_events.json')} vs {meta['total_frames']}")
        probe = ffprobe(os.path.join(dest, "clip.mp4"))
        rep.note("ffprobe", probe)
        rep.check("clip.mp4 decodes with metadata.total_frames frames",
                  probe["nb_read_frames"] == int(meta["total_frames"]), probe["nb_read_frames"])
        rep.check("clip.mp4 is H.264", probe.get("codec_name") == "h264", probe.get("codec_name"))

    before = {n: os.path.getmtime(os.path.join(dest, n)) for n in files}
    time.sleep(1.1)
    second = run(cmd, timeout=300, log=os.path.join(rep.out_dir, "export-again.log"))
    rep.check("second export to the same folder is refused", second.returncode != 0, f"exit={second.returncode}")
    after = {n: os.path.getmtime(os.path.join(dest, n)) for n in os.listdir(dest)}
    rep.check("refused export left the folder untouched", before == after)


if __name__ == "__main__":
    raise SystemExit(scenario_main("export", body, add_args=add_args))
