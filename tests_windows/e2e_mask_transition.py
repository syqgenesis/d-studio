"""E2E 6: app-only masking while the target window changes state.

A large, bright red, NON-topmost "canary" window (a separate process) sits
BEHIND Notepad. While recording ``--source app:notepad.exe`` Notepad is

  maximized -> restored -> maximized + restored quickly -> minimized
  (another app in front: auto-pause) -> restored

Every pixel that is not Notepad's must be black in every frame, also while the
window animates and its rectangle changes (a mask computed from a stale or
early rectangle shows the red window). We decode ALL frames of clip.mp4.

Hard checks: recorder / packager exit 0; no frame has a meaningful number of
saturated red pixels; Notepad is visible in a reasonable share of the frames
(so the test is not passed by an all-black video); capture_mode is app.
Soft: privacy_mask_start / privacy_mask_end around the minimize, >= 2 clips,
validation technical_ok. Leaking frames (masked app-only frames: Notepad plus
whatever leaked) are saved as PNGs for review.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_app_only import caption_point  # noqa: E402
from e2e_lib import (CREATE_NO_WINDOW, Report, bright_fraction, ffmpeg_path, frame_rgb, iter_frames_rgb,  # noqa: E402
                     load_json, package, process_ids, read_jsonl, red_pixel_count, region_stats, scenario_main,
                     screen_to_video, spawn_helper, start_recorder, validate, video_file, wait_exit,
                     wait_recording, win)

TARGET = "notepad.exe"
CANARY_TITLE = "DStudio E2E red canary 51c2"
FPS = 30
SCALE_DIV = 2          # decode at half size, nearest neighbour (saturated red is not blended away)
RED_LIMIT = 40         # red pixels in a half-size frame (~160 at full size) = a leak
VISIBLE_LEVEL = 24     # brightest channel above this = not masked black (dark-mode Notepad is ~32)


def launch_notepad_on_empty_file(rep: Report, procs, folder: str) -> dict:
    """Notepad showing an EMPTY file: no text means no red spell-check squiggles, and
    Windows 11 Notepad's restored session tabs (earlier scenarios) stay in the background."""
    w32 = win()
    path = os.path.join(folder, "mask-canvas.txt")
    with open(path, "w", encoding="utf-8"):
        pass
    stem = os.path.splitext(os.path.basename(path))[0]
    before_pids = set(process_ids(TARGET))
    before_windows = {w["hwnd"] for w in w32.top_windows()}
    procs.add(subprocess.Popen(["notepad.exe", path]))
    found = w32.find_window(lambda w: w32.exe_of_pid(w["pid"]).lower() == TARGET
                            and (w["hwnd"] not in before_windows or stem in w["title"]), timeout=25)
    if found is None:
        raise RuntimeError("Notepad window did not appear")
    if found["pid"] in before_pids:
        rep.note("notepad_shared_process", found["pid"])   # Windows 11: one process for all windows
    else:
        procs.add_pid(found["pid"])
    rep.note("notepad", {"pid": found["pid"], "class": found["class"], "title": found["title"]})
    return found


def real_screen_red_frac(box) -> float:
    """Sanity of the setup: share of red pixels in ``box`` on the real screen (no PNG kept)."""
    w32 = win()
    vx, vy, vw, vh = w32.virtual_screen()
    proc = subprocess.run([ffmpeg_path(), "-v", "error", "-f", "gdigrab", "-framerate", "5", "-i", "desktop",
                           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0 or len(proc.stdout) < vw * vh * 3:
        return -1.0
    return region_stats(proc.stdout, vw, vh, screen_to_video(box, (vx, vy)))["red_frac"]


def body(rep: Report, args, procs) -> None:
    w32 = win()
    pw, ph = w32.primary_size()
    frames_dir = os.path.join(rep.out_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    # ---------------------------------------------------------------- setup
    canary = spawn_helper(procs, rep.out_dir, "canary", color="#FF0000", x=8, y=8, w=pw - 16, h=ph - 96,
                          title=CANARY_TITLE, label="MASK LEAK CANARY")
    np_win = launch_notepad_on_empty_file(rep, procs, rep.out_dir)
    hwnd = np_win["hwnd"]
    w32.place(hwnd, pw // 6, ph // 6, (pw * 2) // 3, (ph * 2) // 3)
    time.sleep(0.6)
    rep.check("Notepad in front of the red canary", w32.force_foreground(hwnd, click_at=caption_point(hwnd)))
    nx0, ny0, nx1, ny1 = w32.window_rect(hwnd)
    cx0, cy0, cx1, cy1 = canary.rect
    strip = (cx0 + 20, max(cy0 + 40, ny1 + 10), cx1 - 20, cy1 - 10)   # canary area below Notepad
    if strip[3] - strip[1] < 20:
        strip = (cx0 + 10, cy0 + 40, max(cx0 + 30, nx0 - 10), cy1 - 10)  # ...or left of it
    try:
        rep.check("red canary visible around Notepad on the real screen", real_screen_red_frac(strip) > 0.5,
                  strip, soft=True)
    except Exception as exc:
        rep.check("real-screen sanity grab", False, repr(exc), soft=True)

    # ---------------------------------------------------------------- record
    out = os.path.join(rep.out_dir, "mask_transition")
    p, log = start_recorder(out, ["--source", f"app:{TARGET}", "--backend", "auto", "--fps", str(FPS),
                                  "--max-seconds", "60"], procs, "mask_transition")
    if not rep.check("recorder started", wait_recording(p, log, out), log):
        wait_exit(p, 10)
        return
    timeline = []

    def mark(label: str) -> None:
        timeline.append([label, round(time.time() * 1000.0, 1)])

    time.sleep(1.0)
    w32.force_foreground(hwnd, click_at=caption_point(hwnd))
    c = w32.client_rect(hwnd)
    w32.move_to((c[0] + c[2]) // 2, (c[1] + c[3]) // 2, duration=0.3)    # cursor over Notepad
    time.sleep(0.8)
    mark("maximize")
    w32.show(hwnd, w32.SW_MAXIMIZE)
    time.sleep(2.0)
    mark("restore")
    w32.show(hwnd, w32.SW_RESTORE)
    time.sleep(2.0)
    mark("maximize+restore (fast)")
    w32.show(hwnd, w32.SW_MAXIMIZE)
    time.sleep(0.35)
    w32.show(hwnd, w32.SW_RESTORE)
    time.sleep(1.5)
    mark("minimize")
    w32.show(hwnd, w32.SW_MINIMIZE)
    time.sleep(2.0)
    mark("restore from minimized")
    w32.show(hwnd, w32.SW_RESTORE)
    rep.check("Notepad back in front after the restore", w32.force_foreground(hwnd, click_at=caption_point(hwnd)),
              soft=True)
    time.sleep(2.5)
    mark("stop (F10)")
    w32.tap(w32.VK_F10)
    rc = wait_exit(p, 60)
    rep.note("timeline_ms", timeline)
    rep.check("recorder exit code 0", rc == 0, f"exit={rc}")

    # ---------------------------------------------------------------- events
    raw_path = os.path.join(out, "raw", "events.jsonl")
    raw_events = read_jsonl(raw_path) if os.path.isfile(raw_path) else []
    starts = [e for e in raw_events if e.get("type") == "privacy_mask_start"]
    ends = [e for e in raw_events if e.get("type") == "privacy_mask_end"]
    rep.check("privacy_mask_start / end around the minimize", bool(starts and ends), f"{len(starts)}/{len(ends)}",
              soft=True)
    if os.path.isfile(raw_path):
        with open(raw_path, encoding="utf-8") as fh:
            rep.check("the canary's window title is never logged", CANARY_TITLE not in fh.read())

    pk = package(out, rep.out_dir, "mask_transition")
    if not rep.check("packager exit code 0", pk.returncode == 0, f"exit={pk.returncode}"):
        return
    meta = load_json(os.path.join(out, "metadata.json"))
    clips = meta.get("clips") or []
    rep.note("clips", [{k: c.get(k) for k in ("frame_start", "frame_count", "gap_before_ms")} for c in clips])
    rep.check(">= 2 clips (time cut while minimized)", len(clips) >= 2, len(clips), soft=True)
    cap_path = os.path.join(out, "raw", "capture.json")
    cap = load_json(cap_path) if os.path.isfile(cap_path) else {}
    rep.note("capture", {k: cap.get(k) for k in ("capture_mode", "capture_rect", "backend", "mask_stats")})
    rep.check("capture.json capture_mode == app", cap.get("capture_mode") == "app", cap.get("capture_mode"))

    # ---------------------------------------------------------------- every frame
    video = video_file(out)
    total = int(meta.get("total_frames") or 0)
    n = visible = 0
    leaks = []
    t_dec = time.time()
    for idx, w, h, rgb in iter_frames_rgb(video, SCALE_DIV):
        n += 1
        red = red_pixel_count(rgb, w, h)
        if red >= RED_LIMIT:
            leaks.append((red, idx))
        if bright_fraction(rgb, w, h, VISIBLE_LEVEL) >= 0.05:
            visible += 1
    rep.note("decode", {"frames": n, "metadata_total_frames": total, "seconds": round(time.time() - t_dec, 1),
                        "visible_frames": visible, "scale_div": SCALE_DIV})
    rep.check("decoded every frame of clip.mp4", n > 0 and (not total or n == total), f"{n} vs {total}")
    worst = sorted(leaks, reverse=True)
    for red, idx in worst[:3]:
        try:
            frame_rgb(video, idx / float(FPS), os.path.join(frames_dir, f"mask_transition-leak-{idx:05d}.png"))
        except Exception:
            pass
    rep.check("no frame shows the red window behind Notepad (masking holds through maximize/restore/minimize)",
              not leaks, {"leaking_frames": len(leaks),
                          "worst": [{"frame": i, "t": round(i / FPS, 2), "red_px_half_size": r} for r, i in worst[:10]]})
    rep.check("Notepad visible in >= 25 % of the frames (not an all-black video)", n > 0 and visible >= 0.25 * n,
              f"{visible}/{n}")
    val = validate(out, rep.out_dir, "mask_transition")
    rep.check("validation technical_ok", bool(val.get("technical_ok")), val.get("technical_errors"), soft=True)


if __name__ == "__main__":
    raise SystemExit(scenario_main("mask_transition", body))
