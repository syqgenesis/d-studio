"""Windows desktop introspection: monitors, foreground app, window geometry,
power/battery, memory pressure, dark-mode appearance, DPI awareness.

Import-safe on macOS/Linux (unit tests): every Win32 binding is created only
when ``sys.platform == "win32"``; the query helpers return neutral values
elsewhere.
"""
from __future__ import annotations

import ctypes
import itertools
import os
import sys
import time
from ctypes import wintypes
from typing import Dict, List, Optional, Tuple

IS_WINDOWS = sys.platform == "win32"

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITORINFOF_PRIMARY = 1
MDT_EFFECTIVE_DPI = 0
# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (pseudo handle)
DPI_CONTEXT_PER_MONITOR_V2 = -4
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
# Private key of Desktop.input_context(): the foreground PID at input time
# (never logged -- the recorder pops it before writing an event).
INPUT_PID_KEY = "_fgPid"

# Friendly names for the most common shells/apps; anything else falls back to
# the executable stem (e.g. "photoshop.exe" -> "photoshop").
FRIENDLY_NAMES = {
    "explorer.exe": "Windows Explorer",
    "brave.exe": "Brave Browser",
    "chrome.exe": "Google Chrome",
    "msedge.exe": "Microsoft Edge",
    "firefox.exe": "Firefox",
    "code.exe": "Visual Studio Code",
    "resolve.exe": "DaVinci Resolve",
    "photoshop.exe": "Adobe Photoshop",
    "premiere pro.exe": "Adobe Premiere Pro",
    "winword.exe": "Microsoft Word",
    "excel.exe": "Microsoft Excel",
    "powerpnt.exe": "Microsoft PowerPoint",
    "notepad.exe": "Notepad",
    "powershell.exe": "Windows PowerShell",
    "windowsterminal.exe": "Windows Terminal",
    "cmd.exe": "Command Prompt",
    "shellexperiencehost.exe": "ShellHost",
    "sldworks.exe": "SOLIDWORKS",
    "acad.exe": "AutoCAD",
    "inventor.exe": "Autodesk Inventor",
    "fusion360.exe": "Autodesk Fusion",
    "rhino.exe": "Rhino",
    "cnext.exe": "CATIA",
    "ugraf.exe": "Siemens NX",
    "xtop.exe": "Creo Parametric",
    "wechat.exe": "WeChat",
    "weixin.exe": "WeChat",
    "qq.exe": "QQ",
    "dingtalk.exe": "DingTalk",
}


def friendly_name(exe: str) -> str:
    base = os.path.basename(exe or "")
    return FRIENDLY_NAMES.get(base.lower(), os.path.splitext(base)[0] or "")


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", wintypes.BYTE),
        ("BatteryFlag", wintypes.BYTE),
        ("BatteryLifePercent", wintypes.BYTE),
        ("Reserved1", wintypes.BYTE),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    try:
        # reads the stored caption, never sends WM_GETTEXT (see Desktop._window_text)
        user32.InternalGetWindowText.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.InternalGetWindowText.restype = ctypes.c_int
    except AttributeError:  # pragma: no cover - present on every NT version
        pass
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(POINT)]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    for _name, _args, _res in (
        ("SetProcessDpiAwarenessContext", [ctypes.c_void_p], wintypes.BOOL),
        ("SetThreadDpiAwarenessContext", [ctypes.c_void_p], ctypes.c_void_p),
        ("GetThreadDpiAwarenessContext", [], ctypes.c_void_p),
        ("AreDpiAwarenessContextsEqual", [ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
        ("GetAwarenessFromDpiAwarenessContext", [ctypes.c_void_p], ctypes.c_int),
    ):
        _fn = getattr(user32, _name, None)  # Windows 10 1607+ only
        if _fn is not None:
            _fn.argtypes = _args
            _fn.restype = _res
    _WINFUNCTYPE = ctypes.WINFUNCTYPE
else:  # pragma: no cover - import-safe stub for unit tests on macOS/Linux
    user32 = kernel32 = None
    _WINFUNCTYPE = ctypes.CFUNCTYPE


# --------------------------------------------------------------------------- DPI
def enable_dpi_awareness() -> str:
    """Make the process per-monitor-v2 DPI aware; returns the awareness in effect.

    Must run before any geometry query, otherwise coordinates are virtualized
    (scaled) on high-DPI monitors and masks / cursor positions drift.  If the
    process awareness was already fixed (manifest or an earlier call), the
    current thread is still switched to per-monitor v2 where possible.
    """
    if not IS_WINDOWS:
        return "n/a"
    try:
        fn = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if fn is not None:
            fn(ctypes.c_void_p(DPI_CONTEXT_PER_MONITOR_V2))
        else:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except Exception:
            pass
    ensure_thread_dpi_awareness()
    name = dpi_awareness_name()
    if name == "unaware":
        try:
            user32.SetProcessDPIAware()  # Vista-era fallback: system aware
        except Exception:
            pass
        name = dpi_awareness_name()
    return name


def ensure_thread_dpi_awareness() -> bool:
    """Per-monitor-v2 for the *calling thread* (capture / watcher / hook threads).

    Cheap and idempotent; protects against a host process (e.g. Tk) that set a
    weaker process awareness before we could.
    """
    if not IS_WINDOWS:
        return False
    fn = getattr(user32, "SetThreadDpiAwarenessContext", None)
    if fn is None:
        return False
    try:
        return bool(fn(ctypes.c_void_p(DPI_CONTEXT_PER_MONITOR_V2)))
    except Exception:
        return False


def dpi_awareness_name() -> str:
    """'per_monitor_v2' | 'per_monitor' | 'system' | 'unaware' | 'unknown' for this thread."""
    if not IS_WINDOWS:
        return "n/a"
    try:
        ctx = user32.GetThreadDpiAwarenessContext()
        if user32.AreDpiAwarenessContextsEqual(ctx, ctypes.c_void_p(DPI_CONTEXT_PER_MONITOR_V2)):
            return "per_monitor_v2"
        return {0: "unaware", 1: "system", 2: "per_monitor"}.get(
            int(user32.GetAwarenessFromDpiAwarenessContext(ctx)), "unknown")
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------- monitors
def _monitors() -> List[Dict]:
    if not IS_WINDOWS:
        return []
    out: List[Dict] = []
    hmon_list: List[int] = []

    @_WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                  ctypes.POINTER(RECT), wintypes.LPARAM)
    def _cb(hmon, hdc, lprc, data):
        hmon_list.append(int(hmon or 0))
        return True

    user32.EnumDisplayMonitors(None, None, _cb, 0)
    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.GetDpiForMonitor.argtypes = [wintypes.HMONITOR, ctypes.c_int,
                                            ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
    except Exception:
        shcore = None

    for idx, hmon in enumerate(hmon_list):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(wintypes.HMONITOR(hmon), ctypes.byref(mi)):
            continue
        scale = 1.0
        if shcore is not None:
            dx, dy = wintypes.UINT(), wintypes.UINT()
            try:
                if shcore.GetDpiForMonitor(wintypes.HMONITOR(hmon), MDT_EFFECTIVE_DPI,
                                           ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value:
                    scale = dx.value / 96.0
            except Exception:
                pass
        out.append({
            "index": idx,
            "originX": mi.rcMonitor.left,
            "originY": mi.rcMonitor.top,
            "width": mi.rcMonitor.right - mi.rcMonitor.left,
            "height": mi.rcMonitor.bottom - mi.rcMonitor.top,
            "visibleWidth": mi.rcWork.right - mi.rcWork.left,
            "visibleHeight": mi.rcWork.bottom - mi.rcWork.top,
            "scaleFactor": round(scale, 4),
            "clipIndex": idx,
            "deviceName": mi.szDevice,
            "primary": bool(mi.dwFlags & MONITORINFOF_PRIMARY),
        })
    return out


def list_monitors() -> List[Dict]:
    """Fresh monitor list (physical px, virtual-screen coordinates)."""
    try:
        return _monitors()
    except Exception:
        return []


def virtual_screen_rect() -> Optional[Tuple[int, int, int, int]]:
    """(x, y, w, h) of the whole virtual desktop in physical px."""
    if not IS_WINDOWS:
        return None
    try:
        w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if w > 0 and h > 0:
            return (user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
                    user32.GetSystemMetrics(SM_YVIRTUALSCREEN), w, h)
    except Exception:
        pass
    return None


def screens_union(screens: List[Dict]) -> Optional[Tuple[int, int, int, int]]:
    if not screens:
        return None
    x0 = min(s["originX"] for s in screens)
    y0 = min(s["originY"] for s in screens)
    x1 = max(s["originX"] + s["width"] for s in screens)
    y1 = max(s["originY"] + s["height"] for s in screens)
    return (x0, y0, x1 - x0, y1 - y0)


def window_client_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """Client area of ``hwnd`` in screen px (what ffmpeg gdigrab ``title=`` grabs)."""
    if not IS_WINDOWS or not hwnd:
        return None
    try:
        r = RECT()
        if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(r)):
            return None
        pt = POINT(0, 0)
        if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt)):
            return None
        return (pt.x, pt.y, r.right - r.left, r.bottom - r.top)
    except Exception:
        return None


class Desktop:
    """Cached view of desktop state, refreshed by a light background poller."""

    def __init__(self, cache_ttl_ms: float = 100.0):
        self.screens = _monitors()
        self._ttl = cache_ttl_ms
        self._last = 0.0
        # (hwnd, app dict, read sequence) published as ONE object: a reader never
        # pairs one read's app with another read's hwnd (see foreground()).
        self._fg_cache: Tuple[int, Optional[Dict], int] = (0, None, 0)
        self._fg_seq = itertools.count(1)

    # tests and older callers use these two names; each write keeps the pair whole
    @property
    def _fg(self) -> Optional[Dict]:
        return self._fg_cache[1]

    @_fg.setter
    def _fg(self, value: Optional[Dict]) -> None:
        hwnd, _, seq = self._fg_cache
        self._fg_cache = (hwnd, value, seq)

    @property
    def _fg_hwnd(self) -> int:
        return self._fg_cache[0]

    @_fg_hwnd.setter
    def _fg_hwnd(self, value: int) -> None:
        _, fg, seq = self._fg_cache
        self._fg_cache = (int(value or 0), fg, seq)

    # ------------------------------------------------------------------ screens
    def screen_index_at(self, x: int, y: int) -> int:
        for s in self.screens:
            if (s["originX"] <= x < s["originX"] + s["width"]
                    and s["originY"] <= y < s["originY"] + s["height"]):
                return int(s["index"])
        return 0

    def screen_config_event(self, reason: str) -> Dict:
        self.screens = _monitors()
        return {"type": "screen_config", "screenCount": len(self.screens),
                "screens": self.screens, "reason": reason}

    # ---------------------------------------------------------------- foreground
    def foreground(self, force: bool = False) -> Dict:
        now = time.perf_counter() * 1000.0
        cached = self._fg_cache[1]
        if not force and cached is not None and now - self._last < self._ttl:
            return cached
        self._last = now
        if not IS_WINDOWS:
            fg = {"name": "", "bundleId": "", "windowTitle": "", "pid": 0,
                  "isFullScreen": False, "hwnd": 0, "windowClass": "", "exePath": ""}
            self._fg_cache = (0, fg, self._fg_cache[2])
            return fg
        seq = next(self._fg_seq)         # atomic: orders concurrent refreshes
        hwnd = user32.GetForegroundWindow() or 0
        title = self._window_text(hwnd)
        cls = self._class_name(hwnd)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = self._exe_path(pid.value) if pid.value else ""
        bundle = os.path.basename(exe) if exe else ""
        name = friendly_name(bundle)
        fg = {
            "name": name,
            "bundleId": bundle,
            "windowTitle": title,
            "pid": int(pid.value),
            "isFullScreen": self._is_fullscreen(hwnd),
            "hwnd": int(hwnd),
            "windowClass": cls,
            "exePath": exe,
        }
        # One store publishes hwnd + app together, and an older read that
        # finishes after a newer one never overwrites it (ctypes calls above
        # release the GIL, so the watcher and the input hook can interleave).
        if seq > self._fg_cache[2]:
            self._fg_cache = (int(hwnd), fg, seq)
        return fg

    def foreground_changed(self) -> bool:
        """True when the live foreground window is not the cached one.

        ``GetForegroundWindow`` is a cheap call, so input hooks can afford it on
        every event; the expensive refresh (title, process path) only runs when
        the window really changed.
        """
        cached_hwnd, cached, _ = self._fg_cache
        if not IS_WINDOWS or cached is None:
            return False
        try:
            return int(user32.GetForegroundWindow() or 0) != int(cached_hwnd or 0)
        except Exception:
            return True

    def event_context(self) -> Dict:
        """Cheap per-input-event context.

        Uses the cached foreground app, but re-reads it as soon as the live
        foreground window differs from the cached one: a key typed right after a
        focus switch must be attributed to the app that receives it, never to the
        app that was in front up to 100 ms earlier (the app-only key filter and
        the password-manager blocklist depend on it).
        """
        fg = self.foreground(force=self.foreground_changed())
        return {"appName": fg["name"], "bundleId": fg["bundleId"], "screen": 0}

    def input_context(self) -> Dict:
        """:meth:`event_context` plus the live foreground PID under
        :data:`INPUT_PID_KEY`, for the input hook: the recorder's app-only key
        filter needs it (helper processes of the target) and strips it before
        anything is logged."""
        fg = self.foreground(force=self.foreground_changed())
        return {"appName": fg["name"], "bundleId": fg["bundleId"], "screen": 0,
                INPUT_PID_KEY: int(fg.get("pid") or 0)}

    def window_rect(self) -> Dict:
        hwnd = self._fg_cache[0]
        if not hwnd or not IS_WINDOWS:
            return {}
        r = RECT()
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
            return {}
        return {"x": r.left, "y": r.top,
                "width": r.right - r.left, "height": r.bottom - r.top}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _window_text(hwnd: int) -> str:
        """Window caption via ``InternalGetWindowText``: it reads the stored text
        and never sends ``WM_GETTEXT``.  ``GetWindowTextW`` sends that message to
        windows of our own process (the GUI bar, its dialogs), which hangs the
        caller while the Tk thread is busy or blocked."""
        if not IS_WINDOWS or not hwnd:
            return ""
        buf = ctypes.create_unicode_buffer(1024)
        fn = getattr(user32, "InternalGetWindowText", None)
        if fn is not None:
            fn(wintypes.HWND(hwnd), buf, 1024)
            return buf.value
        # no InternalGetWindowText (never seen on NT): GetWindowTextW only for other
        # processes' windows, where it reads the caption without a message
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid():
            return ""
        user32.GetWindowTextW(wintypes.HWND(hwnd), buf, 1024)
        return buf.value

    @staticmethod
    def _class_name(hwnd: int) -> str:
        if not IS_WINDOWS or not hwnd:
            return ""
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
        return buf.value

    @staticmethod
    def _exe_path(pid: int) -> str:
        if not IS_WINDOWS or not pid:
            return ""
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            kernel32.CloseHandle(handle)

    def _is_fullscreen(self, hwnd: int) -> bool:
        r = RECT()
        if not hwnd or not IS_WINDOWS or not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
            return False
        for s in self.screens:
            if (abs(r.left - s["originX"]) < 3 and abs(r.top - s["originY"]) < 3
                    and abs((r.right - r.left) - s["width"]) < 3
                    and abs((r.bottom - r.top) - s["height"]) < 3):
                return True
        return False


def power_status() -> Dict:
    if not IS_WINDOWS:
        return {}
    st = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(st)):
        return {}
    return {
        "isOnAC": st.ACLineStatus == 1,
        "isCharging": st.ACLineStatus == 1,
        "batteryLevel": int(st.BatteryLifePercent) if st.BatteryLifePercent != 255 else -1,
    }


def memory_pressure() -> str:
    if not IS_WINDOWS:
        return "unknown"
    ms = MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
        return "unknown"
    load = ms.dwMemoryLoad
    if load >= 90:
        return "critical"
    if load >= 80:
        return "warning"
    return "normal"


def appearance() -> str:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            apps_light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        return "light" if apps_light else "dark"
    except Exception:
        return "unknown"


def find_window(title_substr: str) -> int:
    """Find a top-level window whose title contains ``title_substr``."""
    if not IS_WINDOWS:
        return 0
    matches: List[int] = []

    @_WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            t = Desktop._window_text(int(hwnd or 0))
            if title_substr.lower() in t.lower():
                matches.append(int(hwnd or 0))
        return True

    user32.EnumWindows(_cb, 0)
    return matches[0] if matches else 0
