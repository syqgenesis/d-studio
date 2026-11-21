"""GDI (BitBlt) capture backend -- pure ctypes, no third-party packages.

Used when DXGI duplication is unavailable (no bettercam, remote sessions,
some virtual GPUs, older drivers) and *required* for app-only mode in that
case: unlike ffmpeg ``gdigrab``, frames pass through Python, so the app mask
is painted before encoding.

Per frame, in the capture thread::

    BitBlt(screen DC -> pooled 32-bpp DIB section, SRCCOPY | CAPTUREBLT)
    PatBlt(BLACKNESS) for every mask rectangle      (app-only mode)
    DrawIconEx(current cursor, at pos - hotspot)     (--draw-mouse-cursor)
    GdiFlush()
    -> hand the DIB memory (a memoryview, no copy) to the shared writer thread

The DIB pool has ``queue + 2`` buffers, so a slow encoder shows up as skipped
grabs / duplicated slots (see :class:`recorder.videocap.PipedSegment`), never
as unbounded memory.  numpy is not needed.

``CAPTUREBLT`` includes layered windows; on a few systems it makes the real
mouse pointer flicker while capturing.  Set ``DSTUDIO_GDI_CAPTUREBLT=0`` to
drop it (layered windows are still composed by DWM on Windows 8+).
"""
from __future__ import annotations

import ctypes
import os
import queue
import sys
from typing import Dict, List, Optional, Tuple

from .clock import wall_ms
from .cursor_draw import BLACKNESS, CAPTUREBLT, SRCCOPY, CursorTracker, gdi
from .i18n import L
from .videocap import Frame, PipedSegment, primary_monitor_rect

_MAX_SIDE = 16384
IS_WINDOWS = sys.platform == "win32"


class GdiSegment(PipedSegment):
    """Same interface as :class:`recorder.videocap.VideoSegment`."""

    backend = "gdi"
    paced = True        # BitBlt has no clock of its own: sleep until each CFR slot

    def __init__(self, *args, captureblt: Optional[bool] = None, **kwargs):
        super().__init__(*args, **kwargs)
        env = os.environ.get("DSTUDIO_GDI_CAPTUREBLT", "").strip()
        self.captureblt = (env != "0") if captureblt is None else bool(captureblt)
        self.rop = SRCCOPY | (CAPTUREBLT if self.captureblt else 0)
        self.name = "gdi_bitblt"
        self._api = None
        self._screen_dc = None
        self._mem_dc = None
        self._pool: List[Tuple[int, int, memoryview]] = []
        self._free: "queue.Queue[int]" = queue.Queue()
        self._cursor: Optional[CursorTracker] = None
        self._timer_period = False
        self.blt_failures = 0
        self._blt_ms_total = 0.0
        self._blt_count = 0

    # ------------------------------------------------------------------ geometry
    def _resolve_rect(self) -> Optional[Tuple[int, int, int, int]]:
        if self.capture_rect:
            return tuple(int(v) for v in self.capture_rect)  # type: ignore[return-value]
        if self.offset and self.video_size:
            return (int(self.offset[0]), int(self.offset[1]),
                    int(self.video_size[0]), int(self.video_size[1]))
        if self.source == "desktop":
            primary = primary_monitor_rect()     # --source desktop = one display
            if primary:
                return primary
        from .win_window import list_monitors, screens_union, virtual_screen_rect
        return virtual_screen_rect() or screens_union(list_monitors())

    # -------------------------------------------------------------- source hooks
    def _open(self) -> Tuple[int, int]:
        if not IS_WINDOWS:
            raise RuntimeError(L("GDI 采集只能在 Windows 上使用", "GDI capture is only available on Windows"))
        if self.source.startswith("window:"):
            raise ValueError(L("GDI 后端不支持单窗口录制，请使用 --backend gdigrab",
                               "The GDI backend cannot record a single window; use --backend gdigrab"))
        api = gdi()
        rect = self._resolve_rect()
        if not rect:
            raise RuntimeError(L("无法确定要录制的屏幕区域", "Cannot determine the screen area to capture"))
        x, y, w, h = rect
        if not (0 < w <= _MAX_SIDE and 0 < h <= _MAX_SIDE):
            raise ValueError(L("录制区域尺寸无效：{w}x{h}", "Invalid capture size: {w}x{h}", w=w, h=h))
        # validation blit (a small tile) in the calling thread, so `auto` can fall back
        sdc = api.user32.GetDC(None)
        if not sdc:
            raise OSError(L("无法获取屏幕 DC（GetDC 失败）", "Cannot get the screen DC (GetDC failed)"))
        ok = False
        err = 0
        try:
            mdc = api.gdi32.CreateCompatibleDC(sdc)
            if mdc:
                try:
                    tw, th = min(w, 64), min(h, 64)
                    hbm, _bits = api.create_dib(mdc, tw, th)
                    old = api.gdi32.SelectObject(mdc, hbm)
                    ok = bool(api.gdi32.BitBlt(mdc, 0, 0, tw, th, sdc, x, y, self.rop))
                    err = ctypes.get_last_error() if not ok else 0
                    api.gdi32.SelectObject(mdc, old)
                    api.gdi32.DeleteObject(hbm)
                finally:
                    api.gdi32.DeleteDC(mdc)
        finally:
            api.user32.ReleaseDC(None, sdc)
        if not ok:
            raise OSError(L("GDI 截屏（BitBlt）失败", "GDI screen capture (BitBlt) failed") + f" ({err})")
        self.capture_rect = (int(x), int(y), int(w), int(h))
        if self.masker is not None and hasattr(self.masker, "set_capture_rect") \
                and tuple(self.masker.capture_rect) != self.capture_rect:
            self.masker.set_capture_rect(self.capture_rect)
        return int(w), int(h)

    def _thread_setup(self) -> None:
        api = self._api = gdi()
        if api.winmm is not None:
            try:
                api.winmm.timeBeginPeriod(1)    # 1 ms sleep granularity for slot pacing
                self._timer_period = True
            except Exception:
                pass
        w, h = self._size  # type: ignore[misc]
        self._screen_dc = api.user32.GetDC(None)
        if not self._screen_dc:
            raise OSError("GetDC(NULL) failed")
        self._mem_dc = api.gdi32.CreateCompatibleDC(self._screen_dc)
        if not self._mem_dc:
            raise OSError("CreateCompatibleDC failed")
        size = w * h * 4
        for i in range(self._queue.maxsize + 2):
            hbm, bits = api.create_dib(self._screen_dc, w, h)
            view = memoryview((ctypes.c_ubyte * size).from_address(bits))
            self._pool.append((hbm, bits, view))
            self._free.put(i)
        if self.draw_mouse:
            try:
                self._cursor = CursorTracker()
            except Exception as exc:
                self._tail.append(f"cursor drawing unavailable: {exc}")

    def _grab(self) -> Optional[Frame]:
        try:
            idx = self._free.get(timeout=0.05)
        except queue.Empty:
            return None
        api = self._api
        hbm, _bits, view = self._pool[idx]
        x, y, w, h = self.capture_rect  # type: ignore[misc]
        mdc = self._mem_dc
        old = api.gdi32.SelectObject(mdc, hbm)
        masked = False
        ok = False
        try:
            t0 = wall_ms()
            ok = bool(api.gdi32.BitBlt(mdc, 0, 0, w, h, self._screen_dc, x, y, self.rop))
            t1 = wall_ms()
            if ok:
                self._blt_ms_total += t1 - t0
                self._blt_count += 1
                if self.masker is not None:
                    # computed *after* the blit: a window that appeared just
                    # before the grab is already in the window list
                    rects = self._mask_rects()
                    for x0, y0, x1, y1 in rects:
                        api.gdi32.PatBlt(mdc, x0, y0, x1 - x0, y1 - y0, BLACKNESS)
                    masked = bool(rects)
                if self._cursor is not None and self._cursor.draw_gdi(mdc, x, y):
                    self.cursor_frames += 1
        finally:
            # always deselect, so the DIB can be deleted and the DC keeps its stock bitmap
            api.gdi32.SelectObject(mdc, old)
            if not ok:
                self._free.put(idx)
        if not ok:
            self.blt_failures += 1
            return None
        api.gdi32.GdiFlush()
        free = self._free
        return Frame(view, (t0 + t1) / 2.0, masked, release=lambda: free.put(idx))

    def _thread_teardown(self) -> None:
        api = self._api
        if api is None:
            return
        if self._mem_dc:
            api.gdi32.DeleteDC(self._mem_dc)       # first: no DIB stays selected
            self._mem_dc = None
        pool, self._pool = self._pool, []
        for hbm, _bits, _view in pool:
            try:
                api.gdi32.DeleteObject(hbm)
            except Exception:
                pass
        if self._screen_dc:
            api.user32.ReleaseDC(None, self._screen_dc)
            self._screen_dc = None
        if self._timer_period and api.winmm is not None:
            try:
                api.winmm.timeEndPeriod(1)
            except Exception:
                pass
            self._timer_period = False

    def _source_info(self) -> Dict:
        info = {"captureblt": self.captureblt, "blt_failures": self.blt_failures,
                "avg_blt_ms": round(self._blt_ms_total / self._blt_count, 3) if self._blt_count else None,
                "teardown_skipped": self._teardown_skipped}
        if self._cursor is not None:
            info["cursor"] = self._cursor.stats()
        if self.masker is not None and hasattr(self.masker, "stats"):
            info["mask"] = self.masker.stats()
        return info

    @property
    def cursor_in_video(self) -> bool:
        # the tracker is created in the capture thread; before that, trust the flag
        return self.draw_mouse and (self._cursor is not None or self._api is None)
