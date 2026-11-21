"""Print (and save) what the CI desktop looks like before the E2E tests run.

    python tests_windows/env_report.py --out artifacts/env

Reports: OS build, session id, desktop resolution / virtual screen / DPI /
monitors, whether the cursor is visible, whether we can reach the input
desktop, DXGI desktop duplication (bettercam grab in a subprocess with a
timeout), an ffmpeg gdigrab screenshot, and the FFmpeg encoders. Never fails
the job by itself (exit 0) - it is diagnostics for the steps that follow.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

DXGI_PROBE = r"""
import json, sys, time
res = {}
try:
    import bettercam
    res["outputs"] = str(bettercam.output_info()).strip()[:500]
    cam = bettercam.create(output_color="BGRA")  # as the recorder uses it (no OpenCV)
    frame = None
    deadline = time.time() + 3
    while frame is None and time.time() < deadline:
        frame = cam.grab()
        time.sleep(0.03)
    res["created"] = True
    res["frame_shape"] = None if frame is None else list(frame.shape)
    t0, n = time.time(), 0
    while time.time() - t0 < 1.0:
        if cam.grab() is not None:
            n += 1
        time.sleep(0.002)
    res["new_frames_per_s_static_desktop"] = n
    cam.release()
except Exception as exc:
    res["error"] = repr(exc)
print("DXGI_JSON=" + json.dumps(res))
"""


def win_info() -> dict:
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    info = {}
    try:
        info["dpi_awareness_set"] = bool(u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except Exception:
        info["dpi_awareness_set"] = False
    gm = u.GetSystemMetrics
    info["primary"] = [gm(0), gm(1)]
    info["virtual_screen"] = [gm(76), gm(77), gm(78), gm(79)]
    info["monitors"] = gm(80)
    try:
        info["system_dpi"] = int(u.GetDpiForSystem())
    except Exception:
        info["system_dpi"] = None
    sid = wintypes.DWORD(0)
    k.ProcessIdToSessionId(k.GetCurrentProcessId(), ctypes.byref(sid))
    info["session_id"] = int(sid.value)
    u.OpenInputDesktop.restype = wintypes.HANDLE
    h = u.OpenInputDesktop(0, False, 0x0100)   # DESKTOP_SWITCHDESKTOP
    info["input_desktop_accessible"] = bool(h)
    if h:
        u.CloseDesktop(wintypes.HANDLE(h))

    class CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hCursor", wintypes.HANDLE),
                    ("ptScreenPos", wintypes.POINT)]

    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    u.GetCursorInfo(ctypes.byref(ci))
    info["cursor"] = {"flags": int(ci.flags), "showing": bool(ci.flags & 1), "pos": [ci.ptScreenPos.x, ci.ptScreenPos.y]}
    info["mouse_present"] = bool(gm(19))   # SM_MOUSEPRESENT
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "artifacts", "env"))
    args = ap.parse_args(argv)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    report = {"python": sys.version, "executable": sys.executable, "platform": platform.platform()}
    if sys.platform != "win32":
        report["note"] = "not Windows"
    else:
        try:
            report["windows"] = win_info()
        except Exception as exc:
            report["windows_error"] = repr(exc)
        try:
            proc = subprocess.run([sys.executable, "-c", DXGI_PROBE], capture_output=True, timeout=45)
            line = next((ln for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln.startswith("DXGI_JSON=")), "")
            report["dxgi"] = json.loads(line.split("=", 1)[1]) if line else {
                "error": "no result", "stderr": proc.stderr.decode("utf-8", "replace")[-800:]}
        except subprocess.TimeoutExpired:
            report["dxgi"] = {"error": "timeout"}
        dx = report.get("dxgi") or {}
        report["dxgi_duplication_works"] = bool(dx.get("created") and dx.get("frame_shape"))

    try:
        from recorder import paths

        ff = paths.ffmpeg_path()
        ff = ff if os.path.isfile(ff) else (shutil.which(ff) or ff)
        report["ffmpeg"] = ff
        ver = subprocess.run([ff, "-hide_banner", "-version"], capture_output=True, timeout=30)
        report["ffmpeg_version"] = ver.stdout.decode("utf-8", "replace").splitlines()[:1]
        enc = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, timeout=30).stdout.decode("utf-8", "replace")
        report["ffmpeg_h264_encoders"] = [n for n in ("libx264", "h264_nvenc", "h264_amf", "h264_qsv", "h264_mf")
                                          if f" {n} " in enc]
        if sys.platform == "win32":
            png = os.path.join(out, "desktop-gdigrab.png")
            g = subprocess.run([ff, "-v", "error", "-f", "gdigrab", "-framerate", "5", "-i", "desktop",
                                "-frames:v", "1", "-y", png], capture_output=True, timeout=60)
            report["gdigrab_ok"] = g.returncode == 0 and os.path.isfile(png)
            if g.returncode:
                report["gdigrab_error"] = g.stderr.decode("utf-8", "replace")[-500:]
    except Exception as exc:
        report["ffmpeg_error"] = repr(exc)

    with open(os.path.join(out, "env.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    w = report.get("windows") or {}
    print("=" * 60)
    print(f"Desktop resolution : {w.get('primary')}  virtual {w.get('virtual_screen')}  monitors {w.get('monitors')}"
          f"  DPI {w.get('system_dpi')}")
    print(f"Session / input    : session {w.get('session_id')}  input desktop accessible={w.get('input_desktop_accessible')}"
          f"  mouse present={w.get('mouse_present')}  cursor={w.get('cursor')}")
    print(f"DXGI duplication   : works={report.get('dxgi_duplication_works')}  {json.dumps(report.get('dxgi'))[:400]}")
    print(f"gdigrab screenshot : {report.get('gdigrab_ok')}")
    print(f"FFmpeg             : {report.get('ffmpeg')}  {report.get('ffmpeg_version')}  "
          f"encoders {report.get('ffmpeg_h264_encoders')}")
    print("=" * 60)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        try:
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
                fh.write(f"### CI desktop\n\n- resolution: {w.get('primary')} (virtual {w.get('virtual_screen')}, "
                         f"DPI {w.get('system_dpi')})\n- DXGI duplication works: {report.get('dxgi_duplication_works')}\n"
                         f"- gdigrab: {report.get('gdigrab_ok')}\n- FFmpeg encoders: {report.get('ffmpeg_h264_encoders')}\n\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
