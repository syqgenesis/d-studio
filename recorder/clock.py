"""Clock helpers.

All timestamps in this project are milliseconds:
  * ``wall_ms()``   -> epoch milliseconds (float, sub-ms precision)
  * ``os_time_ms()``-> ticks since boot (GetTickCount64), immune to wall-clock jumps

Event records carry both: ``timestamp`` is used to map into the video timeline,
``osTime`` is used for internal ordering / drift detection.
"""
from __future__ import annotations

import ctypes
import time

_OFFSET_MS = time.time() * 1000.0 - time.perf_counter() * 1000.0
if hasattr(ctypes, "windll"):
    ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong


def wall_ms() -> float:
    """Epoch milliseconds derived from the monotonic performance counter."""
    return time.perf_counter() * 1000.0 + _OFFSET_MS


def os_time_ms() -> int:
    """Milliseconds since system boot."""
    try:
        return int(ctypes.windll.kernel32.GetTickCount64())
    except (AttributeError, OSError):  # non-Windows / very old Windows
        return int(time.monotonic() * 1000.0)
