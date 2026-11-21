"""DXGI Desktop Duplication capture backend (bettercam, falling back to dxcam).

Why this backend exists
-----------------------
gdigrab + ``showinfo`` works, but the frame clock is inferred from ffmpeg's
stderr and the desktop is copied through GDI on every frame.  DXGI duplication
hands us the frame buffer directly, so:

* we timestamp each frame in the capture thread (no anchor estimation);
* dropped frames are visible (the pacer duplicates the last frame to keep the
  CFR grid; padded slots carry their nominal slot time in the sidecar and the
  content's capture time in ``duplicate_runs``);
* no GDI full-screen copy, so CPU cost is lower at 1080p+;
* frames are numpy arrays, so app-only masking (black rectangles) and the
  cursor (DXGI frames carry none; see :mod:`recorder.cursor_draw`) are applied
  in Python before the frame reaches the encoder.

Frames are pushed to ffmpeg as ``rawvideo`` on stdin by the shared
:class:`recorder.videocap.PipedSegment`, so the deliverable is identical to the
other backends.

Monitor selection: DXGI outputs are numbered per adapter and do not follow the
``EnumDisplayMonitors`` order, so a capture monitor is matched by its GDI device
name (``\\\\.\\DISPLAY2``) or by desktop coordinates; the region passed to
bettercam is relative to that output.  If the mapping cannot be established the
backend refuses to start (``auto`` then falls back) rather than recording the
wrong screen.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import threading
from typing import Dict, List, Optional, Tuple

from .clock import wall_ms
from .i18n import L
from .videocap import FramePacer, Frame, PipedSegment, primary_monitor_rect

__all__ = ["FramePacer", "DxcamSegment", "load_backend", "backend_available", "dxgi_outputs"]

RING_FRAMES = 8                 # library ring buffer (default 64 full frames = 0.5-2 GB)
FIRST_FRAME_TIMEOUT_S = 8.0     # secure desktop (lock screen / UAC) can withhold frames


def _private(obj, name: str):
    """A name-mangled attribute (``self.__lock`` -> ``_BetterCam__lock``) of
    ``obj`` or one of its base classes; None when the library has none."""
    for cls in type(obj).__mro__:
        attr = "_" + cls.__name__.lstrip("_") + name
        if hasattr(obj, attr):
            return getattr(obj, attr)
    return None


def latest_frame(camera):
    """A private copy of the newest frame, copied while the library's lock is held.

    bettercam/dxcam's ``get_latest_frame()`` picks the newest ring slot under
    its lock but copies it *after* releasing it, so the capture thread can
    overwrite that slot mid-copy once it wraps around the ring.  The pixels
    are then newer than the frame's timestamp and its app-only mask -- e.g. a
    window caught mid-animation behind a mask computed for its old place.
    Copying under the lock rules that out; libraries without these internals
    fall back to ``get_latest_frame()``.
    """
    lock = _private(camera, "__lock")
    event = _private(camera, "__frame_available")
    size = getattr(camera, "max_buffer_len", None)
    if lock is None or event is None or not size:
        return camera.get_latest_frame()
    import numpy as np

    event.wait()
    with lock:
        buf = _private(camera, "__frame_buffer")
        head = _private(camera, "__head")
        frame = None if buf is None or head is None else np.array(buf[(head - 1) % size])
        event.clear()
    return frame


def first_frame(camera, timeout: float):
    """:func:`latest_frame` with a time limit.

    The library waits on an event with no timeout; on the secure desktop
    (lock screen, UAC prompt) or after a driver reset no frame ever comes, and
    opening a clip would hang the recorder -- including Stop, which waits for
    the same lock.  None on timeout: the caller's cleanup calls ``stop()``,
    which sets the library's event and releases the waiting thread.
    """
    box: Dict[str, object] = {}

    def wait() -> None:
        try:
            box["frame"] = latest_frame(camera)
        except BaseException as exc:  # surfaced to the caller below
            box["error"] = exc

    t = threading.Thread(target=wait, name="dxgi-first-frame", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box.get("frame")


def load_backend():
    """Returns (module, name). Prefers bettercam (maintained, py3.12+)."""
    for name in ("bettercam", "dxcam"):
        try:
            return importlib.import_module(name), name
        except Exception:
            continue
    raise RuntimeError(L("DXGI 采集需要安装 bettercam（pip install bettercam）",
                         "DXGI capture needs 'bettercam' (or 'dxcam'): pip install bettercam"))


def backend_available() -> bool:
    try:
        load_backend()
        return True
    except Exception:
        return False


def dxgi_outputs(module) -> List[Dict]:
    """``[{device_idx, output_idx, name, rect:(x,y,w,h)}]`` from bettercam's factory.

    Uses the library's internal factory object (found by duck typing, so a
    rename does not break us); returns ``[]`` when it cannot be inspected.
    """
    factory = None
    for value in list(vars(module).values()):
        if isinstance(value, type):
            continue
        if hasattr(value, "outputs") and hasattr(value, "devices"):
            factory = value
            break
    if factory is None:
        return []
    out: List[Dict] = []
    try:
        for d, outs in enumerate(factory.outputs):
            for o, output in enumerate(outs):
                try:
                    if hasattr(output, "update_desc"):
                        output.update_desc()
                    desc = output.desc
                    dc = desc.DesktopCoordinates
                    out.append({"device_idx": d, "output_idx": o,
                                "name": str(getattr(output, "devicename", "") or desc.DeviceName),
                                "rect": (int(dc.left), int(dc.top), int(dc.right - dc.left),
                                         int(dc.bottom - dc.top))})
                except Exception:
                    continue
    except Exception:
        return []
    return out


def _contains(outer: Tuple[int, int, int, int], inner: Tuple[int, int, int, int]) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ox <= ix and oy <= iy and ix + iw <= ox + ow and iy + ih <= oy + oh


def select_output(outputs: List[Dict], want: Optional[Tuple[int, int, int, int]] = None,
                  device_name: Optional[str] = None, device_idx: int = 0,
                  output_idx: int = 0) -> Optional[Dict]:
    """Pick the DXGI output for a capture rect / GDI device name (pure)."""
    if not outputs:
        return None
    if device_name:
        for o in outputs:
            if o["name"].lower() == device_name.lower():
                return o
    if want:
        for o in outputs:
            if _contains(o["rect"], want):
                return o
        return None
    for o in outputs:
        if o["device_idx"] == device_idx and o["output_idx"] == output_idx:
            return o
    return None


class DxcamSegment(PipedSegment):
    """Same interface as :class:`recorder.videocap.VideoSegment`."""

    backend = "dxcam"
    # bettercam's video_mode delivers frames on its own timer.  That timer runs
    # at 1000 // fps ms (30.3 Hz at 30 fps, 62.5 Hz at 60 fps), i.e. slightly
    # fast: PipedSegment drops captures whose slot is already filled
    # (``early_captures``), so the video never runs ahead of wall time.
    paced = False

    def __init__(self, *args, output_idx: int = 0, device_idx: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.output_idx = int(output_idx or 0)
        self.device_idx = int(device_idx or 0)
        self.name = "bettercam"
        self._camera = None
        self._np = None
        self._cursor = None
        self._origin = (0, 0)
        self._region = None

    # ----------------------------------------------------------------- source hooks
    def _open(self) -> Tuple[int, int]:
        if self.source.startswith("window:"):
            raise ValueError(L("DXGI 复制无法只录单个窗口，请使用 --backend gdigrab",
                               "DXGI duplication cannot capture a single window; use --backend gdigrab"))
        module, self.name = load_backend()
        import numpy as np  # bettercam depends on numpy
        self._np = np

        want = self.capture_rect
        if want is None and self.offset and self.video_size:
            want = (self.offset[0], self.offset[1], self.video_size[0], self.video_size[1])
        if want is None and self.source == "desktop" and not self.monitor_device \
                and not self.output_idx and not self.device_idx:
            # --source desktop = one display, the primary monitor (DXGI output 0
            # is not guaranteed to be the primary one)
            want = primary_monitor_rect()
        outputs = dxgi_outputs(module)
        chosen = select_output(outputs, want, self.monitor_device, self.device_idx, self.output_idx)
        region = None
        if chosen is not None:
            self.device_idx, self.output_idx = chosen["device_idx"], chosen["output_idx"]
            ox, oy, ow, oh = chosen["rect"]
            if want and tuple(want) != (ox, oy, ow, oh):
                x, y, w, h = want
                region = (x - ox, y - oy, x - ox + w, y - oy + h)
            rect = tuple(want) if want else (ox, oy, ow, oh)
        elif outputs and (want or self.monitor_device):
            raise RuntimeError(L("找不到与所选显示器对应的 DXGI 输出", "No DXGI output matches the selected monitor"))
        elif self.masker is not None or self.monitor_device:
            # app mode needs exact geometry; let `auto` fall back to the GDI grabber
            raise RuntimeError(L("无法确定 DXGI 输出的屏幕位置", "Cannot determine the DXGI output position"))
        else:
            # legacy: library internals unavailable -> explicit output index
            if want:
                x, y, w, h = want
                region = (x, y, x + w, y + h)
            rect = tuple(want) if want else None

        self._region = region
        kwargs = dict(output_idx=self.output_idx, region=region, output_color="BGRA")
        if self.device_idx:
            kwargs["device_idx"] = self.device_idx
        # The library's default ring buffer is 64 full frames (0.5 GB at 1080p,
        # 2 GB at 4K) right next to the CAD app; latest_frame() copies the
        # newest slot under the library's lock, so a few slots are plenty.
        try:
            self._camera = module.create(max_buffer_len=RING_FRAMES, **kwargs)
        except TypeError:                    # a library without max_buffer_len
            self._camera = module.create(**kwargs)
        if self._camera is None:
            raise RuntimeError(L("DXGI 复制无法初始化（没有输出或显卡不支持）",
                                 "DXGI duplication could not initialise (no output / unsupported GPU)"))
        try:
            # bettercam prints a banner on start(); keep the recorder console clean
            with contextlib.redirect_stdout(io.StringIO()):
                self._camera.start(target_fps=self.fps, video_mode=True)
            first = first_frame(self._camera, FIRST_FRAME_TIMEOUT_S)
            if first is None:
                raise RuntimeError(L("DXGI 复制没有返回画面（安全桌面或驱动问题）",
                                     "DXGI duplication returned no frames (secure desktop or driver issue)"))
            h, w = first.shape[:2]
            if rect is None:
                rect = (0, 0, w, h)
            if (rect[2], rect[3]) != (w, h):
                if self.masker is not None:
                    raise RuntimeError(f"DXGI frame {w}x{h} does not match capture rect {rect}")
                rect = (rect[0], rect[1], w, h)
            self.capture_rect = tuple(int(v) for v in rect)
            self._origin = (self.capture_rect[0], self.capture_rect[1])
            if self.draw_mouse:
                try:
                    from .cursor_draw import CursorTracker
                    self._cursor = CursorTracker(want_sprites=True)
                except Exception as exc:
                    self._tail.append(f"cursor drawing unavailable: {exc}")
                    self._cursor = None
            if self.masker is not None and hasattr(self.masker, "set_capture_rect") \
                    and tuple(self.masker.capture_rect) != self.capture_rect:
                self.masker.set_capture_rect(self.capture_rect)
        except BaseException:
            self._close()
            raise
        return w, h

    def _grab(self) -> Optional[Frame]:
        cam = self._camera
        if cam is None:
            return None
        arr = latest_frame(cam)
        wall = wall_ms()
        if arr is None:
            return None
        np = self._np
        if arr.ndim != 3 or arr.shape[2] != 4 or (arr.shape[1], arr.shape[0]) != tuple(self._size or ()):
            self._stalled = True
            self._tail.append(f"stalled: capture size changed {self._size} -> {arr.shape}")
            return None
        # Own the pixels: masking/cursor write in place and the writer may still
        # read an older frame, so never mutate a library ring-buffer view.
        if arr.base is not None or not arr.flags.writeable or not arr.flags.c_contiguous:
            arr = np.array(arr, dtype=np.uint8, order="C", copy=True)
        masked = self._apply_mask_numpy(arr)
        if self._cursor is not None and self._cursor.draw_numpy(arr, *self._origin):
            self.cursor_frames += 1
        return Frame(memoryview(arr).cast("B"), wall, masked)

    def _interrupt(self) -> None:
        # bettercam's stop() sets its frame event, releasing a blocked get_latest_frame()
        cam = self._camera
        if cam is not None:
            try:
                cam.stop()
            except Exception:
                pass

    def _close(self) -> None:
        cam, self._camera = self._camera, None
        if cam is None:
            return
        for method in ("stop", "release"):
            try:
                getattr(cam, method)()
            except Exception:
                pass

    def _source_info(self) -> Dict:
        info = {"device_idx": self.device_idx, "output_idx": self.output_idx,
                "region": list(self._region) if self._region else None}
        if self._cursor is not None:
            info["cursor"] = self._cursor.stats()
        if self.masker is not None and hasattr(self.masker, "stats"):
            info["mask"] = self.masker.stats()
        return info

    @property
    def cursor_in_video(self) -> bool:
        return self.draw_mouse and self._cursor is not None

    # legacy name kept for callers that poked at it
    def _release_camera(self) -> None:
        self._close()
