"""Video capture via ffmpeg (gdigrab) with per-frame wall-clock timestamps.

Why showinfo
------------
gdigrab emits CFR frames; ffmpeg's ``showinfo`` filter prints one line per
frame with ``n`` and ``pts_time`` as it passes through the pipeline.  Reading
those lines in a dedicated thread gives us the frame count *and* a monotonic
timeline, which we anchor to the wall clock at the first frame.  That anchor is
what makes clip -> video_t_ms mapping exact (see docs/data-format.md).

The anchor includes a constant pipeline latency; calibrate it with
``--sync-latency-ms`` (record a few clicks, check the click ripple in the
annotation UI lines up with the on-screen feedback).

This module also holds the machinery shared by the *frame-in-Python* backends
(``dxcam`` = DXGI duplication, ``gdi`` = BitBlt): :class:`FramePacer` (CFR slot
grid) and :class:`PipedSegment` (capture thread -> bounded queue -> writer
thread -> ``ffmpeg -f rawvideo`` on stdin).

Crash safety (v0.2.1)
---------------------
* Raw segments are *fragmented* MP4 (``+frag_keyframe+empty_moov+default_base_moof``,
  a keyframe at least every 2 s): a killed ffmpeg / crashed PC still leaves a
  decodable file up to the last fragment.  The packager remuxes the final
  ``clip.mp4`` into a normal (faststart) MP4.
* A finalizing ffmpeg (after stdin EOF / ``q``) is only killed once its output
  file has stopped growing for a long time (:func:`wait_for_finalize`).
* Every segment reports ``finalized`` / ``returncode`` in ``to_dict()``; the
  recorder probes segments that did not finalize.
* On Windows the gdigrab ffmpeg is put in a Job Object with
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, so it cannot outlive the recorder
  (it ignores a closed stdin and would keep grabbing the desktop).  The piped
  ffmpegs (dxcam / gdi) are deliberately *not* in it: when the recorder dies
  their stdin pipe closes and they finalize the file by themselves (EOF; with
  an audio device ``-shortest`` ends the output together with the video, and
  ``apad`` keeps a lost microphone from ending it early).
* Every backend crops the capture to an even size the same way
  (:data:`EVEN_CROP`); ``to_dict()["output_size"]`` is the encoded size, so
  the recorder can refuse a later clip whose size differs (K3).
"""
from __future__ import annotations

import collections
import ctypes
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .clock import wall_ms
from .i18n import L

# Never pop up a console window for ffmpeg when the GUI runs under pythonw.
_POPEN_FLAGS = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW

_SHOWINFO_RE = re.compile(r"n:\s*(\d+).*?pts:\s*(-?\d+)\s+pts_time:")
_SHOWINFO_SIZE_RE = re.compile(r"\bs:(\d+)x(\d+)")

# yuv420p needs even dimensions: every backend drops the odd last column/row
# with the same filter, so a gdigrab fallback encodes the same size as dxcam/gdi.
EVEN_CROP = "crop=trunc(iw/2)*2:trunc(ih/2)*2"


def even_size(w: int, h: int) -> Tuple[int, int]:
    """Encoded (w, h) of a ``w x h`` capture (what :data:`EVEN_CROP` produces)."""
    return int(w) - int(w) % 2, int(h) - int(h) % 2

_ENCODER_ARGS = {
    "h264_nvenc": ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "{q}", "-b:v", "0"],
    "hevc_nvenc": ["-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "{q}", "-b:v", "0"],
    "h264_amf": ["-quality", "balanced", "-rc", "cqp", "-qp_i", "{q}", "-qp_p", "{q}"],
    "hevc_amf": ["-quality", "balanced", "-rc", "cqp", "-qp_i", "{q}", "-qp_p", "{q}"],
    "h264_qsv": ["-global_quality", "{q}", "-look_ahead", "1"],
    "hevc_qsv": ["-global_quality", "{q}", "-look_ahead", "1"],
    "libx264": ["-preset", "veryfast", "-crf", "{q}"],
    "libx265": ["-preset", "veryfast", "-crf", "{q}"],
}

_ENCODERS_CACHE: Optional[set] = None
_PROBE_CACHE: Dict[str, bool] = {}


def available_encoders(ffmpeg: str = "ffmpeg") -> set:
    global _ENCODERS_CACHE
    if _ENCODERS_CACHE is None:
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                                 text=True, timeout=30, creationflags=_POPEN_FLAGS).stdout
            _ENCODERS_CACHE = set(re.findall(r"^\s*\S+\s+(\w+)", out, re.M))
        except Exception:
            _ENCODERS_CACHE = set()
    return _ENCODERS_CACHE


def probe_encoder(name: str, ffmpeg: str = "ffmpeg") -> bool:
    """Actually encode a few frames: being listed by ``-encoders`` does not mean
    the hardware/driver is present (e.g. h264_nvenc without nvcuda.dll)."""
    if name in _PROBE_CACHE:
        return _PROBE_CACHE[name]
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", "testsrc2=size=320x240:rate=30", "-frames:v", "3",
           "-c:v", name, "-f", "null", "-"]
    try:
        ok = subprocess.run(cmd, capture_output=True, timeout=25,
                            creationflags=_POPEN_FLAGS).returncode == 0
    except Exception:
        ok = False
    _PROBE_CACHE[name] = ok
    return ok


# Hardware H.264 encoders `auto` may fall back to when libx264 does not work.
# HEVC is never picked automatically (Markov clips are H.264).
_AUTO_HW_H264 = ("h264_nvenc", "h264_amf", "h264_qsv")


def pick_encoder(preferred: str = "auto", ffmpeg: str = "ffmpeg") -> str:
    """``auto``: libx264 whenever it works (Markov = libx264 CRF 23); a hardware
    H.264 encoder only when libx264 is unavailable; never HEVC.  An explicit
    choice (``--codec h264_nvenc`` ...) is trusted as is."""
    if preferred and preferred != "auto":
        return preferred
    enc = available_encoders(ffmpeg)
    if "libx264" in enc and probe_encoder("libx264", ffmpeg):
        return "libx264"
    for name in _AUTO_HW_H264:
        if name in enc and probe_encoder(name, ffmpeg):
            return name
    return "libx264"


# ---------------------------------------------------------------- segment muxing
# Fragmented MP4: every fragment is self-contained, so a crash or kill leaves a
# file that decodes up to the last complete fragment (no moov-at-the-end).
SEGMENT_MOVFLAGS = "+frag_keyframe+empty_moov+default_base_moof"
KEYFRAME_INTERVAL_S = 2


def segment_output_args(fps: int) -> List[str]:
    """Muxer/GOP options shared by every raw segment.

    ``-g`` caps the GOP at 2 s for every encoder we use (libx264 keyint, NVENC /
    AMF / QSV GOP length), so a fragment holds at most 2 s of video.  (No
    ``-force_key_frames expr:``: old ffmpeg builds evaluate ``t`` on absolute
    timestamps, which would make every gdigrab frame a keyframe.)"""
    return ["-g", str(max(1, int(fps) * KEYFRAME_INTERVAL_S)), "-movflags", SEGMENT_MOVFLAGS]


def _file_size(path: str) -> int:
    """Current size of a file another process is writing (-1 if missing).

    Read through an open handle: on NTFS the size in the directory entry (what
    a by-name stat may return) can lag behind while ffmpeg holds the file open."""
    try:
        with open(path, "rb") as fh:
            return os.fstat(fh.fileno()).st_size
    except OSError:
        try:
            return os.path.getsize(path)
        except OSError:
            return -1


def wait_for_finalize(proc: Any, path: str, idle_s: float = 30.0, poll_s: float = 0.25,
                      on_poll: Optional[Callable[[], bool]] = None) -> bool:
    """Wait for an ffmpeg that was told to finish (stdin EOF / ``q``).

    Returns True once it exited by itself.  Returns False (the caller may kill
    it) only when it is stuck: its output file did not grow for ``idle_s``
    seconds, or ``on_poll()`` returned False.  It never gives up while the
    file is still growing (the encoder is flushing / the muxer is writing).
    """
    last = _file_size(path)
    quiet_since = time.monotonic()
    while True:
        try:
            proc.wait(timeout=poll_s)
            return True
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        size = _file_size(path)
        if size != last:
            last, quiet_since = size, now
        elif now - quiet_since >= idle_s:
            return False
        if on_poll is not None and on_poll() is False:
            return False


# ------------------------------------------------- Windows: children die with us
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _last_error() -> int:
    get = getattr(ctypes, "get_last_error", None)     # Windows only
    return int(get()) if get is not None else 0


class KillOnCloseJob:
    """A Job Object whose processes are killed when its last handle closes.

    The handle is held for the life of the recorder process and never closed,
    so Windows closes it when the process ends for *any* reason (crash, Task
    Manager, sign-out, a GUI that vanished on WM_CLOSE): the gdigrab ffmpeg,
    which would otherwise keep grabbing the desktop, cannot outlive us.  Only
    gdigrab goes in: a piped ffmpeg must be left to finalize on stdin EOF.
    ``kernel32`` can be a fake for tests; nothing touches Win32 at import time.
    """

    def __init__(self, kernel32: Any = None):
        self._k32 = kernel32
        self._lock = threading.Lock()
        self.handle: Any = None
        self.assigned = 0
        self.error: Optional[str] = None

    def _kernel32(self) -> Any:
        if self._k32 is None:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            k32.CreateJobObjectW.restype = ctypes.c_void_p
            k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                    ctypes.c_uint32]
            k32.SetInformationJobObject.restype = ctypes.c_int
            k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k32.AssignProcessToJobObject.restype = ctypes.c_int
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            k32.CloseHandle.restype = ctypes.c_int
            self._k32 = k32
        return self._k32

    def _ensure(self) -> Any:
        if self.handle:
            return self.handle
        k32 = self._kernel32()
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(f"CreateJobObjectW failed ({_last_error()})")
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            err = _last_error()
            try:
                k32.CloseHandle(job)
            except Exception:
                pass
            raise OSError(f"SetInformationJobObject failed ({err})")
        self.handle = job
        return job

    def assign(self, proc: Any) -> bool:
        """Put a started ``subprocess.Popen`` in the job (best effort, never raises)."""
        handle = getattr(proc, "_handle", None)
        if handle is None:
            return False
        with self._lock:
            try:
                job = self._ensure()
                if not self._kernel32().AssignProcessToJobObject(job, int(handle)):
                    raise OSError(f"AssignProcessToJobObject failed ({_last_error()})")
            except Exception as exc:
                self.error = str(exc)
                return False
            self.assigned += 1
            return True


_KILL_JOB: Optional[KillOnCloseJob] = None


def assign_to_kill_job(proc: Any, log: Optional[Callable[[str], Any]] = None) -> bool:
    """Make ``proc`` die with this process (Windows Job Object); no-op elsewhere.

    For the gdigrab ffmpeg only (:class:`VideoSegment`); never for a piped one."""
    global _KILL_JOB
    if sys.platform != "win32" or proc is None:
        return False
    if _KILL_JOB is None:
        _KILL_JOB = KillOnCloseJob()
    ok = _KILL_JOB.assign(proc)
    if not ok and log is not None:
        try:
            log(f"could not tie ffmpeg to the recorder's lifetime: {_KILL_JOB.error}")
        except Exception:
            pass
    return ok


def primary_monitor_rect() -> Optional[Tuple[int, int, int, int]]:
    """(x, y, w, h) of the primary monitor (``--source desktop`` = one display)."""
    if sys.platform != "win32":
        return None
    try:
        from .win_window import list_monitors
        mons = list_monitors()
    except Exception:
        return None
    for m in mons:
        if m.get("primary"):
            return (int(m["originX"]), int(m["originY"]), int(m["width"]), int(m["height"]))
    return None


def _finalize_fields(rc: Optional[int], killed: bool, clean: bool = True) -> Dict[str, Any]:
    """K1: ``finalized`` = ffmpeg exited 0 by itself after EOF / ``q``."""
    return {"finalized": bool(rc == 0 and not killed and clean), "returncode": rc}


class VideoSegment:
    """One continuous ffmpeg run = one clip in the final deliverable."""

    backend = "gdigrab"

    def __init__(
        self,
        index: int,
        path: str,
        fps: int,
        source: str = "desktop",
        video_size: Optional[Tuple[int, int]] = None,
        offset: Optional[Tuple[int, int]] = None,
        encoder: str = "auto",
        quality: int = 23,
        audio_device: Optional[str] = None,
        draw_mouse: bool = True,
        sync_latency_ms: float = 0.0,
        ffmpeg: str = "ffmpeg",
        capture_rect: Optional[Tuple[int, int, int, int]] = None,
        **_ignored: Any,
    ):
        self.index = index
        self.path = path
        self.fps = fps
        self.encoder = pick_encoder(encoder, ffmpeg)
        self.quality = quality
        self.audio_device = audio_device
        self.draw_mouse = draw_mouse
        self.source = source
        self.video_size = video_size
        self.offset = offset
        self.sync_latency_ms = sync_latency_ms
        self.ffmpeg = ffmpeg
        # (x, y, w, h) shown by the video, for capture.json (informational)
        self.capture_rect = capture_rect
        self.masked_frames = 0          # gdigrab never masks (app mode rejects it)
        self._cmd: List[str] = []
        self._grab_size: Optional[Tuple[int, int]] = None    # -video_size we asked for
        self._shown_size: Optional[Tuple[int, int]] = None   # encoded size seen by showinfo

        self.frames: List[Tuple[int, float]] = []  # (frame_index, wall_ms)
        self.start_wall_ms: float = 0.0
        self.end_wall_ms: float = 0.0
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._first_arrival: Optional[float] = None
        self._anchor: Optional[float] = None
        self._tail: "collections.deque[str]" = collections.deque(maxlen=60)
        self._err_fh = None
        self._clock_offset_ms = wall_ms() - time.time() * 1000.0
        self.returncode: Optional[int] = None
        self._killed = False
        self._stopped = False

    # ------------------------------------------------------------------- public
    def start(self) -> "VideoSegment":
        executable = shutil.which(self.ffmpeg)
        if executable is None:
            raise FileNotFoundError(L("找不到 FFmpeg：{p}；请重新安装 D Studio 或用 --ffmpeg 指定完整路径",
                                      "FFmpeg not found: {p}; reinstall D Studio or pass the full path with --ffmpeg",
                                      p=self.ffmpeg))
        self.ffmpeg = executable
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        cmd = self._build_cmd()
        self._cmd = cmd
        self.start_wall_ms = wall_ms()
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, bufsize=1, text=True, errors="replace",
            creationflags=_POPEN_FLAGS,
        )
        assign_to_kill_job(self._proc, self._tail.append)
        self._err_fh = open(self.path + ".log", "w", encoding="utf-8", buffering=1)
        self._reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        return self

    def request_stop(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.write("q")
                proc.stdin.flush()
            except Exception:
                pass

    def stop(self, timeout: float = 15.0) -> "VideoSegment":
        """Send ``q`` and wait for ffmpeg to finalize.

        ``timeout`` is how long the output may stay unchanged before ffmpeg is
        considered stuck; while the file keeps growing it is never killed.  If
        gdigrab keeps *capturing* (frames still arriving) more than ``timeout``
        seconds after ``q``, it ignored the request and is terminated (the
        fragmented MP4 stays decodable up to the last fragment).
        """
        self.request_stop()
        proc = self._proc
        if proc and proc.poll() is None:
            q_at = time.monotonic()
            state = {"n": len(self.frames), "nudge": q_at + 2.0}

            def still_finishing() -> bool:
                now = time.monotonic()
                if now >= state["nudge"]:          # 'q' is read by polling: repeat it
                    state["nudge"] = now + 2.0
                    self.request_stop()
                capturing = len(self.frames) > state["n"] + self.fps
                return not (capturing and now - q_at > timeout)

            if not wait_for_finalize(proc, self.path, idle_s=timeout, on_poll=still_finishing):
                self._tail.append(f"ffmpeg did not finish after 'q' ({timeout:.0f}s without progress); terminating")
                self._killed = True
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        if proc is not None:
            self.returncode = proc.poll()
            if self.returncode is not None and proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
        if self._reader:
            self._reader.join(timeout=5)
        if proc is not None and proc.stderr is not None and not (self._reader and self._reader.is_alive()):
            try:
                proc.stderr.close()             # reader hit EOF: release the pipe now
            except Exception:
                pass
        self.end_wall_ms = wall_ms()
        if not self.frames and self.start_wall_ms:
            self.end_wall_ms = self.start_wall_ms  # nothing captured
        if self._err_fh:
            self._err_fh.close()
            self._err_fh = None
        self._stopped = True
        return self

    @property
    def finalized(self) -> bool:
        return _finalize_fields(self._returncode_now(), self._killed)["finalized"]

    def _returncode_now(self) -> Optional[int]:
        if self.returncode is not None:
            return self.returncode
        return self._proc.poll() if self._proc is not None else None

    @property
    def error_tail(self) -> str:
        return chr(10).join(self._tail)

    @property
    def crashed(self) -> bool:
        return bool(self._proc and self._proc.poll() not in (None, 0))

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def first_frame_wall(self) -> Optional[float]:
        with self._lock:
            return self.frames[0][1] if self.frames else None

    @property
    def cursor_in_video(self) -> bool:
        return bool(self.draw_mouse)

    @property
    def masked_now(self) -> bool:
        return False

    @property
    def output_size(self) -> Optional[Tuple[int, int]]:
        """Encoded (w, h): what showinfo reported, else the requested size cropped
        to even (None for a window whose size is not known yet)."""
        if self._shown_size:
            return self._shown_size
        return even_size(*self._grab_size) if self._grab_size else None

    def capture_info(self) -> Dict:
        return {"timestamp_source": "gdigrab_wallclock_pts", "time_base": f"1/{self.fps}",
                "sync_latency_ms": self.sync_latency_ms, "draw_mouse": bool(self.draw_mouse),
                "capture_rect": _rect_dict(self.capture_rect)}

    def to_dict(self) -> Dict:
        with self._lock:
            frames = [[n, round(t, 3)] for n, t in list(self.frames)]
        d = {
            "index": self.index,
            "file": os.path.basename(self.path),
            "start_wall_ms": round(self.start_wall_ms, 3),
            "end_wall_ms": round(self.end_wall_ms, 3),
            "fps": self.fps,
            "frame_count": len(frames),
            "frames": frames,
            "encoder": self.encoder,
            "backend": self.backend,
            "capture": self.capture_info(),
            "output_size": list(self.output_size) if self.output_size else None,
            "cmd": " ".join(self._cmd),
            "in_progress": bool(self._proc is not None and not self._stopped),
        }
        d.update(_finalize_fields(self._returncode_now(), self._killed))
        return d

    # ------------------------------------------------------------------ internals
    def _geometry(self) -> Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int]]]:
        """(offset, size) for gdigrab; ``capture_rect`` / the primary monitor when unset."""
        offset, size = self.offset, self.video_size
        if self.source.startswith("window:") or (offset and size):
            return offset, size
        rect = self.capture_rect
        if not rect and self.source == "desktop":
            rect = primary_monitor_rect()       # --source desktop = one display (the primary)
            if rect:
                self.capture_rect = rect
        if rect:
            x, y, w, h = (int(v) for v in rect)
            return (x, y), (w, h)
        return offset, size

    def _build_cmd(self) -> List[str]:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats", "-copyts"]
        if self.audio_device:
            cmd += ["-f", "dshow", "-i", f"audio={self.audio_device}"]
        cmd += ["-use_wallclock_as_timestamps", "1", "-f", "gdigrab", "-framerate", str(self.fps), "-draw_mouse",
                "1" if self.draw_mouse else "0"]
        offset, size = self._geometry()
        self._grab_size = (int(size[0]), int(size[1])) if size else None
        if offset:
            cmd += ["-offset_x", str(offset[0]), "-offset_y", str(offset[1])]
        if size:
            cmd += ["-video_size", f"{size[0]}x{size[1]}"]
        if self.source.startswith("window:"):
            cmd += ["-i", f"title={self.source.split(':', 1)[1]}"]
        else:
            cmd += ["-i", "desktop"]

        q = str(self.quality)
        # Normalize to CFR *before* showinfo: then the frame index we record is
        # exactly the frame index in the final video, even when gdigrab stalls
        # during startup (ffmpeg inserts duplicates to fill the gap).  Even size
        # by cropping, exactly like the piped backends (K3: same encoded size).
        cmd += ["-filter_complex",
                f"[{1 if self.audio_device else 0}:v]{EVEN_CROP},fps={self.fps},showinfo,setpts=PTS-STARTPTS[v]",
                "-map", "[v]"]
        cmd += ["-c:v", self.encoder] + [a.replace("{q}", q) for a in _ENCODER_ARGS.get(self.encoder, _ENCODER_ARGS["libx264"])]
        cmd += ["-pix_fmt", "yuv420p"] + segment_output_args(self.fps)
        if self.audio_device:
            cmd += ["-map", "0:a?", "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
        cmd += ["-y", self.path]
        return cmd

    def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for line in self._proc.stderr:
            m = _SHOWINFO_RE.search(line)
            if not m:
                line = line.rstrip()
                if line:
                    self._tail.append(line)
                    if self._err_fh:
                        self._err_fh.write(line + chr(10))
                continue
            if self._shown_size is None:
                ms = _SHOWINFO_SIZE_RE.search(line)
                if ms:
                    self._shown_size = (int(ms.group(1)), int(ms.group(2)))
            n = int(m.group(1))
            # Integer PTS avoids showinfo's rounded scientific notation at epoch scale.
            frame_wall = int(m.group(2)) * 1000.0 / self.fps + self._clock_offset_ms - self.sync_latency_ms
            with self._lock:
                self.frames.append((n, frame_wall))


# ============================================================================
# Shared machinery for frame-in-Python backends (dxcam, gdi)
# ============================================================================
def _rect_dict(rect: Optional[Tuple[int, int, int, int]]) -> Optional[Dict[str, int]]:
    if not rect:
        return None
    x, y, w, h = (int(v) for v in rect)
    return {"x": x, "y": y, "width": w, "height": h}


class FramePacer:
    """Maps arbitrary capture timestamps onto a fixed CFR slot grid.

    Slot ``k`` is displayed at ``t0 + k * 1000/fps``.  A capture at wall time
    ``c`` owns slot ``floor((c - t0)/dt)``; every slot between the previous
    capture and that one is filled with a duplicate of the previous frame, so
    the encoded video always advances at exactly ``fps`` (any fps, 1..120).
    """

    def __init__(self, fps: int, t0: float):
        self.dt = 1000.0 / fps
        self.t0 = t0
        self._k = 0

    def slot_of(self, capture_wall: float) -> int:
        """The slot whose display interval contains ``capture_wall``."""
        # +1e-9: 1000/33.3333... must land on slot 30, not 29 (float edge)
        return int(math.floor((capture_wall - self.t0) / self.dt + 1e-9))

    def is_early(self, capture_wall: float) -> bool:
        """True when the capture's slot is already filled.  Such a capture must
        be dropped, not pushed into the next slot: a source that runs a little
        fast (bettercam's ``1000 // fps`` ms timer: 30.3 Hz at 30 fps, 62.5 Hz
        at 60 fps) would otherwise make the video run ahead of wall time."""
        return self.slot_of(capture_wall) < self._k

    def advance(self, capture_wall: float) -> Tuple[int, int]:
        """Return the inclusive slot range this capture should fill.

        Callers drop early captures first (:meth:`is_early`); the clamp below
        only keeps the grid monotonic if one slips through."""
        target = max(self.slot_of(capture_wall), self._k)
        first, self._k = self._k, target + 1
        return first, target

    def slot_time(self, slot: int) -> float:
        return self.t0 + slot * self.dt

    @property
    def next_slot(self) -> int:
        return self._k


class Frame:
    """One captured, already masked / cursor-drawn frame.

    ``buf`` is a 1-D, C-contiguous bytes-like view of ``w*h*4`` BGRA bytes that
    is written to ffmpeg *without copying*; ``release()`` hands pooled memory
    back to the capture backend once the writer is done with it.
    """

    __slots__ = ("buf", "wall", "masked", "_release")

    def __init__(self, buf, wall: float, masked: bool = False, release=None):
        self.buf = buf
        self.wall = wall
        self.masked = masked
        self._release = release

    def release(self) -> None:
        fn, self._release = self._release, None
        if fn is not None:
            fn()


class PipedSegment:
    """One continuous capture -> ``ffmpeg -f rawvideo -i -`` run (= one clip).

    Same public interface as :class:`VideoSegment` (start/stop/request_stop,
    frames, frame_count, crashed, error_tail, to_dict).  Subclasses implement
    the source hooks:

    * ``_open() -> (w, h)``      validate + size, raises if unusable (auto falls back)
    * ``_thread_setup()``         per-capture-thread resources (GDI DCs...)
    * ``_grab() -> Frame|None``   one frame, masked + cursor drawn; None = no new frame
    * ``_wait_until(due_wall)``   pace sources that are not self-timed
    * ``_thread_teardown()`` / ``_close()``

    Threads: the capture thread timestamps every grab and maps it onto the CFR
    slot grid; frames go through a *bounded* queue to a writer thread that
    feeds ffmpeg.  When the encoder falls behind, the queue fills and new
    grabs are skipped: the missed slots become duplicates of the last frame
    (visible as ``duplicated_slots`` / ``encoder_drops``) instead of stalling
    the capture clock.  More than ``max_backlog_s`` of owed slots, 2 s without
    a frame, or a dead pipe marks the segment ``crashed``; the recorder then
    starts a fresh segment (a new clip).

    Timing rules of the per-frame sidecar (``frames`` = ``[slot, wall_ms]``):

    * a capture whose slot is already filled is dropped (``early_captures``),
      so the slot index never runs ahead of wall time;
    * a duplicate-padded slot gets its *nominal* time ``t0 + slot * dt``; the
      wall time of the frame it repeats is kept in ``duplicate_runs``;
    * ``--sync-latency-ms`` is subtracted from every sidecar wall (the slot grid
      itself stays on raw capture time, so pacing is unaffected).
    """

    backend = "piped"
    paced = False        # True: base sleeps until each slot (sources without their own clock)
    # stop(): seconds without the output file growing before ffmpeg counts as stuck
    finalize_idle_s = 30.0      # after stdin EOF (finalizing)
    stuck_idle_s = 40.0         # capture/writer threads still busy

    def __init__(
        self,
        index: int,
        path: str,
        fps: int,
        source: str = "desktop",
        video_size: Optional[Tuple[int, int]] = None,
        offset: Optional[Tuple[int, int]] = None,
        encoder: str = "auto",
        quality: int = 23,
        audio_device: Optional[str] = None,
        draw_mouse: bool = True,
        sync_latency_ms: float = 0.0,
        ffmpeg: str = "ffmpeg",
        masker: Any = None,
        capture_rect: Optional[Tuple[int, int, int, int]] = None,
        monitor_device: Optional[str] = None,
        max_backlog_s: float = 5.0,
        queue_frames: Optional[int] = None,
        stall_ms: float = 2000.0,
        **_ignored: Any,
    ):
        self.index = index
        self.path = path
        self.fps = int(fps)
        self.source = source
        self.video_size = video_size
        self.offset = offset
        self.encoder = pick_encoder(encoder, ffmpeg)
        self.quality = quality
        self.audio_device = audio_device
        self.draw_mouse = bool(draw_mouse)
        self.sync_latency_ms = sync_latency_ms
        self.ffmpeg = ffmpeg
        self.masker = masker
        self.capture_rect = tuple(capture_rect) if capture_rect else None
        self.monitor_device = monitor_device
        self.max_backlog_frames = max(1, int(max_backlog_s * self.fps))
        self.queue_frames = queue_frames
        self.stall_ms = float(stall_ms)
        self.name = self.backend

        self.frames: List[Tuple[int, float]] = []
        self.start_wall_ms: float = 0.0
        self.end_wall_ms: float = 0.0
        self.dropped_captures = 0     # grab returned no new frame
        self.duplicated_slots = 0     # CFR slots filled with the previous frame
        self.encoder_drops = 0        # grabs skipped because the encoder queue was full
        self.captures = 0             # unique frames sent to the encoder
        self.masked_frames = 0        # captured frames that had any black rectangle
        self.target_hidden_frames = 0
        self.mask_errors = 0
        self.cursor_frames = 0
        self.early_captures = 0       # grabs whose slot was already filled (dropped)
        self.duplicate_runs: List[List[float]] = []   # [first_slot, last_slot, content wall]
        self.duplicate_runs_dropped = 0
        self._masked_now = False
        self._rate: Optional[float] = None
        self._capture_rate: Optional[float] = None
        self._latency = float(sync_latency_ms or 0.0)
        self._t0: Optional[float] = None  # raw capture wall of slot 0
        self.returncode: Optional[int] = None
        self._killed = False
        self._stopped = False

        self._size: Optional[Tuple[int, int]] = None
        self._cmd: List[str] = []
        self._proc: Optional[subprocess.Popen] = None
        self._queue: "queue.Queue" = queue.Queue(maxsize=2)
        self._thread: Optional[threading.Thread] = None
        self._writer: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._first_ready = threading.Event()
        self._start_error: Optional[BaseException] = None
        self._writer_failed = False
        self._stalled = False
        self._teardown_skipped = False
        self._last_activity = 0.0         # wall ms of the last grab attempt (watchdog)
        self._tail: "collections.deque[str]" = collections.deque(maxlen=200)
        self._err_fh = None
        self._stderr_thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- source hooks
    def _open(self) -> Tuple[int, int]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _thread_setup(self) -> None:
        pass

    def _grab(self) -> Optional[Frame]:  # pragma: no cover - abstract
        raise NotImplementedError

    def _thread_teardown(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _interrupt(self) -> None:
        """Unblock a ``_grab`` that may wait forever (called by stop() before joining)."""

    def _wait_until(self, due_wall: float) -> None:
        while not self._stop.is_set():
            rem = due_wall - wall_ms()
            if rem <= 0:
                return
            time.sleep(min(rem, 50.0) / 1000.0)

    def _source_info(self) -> Dict:
        return {}

    # ----------------------------------------------------------------------- public
    def start(self) -> "PipedSegment":
        w, h = self._open()
        try:
            self._size = (int(w), int(h))
            frame_bytes = max(1, int(w) * int(h) * 4)
            qn = self.queue_frames or max(2, min(6, (96 << 20) // frame_bytes))
            self._queue = queue.Queue(maxsize=int(qn))
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            self._cmd = self._build_cmd(int(w), int(h))
            self._proc = subprocess.Popen(
                self._cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, bufsize=0, creationflags=_POPEN_FLAGS,
            )
            # Not in the kill-on-close job (only gdigrab is): if the recorder dies,
            # stdin closes and this ffmpeg finalizes the file itself (K10).
            self._err_fh = open(self.path + ".log", "w", encoding="utf-8", buffering=1)
            self._stderr_thread = threading.Thread(target=self._read_stderr, name=f"{self.backend}-stderr",
                                                   daemon=True)
            self._stderr_thread.start()
            self._writer = threading.Thread(target=self._writer_loop, name=f"{self.backend}-writer",
                                            daemon=True)
            self._writer.start()
            self._thread = threading.Thread(target=self._capture_loop, name=f"{self.backend}-capture",
                                            daemon=True)
            self._thread.start()
            if not self._first_ready.wait(15):
                raise RuntimeError(L("采集超时：15 秒内没有拿到第一帧", "Capture timed out: no first frame within 15 s"))
            if self._start_error is not None:
                raise self._start_error
        except BaseException:
            self._abort()
            raise
        return self

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self) -> "PipedSegment":
        self._stop.set()
        try:
            self._interrupt()
        except Exception as exc:
            self._tail.append(f"interrupt error: {exc}")
        t = self._thread
        if t is not None and t is not threading.current_thread():
            # waits as long as ffmpeg keeps writing (a slow encoder draining the queue)
            if not self._join_while_growing(t, idle_s=self.stuck_idle_s):
                # stuck in a grab (writer idle) or the writer blocks on a hung encoder
                if not self._end_stream():
                    self._tail.append(f"capture thread stuck ({self.stuck_idle_s:.0f}s without progress); "
                                      "killing ffmpeg")
                    self._kill_proc()
                t.join(timeout=10)
        self._close()
        w = self._writer
        if w is not None and w is not threading.current_thread() and w.is_alive():
            if not self._join_while_growing(w, idle_s=self.finalize_idle_s):
                self._tail.append(f"writer blocked on ffmpeg ({self.finalize_idle_s:.0f}s without progress); "
                                  "killing ffmpeg")
                self._kill_proc()
                w.join(timeout=5)
        proc = self._proc
        if proc is not None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()            # EOF: ffmpeg finalizes the file
            except Exception:
                pass
            if not wait_for_finalize(proc, self.path, idle_s=self.finalize_idle_s):
                self._tail.append(f"ffmpeg did not finish (output unchanged for {self.finalize_idle_s:.0f}s); "
                                  "killing it")
                self._kill_proc()
            self.returncode = proc.poll()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=5)
        self._close_pipes()
        if self._err_fh:
            try:
                self._err_fh.close()
            except Exception:
                pass
            self._err_fh = None
        self.end_wall_ms = wall_ms()
        if self.frames:
            last_slot = self.frames[-1][0]
            self.end_wall_ms = max(self.frames[-1][1], self.start_wall_ms + (last_slot + 1) * 1000.0 / self.fps)
        else:
            self.end_wall_ms = self.start_wall_ms
        self._stopped = True
        return self

    def _join_while_growing(self, thread: threading.Thread, idle_s: float) -> bool:
        """Join ``thread``; give up only after ``idle_s`` without the output file growing."""
        last = _file_size(self.path)
        quiet_since = time.monotonic()
        while thread.is_alive():
            thread.join(timeout=0.25)
            now = time.monotonic()
            size = _file_size(self.path)
            if size != last:
                last, quiet_since = size, now
            elif now - quiet_since >= idle_s and thread.is_alive():
                return False
        return True

    def _end_stream(self) -> bool:
        """Capture thread stuck: if the writer is idle, hand it end-of-stream so
        ffmpeg still gets a clean EOF (and finalizes) instead of being killed."""
        w = self._writer
        if w is None or not w.is_alive():
            return True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return False                            # writer is not consuming: hung encoder
        w.join(timeout=5)
        return not w.is_alive()

    @property
    def finalized(self) -> bool:
        return self._finalize_info()["finalized"]

    def _finalize_info(self) -> Dict[str, Any]:
        rc = self.returncode
        if rc is None and self._proc is not None:
            rc = self._proc.poll()
        return _finalize_fields(rc, self._killed, clean=not self._writer_failed)

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def first_frame_wall(self) -> Optional[float]:
        return self.frames[0][1] if self.frames else (self.start_wall_ms or None)

    @property
    def error_tail(self) -> str:
        return chr(10).join(list(self._tail)[-60:])

    @property
    def crashed(self) -> bool:
        if self._proc is not None and self._proc.poll() not in (None, 0):
            return True
        if self._stalled or self._writer_failed:
            return True
        # watchdog: a grab that never returns (lost DXGI output, hung driver)
        if self._first_ready.is_set() and self._last_activity and not self._stop.is_set():
            if wall_ms() - self._last_activity > max(5000.0, 3 * self.stall_ms):
                if not self._stalled:
                    self._tail.append("stalled: capture thread stopped responding")
                self._stalled = True
                return True
        return False

    @property
    def cursor_in_video(self) -> bool:
        return self.draw_mouse

    @property
    def masked_now(self) -> bool:
        return self._masked_now

    @property
    def output_size(self) -> Optional[Tuple[int, int]]:
        """Encoded (w, h) = the capture size cropped to even (:data:`EVEN_CROP`)."""
        return even_size(*self._size) if self._size else None

    def capture_info(self) -> Dict:
        info = {
            "api": self.name,
            "size": list(self._size) if self._size else None,
            "capture_rect": _rect_dict(self.capture_rect),
            "monitor_device": self.monitor_device,
            "dropped_captures": self.dropped_captures,
            "duplicated_slots": self.duplicated_slots,
            "encoder_drops": self.encoder_drops,
            "captures": self.captures,
            "observed_fps": self._rate,
            "capture_fps": self._capture_rate,
            "queue_frames": self._queue.maxsize,
            "cursor_in_video": self.cursor_in_video,
            "masked": self.masker is not None,
            "masked_frames": self.masked_frames,
            "target_hidden_frames": self.target_hidden_frames,
            "mask_errors": self.mask_errors,
            "cursor_frames": self.cursor_frames,
            "early_captures": self.early_captures,
            "sync_latency_ms": self._latency,
        }
        info.update(self._source_info())
        return info

    def to_dict(self) -> Dict:
        frames = [[n, round(t, 3)] for n, t in list(self.frames)]
        d = {
            "index": self.index,
            "file": os.path.basename(self.path),
            "start_wall_ms": round(self.start_wall_ms, 3),
            "end_wall_ms": round(self.end_wall_ms, 3),
            "fps": self.fps,
            "frame_count": len(frames),
            "frames": frames,
            # content-capture wall of duplicate-padded slots (frames[] has their nominal time)
            "duplicate_runs": [list(r) for r in list(self.duplicate_runs)],
            "encoder": self.encoder,
            "backend": self.backend,
            "capture": self.capture_info(),
            "output_size": list(self.output_size) if self.output_size else None,
            "cmd": " ".join(self._cmd),
            "in_progress": bool(self._proc is not None and not self._stopped),
        }
        if self.duplicate_runs_dropped:
            d["duplicate_runs_dropped"] = self.duplicate_runs_dropped
        d.update(self._finalize_info())
        return d

    # ---------------------------------------------------------------- per-frame helpers
    def _mask_rects(self) -> List[Tuple[int, int, int, int]]:
        """Black rectangles for the frame just grabbed (fails closed: whole frame)."""
        try:
            res = self.masker.compute()
            rects = list(res.black_rects)
            visible = bool(res.target_visible)
        except Exception as exc:
            self.mask_errors += 1
            self._tail.append(f"mask error (frame blacked out): {type(exc).__name__}: {exc}")
            w, h = self._size or (0, 0)
            rects, visible = ([(0, 0, w, h)] if w and h else []), False
        if not visible:
            self.target_hidden_frames += 1
        self._masked_now = bool(rects)
        return rects

    def _apply_mask_numpy(self, arr) -> bool:
        """Mask a numpy BGRA frame in place; returns whether anything was painted."""
        if self.masker is None:
            return False
        rects = self._mask_rects()
        if rects:
            from .app_mask import apply_mask
            apply_mask(arr, rects)
        return bool(rects)

    # --------------------------------------------------------------------- internals
    def _build_cmd(self, w: int, h: int) -> List[str]:
        q = str(self.quality)
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats",
               "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{w}x{h}",
               "-r", str(self.fps), "-i", "-"]
        if self.audio_device:
            cmd += ["-f", "dshow", "-i", f"audio={self.audio_device}"]
        if w % 2 or h % 2:
            cmd += ["-vf", EVEN_CROP]
        cmd += ["-c:v", self.encoder] + [a.replace("{q}", q) for a in
                                         _ENCODER_ARGS.get(self.encoder, _ENCODER_ARGS["libx264"])]
        cmd += ["-pix_fmt", "yuv420p"] + segment_output_args(self.fps)
        if self.audio_device:
            # -shortest: the live dshow input never ends, so without it ffmpeg would
            # ignore stdin EOF and never finalize (stop would wait, then kill it).
            # apad: the audio can then never end first, so losing the microphone
            # mid-recording (unplugged, driver reset) does not end the video too.
            cmd += ["-map", "0:v", "-map", "1:a?", "-af", "apad",
                    "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-shortest"]
        cmd += ["-y", self.path]
        return cmd

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self._tail.append(line)
                fh = self._err_fh
                if fh:
                    try:
                        fh.write(line + chr(10))
                    except Exception:
                        pass
        except Exception:
            pass

    def _grab_first(self) -> Frame:
        deadline = wall_ms() + 5000.0
        while not self._stop.is_set():
            frame = self._grab()
            if frame is not None:
                return frame
            if wall_ms() > deadline:
                break
            time.sleep(0.005)
        raise RuntimeError(L("采集源没有返回画面（可能是安全桌面、锁屏或显卡驱动问题）",
                             "The capture source returned no frames (secure desktop, lock screen or driver issue)"))

    def _capture_loop(self) -> None:
        try:
            from .win_window import ensure_thread_dpi_awareness
            ensure_thread_dpi_awareness()
        except Exception:
            pass
        setup_done = False
        try:
            try:
                setup_done = True              # teardown copes with a partial setup
                self._thread_setup()
                first = self._grab_first()
            except BaseException as exc:
                self._start_error = exc
                return
            t0 = first.wall
            self._t0 = t0
            self.start_wall_ms = t0 - self._latency
            self._last_activity = wall_ms()
            pacer = FramePacer(self.fps, t0)
            pacer.advance(t0)                       # slot 0 = the bootstrap frame
            self._count(first)
            self._queue.put((first, 0, 0))
            self._first_ready.set()
            self._run_loop(pacer, t0)
        except BaseException as exc:  # never die silently: the recorder restarts us
            self._stalled = True
            self._tail.append(f"capture thread error: {type(exc).__name__}: {exc}")
        finally:
            self._first_ready.set()
            self._finish_writer()
            if setup_done:
                if self._writer is not None and self._writer.is_alive():
                    # pooled buffers may still be read by the writer: leak, never free in use
                    self._teardown_skipped = True
                    self._tail.append("writer still busy; capture resources left for process exit")
                else:
                    try:
                        self._thread_teardown()
                    except Exception as exc:
                        self._tail.append(f"teardown error: {exc}")

    def _count(self, frame: Frame) -> None:
        self.captures += 1
        if frame.masked:
            self.masked_frames += 1

    def _run_loop(self, pacer: FramePacer, t0: float) -> None:
        owed = 1                                    # next slot not yet handed to the writer
        last_new = t0
        last_rate = t0
        dt = 1000.0 / self.fps
        q = self._queue
        while not self._stop.is_set() and not self._writer_failed and not self._stalled:
            if self.paced:
                self._wait_until(pacer.slot_time(pacer.next_slot) + 1.0)
                if self._stop.is_set():
                    break
            if q.full():
                # Encoder behind: skip this grab.  The pacer is NOT advanced here
                # (that would consume slots faster than real time); the next
                # real capture claims its slot and every slot in between becomes
                # a duplicate of the last frame.
                self.encoder_drops += 1
                now = self._last_activity = wall_ms()
                elapsed_slot = int((now - t0) // dt)
                if elapsed_slot - owed > self.max_backlog_frames:
                    self._stalled = True
                    self._tail.append(f"stalled: encoder backlog {elapsed_slot - owed} frames")
                    break
                if self.paced:
                    self._wait_until(t0 + (elapsed_slot + 1) * dt + 1.0)
                else:
                    self._stop.wait(min(dt, 5.0) / 1000.0)
                continue
            frame = self._grab()
            self._last_activity = wall_ms()
            if frame is None:
                self.dropped_captures += 1
                now = wall_ms()
                if now - last_new > self.stall_ms:
                    self._stalled = True
                    self._tail.append(f"stalled: no new frame for {int(now - last_new)}ms")
                    break
                self._stop.wait((min(dt, 10.0) if self.paced else 1.0) / 1000.0)
                continue
            last_new = frame.wall
            if pacer.is_early(frame.wall):
                # this slot already has a frame: drop the capture so slots stay tied
                # to wall time (never push it into the next slot = video runs fast)
                frame.release()
                self.early_captures += 1
                continue
            _first, target = pacer.advance(frame.wall)
            if target - owed > self.max_backlog_frames:
                frame.release()
                self._stalled = True
                self._tail.append(f"stalled: capture/encoder backlog {target - owed} frames")
                break
            try:
                q.put_nowait((frame, owed, target))
            except queue.Full:
                frame.release()
                self.encoder_drops += 1
                continue
            self._count(frame)
            owed = target + 1
            if frame.wall - last_rate >= 1000.0:
                last_rate = frame.wall
                elapsed = (frame.wall - t0) / 1000.0
                if elapsed > 0:
                    self._rate = round(owed / elapsed, 2)
                    self._capture_rate = round(self.captures / elapsed, 2)

    def _finish_writer(self) -> None:
        """Hand the writer its end-of-stream marker and wait for it to drain."""
        w = self._writer
        while w is not None and w.is_alive():
            try:
                self._queue.put(None, timeout=0.5)
                break
            except queue.Full:
                continue
        if w is not None:
            w.join(timeout=30)
        if w is None or not w.is_alive():
            while True:                               # writer gone: return leftovers
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    item[0].release()

    _MAX_DUPLICATE_RUNS = 5000

    def _note_duplicate_run(self, first: int, last: int, content_wall: float) -> None:
        if len(self.duplicate_runs) < self._MAX_DUPLICATE_RUNS:
            self.duplicate_runs.append([first, last, round(content_wall - self._latency, 3)])
        else:
            self.duplicate_runs_dropped += 1

    def _writer_loop(self) -> None:
        proc = self._proc
        stdin = proc.stdin if proc is not None else None
        last: Optional[Frame] = None
        dt = 1000.0 / self.fps
        lat = self._latency
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                frame, first, target = item
                t0 = self._t0 if self._t0 is not None else frame.wall
                try:
                    dup = last if last is not None else frame
                    if target > first:
                        self._note_duplicate_run(first, target - 1, dup.wall)
                    for slot in range(first, target):
                        self._write(stdin, dup.buf)
                        # nominal slot time: events during a stall stay spread over its slots
                        self.frames.append((slot, t0 + slot * dt - lat))
                        self.duplicated_slots += 1
                    self._write(stdin, frame.buf)
                    self.frames.append((target, frame.wall - lat))
                except BaseException:
                    frame.release()
                    raise
                if last is not None and last is not frame:
                    last.release()
                last = frame
        except Exception as exc:  # broken pipe, closed file, bad buffer...
            self._writer_failed = True
            self._tail.append(f"ffmpeg pipe closed: {type(exc).__name__}: {exc}")
        finally:
            if last is not None:
                last.release()
            while True:  # release anything still queued so pooled buffers return
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    item[0].release()
            try:
                if stdin is not None:
                    stdin.close()
            except Exception:
                pass

    @staticmethod
    def _write(stdin, buf) -> None:
        # A blocking pipe write may be partial for multi-MB frames; loop.
        view = buf if isinstance(buf, memoryview) else memoryview(buf)
        if view.ndim != 1 or view.itemsize != 1:
            view = view.cast("B")
        while view.nbytes:
            written = stdin.write(view)
            if not written:
                raise BrokenPipeError("ffmpeg stdin closed")
            view = view[written:]

    def _close_pipes(self) -> None:
        proc = self._proc
        if proc is None:
            return
        for pipe in (proc.stdin, proc.stderr):
            try:
                if pipe is not None and not pipe.closed:
                    pipe.close()
            except Exception:
                pass

    def _kill_proc(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._killed = True
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _abort(self) -> None:
        """start() failed half-way: tear everything down, leave no ffmpeg behind."""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=10)
        if t is None:
            # capture thread never ran: the writer still waits on the queue
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        self._kill_proc()
        if self._writer is not None:
            self._writer.join(timeout=5)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
        self._close_pipes()
        try:
            self._close()
        except Exception:
            pass
        if self._err_fh:
            try:
                self._err_fh.close()
            except Exception:
                pass
            self._err_fh = None
