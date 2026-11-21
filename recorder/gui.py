"""Floating recording control bar (always-on-top, excluded from capture).

    python -m recorder gui                       # or double-click overlay.bat
    python -m recorder gui --lang en             # one-off language override
    python -m recorder gui --screenshot OUT.png --lang zh --expand \\
                           --state recording-demo    # CI: render, save PNG, exit 0
    python -m recorder gui --open-data           # open the recordings folder, exit
    python -m recorder gui --legacy-delivery     # this run: legacy CAD export layout

One bar: start / pause / stop+package, live timer + frame/event/fps counters,
a 中/EN switch and a settings panel (fps, quality, Full screen = main display /
App only + allowed overlays, cursor, backend, autopack, data folder, delivery
format).  After a recording the bar offers "Open annotator", "Export
recording", the CAD delivery form + CAD export (Markov CAD by default) and
"Open folder".  Every close request (✕, Alt+F4 / WM_CLOSE, sign-out) stops
and finalizes a running recording first.  At start-up the bar looks for
recordings that never finished (crash, power cut) and offers to package them
(crash recovery, K11).

The bar (and its tooltips / dialogs) use
``SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`` so screen capture does not
see them (Windows 10 2004+); if that fails the bar says so.

Import-safe on macOS/Linux: every Windows API is loaded lazily, the recorder
itself is imported only when a recording starts, and the pure state logic
(button states, labels, recorder argv) lives in plain functions that the unit
tests exercise without a display.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import zlib
from collections import deque
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # Tk is part of every python.org / bundled build; guard anyway so the
    import tkinter as tk  # pure functions below stay importable without it.
    from tkinter import filedialog, messagebox
    from tkinter import font as tkfont
except Exception:  # pragma: no cover - Python built without Tk
    tk = None  # type: ignore[assignment]
    filedialog = messagebox = tkfont = None  # type: ignore[assignment]

from . import i18n
from . import paths
from . import reclock
from .i18n import L
from .settings import load_settings, sanitize_value, update_settings, settings_path

ROOT = paths.ROOT
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ------------------------------------------------------------------- palette
BG = "#0d1117"
PANEL = "#151b23"
FIELD = "#0f151d"
LINE = "#263041"
HOVER = "#1f2836"
TEXT = "#e6edf3"
MUTED = "#8b98a9"
DIM = "#4f5b6b"
ACCENT = "#4da3ff"
ACCENT_HOVER = "#74b8ff"
ACCENT_INK = "#04121f"
SELECTED = "#1d3858"
SELECTED_HOVER = "#24456b"
CHIP = "#13233a"
OK = "#35d19a"
RED = "#ff5d5d"
WARN = "#ffc857"
STOP_FG = "#ffd9d9"
DOT_IDLE = "#4a5568"
DOT_REC_DIM = "#7a2b2b"
TIP_BG = "#1c2430"

# style -> (background, foreground, hover background)
STYLES: Dict[str, Tuple[str, str, str]] = {
    "primary": (ACCENT, ACCENT_INK, ACCENT_HOVER),
    "secondary": (PANEL, TEXT, HOVER),
    "danger": (PANEL, STOP_FG, "#2b1d24"),
    "ghost": (BG, MUTED, PANEL),
    "field": (FIELD, MUTED, HOVER),
    "selected": (SELECTED, TEXT, SELECTED_HOVER),
    "link": (PANEL, ACCENT, HOVER),
    "plain": (PANEL, TEXT, PANEL),
}

UI_FAMILIES = {
    "zh": ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
           "Noto Sans CJK SC", "Source Han Sans SC", "Segoe UI"),
    "en": ("Segoe UI", "Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"),
}
MONO_FAMILIES = ("Cascadia Mono", "Consolas", "Menlo", "SF Mono", "DejaVu Sans Mono", "Courier New")

BACKENDS = ("auto", "dxcam", "gdi", "gdigrab")
ACTIVE_STATES = ("recording", "paused")
BUSY_STATES = ("stopping", "packing", "exporting")
DEMO_STATES = ("idle", "recording-demo", "autopaused-demo", "paused-demo", "done-demo")

WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11
GA_ROOT = 2
ERROR_ALREADY_EXISTS = 183

_single_instance_handle = None


# =========================================================== Windows helpers
_user32_lib = None


def _user32():
    """user32 with the prototypes we need, or None off Windows (lazy)."""
    global _user32_lib
    if sys.platform != "win32":
        return None
    if _user32_lib is None:
        import ctypes
        from ctypes import wintypes

        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.SetWindowDisplayAffinity.restype = wintypes.BOOL
        u.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowDisplayAffinity.restype = wintypes.BOOL
        u.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        u.MonitorFromPoint.restype = wintypes.HMONITOR
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        _user32_lib = u
    return _user32_lib


def enable_dpi_awareness() -> None:
    """Make the process per-monitor DPI aware *before* Tk creates a window.

    Otherwise Windows bitmap-stretches the bar on 125-200 % displays and the
    recorder (which turns awareness on itself) would see virtualised
    coordinates for windows created earlier.  No-op off Windows.
    """
    if sys.platform != "win32":
        return
    import ctypes

    try:  # Windows 10 1703+: DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:  # Windows 8.1+: PROCESS_PER_MONITOR_DPI_AWARE
        if ctypes.WinDLL("shcore").SetProcessDpiAwareness(2) == 0:
            return
    except Exception:
        pass
    try:
        ctypes.WinDLL("user32").SetProcessDPIAware()
    except Exception:
        pass


def toplevel_hwnd(widget) -> int:
    """HWND of the top-level window that contains a Tk widget (0 if unknown)."""
    try:
        wid = int(widget.winfo_id())
    except Exception:
        return 0
    u = _user32()
    hwnd = 0
    if u is not None:
        try:
            hwnd = int(u.GetAncestor(wid, GA_ROOT) or 0)
        except Exception:
            hwnd = 0
    if not hwnd:
        try:
            hwnd = int(widget.winfo_toplevel().wm_frame(), 16)
        except Exception:
            hwnd = wid
    return hwnd


def exclude_from_capture(widget) -> bool:
    """Hide a top-level Tk window from screen capture; True if it worked."""
    u = _user32()
    if u is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        widget.update_idletasks()
        hwnd = toplevel_hwnd(widget)
        if not hwnd:
            return False
        ok = u.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        aff = wintypes.DWORD(0)
        u.GetWindowDisplayAffinity(hwnd, ctypes.byref(aff))
        return bool(ok) and aff.value == WDA_EXCLUDEFROMCAPTURE
    except Exception:
        return False


def acquire_single_instance(name: str = "Local\\DStudio.RecorderBar") -> bool:
    """False if another recorder bar of this user is already running.

    Two bars would install two sets of global hooks and fight over F9/F10.
    Off Windows, or if the mutex cannot be created, we never block start-up.
    """
    global _single_instance_handle
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not handle:
            return True
        if err == ERROR_ALREADY_EXISTS:
            k.CloseHandle(handle)
            return False
        _single_instance_handle = handle  # keep it for the process lifetime
        return True
    except Exception:
        return True


def point_on_a_monitor(x: int, y: int) -> bool:
    """True if (x, y) lies on an attached monitor (saved bar positions)."""
    u = _user32()
    if u is None:
        return True
    try:
        from ctypes import wintypes

        return bool(u.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 0))  # MONITOR_DEFAULTTONULL
    except Exception:
        return True


def work_area_at(x: int, y: int, fallback: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    """(left, top, right, bottom) work area of the monitor nearest (x, y)."""
    u = _user32()
    if u is None:
        return fallback
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        hmon = u.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)  # NEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        get = ctypes.WinDLL("user32").GetMonitorInfoW
        get.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
        get.restype = wintypes.BOOL
        if hmon and get(hmon, ctypes.byref(info)):
            r = info.rcWork
            return (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    return fallback


def open_path(path: str) -> None:
    """Open a folder/file with the OS shell (Explorer on Windows)."""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 - Windows shell open
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        webbrowser.open("file:///" + os.path.abspath(path).replace("\\", "/"))


def open_data_folder() -> int:
    """``--open-data`` (Start-menu "Recordings" shortcut): open the recordings folder, exit."""
    try:
        folder = paths.data_dir()
    except OSError as exc:
        print(L("无法打开录制文件夹：{e}", "Cannot open the recordings folder: {e}", e=exc), file=sys.stderr)
        return 1
    open_path(folder)
    return 0


# ============================================================ pure logic
def fmt_elapsed(ms: Optional[float]) -> str:
    """Timer text: ``MM:SS.d`` below an hour, ``H:MM:SS`` above."""
    try:
        s = max(0.0, float(ms or 0.0) / 1000.0)
    except (TypeError, ValueError):
        s = 0.0
    if s >= 3600:
        return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{int(s % 60):02d}"
    return f"{int(s // 60):02d}:{int(s % 60):02d}.{int((s % 1) * 10)}"


_fmt = fmt_elapsed  # backwards compatible name


def _num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def shorten(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def shorten_path(path: str, limit: int = 52) -> str:
    """Middle-ellipsis for long folder paths (keeps drive and last folder)."""
    path = str(path or "")
    if len(path) <= limit:
        return path
    head = max(8, limit // 3)
    tail = limit - head - 1
    return path[:head] + "…" + path[-tail:]


def msg_text(message: Any) -> str:
    """Render a report message that may be a str or a ``both()`` dict."""
    if isinstance(message, dict):
        lang = i18n.get_language()
        return str(message.get(lang) or message.get("en") or message.get("zh") or "")
    return str(message)


def _items(report: Optional[Dict[str, Any]], key: str) -> List[Any]:
    value = (report or {}).get(key) if isinstance(report, dict) else None
    if isinstance(value, (str, dict)):
        return [value]
    return [m for m in value if m] if isinstance(value, (list, tuple)) else []


# Notes packager/validate.py adds to *every* report.  They describe the check,
# not the recording, so they are never counted as warnings (otherwise every
# clean recording would show "1 note"):
#   * the quick (non-decoding) check's scope note - hidden;
#   * the sync disclaimer - shown uncounted as a reminder in the export dialog.
_CHECK_SCOPE_NOTE = ("frame count from the video header", "视频头部帧数")
_SYNC_DISCLAIMER = ("Sync field checks are not a physical sync-accuracy certification", "同步字段校验不等于物理同步精度认证")
_BOILERPLATE_NOTES = _CHECK_SCOPE_NOTE + _SYNC_DISCLAIMER


def _message_has(message: Any, markers: Tuple[str, ...]) -> bool:
    if isinstance(message, dict):
        text = " ".join(str(v) for v in message.values())
    else:
        text = str(message)
    return any(marker in text for marker in markers)


def report_warnings(report: Optional[Dict[str, Any]]) -> List[Any]:
    """User-facing ``validation.json`` warnings (60 fps, multi-monitor, no cursor, cuts...)."""
    return [m for m in _items(report, "warnings") if not _message_has(m, _BOILERPLATE_NOTES)]


def report_reminders(report: Optional[Dict[str, Any]]) -> List[Any]:
    """Standing reminders of every report (the sync disclaimer): shown, never counted."""
    return [m for m in _items(report, "warnings") if _message_has(m, _SYNC_DISCLAIMER)]


def uncut_privacy_masks(report: Optional[Dict[str, Any]]) -> int:
    """Privacy-marked intervals still in the video (``export_recording`` refuses these)."""
    try:
        return max(0, int((report or {}).get("uncut_privacy_masks") or 0)) if isinstance(report, dict) else 0
    except (TypeError, ValueError):
        return 0


# validate.py's delivery_missing item for uncut privacy masks (shown under "Cannot export:")
_UNCUT_PRIVACY_ITEM = ("privacy-marked interval", "隐私标记时段")


def report_details(report: Optional[Dict[str, Any]], limit: int = 12) -> str:
    """Bulleted export blockers / technical issues / missing delivery items / notes,
    in the current language."""
    missing = _items(report, "delivery_missing")
    blocked: List[Any] = []
    uncut = uncut_privacy_masks(report)
    if uncut:
        # Export recording refuses this as well, not only the CAD delivery (K4)
        blocked = [L("{n} 段隐私标记时段（如密码管理器在前台）仍在视频和日志中，“导出录制”和 CAD 导出都会被拒绝；"
                     "请人工剪除后重新打包，或重新录制",
                     "{n} privacy-marked interval(s) are still in the video and logs, so Export recording and "
                     "the CAD export are refused; cut them out and repackage, or record again", n=uncut)]
        missing = [m for m in missing if not _message_has(m, _UNCUT_PRIVACY_ITEM)]
    sections = ((L("不能导出：", "Cannot export:"), blocked),
                (L("技术问题：", "Technical issues:"), _items(report, "technical_errors")),
                (L("CAD 交付还缺：", "Still needed for the CAD delivery:"), missing),
                (L("提示（交付前请确认）：", "Notes (check before delivery):"), report_warnings(report)))
    lines: List[str] = []
    room = max(1, int(limit))
    for head, items in sections:
        if not items:
            continue
        if lines:
            lines.append("")
        lines.append(head)
        shown = items[:max(1, room)]
        lines.extend("• " + shorten(msg_text(m), 160) for m in shown)
        if len(items) > len(shown):
            lines.append(L("…另有 {n} 项", "… {n} more", n=len(items) - len(shown)))
        room -= len(shown)
    return "\n".join(lines)


def export_done_text(lead: str, path: str, report: Optional[Dict[str, Any]] = None) -> str:
    """Body of the export-complete dialog (lists the validation notes, if any)."""
    text = f"{lead}\n{path}"
    notes = report_warnings(report)
    if notes:
        text += "\n\n" + L("提示（交付前请确认）：", "Notes (check before delivery):") + "\n" + "\n".join(
            "• " + shorten(msg_text(m), 200) for m in notes[:8])
        if len(notes) > 8:
            text += "\n" + L("…另有 {n} 项", "… {n} more", n=len(notes) - 8)
    reminders = report_reminders(report)
    if reminders:
        text += "\n\n" + "\n".join(shorten(msg_text(m), 200) for m in reminders[:2])
    return text + "\n\n" + L("现在打开该文件夹吗？", "Open the folder now?")


def exe_stem(exe: str) -> str:
    base = os.path.basename(str(exe or "").replace("\\", "/"))
    return base[:-4] if base.lower().endswith(".exe") else base


def app_display_name(cfg: Dict[str, Any]) -> str:
    return str(cfg.get("app_name") or "").strip() or exe_stem(cfg.get("app_exe", ""))


def normalize_backend(mode: str, backend: str) -> str:
    """Backend valid for a capture mode (App-only needs frames in Python)."""
    backend = (backend or "auto").strip().lower()
    if backend not in BACKENDS:
        backend = "auto"
    if mode == "app" and backend == "gdigrab":
        return "auto"
    return backend


def mode_chip(cfg: Dict[str, Any], source_override: str = "") -> Tuple[str, str]:
    """(text, colour) of the capture-mode chip on the bar."""
    if cfg.get("capture_mode") == "app":
        name = app_display_name(cfg)
        if not name:
            return L("仅应用：未选择", "App only: not chosen"), WARN
        return L("仅应用：{name}", "App only: {name}", name=shorten(name, 24)), ACCENT
    if source_override and source_override.lower() != "desktop":
        return L("来源：{src}", "Source: {src}", src=shorten(source_override, 24)), ACCENT
    # --source desktop records the primary monitor only, with every backend
    return L("全屏 · 主显示器", "Full screen · main display"), ACCENT


def validate_config(cfg: Dict[str, Any]) -> str:
    """Bilingual problem description, or "" when a recording can start."""
    try:
        fps = int(cfg.get("fps"))
    except (TypeError, ValueError):
        fps = 0
    if not 1 <= fps <= 120:
        return L("帧率需在 1–120 之间", "Frame rate must be between 1 and 120")
    try:
        quality = int(cfg.get("quality"))
    except (TypeError, ValueError):
        quality = -1
    if not 0 <= quality <= 51:
        return L("画质 (CRF) 需在 0–51 之间", "Quality (CRF) must be between 0 and 51")
    if cfg.get("capture_mode") == "app" and not str(cfg.get("app_exe") or "").strip():
        return L("请先选择要录制的应用（⚙ → 选择应用…）",
                 "Choose the app to record first (⚙ → Choose app…)")
    return ""


def build_record_argv(cfg: Dict[str, Any], out: str, *, session_path: Optional[str] = None,
                      ffmpeg: Optional[str] = None, codec: str = "auto",
                      source_override: str = "", extra: Sequence[str] = ()) -> List[str]:
    """Arguments for ``recorder.parse_args`` (contract section 1)."""
    mode = "app" if cfg.get("capture_mode") == "app" else "desktop"
    if mode == "app":
        source = "app:" + str(cfg.get("app_exe") or "").strip()
    else:
        source = (source_override or "desktop").strip() or "desktop"
        if source.lower().startswith("app:"):
            source = "desktop"
    argv = [
        "start", "--out", out,
        "--fps", str(int(cfg.get("fps") or 30)),
        "--quality", str(int(cfg.get("quality") if cfg.get("quality") is not None else 23)),
        "--backend", normalize_backend(mode, str(cfg.get("backend") or "auto")),
        "--source", source,
        "--ffmpeg", ffmpeg or paths.ffmpeg_path(),
        "--codec", codec or "auto",
        "--draw-mouse-cursor" if cfg.get("draw_cursor", True) else "--no-draw-mouse-cursor",
    ]
    if mode == "app":
        allow = [str(a).strip() for a in (cfg.get("app_allow") or []) if str(a).strip()]
        if allow:
            argv += ["--app-allow", *allow]
        overlays = [str(a).strip() for a in (cfg.get("app_overlay_allow") or []) if str(a).strip()]
        if overlays:
            argv += ["--app-overlay-allow", *overlays]
        if not cfg.get("auto_pause_other_apps", True):
            argv.append("--no-auto-pause-other-apps")
    if session_path:
        argv += ["--session-info", session_path]
    argv += [str(a) for a in extra]
    return argv


MIN_FREE_START_BYTES = 3 * 1024 ** 3     # refuse to start below this (recorder warns < 3 GB, stops < 1 GB)


def disk_free_bytes(path: str) -> Optional[int]:
    """Free bytes on the drive holding ``path`` (None when it cannot be read)."""
    try:
        return int(shutil.disk_usage(path).free)
    except (OSError, ValueError):
        return None


def new_output_dir(base: str, now: Optional[dt.datetime] = None) -> str:
    """``<base>/rec-YYYYmmdd-HHMMSS`` that does not exist yet."""
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    return unique_path(os.path.join(base, "rec-" + stamp))


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    for i in range(2, 1000):
        candidate = f"{path}-{i}"
        if not os.path.exists(candidate):
            return candidate
    return f"{path}-{int(time.time() * 1000)}"


def is_inside(child: str, parent: str) -> bool:
    try:
        c = os.path.normcase(os.path.abspath(child))
        p = os.path.normcase(os.path.abspath(parent))
        return c == p or os.path.commonpath([c, p]) == p
    except ValueError:  # different drives
        return False


def viewer_url(port: int, dataset: str = "", lang: Optional[str] = None) -> str:
    """Annotator URL; ``?lang=`` (the bar's language) comes before the ``#dataset`` fragment."""
    from urllib.parse import quote

    lang = (lang or i18n.get_language() or "").strip().lower()
    if lang not in i18n.SUPPORTED:
        lang = "en"
    url = f"http://127.0.0.1:{int(port)}/viewer/?lang={lang}"
    return url + ("#" + quote(dataset) if dataset else "")


def narration_signature(folder: str) -> Optional[Tuple[int, int]]:
    """``(mtime_ns, size)`` of ``folder/narration.json``; None if it does not exist."""
    if not folder:
        return None
    try:
        st = os.stat(os.path.join(folder, "narration.json"))
    except (OSError, TypeError, ValueError):
        return None
    return int(st.st_mtime_ns), int(st.st_size)


def ffmpeg_available(ffmpeg: str) -> bool:
    return bool(ffmpeg) and (os.path.isfile(ffmpeg) or shutil.which(ffmpeg) is not None)


def display_state(state: str, snap: Optional[Dict[str, Any]] = None) -> str:
    """UI state key: idle|recording|paused|auto_paused|stopping|packing|exporting|done|error."""
    snap = snap or {}
    if state in ACTIVE_STATES:
        if snap.get("auto_paused"):
            return "auto_paused"
        if state == "paused" or snap.get("paused"):
            return "paused"
        return "recording"
    if state in BUSY_STATES or state in ("done", "error"):
        return state
    return "idle"


def is_privacy_pause(snap: Optional[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Is this auto-pause the password-manager blocklist (privacy pause)?

    The recorder says why in ``snapshot()["auto_pause_reason"]``: "policy" = a
    password manager is in front (any mode, K4), "app" = App-only mode with
    another app in front.  Older recorders do not: then an auto-pause outside
    App-only mode can only be the blocklist."""
    snap = snap or {}
    if not snap.get("auto_paused"):
        return False
    reason = snap.get("auto_pause_reason")
    if reason:
        return reason == "policy"
    mode = snap.get("capture_mode") or (cfg or {}).get("capture_mode") or "desktop"
    return mode != "app"


_OVERLAY_FLAG = "--app-overlay-allow"


def overlay_problem(message: Any) -> Optional[List[str]]:
    """Exe names when ``message`` is the recorder's "an unknown full-screen overlay
    blacks out the video" explanation (it ends with ``--app-overlay-allow EXE ...``);
    None for any other message."""
    text = msg_text(message) if message else ""
    at = text.rfind(_OVERLAY_FLAG)
    if at < 0:
        return None
    names: List[str] = []
    for token in text[at + len(_OVERLAY_FLAG):].split():
        token = token.strip("，,。.;；:：)）\"'")
        if "." not in token:
            break      # the recorder's names are normalized "x.exe"; e.g. the "放行" after them
        names.append(token)
    # no name: the recorder could not identify the program, so the ⚙ field cannot help
    return names or None


def problem_text(message: Any, limit: int = 48) -> str:
    """Short "⚠ ..." form of the recorder's ``last_error`` for the stats line."""
    names = overlay_problem(message)
    if names:
        # the recorder names its CLI flag; in the bar the fix is the ⚙ field
        return "⚠ " + L("覆盖层 {exe} 使（部分）画面变黑 · 可在 ⚙ 中放行（悬停查看）",
                        "Overlay {exe} blacks out (part of) the video · allow it in ⚙ (hover for details)",
                        exe=shorten(", ".join(names), 28))
    return "⚠ " + shorten(msg_text(message), limit)


def problem_details(message: Any) -> str:
    """Tooltip text for the recorder's ``last_error`` (full text + what to do in the bar)."""
    text = msg_text(message)
    names = overlay_problem(message)
    if names is not None:
        text += "\n\n" + L("在 D Studio 中：停止录制，在 ⚙ →“放行覆盖层”中填入 {exe}，然后重新录制。",
                           "In D Studio: stop, enter {exe} under ⚙ → Allowed overlays, then record again.",
                           exe=", ".join(names))
    return text


def state_text(key: str, *, app: str = "", packaged: bool = True, closing: bool = False,
               privacy: bool = False) -> Tuple[str, str]:
    """(label, colour) for the state next to the timer."""
    if key == "recording":
        return L("录制中", "Recording"), TEXT
    if key == "paused":
        return L("已暂停", "Paused"), WARN
    if key == "auto_paused" and privacy:
        return L("隐私暂停：密码管理器在前台", "Privacy pause: password manager in front"), WARN
    if key == "auto_paused":
        name = shorten(app, 18) or L("目标应用", "the app")
        return L("自动暂停：{app} 不在前台", "Auto-paused: {app} not in front", app=name), WARN
    if key == "stopping":
        return (L("正在收尾，完成后关闭…", "Finishing, then closing…") if closing
                else L("正在停止…", "Stopping…")), WARN
    if key == "packing":
        return (L("打包中，完成后关闭…", "Packaging, then closing…") if closing
                else L("打包中…", "Packaging…")), ACCENT
    if key == "exporting":
        return L("导出中…", "Exporting…"), ACCENT
    if key == "done":
        return (L("已打包", "Packaged") if packaged else L("已停止", "Stopped")), OK
    if key == "error":
        return L("出错", "Error"), RED
    return L("待机", "Ready"), MUTED


def button_states(key: str, *, autopack: bool = True, has_output: bool = False,
                  markov: bool = True) -> Dict[str, Any]:
    """Text / enabled / style of every bar button for a UI state key."""
    active = key in ("recording", "paused", "auto_paused")
    busy = key in BUSY_STATES
    if key == "recording":
        main = (L("⏸ 暂停", "⏸ Pause"), True, "secondary")
    elif key == "paused":
        main = (L("▶ 继续", "▶ Resume"), True, "primary")
    elif key == "auto_paused":
        # same as F9 in the recorder: turn the auto-pause into a manual pause
        main = (L("⏸ 保持暂停", "⏸ Hold pause"), True, "secondary")
    elif busy:
        main = ("⏳", False, "secondary")
    elif key == "done":
        main = (L("● 再录一条", "● New recording"), True, "primary")
    else:
        main = (L("● 开始录制", "● Start recording"), True, "primary")
    usable = has_output and key in ("done", "error")

    def b(text, enabled, style="secondary"):
        return {"text": text, "enabled": bool(enabled), "style": style}

    return {
        "main": b(*main),
        "stop": b(L("■ 停止并打包", "■ Stop & package") if autopack else L("■ 停止", "■ Stop"),
                  active, "danger"),
        "annotator": b(L("↗ 打开标注台", "↗ Open annotator"), not active and not busy, "link"),
        "export_rec": b(L("↓ 导出录制", "↓ Export recording"), usable),
        "session": b(L("CAD 交付信息", "CAD delivery info"), key in ("idle", "done", "error")),
        "export_cad": b(L("核验并导出 Markov CAD", "Verify & export Markov CAD") if markov
                        else L("核验并导出旧版交付包", "Verify & export legacy delivery"),
                        has_output and key == "done"),
        "folder": b(L("打开文件夹", "Open folder"), True),
        "settings_locked": active or busy,
        "show_results": has_output and not active and key != "idle",
    }


class LiveFps:
    """Measured frames/second over a short sliding window."""

    def __init__(self, window_s: float = 2.0):
        self.window_s = window_s
        self.samples: deque = deque()

    def reset(self) -> None:
        self.samples.clear()

    def add(self, t: float, frames: int) -> Optional[float]:
        if self.samples and frames < self.samples[-1][1]:
            self.samples.clear()
        self.samples.append((t, frames))
        while len(self.samples) > 2 and t - self.samples[0][0] > self.window_s:
            self.samples.popleft()
        return self.value()

    def value(self) -> Optional[float]:
        if len(self.samples) < 2:
            return None
        (t0, f0), (t1, f1) = self.samples[0], self.samples[-1]
        if t1 - t0 < 0.4:
            return None
        return max(0.0, (f1 - f0) / (t1 - t0))


def stats_text(key: str, snap: Dict[str, Any], *, cfg: Dict[str, Any], live_fps: Optional[float] = None,
               capture_excluded: bool = True, error: str = "", report: Optional[Dict] = None,
               packaged: bool = False, out_name: str = "", busy_s: float = 0.0) -> Tuple[str, str]:
    """(text, colour) of the second bar line."""
    hotkeys = L("F9 暂停 · F10 停止", "F9 pause · F10 stop")
    if key in ("recording", "paused", "auto_paused"):
        frames = L("{n} 帧", "{n} frames", n=_num(snap.get("frames", 0)))
        events = L("{n} 事件", "{n} events", n=_num(snap.get("events", 0)))
        # the recorder's explanation (e.g. a resume refused because the display
        # changed, or an overlay blacking out the video) replaces the hotkey hint
        # and comes first so it is never cut off; the stats tooltip shows it in full
        problem = problem_text(snap["last_error"]) if snap.get("last_error") else ""
        if key == "auto_paused":
            if is_privacy_pause(snap, cfg):
                hint = L("密码管理器离开前台后自动继续", "resumes when the password manager leaves the front")
            else:
                # the state label already says why; keep this line short
                hint = L("切回 {app} 即自动继续", "switch back to {app} to resume",
                         app=shorten(app_display_name(cfg) or snap.get("target_exe") or "", 18))
            if problem:  # nothing is being recorded: the counters can make room
                return " · ".join([hint, problem]), WARN
            return " · ".join([hint, frames, events, L("F10 停止", "F10 stop")]), WARN
        parts = [frames, events,
                 f"{live_fps:.1f} fps" if (key == "recording" and live_fps is not None) else "– fps"]
        backend = snap.get("backend") or cfg.get("backend") or "auto"
        enc = str(snap.get("encoder") or "").replace("h264_", "").replace("hevc_", "")
        parts.append(f"{backend}/{enc}" if enc else str(backend))
        if snap.get("masked_now"):
            parts.append(L("其他窗口已遮黑", "others masked"))
        if problem:
            return " · ".join([problem] + parts), WARN
        parts.append(hotkeys)
        if backend == "gdigrab" and capture_excluded:
            parts.append(L("gdigrab 可能录到悬浮条", "gdigrab may capture this bar"))
        return " · ".join(parts), MUTED
    if key == "stopping":
        return L("正在写入视频和事件文件…", "Writing video and event files…"), MUTED
    if key == "packing":
        return L("生成 events / frame_events / metadata … {s}s",
                 "Building events / frame_events / metadata … {s}s", s=int(busy_s)), MUTED
    if key == "exporting":
        return L("正在复制并核验文件… {s}s", "Copying and checking files… {s}s", s=int(busy_s)), MUTED
    if key == "error":
        return shorten(error or L("未知错误", "Unknown error"), 150), RED
    if key == "done":
        name = shorten(out_name, 28)
        if not packaged:
            return L("{name} · 未打包（导出或打开标注台时会自动打包）",
                     "{name} · not packaged yet (packaged when you export or annotate)", name=name), MUTED
        tech = _items(report, "technical_errors")
        missing = _items(report, "delivery_missing")
        n_notes = len(report_warnings(report))
        notes = L(" · ⚠ {n} 条提示", " · ⚠ {n} note(s)", n=n_notes) if n_notes else ""
        if tech:
            return L("{name} · 技术检查有 {n} 个问题（悬停查看）", "{name} · {n} technical issue(s) (hover for details)",
                     name=name, n=len(tech)) + notes, WARN
        uncut = uncut_privacy_masks(report)
        if uncut:
            # export_recording / the CAD export refuse this (contract K4)
            return L("{name} · {n} 段隐私时段仍在视频中，不能导出（悬停查看）",
                     "{name} · {n} privacy interval(s) still in the video, cannot export (hover for details)",
                     name=name, n=uncut) + notes, WARN
        if missing:
            return L("{name} · 可导出录制 · CAD 交付还缺 {n} 项", "{name} · recording exportable · CAD delivery needs {n} more item(s)",
                     name=name, n=len(missing)) + notes, MUTED
        return L("{name} · 可回看、标注并导出", "{name} · ready to review, annotate and export", name=name) + notes, OK
    # idle
    parts = [f"{cfg.get('fps', 30)} fps",
             L("光标 开", "cursor on") if cfg.get("draw_cursor", True) else L("光标 关", "cursor off"),
             str(normalize_backend(cfg.get("capture_mode", "desktop"), cfg.get("backend", "auto"))),
             hotkeys]
    if error:
        return shorten(error, 150), RED
    if not capture_excluded:
        return L("⚠ 悬浮条可能被录进画面（需 Windows 10 2004+）", "⚠ This bar may appear in the video (needs Windows 10 2004+)") \
            + " · " + " · ".join(parts[:2]), WARN
    if normalize_backend(cfg.get("capture_mode", "desktop"), cfg.get("backend", "auto")) == "gdigrab":
        parts.append(L("gdigrab 可能录到悬浮条", "gdigrab may capture this bar"))
    else:
        parts.append(L("悬浮条不会录进画面", "bar hidden from video"))
    return " · ".join(parts), MUTED


def default_session_template() -> str:
    legacy = os.path.join(ROOT, "cad-session.json")
    if os.path.isfile(legacy) or not paths.is_installed():
        return legacy
    return os.path.join(os.path.dirname(os.path.abspath(settings_path())), "cad-session.json")


def parse_recorder_args(argv: Sequence[str]):
    """``recorder.parse_args`` with argparse's error text turned into ValueError."""
    from .recorder import parse_args

    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            return parse_args(list(argv))
    except SystemExit as exc:
        lines = [ln for ln in buf.getvalue().strip().splitlines() if ln.strip()]
        raise ValueError(lines[-1] if lines else f"invalid recorder arguments ({exc})") from None


# =========================================================== crash recovery
def _read_json_dict(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def recording_unfinished(folder: str) -> bool:
    """A recording folder that was never packaged and is not being recorded now.

    Unfinished = ``raw/capture.json`` does not say "completed" (crash, power cut,
    killed recorder) or ``raw/segments.json`` exists, and there is no
    ``metadata.json`` yet.  A folder whose recording lock is held
    (:func:`recorder.reclock.is_live`) is never unfinished: someone records into it.
    """
    raw = os.path.join(folder, "raw")
    # packager.build writes validation.json last: metadata.json without it means
    # packaging itself was interrupted (e.g. Windows ended the session).
    if not os.path.isdir(raw) or (os.path.exists(os.path.join(folder, "metadata.json"))
                                  and os.path.exists(os.path.join(folder, "validation.json"))):
        return False
    segments_path = os.path.join(raw, "segments.json")
    if not os.path.isfile(segments_path):
        return False   # no sidecar: no video was ever saved, nothing the packager could build
    if not _segments_recoverable(segments_path):
        return False   # e.g. Start then Stop at once, or app-only with the app never in front
    return not reclock.is_live(raw)


def _segments_recoverable(path: str) -> bool:
    """Whether a segments.json lists anything the packager could turn into video:
    a segment with frames, or one still marked unfinished (in_progress /
    finalized false: the packager probes its file).  Unreadable -> True (let
    the packager report the problem rather than hide the folder)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        segs = data.get("segments") if isinstance(data, dict) else data
        for seg in segs or []:
            if not isinstance(seg, dict) or seg.get("broken"):
                continue
            count = seg.get("frame_count")
            frames = int(count) if isinstance(count, (int, float)) else len(seg.get("frames") or [])
            if frames > 0 or seg.get("in_progress") or seg.get("finalized") is False:
                return True
        return False
    except (OSError, ValueError, TypeError, AttributeError):
        return True


def unfinished_recordings(data_dir: str, dismissed: Iterable[str] = ()) -> List[str]:
    """``rec-*`` folders in ``data_dir`` to offer for recovery, oldest first.
    Folders named in ``dismissed`` (answered "No" before) are skipped.  Never raises."""
    skip = {str(n) for n in dismissed or ()}
    try:
        names = sorted(os.listdir(data_dir))
    except (OSError, TypeError, ValueError):
        return []
    found = []
    for name in names:
        if not name.startswith("rec-") or name in skip:
            continue
        folder = os.path.join(data_dir, name)
        try:
            if os.path.isdir(folder) and recording_unfinished(folder):
                found.append(folder)
        except OSError:
            continue
    return found


def package_recording(out: str, *, title: str = "", session_info: Optional[Dict[str, Any]] = None,
                      markov: bool = True) -> None:
    """The bar's packaging step: build the dataset next to ``out/raw`` (blocking).
    Raises what ``packager.build.build`` raises (SystemExit / ValueError / ...)."""
    from packager.build import build

    name = os.path.basename(os.path.normpath(out))
    build(os.path.join(out, "raw"), out, dataset_id=name, title=(title or "").strip() or name,
          category="cad", skip_video=True, session_info=session_info, markov=markov)


def recover_recording(folder: str, *, markov: bool = True) -> Tuple[str, bool, str]:
    """Package one unfinished recording -> ``(folder, ok, reason)``.  Never deletes
    anything and never touches a folder that is being recorded."""
    if reclock.is_live(os.path.join(folder, "raw")):
        return folder, False, L("仍在录制中，未处理", "it is still being recorded; left alone")
    try:
        package_recording(folder, session_info=_read_json_dict(os.path.join(folder, "session_info.json")),
                          markov=markov)
    except (SystemExit, Exception) as exc:
        return folder, False, str(exc) or type(exc).__name__
    return folder, True, ""


def recovery_question(folders: Sequence[str]) -> str:
    """Text of the start-up "recover these recordings?" question."""
    names = [os.path.basename(os.path.normpath(f)) for f in folders]
    listing = "\n".join("• " + n for n in names[:6])
    if len(names) > 6:
        listing += "\n" + L("…另有 {n} 条", "… {n} more", n=len(names) - 6)
    return (L("发现 {n} 条未正常结束的录制（例如因崩溃或断电）。现在恢复吗？",
              "Found {n} recording(s) that did not finish (e.g. after a crash or power cut). Recover them now?",
              n=len(names))
            + "\n\n" + listing + "\n\n"
            + L("选“否”将不再询问这些录制。不会删除任何文件。",
                "No: you will not be asked about these again. Nothing is deleted."))


def recovery_summary(recovered: Sequence[str], failed: Sequence[Tuple[str, str]]) -> str:
    """Result of a recovery run: recovered folders, then failures with their reason."""
    lines: List[str] = []
    if recovered:
        lines.append(L("已恢复 {n} 条录制：", "Recovered {n} recording(s):", n=len(recovered)))
        lines += ["• " + os.path.basename(os.path.normpath(f)) for f in recovered[:8]]
        if len(recovered) > 8:
            lines.append(L("…另有 {n} 条", "… {n} more", n=len(recovered) - 8))
    if failed:
        if lines:
            lines.append("")
        lines.append(L("{n} 条未能恢复（原始文件仍保留在其文件夹中）：",
                       "Could not recover {n} recording(s) (their raw files are kept in their folders):",
                       n=len(failed)))
        lines += [L("• {name}：{why}", "• {name}: {why}", name=os.path.basename(os.path.normpath(f)),
                    why=shorten(why, 240)) for f, why in failed[:8]]
        if len(failed) > 8:
            lines.append(L("…另有 {n} 条", "… {n} more", n=len(failed) - 8))
    return "\n".join(lines)


# =========================================================== screenshots
def bgra_to_rgb(bgra: bytes, width: int, height: int) -> bytes:
    n = width * height
    src = memoryview(bgra)[: n * 4]
    rgb = bytearray(n * 3)
    rgb[0::3] = src[2::4]
    rgb[1::3] = src[1::4]
    rgb[2::3] = src[0::4]
    return bytes(rgb)


def png_bytes(width: int, height: int, rgb: bytes) -> bytes:
    """Minimal RGB8 PNG encoder (stdlib only)."""
    stride = width * 3
    if len(rgb) < stride * height:
        raise ValueError("pixel buffer too small")
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def write_png(path: str, width: int, height: int, rgb: bytes) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(png_bytes(width, height, rgb))


def _uniform(rgb: bytes) -> bool:
    return len(rgb) < 3 or rgb == rgb[:3] * (len(rgb) // 3)


def _gdi_capture(x: int, y: int, w: int, h: int, hwnd: int = 0) -> Tuple[bytes, int, int]:
    """Screen BitBlt (hwnd=0) or PrintWindow(hwnd) via GDI -> (rgb, w, h)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    HANDLE = ctypes.c_void_p
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = HANDLE
    user32.ReleaseDC.argtypes = [wintypes.HWND, HANDLE]
    user32.PrintWindow.argtypes = [wintypes.HWND, HANDLE, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    gdi32.CreateCompatibleDC.argtypes = [HANDLE]
    gdi32.CreateCompatibleDC.restype = HANDLE
    gdi32.CreateCompatibleBitmap.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = HANDLE
    gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
    gdi32.SelectObject.restype = HANDLE
    gdi32.BitBlt.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             HANDLE, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.BitBlt.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [HANDLE]
    gdi32.DeleteDC.argtypes = [HANDLE]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    gdi32.GetDIBits.argtypes = [HANDLE, HANDLE, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int

    if hwnd:
        rect = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError("empty capture region")
    screen = user32.GetDC(None)
    if not screen:
        raise OSError("GetDC failed")
    mem = bmp = old = None
    try:
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
        if not mem or not bmp:
            raise OSError("CreateCompatibleDC/Bitmap failed")
        old = gdi32.SelectObject(mem, bmp)
        if hwnd:
            if not user32.PrintWindow(hwnd, mem, 2):  # PW_RENDERFULLCONTENT
                raise OSError("PrintWindow failed")
        elif not gdi32.BitBlt(mem, 0, 0, w, h, screen, x, y, 0x00CC0020 | 0x40000000):
            raise OSError("BitBlt failed")
        gdi32.SelectObject(mem, old)
        old = None
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth, bmi.biHeight = w, -h  # negative = top-down rows
        bmi.biPlanes, bmi.biBitCount, bmi.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(w * h * 4)
        if gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0) != h:
            raise OSError("GetDIBits failed")
        return bgra_to_rgb(buf.raw, w, h), w, h
    finally:
        if old:
            gdi32.SelectObject(mem, old)
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem:
            gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, screen)


def grab_window_png(window, path: str) -> str:
    """Save a PNG of a mapped Tk top-level; returns the method used."""
    window.update_idletasks()
    window.update()
    x, y = window.winfo_rootx(), window.winfo_rooty()
    w, h = window.winfo_width(), window.winfo_height()
    if w < 2 or h < 2:
        raise RuntimeError("window is not mapped")
    errors = []
    try:
        from PIL import ImageGrab  # type: ignore

        try:
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        except TypeError:
            img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        img = img.convert("RGB")
        if any(lo != hi for lo, hi in img.getextrema()):
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            img.save(path, "PNG")
            return "pillow"
        errors.append("pillow: blank image")
    except Exception as exc:
        errors.append(f"pillow: {exc}")
    if sys.platform == "win32":
        for label, hwnd in (("gdi-bitblt", 0), ("gdi-printwindow", toplevel_hwnd(window))):
            try:
                rgb, ww, hh = _gdi_capture(x, y, w, h, hwnd=hwnd)
                if not _uniform(rgb):
                    write_png(path, ww, hh, rgb)
                    return label
                errors.append(f"{label}: blank image")
            except Exception as exc:
                errors.append(f"{label}: {exc}")
    elif sys.platform == "darwin":
        try:
            subprocess.run(["screencapture", "-x", f"-R{x},{y},{w},{h}", path],
                           check=True, timeout=20, capture_output=True)
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return "screencapture"
            errors.append("screencapture: no file")
        except Exception as exc:
            errors.append(f"screencapture: {exc}")
    raise RuntimeError("; ".join(errors) or "no screenshot method available")


# ================================================================= widgets
_TkLabel: Any = tk.Label if tk is not None else object
_TkFrame: Any = tk.Frame if tk is not None else object


class FlatButton(_TkLabel):
    """Label-based flat button: identical look on Windows and macOS, hover
    feedback, and ``configure(text=, state=, command=, style=)`` like tk.Button."""

    def __init__(self, master, text: str = "", command: Optional[Callable] = None,
                 style: str = "secondary", font=None, padx: int = 10, pady: int = 3, **kw):
        bg, fg, _ = STYLES.get(style, STYLES["secondary"])
        super().__init__(master, text=text, bg=bg, fg=fg, font=font, padx=padx, pady=pady,
                         bd=0, highlightthickness=0, cursor="hand2", **kw)
        self._command = command
        self._state = "normal"
        self._style = style
        self._hover = False
        self._pressed = False
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")

    def _apply(self) -> None:
        bg, fg, hover = STYLES.get(self._style, STYLES["secondary"])
        if self._state == "disabled":
            if self._style == "primary":
                bg = PANEL
            tk.Label.configure(self, bg=bg, fg=DIM, cursor="arrow")
        else:
            tk.Label.configure(self, bg=hover if self._hover else bg, fg=fg, cursor="hand2")

    def configure(self, cnf=None, **kw):  # type: ignore[override]
        if isinstance(cnf, str):
            return tk.Label.configure(self, cnf)
        if cnf:
            kw = {**cnf, **kw}
        restyle = False
        if "command" in kw:
            self._command = kw.pop("command")
        if "state" in kw:
            self._state = "disabled" if str(kw.pop("state")) == "disabled" else "normal"
            restyle = True
        if "style" in kw:
            self._style = kw.pop("style") or "secondary"
            restyle = True
        for key in ("bg", "background", "fg", "foreground", "activebackground", "activeforeground"):
            kw.pop(key, None)  # colours come from the style
        if kw:
            tk.Label.configure(self, **kw)
        if restyle:
            self._apply()
        return None

    config = configure

    def cget(self, key):  # type: ignore[override]
        if key == "state":
            return self._state
        if key == "command":
            return self._command
        if key == "style":
            return self._style
        return tk.Label.cget(self, key)

    def __getitem__(self, key):
        return self.cget(key)

    def invoke(self):
        if self._state != "disabled" and self._command:
            return self._command()
        return None

    def _on_enter(self, _ev=None):
        self._hover = True
        self._apply()

    def _on_leave(self, _ev=None):
        self._hover = False
        self._pressed = False
        self._apply()

    def _on_press(self, _ev=None):
        self._pressed = True

    def _on_release(self, ev=None):
        was_pressed, self._pressed = self._pressed, False
        if not was_pressed or self._state == "disabled":
            return
        if ev is not None and not (0 <= ev.x < self.winfo_width() and 0 <= ev.y < self.winfo_height()):
            return
        self.invoke()


class Tooltip:
    """Delayed hover tooltip; text is a str or a callable (re-evaluated in the
    current language every time it shows).  Tooltip windows are excluded from
    screen capture too, so hovering the bar never leaks into the video."""

    def __init__(self, widget, text, delay_ms: int = 450, wrap_px: int = 380):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self.wrap_px = wrap_px
        self._job = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        # same sequence as FlatButton/drag bindings so all of them fire
        widget.bind("<ButtonPress-1>", self.hide, add="+")

    def _schedule(self, _ev=None):
        self._cancel()
        try:
            self._job = self.widget.after(self.delay_ms, self.show)
        except Exception:
            self._job = None

    def _cancel(self):
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def show(self):
        self._job = None
        try:
            text = self.text() if callable(self.text) else str(self.text or "")
        except Exception:
            text = ""
        if not text or self._tip is not None:
            return
        try:
            w = self.widget
            tip = tk.Toplevel(w)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            tip.configure(bg=LINE)
            lbl = tk.Label(tip, text=text, justify="left", bg=TIP_BG, fg=TEXT, padx=8, pady=5,
                           wraplength=self.wrap_px, font=getattr(w, "_tip_font", None) or "TkDefaultFont")
            lbl.pack(padx=1, pady=1)
            tip.update_idletasks()
            x = w.winfo_rootx()
            y = w.winfo_rooty() + w.winfo_height() + 4
            sw = w.winfo_screenwidth()
            x = max(0, min(x, sw - tip.winfo_reqwidth() - 4)) if x < sw else x
            tip.geometry(f"+{x}+{y}")
            exclude_from_capture(tip)
            self._tip = tip
        except Exception:
            self._tip = None

    def hide(self, _ev=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


class Segmented(_TkFrame):
    """Segmented radio control: ``[ Full screen | App only ]``."""

    def __init__(self, master, options: Sequence[Tuple[str, Any]], value: str,
                 command: Optional[Callable[[str], None]] = None, font=None,
                 padx: int = 9, pady: int = 2):
        super().__init__(master, bg=LINE, bd=0, highlightthickness=0)
        self._choices = list(options)
        self._value = value
        self._command = command
        self._enabled = True
        self._disabled: set = set()
        self.buttons: Dict[str, FlatButton] = {}
        last = len(self._choices) - 1
        for i, (val, _label) in enumerate(self._choices):
            btn = FlatButton(self, "", command=lambda v=val: self._click(v), style="field",
                             font=font, padx=padx, pady=pady)
            btn.pack(side="left", padx=(1, 1 if i == last else 0), pady=1)
            self.buttons[val] = btn
        self.retranslate()

    def _click(self, value: str) -> None:
        if not self._enabled or value in self._disabled or value == self._value:
            return
        self._value = value
        self._apply()
        if self._command:
            self._command(value)

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        self._value = value
        self._apply()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._apply()

    def set_option_enabled(self, value: str, enabled: bool) -> None:
        if enabled:
            self._disabled.discard(value)
        else:
            self._disabled.add(value)
        self._apply()

    def retranslate(self) -> None:
        for val, label in self._choices:
            text = label() if callable(label) else str(label)
            self.buttons[val].configure(text=text)
        self._apply()

    def _apply(self) -> None:
        for val, btn in self.buttons.items():
            disabled = not self._enabled or val in self._disabled
            btn.configure(style="selected" if val == self._value else "field",
                          state="disabled" if disabled and val != self._value else "normal")
            if disabled and val == self._value:
                btn.configure(cursor="arrow")


class CheckBox(_TkFrame):
    """Dark check box (☑ / ☐ + text) bound to a callback."""

    def __init__(self, master, text, value: bool, command: Optional[Callable[[bool], None]] = None,
                 font=None, bg: str = PANEL):
        super().__init__(master, bg=bg, bd=0, highlightthickness=0)
        self._text = text
        self._value = bool(value)
        self._command = command
        self._enabled = True
        self.box = tk.Label(self, bg=bg, font=font, cursor="hand2", padx=0)
        self.box.pack(side="left")
        self.label = tk.Label(self, bg=bg, font=font, cursor="hand2", anchor="w")
        self.label.pack(side="left", padx=(5, 0))
        for w in (self, self.box, self.label):
            w.bind("<ButtonRelease-1>", self._toggle, add="+")
        self.retranslate()

    def _toggle(self, _ev=None):
        if not self._enabled:
            return
        self._value = not self._value
        self._apply()
        if self._command:
            self._command(self._value)

    def get(self) -> bool:
        return self._value

    def set(self, value: bool) -> None:
        self._value = bool(value)
        self._apply()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._apply()

    def retranslate(self) -> None:
        self.label.configure(text=self._text() if callable(self._text) else str(self._text))
        self._apply()

    def _apply(self) -> None:
        on = self._value
        self.box.configure(text="☑" if on else "☐",
                           fg=(ACCENT if on else MUTED) if self._enabled else DIM,
                           cursor="hand2" if self._enabled else "arrow")
        self.label.configure(fg=TEXT if self._enabled else DIM,
                             cursor="hand2" if self._enabled else "arrow")


# ============================================================= demo recorder
class DemoRecorder:
    """Stand-in recorder for ``--state *-demo`` screenshots (no capture)."""

    def __init__(self, auto_paused: bool = False, paused: bool = False, stopped: bool = False):
        self.auto_paused = auto_paused
        self.paused = paused or auto_paused
        self.stopped = stopped
        self._t0 = time.monotonic()

    def snapshot(self) -> Dict[str, Any]:
        running = not (self.paused or self.stopped)
        dt_s = time.monotonic() - self._t0
        frames = 22702 + (int(dt_s * 30) if running else 0)
        return {"elapsed_ms": frames * 1000.0 / 30, "paused": self.paused, "segments": 3,
                "frames": frames, "events": 48213 + (int(dt_s * 45) if running else 0),
                "backend": "dxcam", "encoder": "libx264", "out": "", "stopped": self.stopped,
                "auto_paused": self.auto_paused, "capture_mode": "app",
                "target_exe": "SLDWORKS.exe", "masked_now": True}

    def toggle_pause(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True


class FinishedRecording:
    """Stand-in recorder for a recording packaged outside this bar session
    (crash recovery): the done line's timer and counters come from metadata.json."""

    def __init__(self, folder: str):
        meta = _read_json_dict(os.path.join(folder, "metadata.json")) or {}

        def num(key: str) -> float:
            try:
                return max(0.0, float(meta.get(key) or 0))
            except (TypeError, ValueError):
                return 0.0

        self._snap = {"elapsed_ms": num("total_duration_ms"), "frames": int(num("total_frames")),
                      "events": int(num("event_count")), "paused": False, "stopped": True, "out": folder}

    def snapshot(self) -> Dict[str, Any]:
        return dict(self._snap)

    def toggle_pause(self) -> None:
        pass

    def stop(self) -> None:
        pass


# ================================================================== the bar
class OverlayUI:
    WIDTH = 780          # logical px (scaled by the monitor DPI)
    TICK_MS = 200
    SESSION_END_STOP_S = 20.0    # sign-out / shutdown: wait this long for the recorder to finalize
    REPORT_CHECK_TICKS = 20      # done state: look for a new narration.json every ~4 s

    def __init__(self, argv: Optional[list] = None, *, gui_args: Optional[argparse.Namespace] = None):
        if tk is None:
            raise RuntimeError("Tkinter is not available in this Python build")
        self.gui_args = gui_args if gui_args is not None else parse_gui_args(list(argv or []))
        self.screenshot = bool(getattr(self.gui_args, "screenshot", None))
        if self.gui_args.lang:
            i18n.set_language(self.gui_args.lang, persist=False)
        self._init_state()

        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.report_callback_exception = self._on_tk_error
        self.scale = max(1.0, float(self.root.winfo_fpixels("1i")) / 96.0)
        self._setup_fonts()
        self.root.title(L("D Studio · 录制", "D Studio · Recorder"))
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self._build()
        self._install_window_handlers()
        self._place()
        self.root.deiconify()
        self.root.update_idletasks()
        if self.screenshot:
            self.capture_excluded = True  # screenshots must see the bar; render the normal state
        else:
            self.capture_excluded = exclude_from_capture(self.root)
        self._lang_cb = i18n.on_change(self._on_language_event)
        self._check_recorder_args()
        self._refresh()
        self._fit()
        self._start_recovery_scan()      # no-op in --screenshot mode (CI)
        self._tick_job = self.root.after(self.TICK_MS, self._tick)

    # ---------------------------------------------------------------- state
    def _init_state(self) -> None:
        a = self.gui_args
        self._persist = not self.screenshot
        cfg = load_settings()
        if getattr(a, "fps", None):
            cfg["fps"] = int(a.fps)
        if getattr(a, "quality", None) is not None:
            cfg["quality"] = int(a.quality)
        if getattr(a, "backend", None):
            cfg["backend"] = a.backend
        # CAD export format: Markov CAD by default (settings), CLI flags override for this run
        if getattr(a, "legacy_delivery", False):
            cfg["markov_delivery"] = False
        elif getattr(a, "markov_delivery", False):
            cfg["markov_delivery"] = True
        self.source_override = ""
        src = (getattr(a, "source", None) or "").strip()
        if src:
            if src.lower().startswith("app:") and src[4:].strip():
                cfg.update(capture_mode="app", app_exe=src[4:].strip(), app_name="")
            elif src.lower() != "desktop":
                cfg["capture_mode"] = "desktop"
                self.source_override = src
            else:
                cfg["capture_mode"] = "desktop"
        cfg["backend"] = normalize_backend(cfg["capture_mode"], cfg["backend"])
        self.cfg: Dict[str, Any] = cfg
        self._rec_cfg: Dict[str, Any] = dict(cfg)
        self.rec = None
        self.thread: Optional[threading.Thread] = None
        self.state = "idle"
        self._state_lock = threading.RLock()
        self._error: Any = ""
        self._error_is_config = False
        self.last_out = ""
        self.capture_excluded = False
        self._packaged = False
        self._closing = False
        self._session_ending = False      # sign-out / Restart Manager: close without dialogs
        self._finalized = False
        self._final_lock = threading.Lock()
        self._delivery_report: Dict[str, Any] = {}
        self._report_sig: Optional[Tuple[int, int]] = None   # narration.json the report saw
        self._report_markov: Optional[bool] = None            # delivery format the report used
        self._report_busy = False
        self._report_gen = 0                                  # bumped whenever a report is loaded
        self._report_ticks = 0
        self._ui_queue: deque = deque()
        self._fps = LiveFps()
        self._last_snap: Dict[str, Any] = {}
        self._busy_since = 0.0
        self._server = None
        self._server_thread: Optional[threading.Thread] = None
        self._server_port: Optional[int] = None
        self._server_lock = threading.Lock()
        self._picker = None
        self._title = str(cfg.get("title") or "")
        self._drag = (0, 0)
        self._dot_on = False
        self._locked: Optional[bool] = None
        self._results_shown = False
        self._settings_shown = False
        self._i18n_items: List[Tuple[Any, Callable[[], str]]] = []
        self._title_job = None
        self._overlay_job = None
        self._recovery_scanned = False
        self._recovery_pending: Optional[List[str]] = None   # found while recording: ask afterwards
        self._hint_until = 0.0
        self._hint_text: Optional[Callable[[], str]] = None
        self._session_template = getattr(a, "session_info", None) or default_session_template()
        self._session_info: Dict[str, Any] = {}
        if os.path.isfile(self._session_template):
            try:
                with open(self._session_template, encoding="utf-8-sig") as fh:
                    info = json.load(fh)
                if not isinstance(info, dict):
                    raise ValueError("not an object")
                self._session_info = info
            except (OSError, ValueError):
                self._session_info = {}
                self._set_error("CAD 交付信息文件无效，请在 ⚙ → CAD 交付信息 中重新填写",
                                "The CAD delivery info file is invalid; re-enter it in ⚙ → CAD delivery info")

    # error text is kept as a callable so it re-renders when the language flips
    @property
    def error(self) -> str:
        err = getattr(self, "_error", "")
        try:
            return err() if callable(err) else str(err or "")
        except Exception:
            return ""

    @error.setter
    def error(self, value: Any) -> None:
        self._error = value

    def _set_error(self, zh: str, en: str, **fmt) -> None:
        self._error = lambda: L(zh, en, **fmt)

    def _fail(self, msg: Any, *, config: bool = False) -> None:
        with self._state_lock:
            self._error = msg
            self._error_is_config = config
            self.state = "error"

    def _transition(self, new: str, only_from: Optional[Iterable[str]] = None) -> bool:
        """Compare-and-set ``self.state`` (the Tk thread and workers both write it)."""
        with self._state_lock:
            if only_from is not None and self.state not in only_from:
                return False
            self.state = new
            return True

    # ------------------------------------------------------------ helpers
    def px(self, n: float) -> int:
        return int(round(n * getattr(self, "scale", 1.0)))

    def _post(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the Tk thread at the next tick (worker threads)."""
        self._ui_queue.append(fn)

    def _log(self, text: str) -> None:
        try:
            folder = paths.data_dir()
            with open(os.path.join(folder, "dstudio-gui.log"), "a", encoding="utf-8") as fh:
                fh.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {text.rstrip()}\n")
        except Exception:
            pass

    def _log_exception(self, context: str) -> None:
        self._log(f"{context}:\n{traceback.format_exc()}")

    def _save(self, **changes: Any) -> None:
        if not getattr(self, "_persist", False):
            return
        try:
            update_settings(**changes)
        except Exception:
            self._log_exception("save settings")

    def _on_tk_error(self, exc_type, exc, tb) -> None:
        self._log("Tk callback error:\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
        if self.state not in ACTIVE_STATES and self.state not in BUSY_STATES:
            self._set_error("界面错误：{e}", "UI error: {e}", e=f"{exc_type.__name__}: {exc}")

    def _tr(self, widget, zh: str, en: str, option: str = "text"):
        """Set a translatable static text and remember it for re-rendering."""
        fn = (lambda: L(zh, en))
        self._i18n_items.append((lambda w=widget, o=option, f=fn: w.configure(**{o: f()}), fn))
        widget.configure(**{option: fn()})
        return widget

    def _tr_call(self, fn: Callable[[], None]) -> None:
        """Remember an arbitrary re-render step (formatted labels, segments...)."""
        self._i18n_items.append((fn, fn))
        fn()

    def _tip(self, widget, text) -> Tooltip:
        widget._tip_font = self.f_small
        return Tooltip(widget, text, wrap_px=self.px(380))

    def _markov(self) -> bool:
        """Markov CAD export (default) or the legacy task_set / clip_set layout (⚙ → DELIVERY)."""
        return bool(self.cfg.get("markov_delivery", True))

    def _install_window_handlers(self) -> None:
        """Route every close request through :meth:`on_close`.

        Without this, WM_CLOSE (Alt+F4 on the active bar, ``taskkill`` without
        /F) lets Tk destroy the window and the process exits with the
        recorder's daemon threads still capturing.  Tk on Windows turns
        WM_QUERYENDSESSION (sign-out / shutdown, and the installer's Restart
        Manager, which sends no WM_CLOSE) into WM_SAVE_YOURSELF.
        Focus/activation re-checks a finished recording.
        """
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.protocol("WM_SAVE_YOURSELF", self.on_session_end)
        for sequence in ("<Activate>", "<FocusIn>"):
            self.root.bind(sequence, self._on_bar_focus, add="+")
        stats = getattr(self, "stats", None)
        if stats is not None:
            stats.bind("<Enter>", self._on_bar_focus, add="+")  # hovering shows the report tooltip

    def _on_bar_focus(self, _ev=None) -> None:
        try:
            self.refresh_report()
        except Exception:
            self._log_exception("refresh report")

    # ---------------------------------------------------------------- fonts
    def _setup_fonts(self) -> None:
        try:
            self._families = set(tkfont.families(self.root))
        except Exception:
            self._families = set()
        base = 12 if sys.platform == "darwin" else 9  # macOS points are smaller
        self.f_ui = tkfont.Font(root=self.root, size=base)
        self.f_small = tkfont.Font(root=self.root, size=base - 1)
        self.f_bold = tkfont.Font(root=self.root, size=base, weight="bold")
        self.f_head = tkfont.Font(root=self.root, size=base - 1, weight="bold")
        self.f_icon = tkfont.Font(root=self.root, size=base + 2)
        self.f_timer = tkfont.Font(root=self.root, size=base + 6, weight="bold",
                                   family=self._pick_family(MONO_FAMILIES))
        self._apply_font_family()

    def _pick_family(self, candidates: Iterable[str]) -> str:
        for name in candidates:
            if name in self._families:
                return name
        try:
            return tkfont.nametofont("TkDefaultFont").actual("family")
        except Exception:
            return "TkDefaultFont"

    def _apply_font_family(self) -> None:
        fam = self._pick_family(UI_FAMILIES.get(i18n.get_language(), UI_FAMILIES["en"]))
        for f in (self.f_ui, self.f_small, self.f_bold, self.f_head, self.f_icon):
            f.configure(family=fam)

    # --------------------------------------------------------------- layout
    def _build(self) -> None:
        s = self.px
        self.frame = tk.Frame(self.root, bg=BG, highlightthickness=1,
                              highlightbackground=LINE, highlightcolor=LINE)
        self.frame.pack(fill="both", expand=True)

        # row 1: status + main controls ---------------------------------
        self.bar = tk.Frame(self.frame, bg=BG)
        self.bar.pack(fill="x", padx=s(8), pady=(s(6), s(2)))
        self.dot = tk.Label(self.bar, text="●", fg=DOT_IDLE, bg=BG, font=self.f_icon)
        self.dot.pack(side="left", padx=(s(2), s(6)))
        self.time_lbl = tk.Label(self.bar, text="00:00.0", fg=TEXT, bg=BG, font=self.f_timer)
        self.time_lbl.pack(side="left")

        self.btn_close = FlatButton(self.bar, "✕", self.on_close, style="ghost", font=self.f_icon, padx=s(7), pady=0)
        self.btn_close.pack(side="right", padx=(s(2), 0))
        self._tip(self.btn_close, lambda: L("关闭（录制中会先停止并打包）", "Close (stops and packages a running recording first)"))
        self.btn_gear = FlatButton(self.bar, "⚙", self.toggle_settings, style="ghost", font=self.f_icon, padx=s(7), pady=0)
        self.btn_gear.pack(side="right", padx=(s(2), 0))
        self._tip(self.btn_gear, lambda: L("设置", "Settings"))
        self.btn_lang = Segmented(self.bar, [("zh", "中"), ("en", "EN")], i18n.get_language(),
                                  command=self._on_lang_clicked, font=self.f_small, padx=s(6), pady=s(1))
        self.btn_lang.pack(side="right", padx=(s(8), s(4)))
        for b in self.btn_lang.buttons.values():
            self._tip(b, lambda: L("界面语言 / Language", "Language / 界面语言"))
        self.btn_stop = FlatButton(self.bar, "", self.stop_and_pack, style="danger", font=self.f_ui, padx=s(10), pady=s(3))
        self.btn_stop.pack(side="right", padx=(s(4), 0))
        self._tip(self.btn_stop, lambda: L("停止录制（快捷键 F10）", "Stop recording (hotkey F10)"))
        self.btn_main = FlatButton(self.bar, "", self.on_main, style="primary", font=self.f_bold, padx=s(12), pady=s(3))
        self.btn_main.pack(side="right", padx=(s(4), 0))
        self._tip(self.btn_main, lambda: L(
            "开始 / 暂停 / 继续（录制中快捷键 F9）。自动暂停时点它会改为手动暂停，切回应用也不会自动继续。",
            "Start / pause / resume (hotkey F9 while recording). While auto-paused it turns the pause into a "
            "manual one, so switching back to the app does not resume."))
        self.state_lbl = tk.Label(self.bar, text="", fg=MUTED, bg=BG, font=self.f_ui, anchor="w")
        self.state_lbl.pack(side="left", padx=(s(10), s(6)), fill="x", expand=True)

        # row 2: mode chip + live stats -----------------------------------
        self.info = tk.Frame(self.frame, bg=BG)
        self.info.pack(fill="x", padx=s(10), pady=(0, s(7)))
        self.mode_lbl = tk.Label(self.info, text="", fg=ACCENT, bg=CHIP, font=self.f_small,
                                 padx=s(7), pady=s(1), cursor="hand2")
        self.mode_lbl.pack(side="left")
        self.mode_lbl.bind("<ButtonRelease-1>", lambda _e: self.show_settings(True))
        self._tip(self.mode_lbl, self._mode_tooltip)
        self.stats = tk.Label(self.info, text="", fg=MUTED, bg=BG, font=self.f_small, anchor="w")
        self.stats.pack(side="left", padx=(s(8), 0), fill="x", expand=True)
        self._tip(self.stats, self._stats_tooltip)

        for w in (self.frame, self.bar, self.dot, self.time_lbl, self.state_lbl, self.info, self.stats):
            w.bind("<ButtonPress-1>", self._drag_start, add="+")
            w.bind("<B1-Motion>", self._drag_move, add="+")
            w.bind("<ButtonRelease-1>", self._drag_end, add="+")

        # row 3: after-recording actions (shown in done / error) ----------
        self.result_row = tk.Frame(self.frame, bg=BG)
        self.btn_annotator = FlatButton(self.result_row, "", self.open_annotator, style="link", font=self.f_small, padx=s(9))
        self.btn_export_rec = FlatButton(self.result_row, "", self.export_recording, font=self.f_small, padx=s(9))
        self.btn_session = FlatButton(self.result_row, "", self.edit_session, font=self.f_small, padx=s(9))
        self.btn_export = FlatButton(self.result_row, "", self.export_delivery, font=self.f_small, padx=s(9))
        self.btn_folder = FlatButton(self.result_row, "", self.open_folder, font=self.f_small, padx=s(9))
        for b in (self.btn_annotator, self.btn_export_rec, self.btn_session, self.btn_export, self.btn_folder):
            b.pack(side="left", padx=(0, s(5)))
        self._tip(self.btn_annotator, lambda: L("在浏览器中回看并标注这条录制", "Review and annotate this recording in your browser"))
        self._tip(self.btn_export_rec, lambda: L(
            "导出 Markov 剪辑集：clip.mp4、events.json、frame_events.json、metadata.json、narration.json（无需任务集）",
            "Export the Markov clip set: clip.mp4, events.json, frame_events.json, metadata.json, narration.json (no task set needed)"))
        self._tip(self.btn_session, lambda: L("填写或核对这条录制的 CAD 交付信息", "Fill in or review the CAD delivery info of this recording"))
        self._tip(self.btn_export, lambda: L(
            "核验后导出 Markov CAD 交付包（10 个文件：task.pdf、task_desc.json、rubrics.json、references.zip、gold.zip + 剪辑集）",
            "Check and export the Markov CAD delivery (10 files: task.pdf, task_desc.json, rubrics.json, "
            "references.zip, gold.zip + the clip set)") if self._markov() else L(
            "核验后导出旧版交付包（task_set / clip_set / evidence 目录）",
            "Check and export the legacy delivery (task_set / clip_set / evidence folders)"))
        self._tip(self.btn_folder, lambda: self.last_out or paths.data_dir(create=False))

        # settings panel ---------------------------------------------------
        self.settings = tk.Frame(self.frame, bg=PANEL)
        self._build_settings(self.settings)

    def _section(self, parent, row: int, zh: str, en: str) -> None:
        s = self.px
        lbl = tk.Label(parent, fg=DIM, bg=PANEL, font=self.f_head, anchor="w")
        self._tr(lbl, zh, en)
        lbl.grid(row=row, column=0, columnspan=2, sticky="w", padx=s(12), pady=(s(10), s(2)))

    def _label(self, parent, row: int, zh: str, en: str) -> None:
        s = self.px
        lbl = tk.Label(parent, fg=MUTED, bg=PANEL, font=self.f_small, anchor="w")
        self._tr(lbl, zh, en)
        lbl.grid(row=row, column=0, sticky="nw", padx=(s(14), s(8)), pady=(s(5), s(3)))

    def _cell(self, parent, row: int) -> tk.Frame:
        s = self.px
        f = tk.Frame(parent, bg=PANEL)
        f.grid(row=row, column=1, sticky="we", padx=(0, s(12)), pady=s(3))
        return f

    def _entry(self, parent, width: int, value: str = "") -> tk.Entry:
        s = self.px
        e = tk.Entry(parent, width=width, bg=FIELD, fg=TEXT, relief="flat", insertbackground=TEXT,
                     disabledbackground=PANEL, disabledforeground=DIM, highlightthickness=1,
                     highlightbackground=LINE, highlightcolor=ACCENT, font=self.f_ui, bd=s(3))
        e.insert(0, value)
        # overrideredirect windows do not always get keyboard focus by themselves
        e.bind("<ButtonPress-1>", lambda _ev, w=e: w.focus_force(), add="+")
        return e

    def _build_settings(self, p) -> None:
        s = self.px
        cfg = self.cfg
        p.grid_columnconfigure(0, minsize=s(104))
        p.grid_columnconfigure(1, weight=1)
        tk.Frame(p, bg=LINE, height=1).grid(row=0, column=0, columnspan=2, sticky="we")

        # --- capture ---------------------------------------------------
        self._section(p, 1, "采集", "CAPTURE")
        self._label(p, 2, "录制范围", "Capture")
        c = self._cell(p, 2)
        self.seg_mode = Segmented(c, [("desktop", lambda: L("全屏", "Full screen")),
                                      ("app", lambda: L("仅应用", "App only"))],
                                  cfg["capture_mode"], command=self._on_mode, font=self.f_small,
                                  padx=s(10), pady=s(2))
        self.seg_mode.pack(side="left")
        self.btn_choose_app = FlatButton(c, "", self.choose_app, style="secondary", font=self.f_small, padx=s(9), pady=s(2))
        self._tr(self.btn_choose_app, "选择应用…", "Choose app…")
        self.btn_choose_app.pack(side="left", padx=(s(8), 0))
        self.app_lbl = tk.Label(c, text="", fg=MUTED, bg=PANEL, font=self.f_small, anchor="w")
        self.app_lbl.pack(side="left", padx=(s(8), 0), fill="x", expand=True)
        self._tip(self.app_lbl, self._mode_tooltip)
        self.mode_hint = tk.Label(p, text="", fg=MUTED, bg=PANEL, font=self.f_small, justify="left",
                                  anchor="w", wraplength=s(self.WIDTH - 150))
        self.mode_hint.grid(row=3, column=1, sticky="we", padx=(0, s(12)), pady=(0, s(2)))

        # App only: full-screen see-through overlays that may stay visible (finding 25)
        self.overlay_lbl = tk.Label(p, fg=MUTED, bg=PANEL, font=self.f_small, anchor="w")
        self._tr(self.overlay_lbl, "放行覆盖层", "Allowed overlays")
        self.overlay_lbl.grid(row=4, column=0, sticky="nw", padx=(s(14), s(8)), pady=(s(5), s(3)))
        self.overlay_cell = self._cell(p, 4)
        self.e_overlay = self._entry(self.overlay_cell, 28, ", ".join(cfg.get("app_overlay_allow") or []))
        self.e_overlay.pack(side="left")
        self.e_overlay.bind("<KeyRelease>", self._on_overlay_allow, add="+")
        self.e_overlay.bind("<FocusOut>", self._on_overlay_allow, add="+")
        overlay_note = tk.Label(self.overlay_cell, fg=DIM, bg=PANEL, font=self.f_small, anchor="w")
        self._tr(overlay_note, "程序名，逗号分隔，如 dimmer.exe", "program names, comma-separated, e.g. dimmer.exe")
        overlay_note.pack(side="left", padx=(s(6), 0))
        self._tip(self.e_overlay, lambda: L(
            "“仅应用”模式下，未知的全屏透明覆盖层（护眼滤镜、水印等）会让整个画面被涂黑。"
            "确认它不显示任何隐私内容后，在这里填入它的程序名即可放行（下一次录制生效）。",
            "In App-only mode an unknown full-screen see-through overlay (screen dimmer, watermark…) blacks "
            "out the whole video. If it shows nothing private, enter its program name here to allow it "
            "(applies to the next recording)."))

        self._label(p, 5, "视频", "Video")
        c = self._cell(p, 5)
        fps_values = ["30", "60"]
        if str(cfg["fps"]) not in fps_values:
            fps_values.append(str(cfg["fps"]))
        self.seg_fps = Segmented(c, [(v, v) for v in fps_values], str(cfg["fps"]), command=self._on_fps,
                                 font=self.f_small, padx=s(10), pady=s(2))
        self.seg_fps.pack(side="left")
        fps_note = tk.Label(c, fg=MUTED, bg=PANEL, font=self.f_small)
        self._tr(fps_note, "fps（Markov = 30）", "fps (Markov = 30)")
        fps_note.pack(side="left", padx=(s(6), s(18)))
        q_lbl = tk.Label(c, fg=MUTED, bg=PANEL, font=self.f_small)
        self._tr(q_lbl, "画质 CRF", "Quality CRF")
        q_lbl.pack(side="left")
        self.e_quality = self._entry(c, 4, str(cfg["quality"]))
        self.e_quality.pack(side="left", padx=(s(6), s(6)))
        self.e_quality.bind("<KeyRelease>", self._on_quality, add="+")
        self.e_quality.bind("<FocusOut>", self._on_quality, add="+")
        q_note = tk.Label(c, fg=DIM, bg=PANEL, font=self.f_small)
        self._tr(q_note, "越小越清晰，Markov = 23", "lower = sharper, Markov = 23")
        q_note.pack(side="left")
        self._tip(self.e_quality, lambda: L("x264 CRF 0–51；23 与 Markov 样例一致", "x264 CRF 0–51; 23 matches the Markov samples"))

        self._label(p, 6, "采集后端", "Backend")
        c = self._cell(p, 6)
        self.seg_backend = Segmented(c, [(b, b) for b in BACKENDS], cfg["backend"], command=self._on_backend,
                                     font=self.f_small, padx=s(9), pady=s(2))
        self.seg_backend.pack(side="left")
        self.backend_hint = tk.Label(c, text="", fg=DIM, bg=PANEL, font=self.f_small, anchor="w")
        self.backend_hint.pack(side="left", padx=(s(8), 0), fill="x", expand=True)
        for name, btn in self.seg_backend.buttons.items():
            self._tip(btn, lambda n=name: self._backend_description(n))

        self._label(p, 7, "鼠标指针", "Cursor")
        c = self._cell(p, 7)
        self.chk_cursor = CheckBox(c, lambda: L("把鼠标指针画进视频（Markov 样例有指针）",
                                                "Draw the mouse cursor into the video (Markov clips have it)"),
                                   cfg["draw_cursor"], command=self._on_cursor, font=self.f_small)
        self.chk_cursor.pack(side="left")

        # --- output ----------------------------------------------------
        self._section(p, 8, "输出", "OUTPUT")
        self._label(p, 9, "标题", "Title")
        c = self._cell(p, 9)
        self.e_title = self._entry(c, 46, self._title)
        self.e_title.pack(side="left", fill="x", expand=True)
        self.e_title.bind("<KeyRelease>", self._on_title, add="+")
        self._tip(self.e_title, lambda: L("写入 metadata.json 的标题；留空则用文件夹名", "Title written to metadata.json; empty = folder name"))

        self._label(p, 10, "数据文件夹", "Data folder")
        c = self._cell(p, 10)
        self.data_lbl = tk.Label(c, text="", fg=ACCENT, bg=PANEL, font=self.f_small, anchor="w")
        self.data_lbl.pack(side="left", fill="x", expand=True)
        self._tip(self.data_lbl, lambda: paths.data_dir(create=False))
        self.btn_open_data = FlatButton(c, "", self.open_data_folder, font=self.f_small, padx=s(9), pady=s(2))
        self._tr(self.btn_open_data, "打开", "Open")
        self.btn_open_data.pack(side="right", padx=(s(5), 0))
        self.btn_data = FlatButton(c, "", self.change_data_dir, font=self.f_small, padx=s(9), pady=s(2))
        self._tr(self.btn_data, "更改…", "Change…")
        self.btn_data.pack(side="right", padx=(s(5), 0))

        c = self._cell(p, 11)
        self.chk_autopack = CheckBox(c, lambda: L("停止后自动打包（生成 clip.mp4 / events / metadata）",
                                                  "Package automatically after stop (clip.mp4 / events / metadata)"),
                                     cfg["autopack"], command=self._on_autopack, font=self.f_small)
        self.chk_autopack.pack(side="left")

        # --- delivery ----------------------------------------------------
        self._section(p, 12, "交付", "DELIVERY")
        c = self._cell(p, 13)
        self.btn_session_tpl = FlatButton(c, "", self.edit_session, font=self.f_small, padx=s(9), pady=s(2))
        self._tr(self.btn_session_tpl, "CAD 交付信息…", "CAD delivery info…")
        self.btn_session_tpl.pack(side="left")
        self.btn_annotator_tpl = FlatButton(c, "", self.open_annotator, style="link", font=self.f_small, padx=s(9), pady=s(2))
        self._tr(self.btn_annotator_tpl, "↗ 打开标注台", "↗ Open annotator")
        self.btn_annotator_tpl.pack(side="left", padx=(s(6), 0))
        c = self._cell(p, 14)
        self.chk_legacy = CheckBox(c, lambda: L("旧版交付格式（task_set / clip_set / evidence 目录，需自备 references.zip 和 gold.zip）",
                                                "Legacy delivery format (task_set / clip_set / evidence folders; "
                                                "you supply references.zip and gold.zip)"),
                                   not self._markov(), command=self._on_legacy, font=self.f_small)
        self.chk_legacy.pack(side="left")
        self._tip(self.chk_legacy.label, lambda: L(
            "不勾选（默认）= Markov CAD 样例格式：task.pdf、task_desc.json、rubrics.json、references.zip、gold.zip "
            "+ 剪辑集 5 个文件；ZIP 可由任务集中的 task_files/ 和 output_files/ 自动生成。",
            "Unchecked (default) = the Markov CAD sample: task.pdf, task_desc.json, rubrics.json, references.zip, "
            "gold.zip + the 5 clip-set files; the ZIPs can be built from task_files/ and output_files/ in the task set."))
        tk.Frame(p, bg=PANEL, height=s(8)).grid(row=15, column=0, columnspan=2)

        self._tr_call(self._render_settings_texts)

    def _place(self) -> None:
        self.root.update_idletasks()
        w = self.px(self.WIDTH)
        h = max(self.px(56), self.frame.winfo_reqheight())
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x, y = max(0, (sw - w) // 2), self.px(14)
        pos = self.cfg.get("bar_pos") or []
        if self.screenshot:
            x, y = self.px(40), self.px(40)
        elif len(pos) == 2 and point_on_a_monitor(pos[0] + 20, pos[1] + 10):
            x, y = int(pos[0]), int(pos[1])
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _fit(self) -> None:
        """Resize the window to its content (rows appear / fonts change)."""
        try:
            self.root.update_idletasks()
            w = self.px(self.WIDTH)
            h = self.frame.winfo_reqheight()
            x, y = self.root.winfo_x(), self.root.winfo_y()
            left, top, right, bottom = work_area_at(
                x + w // 2, y + 5, (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()))
            if y + h > bottom and h < bottom - top:
                y = max(top, bottom - h)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    # --------------------------------------------------------------- window
    def _drag_start(self, ev) -> None:
        self._drag = (ev.x_root - self.root.winfo_x(), ev.y_root - self.root.winfo_y())

    def _drag_move(self, ev) -> None:
        self.root.geometry(f"+{ev.x_root - self._drag[0]}+{ev.y_root - self._drag[1]}")

    def _drag_end(self, _ev=None) -> None:
        self._save(bar_pos=[self.root.winfo_x(), self.root.winfo_y()])

    def toggle_settings(self) -> None:
        self.show_settings(not self._settings_shown)

    def show_settings(self, show: bool) -> None:
        if show == self._settings_shown:
            return
        self._settings_shown = show
        if show:
            self.settings.pack(fill="x", side="bottom")
        else:
            self.settings.pack_forget()
        self._fit()

    # ------------------------------------------------------------- language
    def _on_lang_clicked(self, lang: str) -> None:
        if lang != i18n.get_language():
            i18n.set_language(lang, persist=self._persist)

    def _on_language_event(self, _lang: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self._on_language_changed()
        else:
            self._post(self._on_language_changed)

    def _on_language_changed(self) -> None:
        try:
            self._apply_font_family()
            self.root.title(L("D Studio · 录制", "D Studio · Recorder"))
            self.btn_lang.set(i18n.get_language())
            for apply, _fn in list(self._i18n_items):
                try:
                    apply()
                except Exception:
                    pass
            for widget in (self.seg_mode, self.chk_cursor, self.chk_autopack, self.chk_legacy):
                widget.retranslate()
            self._refresh()
            self._fit()
        except Exception:
            self._log_exception("language change")

    # ------------------------------------------------------------- settings
    def _backend_description(self, name: str) -> str:
        return {
            "auto": L("自动：优先 DXGI，不可用时用 GDI / gdigrab", "Auto: DXGI first, else GDI / gdigrab"),
            "dxcam": L("DXGI 桌面复制（快，需 GPU 驱动）", "DXGI desktop duplication (fast, needs GPU driver)"),
            "gdi": L("GDI BitBlt（兼容性好，CPU 占用略高）", "GDI BitBlt (most compatible, more CPU)"),
            "gdigrab": L("FFmpeg gdigrab（仅全屏，不支持“仅应用”）", "FFmpeg gdigrab (full screen only, not App only)"),
        }.get(name, name)

    def _mode_tooltip(self) -> str:
        cfg = self._rec_cfg if self.state in ACTIVE_STATES else self.cfg
        if cfg.get("capture_mode") == "app":
            exe = cfg.get("app_exe") or L("（未选择）", "(none)")
            return L("仅录制 {name}（{exe}）。其他窗口、通知、任务栏会被涂黑；切到其他应用时自动暂停。",
                     "Recording only {name} ({exe}). Other windows, notifications and the taskbar are "
                     "blacked out; switching to another app pauses automatically.",
                     name=app_display_name(cfg) or exe, exe=exe)
        if self.source_override and self.source_override.lower() != "desktop":
            return L("录制来源：{src}", "Recording source: {src}", src=self.source_override)
        return L("录制主显示器（原始分辨率，仅一个显示器，与 Markov 样例一致）；其他显示器不会录入。",
                 "Recording the main display (native resolution, one display, like the Markov samples); "
                 "other monitors are not recorded.")

    def _stats_tooltip(self) -> str:
        key = display_state(self.state, self._last_snap)
        if key == "error" or (key == "idle" and self.error):
            return self.error
        if key in ("recording", "paused", "auto_paused") and self._last_snap.get("last_error"):
            return problem_details(self._last_snap["last_error"])
        if key == "done":
            details = report_details(self._delivery_report) if self._packaged else ""
            return self.last_out + ("\n\n" + details if details else "")
        return ""

    def _render_settings_texts(self) -> None:
        cfg = self.cfg
        if cfg.get("capture_mode") == "app":
            name = app_display_name(cfg)
            if name:
                self.app_lbl.configure(text=f"{shorten(name, 26)} · {cfg.get('app_exe', '')}", fg=TEXT)
            else:
                self.app_lbl.configure(text=L("尚未选择应用", "No app chosen yet"), fg=WARN)
            hint = L("其他窗口、通知、任务栏会被涂黑；切换到其他应用时自动暂停，切回后继续。视频仍为整屏分辨率。",
                     "Other windows, notifications and the taskbar are blacked out. Switching to another app "
                     "pauses automatically and resumes when you come back. The video keeps full-screen resolution.")
        else:
            self.app_lbl.configure(text="", fg=MUTED)
            hint = L("录制主显示器（原始分辨率），与 Markov 样例一致；其他显示器不会录入。",
                     "Records the main display at native resolution, like the Markov samples; "
                     "other monitors are not recorded.")
        if self._hint_text and time.monotonic() < self._hint_until:
            hint = self._hint_text()
            self.mode_hint.configure(text=hint, fg=WARN)
        else:
            self.mode_hint.configure(text=hint, fg=MUTED)
        self.backend_hint.configure(text=self._backend_description(cfg.get("backend", "auto")))
        self.data_lbl.configure(text=shorten_path(paths.data_dir(create=False), 56))
        self._show_overlay_row(cfg.get("capture_mode") == "app")

    def _show_overlay_row(self, show: bool) -> None:
        """The "Allowed overlays" field belongs to App-only mode."""
        for widget in (getattr(self, "overlay_lbl", None), getattr(self, "overlay_cell", None)):
            if widget is None:
                continue
            try:
                if show:
                    widget.grid()
                else:
                    widget.grid_remove()
            except Exception:
                pass

    def _sync_mode_widgets(self) -> None:
        app = self.cfg.get("capture_mode") == "app"
        self.seg_mode.set(self.cfg.get("capture_mode", "desktop"))
        self.seg_backend.set(self.cfg.get("backend", "auto"))
        self.seg_backend.set_option_enabled("gdigrab", not app and not self._locked)
        self._render_settings_texts()

    def _settings_changed(self) -> None:
        if self.state == "error" and self._error_is_config:
            self.state, self._error, self._error_is_config = "idle", "", False
        self._render_settings_texts()
        self._refresh()
        self._fit()

    def _flash_hint(self, fn: Callable[[], str], seconds: float = 6.0) -> None:
        self._hint_text = fn
        self._hint_until = time.monotonic() + seconds

    def _on_mode(self, mode: str) -> None:
        if self._locked:
            self.seg_mode.set(self.cfg["capture_mode"])
            return
        self.cfg["capture_mode"] = mode
        if mode == "app" and self.cfg.get("backend") == "gdigrab":
            self.cfg["backend"] = "auto"
            self._flash_hint(lambda: L("“仅应用”不能用 gdigrab，后端已改为 auto。",
                                       "gdigrab can't record “App only”; backend switched to auto."))
        self._save(capture_mode=mode, backend=self.cfg["backend"])
        self._sync_mode_widgets()
        self._settings_changed()
        if mode == "app" and not self.cfg.get("app_exe"):
            self.choose_app()

    def _on_fps(self, value: str) -> None:
        try:
            fps = int(value)
        except ValueError:
            return
        self.cfg["fps"] = fps
        self._save(fps=fps)
        self._settings_changed()

    def _on_backend(self, value: str) -> None:
        value = normalize_backend(self.cfg.get("capture_mode", "desktop"), value)
        self.cfg["backend"] = value
        self.seg_backend.set(value)
        self._save(backend=value)
        self._settings_changed()

    def _on_cursor(self, value: bool) -> None:
        self.cfg["draw_cursor"] = bool(value)
        self._save(draw_cursor=bool(value))
        self._settings_changed()

    def _on_autopack(self, value: bool) -> None:
        self.cfg["autopack"] = bool(value)
        self._save(autopack=bool(value))
        self._refresh()

    def _on_legacy(self, legacy: bool) -> None:
        markov = not legacy
        if markov == self._markov():
            return
        self.cfg["markov_delivery"] = markov
        self._save(markov_delivery=markov)
        self._refresh()
        self.refresh_report()  # the done line now counts the other format's missing items

    def _on_quality(self, _ev=None) -> None:
        text = self.e_quality.get().strip()
        try:
            q = int(text)
            ok = 0 <= q <= 51
        except ValueError:
            q, ok = 0, False
        self.e_quality.configure(fg=TEXT if ok else RED)
        if ok and q != self.cfg.get("quality"):
            self.cfg["quality"] = q
            self._save(quality=q)
            self._settings_changed()
        elif not ok:
            self.cfg["quality"] = -1  # validate_config reports it on start

    def _on_overlay_allow(self, _ev=None) -> None:
        """⚙ → Allowed overlays: comma-separated exe names, cleaned like ``app_allow``."""
        try:
            exes = sanitize_value("app_overlay_allow", self.e_overlay.get())
        except (KeyError, ValueError):
            exes = []
        if exes == list(self.cfg.get("app_overlay_allow") or []):
            return
        self.cfg["app_overlay_allow"] = exes
        if self._overlay_job is not None:
            try:
                self.root.after_cancel(self._overlay_job)
            except Exception:
                pass
        self._overlay_job = self.root.after(600, lambda: self._save(app_overlay_allow=self.cfg["app_overlay_allow"]))

    def _on_title(self, _ev=None) -> None:
        self._title = self.e_title.get().strip()
        self.cfg["title"] = self._title
        if self._title_job is not None:
            try:
                self.root.after_cancel(self._title_job)
            except Exception:
                pass
        self._title_job = self.root.after(600, lambda: self._save(title=self._title))

    def choose_app(self) -> None:
        if self._locked:
            return
        try:
            if self._picker is not None and self._picker.alive():
                self._picker.focus()
                return
            from .app_picker import AppPicker

            self._picker = AppPicker(self.root, self._on_app_picked, current_exe=self.cfg.get("app_exe", ""),
                                     scale=self.scale, fonts={"ui": self.f_ui, "small": self.f_small,
                                                              "bold": self.f_bold, "head": self.f_head},
                                     anchor=(self.root.winfo_x(), self.root.winfo_y() + self.root.winfo_height() + 6))
        except Exception as exc:
            self._log_exception("app picker")
            messagebox.showerror(L("无法打开应用列表", "Cannot open the app list"), str(exc), parent=self.root)

    def _on_app_picked(self, app: Dict[str, Any]) -> None:
        exe = str(app.get("exe") or "").strip()
        if not exe:
            return
        name = str(app.get("name") or "").strip() or exe_stem(exe)
        self.cfg.update(capture_mode="app", app_exe=exe, app_name=name)
        if self.cfg.get("backend") == "gdigrab":
            self.cfg["backend"] = "auto"
        self._save(capture_mode="app", app_exe=exe, app_name=name, backend=self.cfg["backend"])
        self._sync_mode_widgets()
        self._settings_changed()

    def change_data_dir(self) -> None:
        if self._locked:
            return
        current = paths.data_dir(create=False)
        chosen = filedialog.askdirectory(parent=self.root, initialdir=current if os.path.isdir(current) else None,
                                         title=L("选择保存录制的文件夹", "Choose the folder for recordings"))
        if not chosen:
            return
        chosen = os.path.abspath(chosen)
        try:
            os.makedirs(chosen, exist_ok=True)
            probe = os.path.join(chosen, ".dstudio-write-test")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
        except OSError as exc:
            messagebox.showerror(L("文件夹不可写", "Folder is not writable"),
                                 L("无法写入 {p}：\n{e}", "Cannot write to {p}:\n{e}", p=chosen, e=exc), parent=self.root)
            return
        self.cfg["data_dir"] = chosen
        if self._persist:
            self._save(data_dir=chosen)
        effective = paths.data_dir(create=False)
        if os.path.normcase(effective) != os.path.normcase(chosen):
            messagebox.showwarning(
                L("数据文件夹被覆盖", "Data folder overridden"),
                L("环境变量 DSTUDIO_DATA_DIR 指定了 {p}，它优先于此设置。",
                  "The DSTUDIO_DATA_DIR environment variable points to {p}; it takes precedence.", p=effective),
                parent=self.root)
        self._settings_changed()

    def open_data_folder(self) -> None:
        try:
            open_path(paths.data_dir())
        except Exception as exc:
            messagebox.showerror(L("无法打开文件夹", "Cannot open folder"), str(exc), parent=self.root)

    def _sync_settings_lock(self, locked: bool) -> None:
        if locked == self._locked:
            return
        self._locked = locked
        on = not locked
        try:
            self.seg_mode.set_enabled(on)
            self.seg_fps.set_enabled(on)
            self.seg_backend.set_enabled(on)
            self.seg_backend.set_option_enabled("gdigrab", on and self.cfg.get("capture_mode") != "app")
            self.chk_cursor.set_enabled(on)
            self.btn_choose_app.configure(state="normal" if on else "disabled")
            self.btn_data.configure(state="normal" if on else "disabled")
            self.e_quality.configure(state="normal" if on else "disabled")
            self.e_overlay.configure(state="normal" if on else "disabled")
            self.chk_legacy.set_enabled(on)
        except Exception:
            pass

    def _check_recorder_args(self) -> None:
        """Dry-run the recorder argv once so bad pass-through flags show early."""
        if self.screenshot:
            return
        cfg = dict(self.cfg)
        if cfg.get("capture_mode") == "app" and not cfg.get("app_exe"):
            cfg["capture_mode"] = "desktop"  # "no app chosen yet" is reported on start instead
        try:
            argv = build_record_argv(cfg, os.path.join(paths.data_dir(create=False), "rec-check"),
                                     ffmpeg=self.gui_args.ffmpeg or paths.ffmpeg_path(),
                                     codec=self.gui_args.codec, source_override=self.source_override,
                                     extra=self.gui_args.extra)
            parse_recorder_args(argv)
        except ImportError:
            pass  # recorder needs Windows; the GUI still renders elsewhere
        except ValueError as exc:
            self._fail(lambda e=str(exc): L("录制参数无效：{e}", "Invalid recording option: {e}", e=e), config=True)
        except Exception:
            pass

    # -------------------------------------------------------------- actions
    def on_main(self) -> None:
        if self.state in ACTIVE_STATES:
            busy = getattr(self, "_toggle_thread", None)
            if busy is not None and busy.is_alive():
                return  # a pause/resume is still switching FFmpeg segments
            rec = self.rec

            def toggle():
                try:
                    rec.toggle_pause()
                except Exception as exc:
                    self._log_exception("toggle pause")
                    self._set_error("暂停/继续失败：{e}", "Pause/resume failed: {e}", e=exc)

            # toggle_pause stops/starts an FFmpeg segment: keep the bar responsive
            self._toggle_thread = threading.Thread(target=toggle, name="dstudio-pause", daemon=True)
            self._toggle_thread.start()
            self._toggle_thread.join(0.05)
            self._fps.reset()
            self._refresh()
            return
        if self.state in BUSY_STATES:
            return
        self.start_recording()
        self._refresh()

    def start_recording(self) -> None:
        if self.state in ACTIVE_STATES or self.state in BUSY_STATES:
            return
        cfg = dict(self.cfg)
        cfg["title"] = self._title
        if validate_config(cfg):
            self._fail(lambda c=cfg: validate_config(c), config=True)
            return
        ffmpeg = self.gui_args.ffmpeg or paths.ffmpeg_path()
        if not ffmpeg_available(ffmpeg):
            self._fail(lambda: L("找不到 FFmpeg（{p}）。请重新安装 D Studio，或用 --ffmpeg 指定路径。",
                                 "FFmpeg not found ({p}). Reinstall D Studio or pass --ffmpeg PATH.", p=ffmpeg),
                       config=True)
            return
        try:
            base = paths.data_dir(create=True)
        except OSError as exc:
            self._fail(lambda e=str(exc): L("无法创建数据文件夹：{e}", "Cannot create the data folder: {e}", e=e), config=True)
            return
        free = disk_free_bytes(base)
        if free is not None and free < MIN_FREE_START_BYTES:
            self._fail(lambda gb=round(free / 1024 ** 3, 1), p=base: L(
                "磁盘空间不足：{p} 所在磁盘只剩 {gb} GB（至少需要 3 GB）。请清理磁盘，或在 ⚙ 中换一个数据文件夹。",
                "Not enough disk space: only {gb} GB left on the drive of {p} (3 GB needed). "
                "Free some space or choose another data folder under ⚙.", gb=gb, p=p), config=True)
            return
        out = new_output_dir(base)
        session_info = dict(self._session_info)
        # Review confirmations never carry over from a previous recording.
        for key in ("task_completed", "sync_verified", "quality_reviewed"):
            session_info[key] = False
        try:
            os.makedirs(out, exist_ok=False)
            session_path = os.path.join(out, "session_info.json")
            with open(session_path, "w", encoding="utf-8") as fh:
                json.dump(session_info, fh, ensure_ascii=False, indent=2)
            argv = build_record_argv(cfg, out, session_path=session_path, ffmpeg=ffmpeg,
                                     codec=self.gui_args.codec, source_override=self.source_override,
                                     extra=self.gui_args.extra)
            args = parse_recorder_args(argv)
            from .recorder import Recorder

            rec = Recorder(args)
            rec.quiet = True
        except Exception as exc:
            self._log_exception("start recording")
            self._cleanup_failed_out(out)
            self._fail(lambda e=str(exc) or type(exc).__name__: L("无法开始录制：{e}", "Could not start recording: {e}", e=e))
            return

        self._session_info = session_info
        self.rec = rec
        self._error, self._error_is_config = "", False
        self._finalized = False
        self._packaged = False
        self._delivery_report = {}
        self._report_sig = self._report_markov = None
        self.last_out = out
        self._rec_cfg = cfg
        self._fps.reset()
        self._last_snap = {}
        self.state = "recording"
        if self._picker is not None:
            try:
                self._picker.close()
            except Exception:
                pass
        self.thread = threading.Thread(target=self._run_recorder, name="dstudio-recorder", daemon=True)
        self.thread.start()
        self._refresh()

    def _cleanup_failed_out(self, out: str) -> None:
        """Remove a just-created output folder if nothing was recorded into it."""
        try:
            for folder, _dirs, files in os.walk(out):
                for name in files:
                    if name != "session_info.json" and os.path.getsize(os.path.join(folder, name)) > 0:
                        return
            shutil.rmtree(out, ignore_errors=True)
        except OSError:
            pass

    def _run_recorder(self) -> None:
        try:
            self.rec.run()
        except Exception as exc:
            try:
                with open(os.path.join(self.last_out, "recording-error.log"), "w", encoding="utf-8") as fh:
                    fh.write(traceback.format_exc())
            except OSError:
                pass
            self._log_exception("recorder run")
            self._fail(lambda e=str(exc) or type(exc).__name__: L("录制失败：{e}", "Recording failed: {e}", e=e))
            return
        if self.state in ("recording", "paused", "stopping"):
            # stopped via hotkey (F10), max-seconds or the Stop button
            self._after_stopped()

    def _after_stopped(self) -> None:
        with self._final_lock:
            if self._finalized:
                return
            self._finalized = True
        self._busy_since = time.monotonic()
        if self.cfg.get("autopack", True):
            self._transition("packing")
            threading.Thread(target=self._package_worker, name="dstudio-package", daemon=True).start()
        else:
            self._transition("done")

    def stop_and_pack(self) -> None:
        if not self._transition("stopping", only_from=ACTIVE_STATES):
            return
        self._busy_since = time.monotonic()
        threading.Thread(target=self._stop_worker, name="dstudio-stop", daemon=True).start()
        self._refresh()

    def _stop_worker(self) -> None:
        try:
            self.rec.stop()
            if self.thread:
                self.thread.join(timeout=20)
        except Exception as exc:
            self._log_exception("stop")
            self._fail(lambda e=str(exc): L("停止失败：{e}", "Stopping failed: {e}", e=e))
            return
        self._after_stopped()

    def _package_worker(self) -> None:
        self._transition("done" if self._package_now() else "error")

    def _package_now(self) -> bool:
        """Build the dataset next to raw/ (blocking; worker threads only)."""
        out = self.last_out
        markov = self._markov()
        try:
            package_recording(out, title=self._title, session_info=self._session_info, markov=markov)
        except ImportError as exc:
            self._log_exception("import packager")
            self._fail(lambda e=str(exc): L("找不到打包模块：{e}", "Packager not available: {e}", e=e))
            return False
        except (SystemExit, Exception) as exc:
            self._log_exception("package")
            self._fail(lambda e=str(exc) or type(exc).__name__: L("打包失败：{e}", "Packaging failed: {e}", e=e))
            return False
        self._packaged = True
        self._error, self._error_is_config = "", False
        self._load_report(markov)
        return True

    def _ensure_packaged(self) -> bool:
        return self._packaged or self._package_now()

    def _load_report(self, markov: Optional[bool] = None) -> None:
        """Read ``validation.json`` (written by the packager / an export with ``markov``)."""
        out = self.last_out
        sig = narration_signature(out)
        try:
            with open(os.path.join(out, "validation.json"), encoding="utf-8-sig") as fh:
                report = json.load(fh)
            if isinstance(report, dict):
                self._delivery_report = report
                self._report_sig = sig
                self._report_markov = self._markov() if markov is None else bool(markov)
                self._report_gen += 1
        except (OSError, ValueError):
            pass
        if self._report_markov is not None and self._report_markov != self._markov():
            # e.g. "Export recording" validates without the task set: re-check in the chosen
            # format on the first tick back in the done state
            self._report_ticks = self.REPORT_CHECK_TICKS

    def refresh_report(self) -> bool:
        """Re-check a finished recording on a worker thread when ``narration.json``
        changed since the report was made (saved from the annotator) or the
        delivery format was switched.  Quick check: no full decode, nothing is
        written; the done line and its tooltip pick up the new report."""
        if (self.state != "done" or not self._packaged or not self._has_output()
                or self._report_busy or getattr(self, "screenshot", False)):
            return False
        out, known_sig, known_markov = self.last_out, self._report_sig, self._report_markov
        gen, markov = self._report_gen, self._markov()
        task_set = self._session_info.get("task_set")
        self._report_busy = True

        def work():
            report, sig = None, None
            try:
                sig = narration_signature(out)
                if sig != known_sig or markov != known_markov:
                    from packager.validate import validate_dataset

                    result = validate_dataset(out, decode=False, task_set=task_set, markov=markov)
                    report = result if isinstance(result, dict) else None
            except Exception:
                self._log_exception("refresh report")
            finally:
                self._post(lambda: self._report_refreshed(out, gen, sig, markov, report))

        try:
            threading.Thread(target=work, name="dstudio-report", daemon=True).start()
        except Exception:
            self._report_busy = False
            raise
        return True

    def _report_refreshed(self, out: str, gen: int, sig: Optional[Tuple[int, int]], markov: bool,
                          report: Optional[Dict[str, Any]]) -> None:
        """Tk thread: apply a :meth:`refresh_report` result unless the recording
        changed or a newer report (packaging / export) was loaded meanwhile."""
        self._report_busy = False
        if out != self.last_out:
            return
        if self.state == "done" and gen == self._report_gen:
            if report is not None:
                self._delivery_report = report
                self._report_gen += 1
            # remembered even if the check failed, so a broken dataset is not re-probed every few seconds
            self._report_sig, self._report_markov = sig, markov
        if self._report_markov is not None and self._report_markov != self._markov():
            self.refresh_report()  # format switched again while checking

    def _run_task(self, busy_state: str, fn: Callable[[], None], title: Callable[[], str]) -> None:
        """Run ``fn`` on a worker while the bar shows ``busy_state``."""
        prev = self.state
        self.state = busy_state
        self._busy_since = time.monotonic()

        def work():
            try:
                fn()
            except Exception as exc:
                self._log_exception(f"task {busy_state}")
                self._post(lambda e=exc: self._show_task_error(title(), e))
            finally:
                self.state = "done" if (self._packaged or prev == "done") else prev

        threading.Thread(target=work, name=f"dstudio-{busy_state}", daemon=True).start()
        try:
            self._refresh()
        except Exception:
            pass

    def _show_task_error(self, title: str, exc: BaseException) -> None:
        if self._session_ending:   # nobody can answer a dialog while Windows ends the session
            self._log(f"{title}: {exc}")
            return
        if isinstance(exc, (ValueError, RuntimeError, OSError)) and str(exc):
            text = str(exc)  # packager / server messages are already user-readable
        else:
            text = L("{t}：{e}", "{t}: {e}", t=type(exc).__name__, e=exc)
        messagebox.showwarning(title, text[:3000], parent=self.root)

    def _has_output(self) -> bool:
        return bool(self.last_out) and os.path.isdir(self.last_out)

    def _ask_export_parent(self, title: str) -> str:
        initial = self.cfg.get("export_dir") or os.path.expanduser("~")
        chosen = filedialog.askdirectory(parent=self.root, title=title,
                                         initialdir=initial if os.path.isdir(initial) else None)
        if chosen:
            chosen = os.path.abspath(chosen)
            self.cfg["export_dir"] = chosen
            self._save(export_dir=chosen)
        return chosen or ""

    def export_recording(self) -> None:
        if self.state not in ("done", "error") or not self._has_output():
            return
        parent = self._ask_export_parent(L("选择导出位置（会在其中新建一个文件夹）",
                                           "Choose where to export (a new folder is created inside)"))
        if not parent:
            return
        dataset = self.last_out
        target = unique_path(os.path.join(parent, os.path.basename(dataset)))
        if is_inside(target, dataset):
            messagebox.showwarning(L("位置无效", "Invalid location"),
                                   L("请选择录制文件夹之外的位置。", "Choose a folder outside the recording."),
                                   parent=self.root)
            return

        def fn():
            if not self._ensure_packaged():
                raise RuntimeError(self.error)
            try:
                from packager.export_cad import export_recording
            except ImportError as exc:
                raise RuntimeError(L("导出模块不可用：{e}", "Export module not available: {e}", e=exc))
            result = export_recording(dataset, target) or target
            self._load_report(markov=False)  # the recording-only check skips the task set
            report = dict(self._delivery_report)
            self._post(lambda: self._export_done(str(result), L(
                "已导出录制（clip.mp4、events.json、frame_events.json、metadata.json、narration.json）到：",
                "Exported the recording (clip.mp4, events.json, frame_events.json, metadata.json, narration.json) to:"),
                report))

        self._run_task("exporting", fn, lambda: L("导出未完成", "Export not completed"))

    def export_delivery(self) -> None:
        if self.state != "done" or not self._has_output():
            messagebox.showinfo(L("尚未完成录制", "No finished recording"),
                                L("请先完成一条录制。", "Finish a recording first."), parent=self.root)
            return
        parent = self._ask_export_parent(L("选择交付包的保存位置", "Choose where to save the delivery"))
        if not parent:
            return
        dataset = self.last_out
        target = unique_path(os.path.join(parent, "CAD-" + os.path.basename(dataset)))
        if is_inside(target, dataset):
            messagebox.showwarning(L("位置无效", "Invalid location"),
                                   L("请选择录制文件夹之外的位置。", "Choose a folder outside the recording."),
                                   parent=self.root)
            return
        task_set = self._session_info.get("task_set")
        markov = self._markov()

        def fn():
            if not self._ensure_packaged():
                raise RuntimeError(self.error)
            from packager.export_cad import export_cad

            try:
                result = export_cad(dataset, target, task_set, markov=markov) or target
            finally:
                self._load_report(markov=markov)
            report = dict(self._delivery_report)
            lead = (L("Markov CAD 交付包（10 个文件）已导出到：", "Markov CAD delivery (10 files) exported to:") if markov
                    else L("旧版交付包已导出到：", "Legacy delivery exported to:"))
            self._post(lambda: self._export_done(str(result), lead, report))

        self._run_task("exporting", fn, lambda: L("尚未达到交付条件", "Delivery requirements not met yet"))

    def _export_done(self, path: str, lead: str, report: Optional[Dict[str, Any]] = None) -> None:
        if self._session_ending:
            self._log(f"export finished: {path}")
            return
        if messagebox.askyesno(L("导出完成", "Export complete"), export_done_text(lead, path, report),
                               parent=self.root):
            open_path(path)

    def edit_session(self) -> None:
        if self.state not in ("idle", "done", "error"):
            return
        from .session_form import edit_session

        current = self.state == "done" and self._has_output()
        path = os.path.join(self.last_out, "session_info.json") if current else self._session_template

        def saved(info: Dict[str, Any]) -> None:
            self._session_info = info
            if current:
                def repackage():
                    self._packaged = False
                    if not self._package_now():
                        raise RuntimeError(self.error)
                self._run_task("packing", repackage, lambda: L("打包失败", "Packaging failed"))

        try:
            edit_session(self.root, path, saved, markov=self._markov())
        except Exception as exc:
            self._log_exception("session form")
            messagebox.showerror(L("无法打开表单", "Cannot open the form"), str(exc), parent=self.root)

    def open_folder(self) -> None:
        target = self.last_out if self._has_output() else paths.data_dir()
        open_path(target)

    # ------------------------------------------------------------ annotator
    def open_annotator(self) -> None:
        if self.state in ACTIVE_STATES or self.state in BUSY_STATES:
            return
        dataset = os.path.basename(self.last_out) if self._has_output() else ""
        need_pack = bool(dataset) and not self._packaged

        def fn():
            if need_pack and not self._ensure_packaged():
                raise RuntimeError(self.error)
            port = self._ensure_viewer(dataset)
            url = viewer_url(port, dataset)
            # ShellExecute from the Tk thread (COM is not initialised on workers)
            self._post(lambda: webbrowser.open(url))

        title = lambda: L("无法打开标注台", "Cannot open the annotator")  # noqa: E731
        if need_pack:
            self._run_task("packing", fn, title)
        else:
            def work():
                try:
                    fn()
                except Exception as exc:
                    self._log_exception("annotator")
                    self._post(lambda e=exc: self._show_task_error(title(), e))
            threading.Thread(target=work, name="dstudio-annotator", daemon=True).start()

    def _ensure_viewer(self, dataset: str) -> int:
        """Port of a viewer server that serves our data folder (start one in-process)."""
        with self._server_lock:
            data_dir = paths.data_dir()
            if self._server is not None:
                same_dir = (os.path.normcase(getattr(self, "_server_dir", "") or "")
                            == os.path.normcase(data_dir))
                if same_dir and (self._server_thread is None or self._server_thread.is_alive()):
                    return int(self._server_port)
                self._stop_viewer()  # data folder changed or the server died
            port0 = int(self.cfg.get("viewer_port") or 8000)
            if dataset and _server_lists_dataset(port0, dataset):
                return port0  # e.g. START-ALL.bat already runs tools/serve.py
            serve = _load_serve_module()
            try:
                result = serve.start_server(port=port0, data_dir=data_dir, open_browser=False)
            except TypeError:
                result = serve.start_server(port0, data_dir)
            server, thread = (tuple(result) + (None,))[:2] if isinstance(result, tuple) else (result, None)
            if thread is None:
                thread = threading.Thread(target=server.serve_forever, name="dstudio-viewer", daemon=True)
                thread.start()
            port = int(server.server_address[1])
            self._server, self._server_thread, self._server_port = server, thread, port
            self._server_dir = data_dir
            return port

    def _stop_viewer(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        try:
            serve = _load_serve_module()
            if hasattr(serve, "stop_server"):
                serve.stop_server(server, timeout=2.0)
                return
        except Exception:
            pass
        try:
            t = threading.Thread(target=server.shutdown, daemon=True)
            t.start()
            t.join(2.0)
            server.server_close()
        except Exception:
            pass

    # ------------------------------------------------------- crash recovery
    def _start_recovery_scan(self) -> bool:
        """Start-up: look for recordings that never finished (crash, power cut,
        killed process) on a worker; :meth:`_offer_recovery` asks about them.
        Never in ``--screenshot`` mode (CI) and only once per bar."""
        if getattr(self, "screenshot", False) or self._recovery_scanned:
            return False
        self._recovery_scanned = True
        dismissed = list(self.cfg.get("recovery_dismissed") or [])
        try:
            data_dir = paths.data_dir(create=False)
        except Exception:
            return False

        def work():
            try:
                found = unfinished_recordings(data_dir, dismissed)
            except Exception:
                self._log_exception("recovery scan")
                return
            if found:
                self._post(lambda: self._offer_recovery(found))

        self._recovery_thread = threading.Thread(target=work, name="dstudio-recovery-scan", daemon=True)
        self._recovery_thread.start()
        return True

    def _offer_recovery(self, found: Sequence[str]) -> None:
        """Tk thread: ask once whether to package the unfinished recordings."""
        if self._closing:
            return
        if self.state in ACTIVE_STATES or self.state in BUSY_STATES:
            # never over a running recording (dialogs are not hidden from capture)
            self._recovery_pending = list(found)
            return
        current = os.path.normcase(os.path.abspath(self.last_out)) if self.last_out else ""
        folders = [f for f in found
                   if os.path.normcase(os.path.abspath(f)) != current and recording_unfinished(f)]
        if not folders:
            return
        if messagebox.askyesno(L("恢复未完成的录制", "Recover unfinished recordings"),
                               recovery_question(folders), parent=self.root):
            self.recover_recordings(folders)
        else:
            dismissed = list(self.cfg.get("recovery_dismissed") or [])
            dismissed += [n for n in (os.path.basename(os.path.normpath(f)) for f in folders) if n not in dismissed]
            self.cfg["recovery_dismissed"] = dismissed
            self._save(recovery_dismissed=dismissed)

    def recover_recordings(self, folders: Sequence[str]) -> bool:
        """Package ``folders`` on a worker (normal packaging path, current
        delivery format) while the bar shows "Packaging…"."""
        with self._state_lock:
            prev = self.state
            if prev not in ("idle", "done", "error"):
                return False
            self.state = "packing"
        self._busy_since = time.monotonic()
        markov = self._markov()
        folders = list(folders)

        def work():
            results: List[Tuple[str, bool, str]] = []
            try:
                for folder in folders:
                    results.append(recover_recording(folder, markov=markov))
            except Exception:
                self._log_exception("recovery")
            finally:
                self._post(lambda: self._recovery_done(results, prev))

        threading.Thread(target=work, name="dstudio-recovery", daemon=True).start()
        try:
            self._refresh()
        except Exception:
            pass
        return True

    def _recovery_done(self, results: Sequence[Tuple[str, bool, str]], prev: str) -> None:
        """Tk thread: the newest recovered recording becomes the bar's finished
        recording (if the bar has none), then a summary is shown."""
        recovered = [f for f, ok, _why in results if ok]
        failed = [(f, why) for f, ok, why in results if not ok]
        for f, why in failed:
            self._log(f"recovery of {f} failed: {why}")
        if recovered and (prev == "idle" or (prev == "error" and not self._has_output())):
            self._adopt_recording(max(recovered, key=lambda f: os.path.basename(os.path.normpath(f))))
        else:
            self._transition(prev, only_from=("packing",))
        if self._closing or self._session_ending or not results:
            return
        summary = recovery_summary(recovered, failed) + "\n\n" + L("现在打开文件夹吗？", "Open the folder now?")
        if messagebox.askyesno(L("恢复完成", "Recovery finished"), summary, parent=self.root):
            open_path(recovered[0] if len(recovered) == 1 else paths.data_dir())

    def _adopt_recording(self, folder: str) -> None:
        """Make a packaged recording the bar's "done" recording (Open annotator,
        Export recording, CAD export and Open folder then act on it)."""
        info = _read_json_dict(os.path.join(folder, "session_info.json"))
        if info is not None:
            self._session_info = info
        self.rec = FinishedRecording(folder)
        self.last_out = folder
        self._packaged = self._finalized = True
        self._error, self._error_is_config = "", False
        self._delivery_report = {}
        self._report_sig = self._report_markov = None
        self._fps.reset()
        self._last_snap = {}
        self._load_report(self._markov())
        self._transition("done")

    # ---------------------------------------------------------------- close
    def on_close(self) -> None:
        """✕ button and WM_DELETE_WINDOW (Alt+F4, ``taskkill`` without /F):
        a running recording is stopped and packaged first, then the bar closes.
        (The Restart Manager arrives as WM_SAVE_YOURSELF: :meth:`on_session_end`.)"""
        if self.state in BUSY_STATES:
            self._closing = True
            return
        if self._transition("stopping", only_from=ACTIVE_STATES):
            # Stop gracefully, package in the background, then close when done
            # (otherwise a long session could be quit before it is written out).
            self._closing = True
            self._busy_since = time.monotonic()
            threading.Thread(target=self._stop_worker, name="dstudio-stop", daemon=True).start()
            self._refresh()
            return
        self._finish_close()

    def on_session_end(self) -> None:
        """WM_SAVE_YOURSELF: Tk's translation of WM_QUERYENDSESSION, sent at
        sign-out / shutdown and by the installer's Restart Manager
        (ENDSESSION_CLOSEAPP, which never sends WM_CLOSE).

        There may be no time to package (and no one to answer dialogs): stop
        the recorder *synchronously* so FFmpeg finalizes the segment and
        raw/segments.json + capture.json are written before Windows ends the
        process; the raw folder can be packaged later.  If the process lives
        on (Restart Manager, or a cancelled sign-out), the normal stop path
        packages the recording and the bar then closes, like Alt+F4, without
        any dialog (nobody is there to answer it).  Already packaging or
        exporting: close once that is done.
        """
        if self.state in ("packing", "exporting"):
            self._closing = self._session_ending = True
            return
        rec = self.rec
        if rec is None or self.state not in ACTIVE_STATES + ("stopping",):
            return
        self._session_ending = True
        if self._transition("stopping", only_from=ACTIVE_STATES):
            self._busy_since = time.monotonic()
        finished = threading.Event()

        def stop():
            try:
                rec.stop()  # idempotent and blocking; joins an in-flight stop
            except Exception:
                self._log_exception("stop at sign-out")
            finally:
                finished.set()

        threading.Thread(target=stop, name="dstudio-stop-session-end", daemon=True).start()
        if not finished.wait(self.SESSION_END_STOP_S):
            self._log("sign-out: recorder did not finish stopping in time")
        # Restart Manager waits for the process to exit: close once packaged
        self._closing = True

    def _finish_close(self) -> bool:
        if self._session_ending:
            # sign-out / Restart Manager: a modal dialog would hold the exit and
            # nobody answers it; the annotator has to stop anyway
            if self.state == "error" and self.error:
                self._log("closing at session end after an error: " + self.error)
            self._stop_viewer()
            self.destroy()
            return True
        if self._closing and self.state == "error" and self.error:
            messagebox.showwarning(L("录制收尾出错", "Problem while finishing the recording"),
                                   self.error[:3000], parent=self.root)
        if self._server is not None and not self.screenshot:
            if not messagebox.askyesno(
                    L("关闭 D Studio？", "Close D Studio?"),
                    L("浏览器里的标注台会随之停止，未保存的标注将无法再保存。确定关闭吗？",
                      "The annotator in your browser stops with D Studio; unsaved annotations can no longer be "
                      "saved. Close anyway?"), parent=self.root):
                self._closing = False
                return False
        self._stop_viewer()
        self.destroy()
        return True

    def destroy(self) -> None:
        """Tear the window down without leaving Tcl ``after`` jobs behind."""
        try:
            i18n.off_change(self._lang_cb)
        except Exception:
            pass
        if getattr(self, "_overlay_job", None) is not None:   # typed just before closing
            self._save(app_overlay_allow=self.cfg.get("app_overlay_allow") or [])
        for name in ("_tick_job", "_title_job", "_overlay_job"):
            job = getattr(self, name, None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                setattr(self, name, None)
        try:
            self.root.destroy()
        except Exception:
            pass

    # -------------------------------------------------------------- refresh
    def _snapshot(self) -> Dict[str, Any]:
        """Latest ``Recorder.snapshot()`` without ever blocking the Tk thread.

        The recorder holds its lock while it stops/starts FFmpeg segments
        (pause, resume, app-mode auto-pause), which can take a second or more;
        the snapshot is fetched on a helper thread and the bar shows the last
        value until the fresh one arrives.
        """
        rec = self.rec
        if rec is None:
            return {}
        if getattr(self, "_snap_owner", None) is not rec:
            self._snap_owner, self._snap_cache, self._snap_thread = rec, {}, None
        thread = getattr(self, "_snap_thread", None)
        if thread is None or not thread.is_alive():
            def fetch(r=rec):
                try:
                    snap = r.snapshot()
                except Exception:
                    return
                if isinstance(snap, dict) and self._snap_owner is r:
                    self._snap_cache = snap
            thread = threading.Thread(target=fetch, name="dstudio-snapshot", daemon=True)
            self._snap_thread = thread
            thread.start()
        thread.join(0.05)
        return dict(self._snap_cache)

    def _drain_ui_queue(self) -> None:
        while self._ui_queue:
            fn = self._ui_queue.popleft()
            try:
                fn()
            except Exception:
                self._log_exception("ui task")

    def _tick(self) -> None:
        try:
            self._drain_ui_queue()
            if (self._recovery_pending and not self._closing and self.state not in ACTIVE_STATES
                    and self.state not in BUSY_STATES):
                found, self._recovery_pending = self._recovery_pending, None
                self._offer_recovery(found)
            self._refresh()
            if self.state == "done":
                self._report_ticks += 1
                if self._report_ticks >= self.REPORT_CHECK_TICKS and self.refresh_report():
                    self._report_ticks = 0  # narration.json saved from the annotator?
        except Exception:
            self._log_exception("tick")
        if (self._closing and self.state not in ACTIVE_STATES and self.state not in BUSY_STATES):
            if self._finish_close():
                return
        try:
            self._tick_job = self.root.after(self.TICK_MS, self._tick)
        except Exception:
            pass

    def _refresh(self) -> None:
        snap = self._snapshot()
        if self.state in ACTIVE_STATES and self.rec is not None:
            paused = bool(snap.get("paused", getattr(self.rec, "paused", False)))
            self._transition("paused" if paused else "recording", only_from=ACTIVE_STATES)
        self._last_snap = snap
        key = display_state(self.state, snap)
        cfg = self._rec_cfg if key in ("recording", "paused", "auto_paused", "stopping") else self.cfg

        # timer + dot
        if key == "idle":
            self.time_lbl.configure(text="00:00.0", fg=TEXT)
        else:
            self.time_lbl.configure(text=fmt_elapsed(snap.get("elapsed_ms", 0.0)),
                                    fg=MUTED if key in ("paused", "auto_paused") else TEXT)
        self._dot_on = not self._dot_on
        dot = {"recording": RED if self._dot_on else DOT_REC_DIM,
               "paused": WARN, "auto_paused": WARN if self._dot_on else "#7a6326",
               "stopping": WARN, "packing": ACCENT, "exporting": ACCENT,
               "done": OK, "error": RED}.get(key, DOT_IDLE)
        self.dot.configure(fg=dot)

        # state + mode chip + stats
        app = app_display_name(cfg) or snap.get("target_exe") or ""
        text, color = state_text(key, app=str(app or ""), packaged=self._packaged, closing=self._closing,
                                 privacy=is_privacy_pause(snap, cfg))
        self.state_lbl.configure(text=text, fg=color)
        chip, chip_color = mode_chip(cfg, self.source_override)
        self.mode_lbl.configure(text=chip, fg=chip_color)
        live = None
        if key == "recording":
            live = self._fps.add(time.monotonic(), int(snap.get("frames", 0) or 0))
        elif key in ("paused", "auto_paused"):
            self._fps.reset()
        busy_s = time.monotonic() - self._busy_since if self._busy_since else 0.0
        stats, stats_color = stats_text(
            key, snap, cfg=cfg, live_fps=live, capture_excluded=self.capture_excluded, error=self.error,
            report=self._delivery_report, packaged=self._packaged,
            out_name=os.path.basename(self.last_out or ""), busy_s=busy_s)
        self.stats.configure(text=stats, fg=stats_color)
        self._sync_buttons(key)

    def _sync_buttons(self, key: Optional[str] = None) -> None:
        if key is None:
            key = display_state(self.state, getattr(self, "_last_snap", {}))
        has_output = self._has_output()
        states = button_states(key, autopack=bool(self.cfg.get("autopack", True)), has_output=has_output,
                               markov=self._markov())
        for name, attr in (("main", "btn_main"), ("stop", "btn_stop"), ("annotator", "btn_annotator"),
                           ("export_rec", "btn_export_rec"), ("session", "btn_session"),
                           ("export_cad", "btn_export"), ("folder", "btn_folder")):
            widget = getattr(self, attr, None)
            if widget is None:
                continue
            st = states[name]
            widget.configure(text=st["text"], state="normal" if st["enabled"] else "disabled", style=st["style"])
        for attr, name in (("btn_session_tpl", "session"), ("btn_annotator_tpl", "annotator")):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.configure(state="normal" if states[name]["enabled"] else "disabled")
        self._sync_results(bool(states["show_results"]))
        if hasattr(self, "seg_mode"):
            self._sync_settings_lock(bool(states["settings_locked"]))

    def _sync_results(self, show: bool) -> None:
        row = getattr(self, "result_row", None)
        if row is None or show == self._results_shown:
            return
        self._results_shown = show
        try:
            if show:
                row.pack(fill="x", padx=self.px(10), pady=(0, self.px(8)), after=self.info)
            else:
                row.pack_forget()
            self._fit()
        except Exception:
            pass

    # ------------------------------------------------------------ demo/CI
    def apply_demo_state(self, name: str) -> None:
        if name in ("recording-demo", "autopaused-demo", "paused-demo"):
            self._rec_cfg = dict(self.cfg, capture_mode="app", app_exe="SLDWORKS.exe", app_name="SOLIDWORKS",
                                 backend="dxcam")
            self.rec = DemoRecorder(auto_paused=name == "autopaused-demo", paused=name == "paused-demo")
            self.state = "paused" if name != "recording-demo" else "recording"
        elif name == "done-demo":
            self.rec = DemoRecorder(stopped=True)
            self.last_out = os.path.join(paths.data_dir(create=False), "rec-20261118-101500")
            self._has_output = lambda: True  # type: ignore[assignment]
            self._packaged = True
            self._delivery_report = {"technical_ok": True, "technical_errors": [],
                                     "delivery_missing": ["task_set", "license_reference"],
                                     "warnings": [i18n.both("帧率为 60 fps；Markov 样例为 30 fps，交付前请与客户确认",
                                                            "Frame rate is 60 fps; Markov samples are 30 fps - "
                                                            "confirm with the customer before delivery")]}
            self.state = "done"
        self._refresh()
        self._fit()

    def run(self) -> int:
        self.root.mainloop()
        return 0


# ------------------------------------------------------------ viewer helpers
def _no_proxy_opener():
    import urllib.request

    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _server_lists_dataset(port: int, dataset: str) -> bool:
    try:
        with _no_proxy_opener().open(f"http://127.0.0.1:{int(port)}/api/datasets", timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return False
    items = data.get("datasets", []) if isinstance(data, dict) else data
    return any(isinstance(d, dict) and d.get("id") == dataset for d in items or [])


_serve_module = None


def _load_serve_module():
    """``tools/serve.py`` (by package import, else by file path)."""
    global _serve_module
    if _serve_module is not None:
        return _serve_module
    import importlib
    import importlib.util

    try:
        mod = importlib.import_module("tools.serve")
        if hasattr(mod, "start_server"):
            _serve_module = mod
            return mod
    except Exception:
        pass
    path = os.path.join(ROOT, "tools", "serve.py")
    spec = importlib.util.spec_from_file_location("dstudio_tools_serve", path)
    if spec is None or spec.loader is None:
        raise ImportError(L("找不到标注台服务 {p}", "Annotator server not found: {p}", p=path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dstudio_tools_serve"] = mod
    spec.loader.exec_module(mod)
    if not hasattr(mod, "start_server"):
        raise ImportError(L("标注台服务版本过旧（缺少 start_server）", "Annotator server is too old (no start_server)"))
    _serve_module = mod
    return mod


# ---------------------------------------------------------------- entry
def parse_gui_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m recorder gui",
                                description="D Studio floating recorder bar. Unknown options are passed to the recorder.")
    p.add_argument("--lang", choices=list(i18n.SUPPORTED), help="UI language for this run (not saved)")
    p.add_argument("--screenshot", metavar="OUT.png", help="render the bar, save a PNG and exit (CI)")
    p.add_argument("--expand", action="store_true", help="with --screenshot: open the settings panel")
    p.add_argument("--state", choices=list(DEMO_STATES), default="idle", help="with --screenshot: demo state")
    p.add_argument("--fps", type=int, help="initial frame rate (overrides settings)")
    p.add_argument("--quality", type=int, help="initial CRF (overrides settings)")
    p.add_argument("--backend", choices=list(BACKENDS), help="initial capture backend (overrides settings)")
    p.add_argument("--source", help="desktop | app:EXE | monitor:N | region:x,y,w,h | window:TITLE")
    p.add_argument("--ffmpeg", help="ffmpeg executable (default: bundled / PATH)")
    p.add_argument("--codec", default="auto", help="video encoder passed to the recorder")
    p.add_argument("--session-info", help="CAD session template JSON")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--markov-delivery", action="store_true",
                     help="CAD export in the Markov sample format (the default; overrides settings for this run)")
    fmt.add_argument("--legacy-delivery", action="store_true",
                     help="CAD export in the legacy task_set / clip_set / evidence layout for this run")
    p.add_argument("--data-dir", help="recordings folder for this run (like DSTUDIO_DATA_DIR)")
    p.add_argument("--open-data", action="store_true", help="open the recordings folder in Explorer and exit")
    p.add_argument("--no-single-instance", action="store_true", help=argparse.SUPPRESS)
    args, extra = p.parse_known_args(list(argv))
    args.extra = list(extra)
    return args


def run_screenshot(args: argparse.Namespace) -> int:
    out = os.path.abspath(args.screenshot)
    ui = OverlayUI(gui_args=args)
    result = {"code": 3}

    def shoot():
        try:
            ui._refresh()
            ui._fit()
            ui.root.lift()
            ui.root.update()
            method = grab_window_png(ui.root, out)
            print(f"[gui] screenshot ({method}, {ui.root.winfo_width()}x{ui.root.winfo_height()}, "
                  f"lang={i18n.get_language()}, state={args.state}, expand={args.expand}) -> {out}")
            result["code"] = 0
        except Exception as exc:
            print(f"[gui] screenshot failed: {exc}", file=sys.stderr)
            result["code"] = 3
        finally:
            ui._closing = False
            ui.root.after_cancel(guard)
            ui.destroy()

    def watchdog():
        print("[gui] screenshot timed out", file=sys.stderr)
        result["code"] = 4
        ui.destroy()

    if args.state and args.state != "idle":
        ui.apply_demo_state(args.state)
    if args.expand:
        ui.show_settings(True)
    ui.root.after(900, shoot)
    guard = ui.root.after(30000, watchdog)
    ui.root.mainloop()
    return result["code"]


def _fatal(message: str) -> None:
    sys.stderr and print(message, file=sys.stderr)
    if tk is None:
        return
    try:
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("D Studio", message, parent=r)
        r.destroy()
    except Exception:
        pass


def main(argv: Optional[list] = None) -> int:
    i18n.safe_stdio()
    args = parse_gui_args(list(sys.argv[1:] if argv is None else argv))
    if args.data_dir:
        os.environ["DSTUDIO_DATA_DIR"] = os.path.abspath(args.data_dir)
    if args.lang:
        i18n.set_language(args.lang, persist=False)
    if args.open_data:
        return open_data_folder()
    if tk is None:
        print("Tkinter is not available in this Python build", file=sys.stderr)
        return 2
    if args.screenshot:
        return run_screenshot(args)
    if not args.no_single_instance and not acquire_single_instance():
        _fatal(L("D Studio 录制条已经在运行（屏幕顶部的悬浮条）。",
                 "The D Studio recorder bar is already running (floating bar at the top of the screen)."))
        return 1
    try:
        return OverlayUI(gui_args=args).run()
    except Exception as exc:
        tb = traceback.format_exc()
        try:
            with open(os.path.join(paths.data_dir(), "dstudio-gui.log"), "a", encoding="utf-8") as fh:
                fh.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] fatal:\n{tb}\n")
        except Exception:
            pass
        _fatal(L("D Studio 启动失败：{e}", "D Studio failed to start: {e}", e=exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
