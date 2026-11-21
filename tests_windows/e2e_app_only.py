"""E2E 3: APP-ONLY mode (--source app:notepad.exe) with a fake notification.

Setup: Notepad with known text, plus a bright red, borderless, always-on-top
"notification" window from a *separate* Python process placed over Notepad's
text area. Then:

  phase A  Notepad in front   - type / click inside Notepad          (recorded)
  phase B  the popup in front - type secret keys "qxz789"             (auto-paused)
  phase C  Notepad in front   - type more, then F10 stops the recorder (recorded)

Checks:
* in decoded frames the popup rectangle is black (no red at all), Notepad's
  text area is not black, and the desktop/taskbar corner is black
* raw events contain privacy_mask_start (trigger non_target_app) and a later
  privacy_mask_end; >= 2 clips
* no key event for q/x/z/7/8/9 anywhere (raw events.jsonl and events.json)
* the popup's window title is never logged
* (soft) capture.json: capture_mode "app", app_target notepad.exe, mask_stats
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_lib import (CREATE_NO_WINDOW, Report, capture_geometry, copy_small_dataset, ffmpeg_path,  # noqa: E402
                     ffprobe, frame_rgb, keys_with_codes, load_json, package, process_ids, read_jsonl,
                     region_stats, scenario_main, screen_to_video, spawn_helper, start_recorder, validate,
                     video_file, wait_exit, wait_recording, win)

TARGET = "notepad.exe"
SECRET = "qxz789"                       # typed only while the popup is in front
SECRET_VKS = [ord(c.upper()) for c in SECRET]
POPUP_TITLE = "DStudio E2E fake notification 7f3a"
FPS = 30


def launch_notepad(rep: Report, procs) -> dict:
    w32 = win()
    before_pids = set(process_ids(TARGET))
    before_windows = {w["hwnd"] for w in w32.top_windows()}
    procs.add(subprocess.Popen(["notepad.exe"]))
    found = w32.find_window(lambda w: w["hwnd"] not in before_windows
                            and w32.exe_of_pid(w["pid"]).lower() == TARGET, timeout=25)
    if found is None:
        raise RuntimeError("Notepad window did not appear")
    if found["pid"] in before_pids:
        # Windows 11 Notepad reuses one process for all windows: never kill it
        rep.note("notepad_shared_process", found["pid"])
    else:
        procs.add_pid(found["pid"])
    rep.note("notepad", {"pid": found["pid"], "class": found["class"], "title": found["title"]})
    return found


def caption_point(hwnd: int):
    x0, y0, x1, _ = win().window_rect(hwnd)
    return max(x0 + 40, x1 - 180), y0 + 10


def gdigrab_rgb(png: str):
    """The real screen (no masking), for a sanity check of the test setup."""
    w32 = win()
    vx, vy, vw, vh = w32.virtual_screen()
    base = [ffmpeg_path(), "-v", "error", "-f", "gdigrab", "-framerate", "5", "-i", "desktop", "-frames:v", "1"]
    subprocess.run(base + ["-y", png], capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
    proc = subprocess.run(base + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=60,
                          creationflags=CREATE_NO_WINDOW)
    return vw, vh, proc.stdout, (vx, vy)


def body(rep: Report, args, procs) -> None:
    w32 = win()
    pw, ph = w32.primary_size()
    frames_dir = os.path.join(rep.out_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    # ---------------------------------------------------------------- setup
    np = launch_notepad(rep, procs)
    hwnd = np["hwnd"]
    nw, nh = min(900, pw - 160), min(560, ph - 200)
    w32.place(hwnd, 60, 60, nw, nh)
    time.sleep(0.6)
    rep.check("Notepad focused", w32.force_foreground(hwnd, click_at=caption_point(hwnd)))
    client = w32.client_rect(hwnd)
    cx0, cy0, cx1, cy1 = client
    text_click = (cx0 + 40, cy0 + 60)
    w32.move_to(*text_click, duration=0.2)
    w32.click()
    for line in ("D Studio app-only test", "markov dstudio notepad text"):
        w32.type_text(line)
        w32.tap(w32.VK_RETURN)

    pop_w, pop_h = min(320, (cx1 - cx0) // 3), 150
    pop_x, pop_y = cx0 + (cx1 - cx0) // 2 + 20, cy0 + (cy1 - cy0) // 3
    popup = spawn_helper(procs, rep.out_dir, "popup", color="#FF0000", x=pop_x, y=pop_y, w=pop_w, h=pop_h,
                         title=POPUP_TITLE, topmost=True, popup=True, label="FAKE NOTIFICATION")
    pop_rect = popup.rect
    rep.note("rects", {"notepad": list(w32.window_rect(hwnd)), "client": list(client), "popup": list(pop_rect)})
    popup_exe = w32.exe_of_pid(popup.pid).lower()
    rep.note("popup_exe", popup_exe)
    rep.check("Notepad focused again before recording", w32.force_foreground(hwnd, click_at=caption_point(hwnd)))

    try:   # sanity: the popup really is red on the real screen
        sw, sh, srgb, sorigin = gdigrab_rgb(os.path.join(frames_dir, "app_only-real-screen.png"))
        st = region_stats(srgb, sw, sh, screen_to_video(pop_rect, sorigin), dark_max=16) if srgb else {}
        rep.check("popup visible (red) on the real screen", st.get("red_frac", 0) > 0.5, st, soft=True)
    except Exception as exc:
        rep.check("real-screen sanity grab", False, repr(exc), soft=True)

    # ---------------------------------------------------------------- record
    out = os.path.join(rep.out_dir, "app_only")
    p, log = start_recorder(out, ["--source", f"app:{TARGET}", "--backend", "auto", "--fps", str(FPS),
                                  "--max-seconds", "90"], procs, "app_only")
    if not rep.check("recorder started", wait_recording(p, log, out), log):
        wait_exit(p, 10)
        return

    # phase A: Notepad in front
    time.sleep(1.0)
    w32.force_foreground(hwnd, click_at=caption_point(hwnd))
    w32.move_to(cx0 + 80, cy0 + 120, duration=0.3)
    w32.click()
    vks_a = w32.type_text("markov")
    w32.tap(w32.VK_RETURN)
    time.sleep(1.5)

    # phase B: the popup (another process) in front -> auto-pause
    focused = w32.force_foreground(popup.hwnd, click_at=popup.center())
    rep.check("popup took the foreground", focused, f"fg={w32.foreground()} popup={popup.hwnd}")
    t_b0 = time.time() * 1000.0
    time.sleep(1.6)                              # > 300 ms debounce + segment stop
    w32.move_to(*popup.center(), duration=0.2)
    secret_typed = w32.type_text(SECRET)
    w32.move_to(popup.center()[0] + 30, popup.center()[1] + 10, duration=0.3)
    time.sleep(1.0)
    t_b1 = time.time() * 1000.0

    # phase C: back to Notepad -> auto-resume
    rep.check("Notepad back in front", w32.force_foreground(hwnd, click_at=caption_point(hwnd)))
    time.sleep(2.2)                              # resume + new segment
    w32.move_to(cx0 + 80, cy0 + 150, duration=0.2)
    w32.click()
    vks_c = w32.type_text("ok")
    time.sleep(1.5)
    w32.tap(w32.VK_F10)                          # stop
    rep.note("timeline_ms", {"phase_b_start": t_b0, "phase_b_end": t_b1})
    rc = wait_exit(p, 60)
    rep.check("recorder exit code 0", rc == 0, f"exit={rc}")

    # ---------------------------------------------------------------- events
    raw_events = read_jsonl(os.path.join(out, "raw", "events.jsonl"))
    starts = [e for e in raw_events if e.get("type") == "privacy_mask_start"]
    ends = [e for e in raw_events if e.get("type") == "privacy_mask_end"]
    rep.note("privacy_events_raw", starts[:3] + ends[:3])
    rep.check("raw: privacy_mask_start with trigger non_target_app",
              any(e.get("trigger") == "non_target_app" or e.get("maskTrigger") == "non_target_app" for e in starts),
              len(starts))
    rep.check("raw: privacy_mask_start names the popup's exe",
              any(str(e.get("maskDetail", "")).lower() == popup_exe for e in starts), popup_exe, soft=True)
    rep.check("raw: privacy_mask_end after privacy_mask_start",
              bool(starts and ends and max(float(e.get("timestamp", 0)) for e in ends)
                   > min(float(e.get("timestamp", 0)) for e in starts)), len(ends))
    rep.check("raw: no key event for keys typed while another app was in front",
              not keys_with_codes(raw_events, SECRET_VKS), keys_with_codes(raw_events, SECRET_VKS)[:5])
    rep.check("raw: keys typed in Notepad were logged",
              len(keys_with_codes(raw_events, set(vks_a + vks_c))) >= 4)
    raw_text = open(os.path.join(out, "raw", "events.jsonl"), encoding="utf-8").read()
    rep.check("raw: the other app's window title is never logged", POPUP_TITLE not in raw_text)
    rep.note("secret_vks", secret_typed)

    pk = package(out, rep.out_dir, "app_only")
    if not rep.check("packager exit code 0", pk.returncode == 0, f"exit={pk.returncode}"):
        return
    meta = load_json(os.path.join(out, "metadata.json"))
    clips = meta.get("clips") or []
    rep.note("clips", [{k: c.get(k) for k in ("frame_start", "frame_count", "gap_before_ms")} for c in clips])
    rep.check(">= 2 clips (time cut while the other app was in front)", len(clips) >= 2, len(clips))
    events = read_jsonl(os.path.join(out, "events.json"))
    rep.check("events.json: no secret keys", not keys_with_codes(events, SECRET_VKS))
    types = {e.get("type") for e in events}
    rep.check("events.json keeps privacy_mask_start/end", {"privacy_mask_start", "privacy_mask_end"} <= types,
              sorted(t for t in types if str(t).startswith("privacy")), soft=True)
    packaged_text = open(os.path.join(out, "events.json"), encoding="utf-8").read()
    rep.check("events.json: the other app's window title is never logged", POPUP_TITLE not in packaged_text)
    val = validate(out, rep.out_dir, "app_only")
    rep.check("validation technical_ok", bool(val.get("technical_ok")), val.get("technical_errors"))

    cap_path = os.path.join(out, "raw", "capture.json")
    cap = load_json(cap_path) if os.path.isfile(cap_path) else {}
    rep.note("capture", {k: cap.get(k) for k in ("capture_mode", "capture_rect", "cursor_in_video", "app_target",
                                                 "mask_stats", "backend")})
    rep.check("capture.json capture_mode == app", cap.get("capture_mode") == "app", cap.get("capture_mode"), soft=True)
    rep.check("capture.json app_target.exe is notepad.exe",
              str((cap.get("app_target") or {}).get("exe", "")).lower() == TARGET, cap.get("app_target"), soft=True)
    rep.check("capture.json mask_stats.auto_pauses >= 1",
              int((cap.get("mask_stats") or {}).get("auto_pauses") or 0) >= 1, cap.get("mask_stats"), soft=True)

    # ---------------------------------------------------------------- pixels
    video = video_file(out)
    probe = ffprobe(video)
    rep.note("ffprobe", probe)
    rep.check("app mode video keeps 30 fps", abs(probe["avg_fps"] - FPS) < 0.01, probe["avg_frame_rate"])
    origin, scale, _ = capture_geometry(out)
    rep.note("video_geometry", {"origin": origin, "scale": scale})
    pop_v = screen_to_video(pop_rect, origin, scale)
    pop_box = (pop_v[0] + 8, pop_v[1] + 8, pop_v[2] - 8, pop_v[3] - 8)
    np_text = screen_to_video((cx0 + 10, cy0 + 10, min(pop_rect[0] - 10, cx0 + 300), min(cy1 - 10, cy0 + 200)),
                              origin, scale)
    corner = (pw - 150, ph - 50, pw - 20, ph - 8)          # taskbar / desktop corner (screen px)
    nx0, ny0, nx1, ny1 = w32.window_rect(hwnd)
    corner_free = corner[0] >= nx1 or corner[1] >= ny1
    corner_v = screen_to_video(corner, origin, scale)
    picks = []
    if clips:
        for label, c in (("first", clips[0]), ("last", clips[-1])):
            idx = int(c.get("frame_start", 0)) + max(0, int(c.get("frame_count", 1)) // 2)
            picks.append((label, idx / FPS))
    for label, t in picks:
        w, h, rgb = frame_rgb(video, t, os.path.join(frames_dir, f"app_only-{label}-clip.png"))
        st_pop = region_stats(rgb, w, h, pop_box)
        st_np = region_stats(rgb, w, h, np_text)
        rep.note(f"pixels_{label}_clip", {"t": round(t, 2), "popup": st_pop, "notepad_text": st_np})
        rep.check(f"{label} clip: popup area is black", st_pop["n"] > 0 and st_pop["dark_frac"] >= 0.97, st_pop)
        rep.check(f"{label} clip: no red from the popup", st_pop["red_frac"] < 0.001, st_pop["red_frac"])
        rep.check(f"{label} clip: Notepad text area is not black", st_np["n"] > 0 and st_np["dark_frac"] <= 0.5, st_np)
        if corner_free:
            st_c = region_stats(rgb, w, h, corner_v)
            rep.check(f"{label} clip: desktop/taskbar corner is black", st_c["n"] > 0 and st_c["dark_frac"] >= 0.97, st_c)
    rep.note("dataset", copy_small_dataset(out, rep.out_dir))


if __name__ == "__main__":
    raise SystemExit(scenario_main("app_only", body))
