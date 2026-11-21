"""Native WM_INPUT mouse packets, independent of cursor acceleration/clipping."""
from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes as W

from .clock import wall_ms
from .i18n import L

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - import-safe stub (packet parsing is unit-tested anywhere)
    user32 = kernel32 = None
WNDPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON),
                ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH),
                ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]


class DEVICE(ctypes.Structure):
    _fields_ = [("usagePage", W.USHORT), ("usage", W.USHORT), ("flags", W.DWORD), ("target", W.HWND)]


class HEADER(ctypes.Structure):
    _fields_ = [("type", W.DWORD), ("size", W.DWORD), ("device", W.HANDLE), ("wParam", W.WPARAM)]


class MOUSE(ctypes.Structure):
    _fields_ = [("flags", W.USHORT), ("buttons", W.ULONG), ("rawButtons", W.ULONG),
                ("dx", W.LONG), ("dy", W.LONG), ("extra", W.ULONG)]


def _bind() -> None:
    kernel32.GetModuleHandleW.argtypes = [W.LPCWSTR]
    kernel32.GetModuleHandleW.restype = W.HMODULE
    kernel32.GetCurrentThreadId.restype = W.DWORD
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    user32.RegisterClassW.restype = W.ATOM
    user32.UnregisterClassW.argtypes = [W.LPCWSTR, W.HINSTANCE]
    user32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
    user32.CreateWindowExW.restype = W.HWND
    user32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DestroyWindow.argtypes = [W.HWND]
    user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(DEVICE), W.UINT, W.UINT]
    user32.GetRawInputData.argtypes = [W.HANDLE, W.UINT, W.LPVOID, ctypes.POINTER(W.UINT), W.UINT]
    user32.GetRawInputData.restype = W.UINT
    user32.PostThreadMessageW.argtypes = [W.DWORD, W.UINT, W.WPARAM, W.LPARAM]
    user32.GetMessageW.argtypes = [ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(W.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t


if IS_WINDOWS:
    _bind()


def relative_packet(data: bytes):
    if len(data) < ctypes.sizeof(HEADER) + ctypes.sizeof(MOUSE):
        raise ValueError("Incomplete WM_INPUT mouse packet")
    header = HEADER.from_buffer_copy(data)
    if header.type != 0:
        return None
    mouse = MOUSE.from_buffer_copy(data, ctypes.sizeof(HEADER))
    # Absolute devices do not report relative counts; never relabel their coordinates.
    if mouse.flags & 1 or not (mouse.dx or mouse.dy):
        return None
    return int(mouse.dx), int(mouse.dy)


class RawMouse:
    def __init__(self, emit, context):
        self.emit, self.context = emit, context
        self.ready = threading.Event()
        self.thread = None
        self.thread_id = None
        self.error = ""
        self.errors = 0
        self.packets = 0
        self.available = False

    def start(self):
        self.thread = threading.Thread(target=self._run, name="raw-mouse", daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            self.stop()
            raise RuntimeError(L("鼠标原始输入初始化超时", "Raw mouse input timed out while starting"))
        if self.error:
            raise RuntimeError(L("鼠标原始输入初始化失败：", "Raw mouse input failed to start: ") + self.error)
        return self

    def stop(self):
        self.request_stop()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError(L("鼠标原始输入线程未停止", "The raw mouse input thread did not stop"))

    def request_stop(self):
        if self.thread_id and user32 is not None:
            user32.PostThreadMessageW(self.thread_id, 0x12, 0, 0)

    def _run(self):
        name = f"DestudioRawMouse_{id(self)}"
        instance = kernel32.GetModuleHandleW(None)
        hwnd = None
        registered = False

        @WNDPROC
        def callback(window, message, wparam, lparam):
            if message == 0xFF:  # WM_INPUT
                timestamp = wall_ms()
                try:
                    size = W.UINT()
                    failed = 0xFFFFFFFF
                    if user32.GetRawInputData(lparam, 0x10000003, None, ctypes.byref(size), ctypes.sizeof(HEADER)) == failed:
                        raise ctypes.WinError(ctypes.get_last_error())
                    buf = ctypes.create_string_buffer(size.value)
                    count = user32.GetRawInputData(lparam, 0x10000003, buf, ctypes.byref(size), ctypes.sizeof(HEADER))
                    if count == failed:
                        raise ctypes.WinError(ctypes.get_last_error())
                    packet = relative_packet(buf.raw[:count])
                    if packet:
                        self.packets += 1
                        self.emit({"type": "mouse_delta", "dx": packet[0], "dy": packet[1],
                                   "packets": 1, **self.context(), "timestamp": timestamp,
                                   "source": "wm_input", "coordinate_space": "raw_hid_relative"})
                except Exception as exc:
                    self.errors += 1
                    self.error = str(exc)
            return user32.DefWindowProcW(window, message, wparam, lparam)

        try:
            self.thread_id = kernel32.GetCurrentThreadId()
            wc = WNDCLASS(lpfnWndProc=callback, hInstance=instance, lpszClassName=name)
            if not user32.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            hwnd = user32.CreateWindowExW(0, name, name, 0, 0, 0, 0, 0, W.HWND(-3), None, instance, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            device = DEVICE(1, 2, 0x100, hwnd)  # Generic Desktop / mouse, INPUTSINK
            if not user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(DEVICE)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.available = True
            self.ready.set()
            msg = W.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self.error = str(exc)
            self.errors += 1
        finally:
            self.ready.set()
            if hwnd:
                device = DEVICE(1, 2, 1, None)  # RIDEV_REMOVE
                user32.RegisterRawInputDevices(ctypes.byref(device), 1, ctypes.sizeof(DEVICE))
                user32.DestroyWindow(hwnd)
            if registered:
                user32.UnregisterClassW(name, instance)
