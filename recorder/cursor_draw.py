"""Mouse cursor rendering for the frame-in-Python capture backends.

DXGI desktop duplication frames contain no cursor and GDI ``BitBlt`` of the
screen DC does not include the hardware cursor either, while Markov clips
show the OS cursor.  Two strategies:

* **GDI backend** -- draw straight into the capture DC with ``DrawIconEx``
  (:meth:`CursorTracker.draw_gdi`); GDI handles colour, alpha and
  monochrome/XOR cursors natively.
* **DXGI backend** (numpy frames) -- render each ``HCURSOR`` once onto a black
  and a white 32-bit DIB.  Per channel the result of any cursor pixel on a
  background ``d`` is linear in ``d``::

      out = on_black + d * (on_white - on_black) / 255

  which is exact for opaque pixels (k=0), transparent pixels (k=255), alpha
  blending (0<k<255) and *inverting* XOR pixels (on_black=255, on_white=0 ->
  ``255 - d``, the text I-beam).  :func:`blend_sprite` applies that with a few
  numpy ops on the (cropped) cursor rectangle.

Hotspots come from ``GetIconInfo`` (whose HBITMAPs are always deleted).
Positions come from ``GetCursorInfo`` in physical px (callers are per-monitor
DPI aware).  Import-safe off Windows; :func:`make_sprite` / :func:`blend_sprite`
are pure numpy and unit-tested anywhere numpy exists.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Dict, Optional, Tuple

CURSOR_SHOWING = 0x1
CURSOR_SUPPRESSED = 0x2
DI_NORMAL = 0x3
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
BLACKNESS = 0x00000042
WHITENESS = 0x00FF0062
DIB_RGB_COLORS = 0
BI_RGB = 0

_GDI = None
_GDI_LOCK = threading.Lock()


def gdi() -> "_GdiApi":
    """Lazily bound user32/gdi32 calls (own WinDLL instances; Windows only)."""
    global _GDI
    if _GDI is None:
        if sys.platform != "win32":
            raise OSError("GDI is only available on Windows")
        with _GDI_LOCK:
            if _GDI is None:
                _GDI = _GdiApi()
    return _GDI


class _GdiApi:
    def __init__(self):
        import ctypes
        from ctypes import wintypes as W

        self.ctypes, self.W = ctypes, W

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG),
                        ("biPlanes", W.WORD), ("biBitCount", W.WORD), ("biCompression", W.DWORD),
                        ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG),
                        ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", W.DWORD * 3)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class CURSORINFO(ctypes.Structure):
            _fields_ = [("cbSize", W.DWORD), ("flags", W.DWORD), ("hCursor", ctypes.c_void_p),
                        ("ptScreenPos", POINT)]

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", W.BOOL), ("xHotspot", W.DWORD), ("yHotspot", W.DWORD),
                        ("hbmMask", ctypes.c_void_p), ("hbmColor", ctypes.c_void_p)]

        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", W.LONG), ("bmWidth", W.LONG), ("bmHeight", W.LONG),
                        ("bmWidthBytes", W.LONG), ("bmPlanes", W.WORD), ("bmBitsPixel", W.WORD),
                        ("bmBits", ctypes.c_void_p)]

        self.BITMAPINFO, self.CURSORINFO, self.ICONINFO, self.BITMAP = BITMAPINFO, CURSORINFO, ICONINFO, BITMAP
        u = ctypes.WinDLL("user32", use_last_error=True)
        g = ctypes.WinDLL("gdi32", use_last_error=True)
        vp = ctypes.c_void_p
        sig = {
            (u, "GetDC"): ([vp], vp),
            (u, "ReleaseDC"): ([vp, vp], ctypes.c_int),
            (u, "GetCursorInfo"): ([ctypes.POINTER(CURSORINFO)], W.BOOL),
            (u, "GetIconInfo"): ([vp, ctypes.POINTER(ICONINFO)], W.BOOL),
            (u, "DrawIconEx"): ([vp, ctypes.c_int, ctypes.c_int, vp, ctypes.c_int, ctypes.c_int,
                                 W.UINT, vp, W.UINT], W.BOOL),
            (u, "GetSystemMetrics"): ([ctypes.c_int], ctypes.c_int),
            (g, "CreateCompatibleDC"): ([vp], vp),
            (g, "DeleteDC"): ([vp], W.BOOL),
            (g, "CreateDIBSection"): ([vp, ctypes.POINTER(BITMAPINFO), W.UINT, ctypes.POINTER(vp),
                                       vp, W.DWORD], vp),
            (g, "SelectObject"): ([vp, vp], vp),
            (g, "DeleteObject"): ([vp], W.BOOL),
            (g, "BitBlt"): ([vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, vp,
                             ctypes.c_int, ctypes.c_int, W.DWORD], W.BOOL),
            (g, "PatBlt"): ([vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.DWORD], W.BOOL),
            (g, "GdiFlush"): ([], W.BOOL),
            (g, "GetObjectW"): ([vp, ctypes.c_int, vp], ctypes.c_int),
        }
        for (dll, name), (args, res) in sig.items():
            fn = getattr(dll, name)
            fn.argtypes = args
            fn.restype = res
        self.user32, self.gdi32 = u, g
        try:
            self.winmm = ctypes.WinDLL("winmm")
            self.winmm.timeBeginPeriod.argtypes = [W.UINT]
            self.winmm.timeEndPeriod.argtypes = [W.UINT]
        except (OSError, AttributeError):
            self.winmm = None

    # ------------------------------------------------------------------ helpers
    def bitmapinfo(self, w: int, h: int):
        bmi = self.BITMAPINFO()
        hdr = bmi.bmiHeader
        hdr.biSize = self.ctypes.sizeof(hdr)
        hdr.biWidth = int(w)
        hdr.biHeight = -int(h)            # negative = top-down rows (matches numpy/ffmpeg)
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        hdr.biCompression = BI_RGB
        return bmi

    def create_dib(self, hdc, w: int, h: int) -> Tuple[int, int]:
        """-> (HBITMAP, bits address) of a top-down 32-bpp BGRA DIB section."""
        bits = self.ctypes.c_void_p()
        bmi = self.bitmapinfo(w, h)
        hbm = self.gdi32.CreateDIBSection(hdc, self.ctypes.byref(bmi), DIB_RGB_COLORS,
                                          self.ctypes.byref(bits), None, 0)
        if not hbm or not bits.value:
            if hbm:
                self.gdi32.DeleteObject(hbm)
            raise OSError(f"CreateDIBSection({w}x{h}) failed: {self.ctypes.get_last_error()}")
        return hbm, bits.value

    def cursor(self) -> Optional[Tuple[int, int, int]]:
        """-> (hcursor, x, y) of the visible cursor in physical screen px, else None."""
        ci = self.CURSORINFO()
        ci.cbSize = self.ctypes.sizeof(ci)
        if not self.user32.GetCursorInfo(self.ctypes.byref(ci)):
            return None
        if not (ci.flags & CURSOR_SHOWING) or (ci.flags & CURSOR_SUPPRESSED) or not ci.hCursor:
            return None
        return int(ci.hCursor), int(ci.ptScreenPos.x), int(ci.ptScreenPos.y)

    def icon_meta(self, hcur: int) -> Tuple[int, int, int, int]:
        """-> (hotspot_x, hotspot_y, width, height); frees the HBITMAPs GetIconInfo creates."""
        ii = self.ICONINFO()
        if not self.user32.GetIconInfo(hcur, self.ctypes.byref(ii)):
            return (0, 0, 32, 32)
        try:
            w = h = 32
            bm = self.BITMAP()
            hb = ii.hbmColor or ii.hbmMask
            if hb and self.gdi32.GetObjectW(hb, self.ctypes.sizeof(bm), self.ctypes.byref(bm)):
                w, h = int(bm.bmWidth), int(abs(bm.bmHeight))
                if not ii.hbmColor:
                    h //= 2               # monochrome: AND mask on top of XOR mask
            return (int(ii.xHotspot), int(ii.yHotspot), max(1, w), max(1, h))
        finally:
            if ii.hbmMask:
                self.gdi32.DeleteObject(ii.hbmMask)
            if ii.hbmColor:
                self.gdi32.DeleteObject(ii.hbmColor)

    def render_cursor(self, hcur: int, w: int, h: int) -> Tuple[bytes, bytes]:
        """BGRA bytes of the cursor drawn on black and on white (for :func:`make_sprite`)."""
        hdc = self.gdi32.CreateCompatibleDC(None)
        if not hdc:
            raise OSError("CreateCompatibleDC failed")
        hbm = old = None
        try:
            hbm, bits = self.create_dib(hdc, w, h)
            old = self.gdi32.SelectObject(hdc, hbm)
            out = []
            for rop in (BLACKNESS, WHITENESS):
                self.gdi32.PatBlt(hdc, 0, 0, w, h, rop)
                self.user32.DrawIconEx(hdc, 0, 0, hcur, w, h, 0, None, DI_NORMAL)
                self.gdi32.GdiFlush()
                out.append(self.ctypes.string_at(bits, w * h * 4))
            return out[0], out[1]
        finally:
            if old:
                self.gdi32.SelectObject(hdc, old)
            if hbm:
                self.gdi32.DeleteObject(hbm)
            self.gdi32.DeleteDC(hdc)


# ======================================================================= pure numpy
def make_sprite(on_black: bytes, on_white: bytes, w: int, h: int):
    """-> ``(base, k, x_off, y_off)`` int32 arrays cropped to the non-transparent
    bounding box, or ``None`` if the cursor is fully transparent."""
    import numpy as np

    b = np.frombuffer(on_black, dtype=np.uint8).reshape(h, w, 4)[:, :, :3].astype(np.int32)
    wt = np.frombuffer(on_white, dtype=np.uint8).reshape(h, w, 4)[:, :, :3].astype(np.int32)
    k = wt - b                                   # 255 = transparent, 0 = opaque, <0 = invert
    active = ~((b == 0) & (k == 255)).all(axis=2)
    if not active.any():
        return None
    ys = np.nonzero(active.any(axis=1))[0]
    xs = np.nonzero(active.any(axis=0))[0]
    y0, y1, x0, x1 = int(ys[0]), int(ys[-1]) + 1, int(xs[0]), int(xs[-1]) + 1
    return (np.ascontiguousarray(b[y0:y1, x0:x1]), np.ascontiguousarray(k[y0:y1, x0:x1]), x0, y0)


def blend_sprite(frame, base, k, x: int, y: int) -> bool:
    """Composite a sprite (top-left at frame px ``x, y``) into a BGRA frame in place."""
    import numpy as np

    fh, fw = frame.shape[:2]
    sh, sw = base.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(fw, x + sw), min(fh, y + sh)
    if x0 >= x1 or y0 >= y1:
        return False
    sx, sy = x0 - x, y0 - y
    dst = frame[y0:y1, x0:x1, :3]
    b = base[sy:sy + (y1 - y0), sx:sx + (x1 - x0)]
    kk = k[sy:sy + (y1 - y0), sx:sx + (x1 - x0)]
    out = b + (dst.astype(np.int32) * kk + 127) // 255
    np.clip(out, 0, 255, out=out)
    dst[...] = out.astype(np.uint8)
    return True


# =========================================================================== tracker
class CursorTracker:
    """Current cursor + cached per-HCURSOR metadata / sprites.

    Handles are cached for ``ttl_s`` so a cursor an app destroyed (and whose
    handle value got reused) is re-read within a few seconds.
    """

    def __init__(self, want_sprites: bool = False, ttl_s: float = 5.0):
        self._api = gdi()
        self._want_sprites = want_sprites
        self._ttl = ttl_s
        self._meta: Dict[int, Tuple[float, Tuple[int, int, int, int]]] = {}
        self._sprites: Dict[int, Tuple[float, object]] = {}
        self.draws = 0
        self.errors = 0
        self.last_error = ""

    def _meta_for(self, hcur: int) -> Tuple[int, int, int, int]:
        now = time.monotonic()
        hit = self._meta.get(hcur)
        if hit is None or now - hit[0] > self._ttl:
            if len(self._meta) > 128:
                self._meta.clear()
            hit = (now, self._api.icon_meta(hcur))
            self._meta[hcur] = hit
        return hit[1]

    def draw_gdi(self, hdc, origin_x: int, origin_y: int) -> bool:
        """Draw the current cursor into ``hdc`` whose (0,0) is screen (origin_x, origin_y)."""
        try:
            cur = self._api.cursor()
            if cur is None:
                return False
            hcur, x, y = cur
            hx, hy, _w, _h = self._meta_for(hcur)
            ok = bool(self._api.user32.DrawIconEx(hdc, x - hx - origin_x, y - hy - origin_y,
                                                  hcur, 0, 0, 0, None, DI_NORMAL))
            self.draws += ok
            return ok
        except Exception as exc:  # never break capture for a cursor
            self.errors += 1
            self.last_error = str(exc)
            return False

    def draw_numpy(self, frame, origin_x: int, origin_y: int) -> bool:
        """Blend the current cursor into a numpy BGRA frame (DXGI frames)."""
        try:
            cur = self._api.cursor()
            if cur is None:
                return False
            hcur, x, y = cur
            hx, hy, w, h = self._meta_for(hcur)
            now = time.monotonic()
            hit = self._sprites.get(hcur)
            if hit is None or now - hit[0] > self._ttl:
                if len(self._sprites) > 64:
                    self._sprites.clear()
                on_black, on_white = self._api.render_cursor(hcur, w, h)
                hit = (now, make_sprite(on_black, on_white, w, h))
                self._sprites[hcur] = hit
            sprite = hit[1]
            if sprite is None:
                return False
            base, k, xo, yo = sprite  # type: ignore[misc]
            ok = blend_sprite(frame, base, k, x - hx - origin_x + xo, y - hy - origin_y + yo)
            self.draws += ok
            return ok
        except Exception as exc:
            self.errors += 1
            self.last_error = str(exc)
            return False

    def stats(self) -> Dict:
        return {"draws": self.draws, "errors": self.errors, "last_error": self.last_error,
                "cached_cursors": len(self._meta)}
