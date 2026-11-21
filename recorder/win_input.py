"""Low-level Windows input capture (mouse + keyboard).

Design notes
------------
* ``WH_MOUSE_LL`` / ``WH_KEYBOARD_LL`` callbacks run on the thread that installed
  the hook, which must pump a message loop.  The callback itself must stay
  extremely cheap (a few microseconds) or it will stall *global* input, so we
  only parse the struct and push into a queue; a writer thread consumes it.
* ``GetCursorPos`` polling at ~250 Hz is kept as a fallback: some injected /
  remote-control inputs do not fire LL hooks.
* The callback timestamps with ``perf_counter`` at entry (QPC), never with
  ``GetTickCount`` from the hook struct (15.6 ms granularity).
"""
from __future__ import annotations

import ctypes
import queue
import sys
import threading
import time
from ctypes import wintypes
from typing import Callable, Dict, Optional

from .clock import wall_ms
from .i18n import L
from .raw_mouse import RawMouse
from .win_window import ensure_thread_dpi_awareness

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - import-safe stub for unit tests on macOS/Linux
    user32 = kernel32 = None

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
WM_MOUSEHWHEEL = 0x020E

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10
LLKHF_UP = 0x80
LLMHF_INJECTED = 0x01

MOD_SHIFT, MOD_CTRL, MOD_ALT, MOD_WIN, MOD_CAPS = 1, 2, 4, 8, 16
# A held Shift/Ctrl/Alt/Win turns F9/F10 into an application shortcut
# (Shift+F10 = context menu, Ctrl+F9 ...), never a recorder hotkey.
HOTKEY_BLOCKING_MODS = MOD_SHIFT | MOD_CTRL | MOD_ALT | MOD_WIN
# Auto-repeat key-downs of a held hotkey arrive at least every ~1 s (the
# slowest Windows "repeat delay"); a key-down later than this after the last
# one is a new press even if its key-up was never seen (secure desktop...).
HOTKEY_REPEAT_WINDOW_MS = 1500.0
WHEEL_DELTA = 120

VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_CAPITAL, VK_LWIN, VK_RWIN = 0x14, 0x5B, 0x5C
VK_LBUTTON, VK_RBUTTON, VK_MBUTTON, VK_XBUTTON1, VK_XBUTTON2 = 0x01, 0x02, 0x04, 0x05, 0x06
SM_SWAPBUTTON = 23


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


HOOKPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


def _bind() -> None:
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    user32.GetDoubleClickTime.restype = wintypes.UINT
    user32.GetKeyboardState.argtypes = [ctypes.POINTER(ctypes.c_ubyte)]
    user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
    user32.GetKeyboardLayout.restype = ctypes.c_void_p
    user32.ToUnicodeEx.argtypes = [
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_ubyte),
        wintypes.LPWSTR, ctypes.c_int, wintypes.UINT, ctypes.c_void_p,
    ]
    user32.ToUnicodeEx.restype = ctypes.c_int
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


if IS_WINDOWS:
    _bind()

_VK_BUTTONS = {
    WM_LBUTTONDOWN: "left", WM_LBUTTONUP: "left",
    WM_RBUTTONDOWN: "right", WM_RBUTTONUP: "right",
    WM_MBUTTONDOWN: "middle", WM_MBUTTONUP: "middle",
}


def cursor_pos() -> tuple[int, int]:
    if user32 is None:
        return 0, 0
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def modifiers_mask() -> int:
    if user32 is None:
        return 0
    m = 0
    if user32.GetAsyncKeyState(VK_SHIFT) & 0x8000:
        m |= MOD_SHIFT
    if user32.GetAsyncKeyState(VK_CONTROL) & 0x8000:
        m |= MOD_CTRL
    if user32.GetAsyncKeyState(VK_MENU) & 0x8000:
        m |= MOD_ALT
    if (user32.GetAsyncKeyState(VK_LWIN) | user32.GetAsyncKeyState(VK_RWIN)) & 0x8000:
        m |= MOD_WIN
    if user32.GetAsyncKeyState(VK_CAPITAL) & 1:
        m |= MOD_CAPS
    return m


def button_state() -> Dict[str, bool]:
    """Logical mouse buttons held right now (``left/right/middle/other_3/other_4``)."""
    state = {"left": False, "right": False, "middle": False, "other_3": False, "other_4": False}
    if user32 is None:
        return state
    try:
        down = lambda vk: bool(user32.GetAsyncKeyState(vk) & 0x8000)  # noqa: E731
        left, right = down(VK_LBUTTON), down(VK_RBUTTON)
        # GetAsyncKeyState reports *physical* buttons; the hook reports logical ones
        if user32.GetSystemMetrics(SM_SWAPBUTTON):
            left, right = right, left
        state.update(left=left, right=right, middle=down(VK_MBUTTON),
                     other_3=down(VK_XBUTTON1), other_4=down(VK_XBUTTON2))
    except Exception:
        pass
    return state


class InputHook:
    """Captures global mouse/keyboard events.

    Parameters
    ----------
    emit:
        Callback receiving a ready-to-log record dict (without seq/osTime).
    context:
        Zero-arg callable returning ``{"appName": str, "bundleId": str, "screen": int}``.
        Must be cheap (cached by the caller); it is called for every event.
    hotkeys:
        ``{vk_code: callable}``; callbacks run on a helper thread.  A hotkey
        fires only when no Shift/Ctrl/Alt/Win is held; the key-down/repeats/
        key-up it consumes are swallowed (never reach the foreground app) and
        never logged.  With a modifier held the key is an ordinary key.
    enabled:
        Initial state of the recording gate.  While disabled (recording paused,
        between segments) events are dropped *at capture time* -- nothing is
        queued or written -- but key/button state keeps being tracked and the
        global hotkeys keep working.  See :meth:`set_enabled`.
    """

    def __init__(
        self,
        emit: Callable[[Dict], None],
        context: Callable[[], Dict],
        hotkeys: Optional[Dict[int, Callable[[], None]]] = None,
        poll_hz: int = 250,
        enabled: bool = True,
    ):
        self._emit = emit
        self._context = context
        self._hotkeys = hotkeys or {}
        self._poll_interval = 1.0 / poll_hz

        self._queue: "queue.Queue[Dict]" = queue.Queue(maxsize=200_000)
        self._stop = threading.Event()
        self._thread_id: Optional[int] = None
        self._hooks: list = []
        self._procs: list = [_ for _ in ()]  # keep WINFUNCTYPE refs alive

        self._buttons = {"left": False, "right": False, "middle": False,
                         "other_3": False, "other_4": False}
        self._last_pos = cursor_pos()
        self._last_move_emit = 0.0
        self._double_click_ms = int(user32.GetDoubleClickTime())
        self._last_click = {"time": 0.0, "x": 0, "y": 0, "button": "", "count": 0}
        self._last_vk = {"vk": -1, "down_time": 0.0}
        self._pressed_keys = set()
        self._swallowed: Dict[int, float] = {}   # hotkey vk -> wall ms of its last swallowed key-down
        self.hotkeys_fired = 0
        self._ready = threading.Event()
        self._hook_error = ""
        self._enabled = bool(enabled)
        self.gated_events = 0       # dropped because recording was paused
        self.dropped_events = 0
        self.write_errors = 0
        self.callback_errors = 0
        self._raw_mouse = RawMouse(self._put, self._ctx)

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> "InputHook":
        self._writer = threading.Thread(target=self._drain, name="input-writer", daemon=True)
        self._writer.start()
        if self._enabled:  # the recorder emits its own snapshot when it starts gated
            self._put({"type": "mouse_move", **self._ctx(), "x": self._last_pos[0], "y": self._last_pos[1],
                       "timestamp": wall_ms(), "source": "initial_cursor", "synthetic": True})
        self._poller = threading.Thread(target=self._poll_cursor, name="cursor-poll", daemon=True)
        self._poller.start()
        self._hook_thread = threading.Thread(target=self._run_hooks, name="input-hooks", daemon=True)
        self._hook_thread.start()
        try:
            if not self._ready.wait(5) or self._hook_error:
                raise RuntimeError(L("键鼠监听初始化失败：", "Keyboard/mouse hook failed to start: ")
                                   + (self._hook_error or L("超时", "timeout")))
            self._raw_mouse.start()
        except Exception:
            self.stop()
            raise
        return self

    def set_enabled(self, enabled: bool) -> None:
        """Open/close the recording gate (cheap; called on pause/resume)."""
        self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def buttons(self) -> Dict[str, bool]:
        """Mouse buttons held now, as tracked by the hook (also while gated)."""
        return dict(self._buttons)

    def request_stop(self) -> None:
        self._stop.set()
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
        self._raw_mouse.request_stop()

    def stop(self) -> None:
        self.request_stop()
        self._hook_thread.join(timeout=2)
        self._poller.join(timeout=2)
        self._raw_mouse.stop()
        self._queue.put({"type": "__eof__"}, timeout=5)
        self._writer.join(timeout=10)
        if self._writer.is_alive() or self._hook_thread.is_alive():
            raise RuntimeError(L("键鼠日志线程未正常停止，当前录制不能交付",
                                 "The input logging thread did not stop cleanly; this recording cannot be delivered"))

    def health(self) -> Dict:
        return {"dropped_events": self.dropped_events, "write_errors": self.write_errors,
                "gated_events": self.gated_events,
                "callback_errors": self.callback_errors,
                "raw_mouse_available": self._raw_mouse.available,
                "raw_mouse_packets": self._raw_mouse.packets,
                "raw_mouse_errors": self._raw_mouse.errors, "hook_error": self._hook_error,
                "hotkeys_fired": self.hotkeys_fired}

    # ------------------------------------------------------------------ threads
    def _drain(self) -> None:
        while True:
            rec = self._queue.get()
            if rec.get("type") == "__eof__":
                return
            try:
                self._emit(rec)
            except Exception:
                self.write_errors += 1

    def _poll_cursor(self) -> None:
        """Fallback: emit a move if the hook went quiet but the cursor still moves."""
        ensure_thread_dpi_awareness()  # GetCursorPos is DPI-virtualized per thread
        while not self._stop.is_set():
            time.sleep(self._poll_interval)
            pos = cursor_pos()
            now = wall_ms()
            # 30ms of hook silence means the movement did not come through
            # WH_MOUSE_LL (remote desktop, SendInput, some virtual drivers).
            if pos != self._last_pos and now - self._last_move_emit > 30.0:
                self._on_move(*pos, synthetic_move=True, pressure=None, flags=LLMHF_INJECTED)

    def _run_hooks(self) -> None:
        ensure_thread_dpi_awareness()
        self._mouse_proc = HOOKPROC(self._mouse_cb)
        self._kb_proc = HOOKPROC(self._kb_cb)
        self._hooks.append(user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, None, 0))
        self._hooks.append(user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, None, 0))
        if not all(self._hooks):
            self._hook_error = str(ctypes.WinError(ctypes.get_last_error()))
        self._thread_id = kernel32.GetCurrentThreadId()
        self._ready.set()
        if self._hook_error:
            for h in self._hooks:
                if h:
                    user32.UnhookWindowsHookEx(h)
            self._hooks.clear()
            return
        if self._stop.is_set():
            user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        for h in self._hooks:
            if h:
                user32.UnhookWindowsHookEx(h)
        self._hooks.clear()

    # --------------------------------------------------------------- callbacks
    def _mouse_cb(self, code: int, wparam: int, lparam: int) -> int:
        if code >= 0:
            try:
                self._handle_mouse(wparam, ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents)
            except Exception:
                self.callback_errors += 1
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _kb_cb(self, code: int, wparam: int, lparam: int) -> int:
        if code >= 0:
            try:
                if self._handle_key(wparam, ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents):
                    return 1        # consumed as a recorder hotkey: the foreground app never sees it
            except Exception:
                self.callback_errors += 1
        return user32.CallNextHookEx(None, code, wparam, lparam)

    # ------------------------------------------------------------------ parsing
    def _put(self, rec: Dict) -> None:
        if self._stop.is_set():
            return
        if not self._enabled:
            self.gated_events += 1
            return
        try:
            self._queue.put_nowait(rec)
        except queue.Full:
            self.dropped_events += 1

    def _handle_mouse(self, wparam: int, ms: MSLLHOOKSTRUCT) -> None:
        now = wall_ms()
        x, y = int(ms.pt.x), int(ms.pt.y)
        injected = bool(ms.flags & LLMHF_INJECTED)

        if wparam == WM_MOUSEMOVE:
            self._on_move(x, y, synthetic_move=False, pressure=None, flags=ms.flags)
            # a drag sample is emitted alongside every move while a button is held
            held = next((b for b in ("left", "right", "middle", "other_3", "other_4")
                         if self._buttons[b]), None)
            if held:
                self._put({"type": "drag", **self._ctx(), "x": x, "y": y,
                           "button": held, "pressure": 1, "modifiers": modifiers_mask(),
                           "synthetic": injected, "timestamp": now})
            return

        if wparam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN,
                      WM_LBUTTONUP, WM_RBUTTONUP, WM_MBUTTONUP):
            button = _VK_BUTTONS[wparam]
            is_down = wparam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN)
            self._buttons[button] = is_down
            count = self._click_count(now, x, y, button) if is_down else self._last_click["count"]
            self._put({"type": "click", **self._ctx(), "x": x, "y": y, "button": button,
                       "isDown": is_down, "clickCount": count, "pressure": 1,
                       "modifiers": modifiers_mask(), "synthetic": injected, "timestamp": now})
            return

        if wparam in (WM_XBUTTONDOWN, WM_XBUTTONUP):
            hi = (ms.mouseData >> 16) & 0xFFFF
            button = "other_3" if hi == 1 else "other_4"
            is_down = wparam == WM_XBUTTONDOWN
            self._buttons[button] = is_down
            count = self._click_count(now, x, y, button) if is_down else 1
            self._put({"type": "click", **self._ctx(), "x": x, "y": y, "button": button,
                       "isDown": is_down, "clickCount": count, "pressure": 1,
                       "modifiers": modifiers_mask(), "synthetic": injected, "timestamp": now})
            return

        if wparam in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            raw = ctypes.c_short((ms.mouseData >> 16) & 0xFFFF).value
            delta = raw / WHEEL_DELTA
            horizontal = wparam == WM_MOUSEHWHEEL
            # positive = wheel up / right, negative = wheel down / left
            self._put({"type": "scroll", **self._ctx(), "x": x, "y": y,
                       "deltaX": delta if horizontal else 0,
                       "deltaY": 0 if horizontal else delta,
                       "phase": 0, "momentumPhase": 0, "isPrecise": False,
                       "channel": "wheel", "modifiers": modifiers_mask(),
                       "synthetic": injected, "timestamp": now})

    def _on_move(self, x: int, y: int, synthetic_move: bool, pressure, flags: int) -> None:
        now = wall_ms()
        self._last_pos = (x, y)
        self._last_move_emit = now
        self._put({"type": "mouse_move", **self._ctx(), "x": x, "y": y,
                   "synthetic": bool(flags & LLMHF_INJECTED),
                   "source": "cursor_poll" if synthetic_move else "hook",
                   "timestamp": now})

    def _consume_hotkey(self, vk: int, is_down: bool, repeat: bool, now: float) -> bool:
        """True when this key event is (part of) a recorder hotkey press: swallow it.

        Only a plain press fires (no Shift/Ctrl/Alt/Win held), so Shift+F10 or
        Ctrl+F9 keep working in the foreground app and are logged as keys.  The
        consumed key-down, its auto-repeats and its key-up are all swallowed.
        """
        if vk not in self._hotkeys:
            return False
        if not is_down:
            return self._swallowed.pop(vk, None) is not None
        last = self._swallowed.get(vk)
        if last is not None and now - last <= HOTKEY_REPEAT_WINDOW_MS:
            self._swallowed[vk] = now          # auto-repeat of a consumed press
            return True
        self._swallowed.pop(vk, None)          # stale (key-up never seen): a new press
        if repeat or modifiers_mask() & HOTKEY_BLOCKING_MODS:
            return False
        self._swallowed[vk] = now
        self.hotkeys_fired += 1
        threading.Thread(target=self._hotkeys[vk], daemon=True).start()
        return True

    def _handle_key(self, wparam: int, kb: KBDLLHOOKSTRUCT) -> bool:
        """Parse one key event; returns True when it was consumed as a hotkey."""
        now = wall_ms()
        vk = int(kb.vkCode)
        is_down = not (kb.flags & LLKHF_UP)
        repeat = is_down and vk in self._pressed_keys
        if self._consume_hotkey(vk, is_down, repeat, now):
            return True                        # never logged, never passed on
        if is_down:
            self._pressed_keys.add(vk)
        else:
            self._pressed_keys.discard(vk)
        if is_down and not repeat:
            self._last_vk = {"vk": vk, "down_time": now}

        char, dead = ("", False)
        if is_down:
            char, dead = self._vk_to_char(vk, int(kb.scanCode))

        self._put({"type": "key", **self._ctx(), "keyCode": vk, "characters": char,
                   "isDown": is_down, "isRepeat": repeat, "modifiers": modifiers_mask(),
                   "scanCode": int(kb.scanCode),
                   "extended": bool(kb.flags & LLKHF_EXTENDED),
                   "hkl": self._hkl_hex(), "dead": dead,
                   "synthetic": bool(kb.flags & LLKHF_INJECTED), "timestamp": now})
        return False

    def _ctx(self) -> Dict:
        try:
            return self._context()
        except Exception:
            return {"appName": "", "bundleId": "", "screen": 0}

    def _click_count(self, now: float, x: int, y: int, button: str) -> int:
        prev = self._last_click
        same = (button == prev["button"] and now - prev["time"] <= self._double_click_ms
                and abs(x - prev["x"]) <= 4 and abs(y - prev["y"]) <= 4)
        count = prev["count"] + 1 if same else 1
        self._last_click = {"time": now, "x": x, "y": y, "button": button, "count": count}
        return count

    def _vk_to_char(self, vk: int, scan: int) -> tuple[str, bool]:
        state = (ctypes.c_ubyte * 256)()
        if not user32.GetKeyboardState(state):
            return "", False
        hwnd = user32.GetForegroundWindow()
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        hkl = user32.GetKeyboardLayout(tid)
        buf = ctypes.create_unicode_buffer(8)
        n = user32.ToUnicodeEx(vk, scan, state, buf, 8, 0, hkl)
        if n == -1:  # dead key
            return "", True
        if n <= 0:
            return "", False
        return buf.value[:n], False

    @staticmethod
    def _hkl_hex() -> str:
        hwnd = user32.GetForegroundWindow()
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        hkl = user32.GetKeyboardLayout(tid) or 0
        return f"{hkl & 0xFFFFFFFF:08X}"
