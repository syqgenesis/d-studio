"""Capture backend selection.

* ``dxcam``   DXGI desktop duplication (bettercam) -> frames in Python
* ``gdi``     pure-ctypes BitBlt grabber              -> frames in Python
* ``gdigrab`` ffmpeg's own GDI grabber                 -> frames never reach Python
* ``auto``    dxcam when usable; else gdi for app-only mode, gdigrab otherwise
              (the recorder also falls back the same way if ``start()`` fails)

App-only mode paints non-target pixels black before encoding, so it needs a
frame-in-Python backend; ``gdigrab`` is rejected for it.
"""
from __future__ import annotations

from typing import Any

from .dxcam_capture import DxcamSegment, backend_available
from .gdi_capture import GdiSegment
from .i18n import L
from .videocap import VideoSegment

BACKENDS = ("auto", "dxcam", "gdi", "gdigrab")

# kwargs only the frame-in-Python backends understand
_PIPED_ONLY = {"output_idx", "device_idx", "max_backlog_s", "masker", "monitor_device",
               "queue_frames", "stall_ms", "captureblt"}


def app_gdigrab_error() -> str:
    return L("“仅录制应用”模式不能使用 gdigrab 后端（无法实时遮挡其他窗口），请选择 auto、dxcam 或 gdi",
             "App-only mode cannot use the gdigrab backend (it cannot black out other windows "
             "in real time); choose auto, dxcam or gdi")


def is_app_source(source: str) -> bool:
    return str(source or "").startswith("app:")


def fallback_backend(source: str) -> str:
    """Backend to try when ``auto`` picked one whose ``start()`` failed."""
    return "gdi" if is_app_source(source) else "gdigrab"


def create_segment(backend: str, **kwargs: Any):
    """Build a segment recorder. ``auto`` prefers DXGI when it is usable."""
    source = str(kwargs.get("source") or "desktop")
    if backend not in BACKENDS:
        raise ValueError(L("未知的采集后端 {b}；可选：{all}", "Unknown backend {b!r}; choose one of {all}",
                           b=backend, all=", ".join(BACKENDS)))
    app = is_app_source(source)
    if backend == "gdigrab":
        if app:
            raise ValueError(app_gdigrab_error())
        return VideoSegment(**_for_gdigrab(kwargs))

    if source.startswith("window:"):
        if backend in ("dxcam", "gdi"):
            raise ValueError(L("单窗口录制只能使用 gdigrab 后端", "Single-window recording needs --backend gdigrab"))
        return VideoSegment(**_for_gdigrab(kwargs))

    if backend == "gdi":
        return GdiSegment(**kwargs)
    if backend == "dxcam" or backend_available():
        return DxcamSegment(**kwargs)
    if app:
        return GdiSegment(**kwargs)
    return VideoSegment(**_for_gdigrab(kwargs))


def _for_gdigrab(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items() if k not in _PIPED_ONLY}


# backwards-compatible name
_without_dxcam = _for_gdigrab
