"""Desktop trajectory recorder (Windows).

Records a screen video (cursor drawn in, like Markov clips) plus a
full-fidelity event log, and keeps a per-frame wall-clock sidecar so the
packager can map any event onto the video timeline exactly.

    python -m recorder start --out data/my-recording --fps 30
    python -m recorder start --source app:SLDWORKS.exe          # app-only mode
    # F9 = pause/resume, F10 = stop (or Ctrl+C)

Output (raw inputs, never edited):
    <out>/raw/events.jsonl      all input/window/system events (JSONL)
    <out>/raw/segments.json     clips + per-frame wall-clock timestamps
    <out>/raw/capture.json      recorder configuration / session summary
    <out>/raw/segments/*.mp4    one file per uninterrupted stretch

Pause semantics: while paused (manual F9 *or* app-mode auto-pause) nothing is
written -- the input hook drops events at capture time and the window/system
watchers are idle; hotkeys keep working.  Every segment opens with a state
snapshot (screen_config "open", active_app, appearance, power, memory,
cursor position), exactly like Markov clips.

App-only mode (``--source app:<exe>``): the video is the full capture monitor,
but every pixel that is not the target app's is painted black before encoding
(see :mod:`recorder.app_mask`).  When another app stays in front for 300 ms the
recording auto-pauses (``privacy_mask_start``) and resumes when the target is
back (``privacy_mask_end``).  Window titles of other apps are never logged and
keystrokes typed into other apps are dropped.

Password managers (``--blocklist``, always including 1Password / KeePass /
KeePassXC / Bitwarden) are handled the same way in *every* mode: while one is
in front the recording is auto-paused between ``privacy_mask_start`` /
``privacy_mask_end`` (trigger ``policy_blocked_app``) and keys typed into it
are never logged.

``--source desktop`` records one display, the primary monitor (Markov clips
are single-display).  The first clip pins the capture rectangle: every later
clip (resume, crash restart, backend fallback) must have exactly the same
geometry or it is not kept.

Crash safety: ``raw/segments.json`` is rewritten atomically after every clip,
every ~10 s while recording (the running clip is marked ``in_progress``) and
when stopping; a clip whose FFmpeg did not finalize is probed (off the
recorder lock) and its frame list cut to what the file really holds (or marked
``broken``).  ``raw/.recording.lock`` (:mod:`recorder.reclock`) is held from
before the first clip until everything is written, so the packager can tell a
crashed session (lock free, status not "completed") from a live one.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

from .app_mask import (AUTO_PAUSE_DEBOUNCE_MS, DEFAULT_APP_ALLOW, FG_NONE, FG_OTHER, FG_TARGET,
                       AppMasker, AutoPauseController, normalize_exe, pick_capture_monitor,
                       screen_rect)
from . import reclock
from .capture import BACKENDS, app_gdigrab_error, create_segment, fallback_backend
from .clock import wall_ms
from .i18n import L, safe_stdio
from .paths import data_dir, ffmpeg_path, ffprobe_path
from .rawlog import EventLog
from .videocap import pick_encoder
from .win_input import InputHook, button_state, cursor_pos
from .win_window import (INPUT_PID_KEY, Desktop, appearance, enable_dpi_awareness,
                         ensure_thread_dpi_awareness, find_window, list_monitors, memory_pressure,
                         power_status, screens_union, virtual_screen_rect, window_client_rect)

VK_F9, VK_F10 = 120, 121
MAX_FPS = 120
CAPTURE_MODES = ("desktop", "monitor", "region", "window", "app")
# Shift/Ctrl/Alt/CapsLock/Win (+ left/right variants): kept even when another
# app is in front so modifier state never gets stuck in the log.
MODIFIER_VKS = frozenset({0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5})
_FG_KEYS = ("name", "bundleId", "windowTitle", "pid", "isFullScreen", "hwnd", "windowClass")
# Password managers: always on the privacy blocklist, whatever --blocklist adds.
_DEFAULT_BLOCKLIST = ["1password.exe", "keepass.exe", "keepassxc.exe", "bitwarden.exe"]
POLICY_TRIGGER = "policy_blocked_app"     # blocklisted app in front (any mode)
APP_TRIGGER = "non_target_app"            # app-only mode: another app in front
SIDECAR_EVERY_S = 10.0                    # raw/segments.json refresh while recording
RESUME_BACKOFF_MS = 1000.0                # failed auto-resume: retry after 1, 2, 4 ... s
RESUME_BACKOFF_MAX_MS = 30000.0
DISK_CHECK_EVERY_S = 5.0                  # free-space check of the recording drive
DISK_WARN_BYTES = 3 * 1024 ** 3           # below this: warning in the bar
DISK_STOP_BYTES = 1 * 1024 ** 3           # below this: stop cleanly, keep what was recorded
CRASH_LOOP_COUNT = 3                      # this many clip crashes ...
CRASH_LOOP_WINDOW_S = 60.0                # ... within this window -> warning in the bar
FIRST_FRAME_POLL_S = 0.05                 # privacy rules re-checked this often while a clip starts
# ffprobe stderr that names the *input* as unreadable (the clip is broken, not the tool)
_PROBE_INVALID_INPUT = ("invalid data found", "moov atom not found", "no such file",
                        "does not contain any stream")
# Never pop up a console window for ffprobe when the GUI runs under pythonw.
_POPEN_FLAGS = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW

# Defaults for attributes that older callers' Namespaces may lack.
_ARG_DEFAULTS = {
    "source": "desktop", "fps": 30, "backend": "auto", "dxcam_output": 0, "codec": "auto",
    "quality": 23, "audio_device": None, "draw_mouse_cursor": True, "sync_latency_ms": 0.0,
    "blocklist": list(_DEFAULT_BLOCKLIST), "hotkey_pause": VK_F9, "hotkey_stop": VK_F10,
    "max_seconds": 0, "session_info": None, "markov_delivery": False, "app_allow": [],
    "app_monitor": None, "auto_pause_other_apps": True, "app_overlay_allow": [],
}


def _human(ms: float) -> str:
    s = int(ms // 1000)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def capture_mode_of(source: str) -> str:
    s = str(source or "desktop")
    for mode in ("monitor", "region", "window", "app"):
        if s.startswith(mode + ":"):
            return mode
    return "desktop"


def app_target_of(source: str) -> str:
    """``'app: SLDWORKS.exe'`` -> ``'SLDWORKS.exe'`` (as given, trimmed)."""
    s = str(source or "")
    return s.split(":", 1)[1].strip().strip('"').strip() if s.startswith("app:") else ""


def _rect_dict(rect) -> Optional[Dict[str, int]]:
    if not rect:
        return None
    x, y, w, h = (int(v) for v in rect)
    return {"x": x, "y": y, "width": w, "height": h}


def primary_screen(screens) -> Optional[Dict]:
    """The primary monitor of a :func:`list_monitors` list (else the first one)."""
    screens = list(screens or [])
    if not screens:
        return None
    return next((s for s in screens if s.get("primary")), screens[0])


def write_text_atomic(path: str, text: str, attempts: int = 20, delay_s: float = 0.05) -> None:
    """Crash-safe replace of ``path``: temp file + fsync + ``os.replace``.

    A reader (packager, antivirus, file preview) holding the old file open makes
    ``os.replace`` fail with PermissionError on Windows for a moment: retried.
    """
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        for attempt in range(max(1, int(attempts))):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(delay_s)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _frames_of(seg) -> Optional[list]:
    """``[[n, wall], ...]`` of a segment (its live list, or ``to_dict()``'s copy)."""
    frames = getattr(seg, "frames", None)
    if frames is not None:
        return frames
    try:
        return list(seg.to_dict().get("frames") or [])
    except Exception:
        return None


def first_frame_wall(seg) -> Optional[float]:
    """Wall ms of the first frame really in ``seg``'s video (None: no frame)."""
    if seg is None:
        return None
    frames = getattr(seg, "frames", None)
    if frames is not None:
        return float(frames[0][1]) if len(frames) else None
    if not getattr(seg, "frame_count", 0):
        return None
    t = getattr(seg, "first_frame_wall", None)
    return float(t) if t else None


def last_frame_wall(seg) -> Optional[float]:
    """Wall ms of the last frame in ``seg``'s video (None: no frame)."""
    if seg is None:
        return None
    frames = _frames_of(seg)
    if not frames:
        return None
    try:
        return max(float(f[1]) for f in list(frames[-4:]))
    except (TypeError, ValueError, IndexError):
        return None


def _output_size(seg) -> Optional[Tuple[int, int]]:
    """Encoded ``(w, h)`` a segment reports (``output_size``; None if unknown)."""
    size = getattr(seg, "output_size", None)
    try:
        if size and len(size) == 2:
            w, h = (int(v) for v in size)
            return (w, h) if w > 0 and h > 0 else None
    except (TypeError, ValueError):
        pass
    return None


def segment_finalization(seg) -> Tuple[bool, Optional[int]]:
    """(finalized, returncode) of a stopped segment (contract K1, read defensively).

    ``finalized`` = ffmpeg exited 0 by itself after EOF / 'q', so the mp4 is
    complete.  Segments that do not report it fall back to the process exit
    code; objects without a process (test fakes) count as finalized.
    """
    fin = getattr(seg, "finalized", None)
    rc = getattr(seg, "returncode", None)
    if callable(fin):
        fin = None
    if fin is None:
        try:
            d = seg.to_dict()
        except Exception:
            d = {}
        fin = d.get("finalized")
        rc = d.get("returncode", rc)
    proc = getattr(seg, "_proc", None)
    if rc is None and proc is not None:
        rc = getattr(proc, "returncode", None)
    if fin is None:
        fin = True if proc is None else rc == 0
    return bool(fin), (int(rc) if isinstance(rc, int) else None)


def probe_frame_count(path: str, fps: float, ffprobe: str, timeout: float = 120.0) -> Optional[int]:
    """Frames a (possibly unfinalized) clip really holds, counted from its start.

    Reads packet timestamps only (no decoding: seconds even for long clips) and
    returns the length of the gap-free prefix of frame slots.  With B-frames a
    killed encoder can leave a later frame in the file before an earlier one,
    so a plain packet count could overstate what plays back.  A packet that
    reaches the end of the file may have been cut short by the kill, so it is
    not counted (conservative: the packager re-probes and may extend).
    Returns 0 when the file is missing or empty, when ffprobe says the input is
    unreadable, or when it ran fine and found no video packet.  Returns None
    ("unavailable": the packager probes again) when ffprobe could not be run or
    failed without output for a reason of its own (non-zero exit: missing DLL,
    antivirus, crash) -- a clip that may be fine is never declared broken.
    """
    if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
        return 0
    file_size = os.path.getsize(path)
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "packet=pts_time,size,pos", "-of", "compact=p=0", path]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                             creationflags=_POPEN_FLAGS)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    def _text(data) -> str:
        return data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data or "")
    out = _text(getattr(res, "stdout", b""))
    times = []
    for line in out.splitlines():
        fields: Dict[str, str] = {}
        bare: List[str] = []
        for part in line.strip().split("|"):
            key, sep, value = part.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
            elif part.strip():
                bare.append(part.strip())
        raw = fields.get("pts_time", bare[0].split(",", 1)[0] if bare else "")
        try:
            t = float(raw)
        except ValueError:
            continue                      # "N/A", warnings...
        if t != t or abs(t) == float("inf"):
            continue
        try:
            end = int(fields["pos"]) + int(fields["size"])
        except (KeyError, ValueError):
            end = None
        if end is not None and end >= file_size:
            continue                      # may be a partial packet: the file ends inside it
        times.append(t)
    if not times:
        err = _text(getattr(res, "stderr", b"")).lower()
        if any(marker in err for marker in _PROBE_INVALID_INPUT):
            return 0                      # ffprobe positively reports the clip as unreadable
        rc = getattr(res, "returncode", 0)
        if isinstance(rc, int) and rc != 0:
            return None                   # ffprobe itself failed: not proof the clip is broken
        return 0
    rate = float(fps) if fps and float(fps) > 0 else 30.0
    t0 = min(times)
    slots = {int(round((t - t0) * rate)) for t in times}
    n = 0
    while n in slots:
        n += 1
    return n


class Recorder:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        for key, value in _ARG_DEFAULTS.items():
            if not hasattr(args, key):
                setattr(args, key, list(value) if isinstance(value, list) else value)
        if not getattr(args, "ffmpeg", None):
            args.ffmpeg = ffmpeg_path()
        if not getattr(args, "out", None):
            args.out = _default_out()

        # ---- validate before touching the disk
        self.capture_mode = capture_mode_of(args.source)
        self.app_mode = self.capture_mode == "app"
        self.target_exe_raw = app_target_of(args.source) if self.app_mode else ""
        self.target_exe = normalize_exe(self.target_exe_raw)
        if self.app_mode and not self.target_exe:
            raise ValueError(L("请指定要录制的应用，例如 --source app:SLDWORKS.exe",
                               "Name the application to record, e.g. --source app:SLDWORKS.exe"))
        if self.app_mode and args.backend == "gdigrab":
            raise ValueError(app_gdigrab_error())

        self.session_info = {}
        if args.session_info:
            with open(args.session_info, encoding="utf-8-sig") as fh:
                self.session_info = json.load(fh)
            if not isinstance(self.session_info, dict):
                raise ValueError(L("CAD交付信息必须是 JSON 对象", "CAD session info must be a JSON object"))
        self.out = os.path.abspath(args.out)
        self.raw = os.path.join(self.out, "raw")
        self.seg_dir = os.path.join(self.raw, "segments")

        self.dpi_awareness = enable_dpi_awareness()
        self.desktop = Desktop()

        # ---- app-only mode: target, allow list, mask, capture monitor
        self.allow_exes: List[str] = []
        self.user_allow_exes: frozenset = frozenset()
        self.overlay_allow: List[str] = []   # --app-overlay-allow (benign full-screen overlays)
        self.masker: Optional[AppMasker] = None
        self.auto_pause_enabled = False
        self.capture_monitor: Optional[Dict] = None
        self.capture_rect: Optional[Tuple[int, int, int, int]] = None
        if self.app_mode:
            allow = list(DEFAULT_APP_ALLOW) + list(args.app_allow or [])
            self.allow_exes = sorted({normalize_exe(a) for a in allow if normalize_exe(a)} - {self.target_exe})
            # exes the user named with --app-allow (companion apps of the work): their
            # keys are logged; the default IME helpers stay visible but are not
            self.user_allow_exes = frozenset(normalize_exe(a) for a in (args.app_allow or [])
                                             if normalize_exe(a)) \
                - {normalize_exe(a) for a in DEFAULT_APP_ALLOW} - {self.target_exe}
            self.auto_pause_enabled = bool(args.auto_pause_other_apps)
            self.overlay_allow = sorted({normalize_exe(e) for e in (args.app_overlay_allow or [])
                                         if normalize_exe(e)})
            mask_kw = {"overlay_exes": self.overlay_allow} if self.overlay_allow else {}
            self.masker = AppMasker(self.target_exe, self.allow_exes, own_pid=os.getpid(), **mask_kw)
            target_rects = self._choose_app_monitor()
            if not target_rects:
                print(f"[recorder] note: no visible window of {self.target_exe_raw} yet; "
                      f"recording monitor {self.capture_monitor.get('index')} "
                      "(frames stay black until the app appears)")
        else:
            self.capture_rect = self._static_capture_rect()
            self.capture_monitor = self._static_capture_monitor()
            if self.capture_mode == "monitor" and self.capture_monitor is None:
                # never fall back to the whole virtual desktop (K3: one display)
                have = ", ".join(str(s.get("index")) for s in (self.desktop.screens or [])) or "-"
                raise ValueError(L("找不到显示器 {i}（现有显示器编号：{have}）；请用 --source monitor:<编号> 选择现有的显示器，"
                                   "或用 --source desktop 录制主显示器",
                                   "Monitor {i} was not found (available monitors: {have}); choose an existing "
                                   "one with --source monitor:<index>, or record the primary monitor with "
                                   "--source desktop", i=str(args.source).split(":", 1)[1].strip(), have=have))

        os.makedirs(self.seg_dir, exist_ok=True)
        self.log = EventLog(os.path.join(self.raw, "events.jsonl"))
        self.segments: List = []
        self.segment = None
        self.paused = False
        self.auto_paused = False
        self._stop_done = threading.Event()
        self._stopped_at: Optional[float] = None
        self._stop_error: Optional[Exception] = None
        self.quiet = False          # set by the overlay UI to silence status spam
        self.active_backend = args.backend
        self.stopped = threading.Event()
        self.lock = threading.RLock()

        self.started_at = wall_ms()
        self.paused_ms = 0.0
        self._paused_at: Optional[float] = None
        self._blocklist = sorted({normalize_exe(b) for b in list(_DEFAULT_BLOCKLIST) + list(args.blocklist or [])
                                  if normalize_exe(b)})
        self._blocklist_set = frozenset(self._blocklist)
        self._watcher: Optional[threading.Thread] = None
        self._hook: Optional[InputHook] = None
        self._last = {"hwnd": 0, "rect": {}, "power": {}, "mem": "", "screens": "",
                      "appearance": "", "pids": set(), "rect_at": 0.0, "slow_at": 0.0}

        self._capturing = False             # input gate (mirrors the hook's)
        self._auto = AutoPauseController(AUTO_PAUSE_DEBOUNCE_MS)
        self._auto_since: Optional[float] = None
        self.auto_paused_ms = 0.0
        self.auto_pauses = 0
        self._mask_open: Optional[Dict] = None   # fields of the open privacy_mask interval
        self._mask_open_ts: Optional[float] = None
        self._mask_known: set = set()            # ids of the clips that existed when it opened
        self._auto_reason: Optional[str] = None  # "app" | "policy" while auto-paused
        self.policy_pauses = 0
        self._resume_failures = 0
        self._resume_retry_at = 0.0              # watcher clock (wall ms) of the next auto-resume try
        self._fg_info: Dict = {}
        self._fg_state = FG_NONE
        self.suppressed_keys = 0
        self.suppressed_inputs = 0               # clicks/scrolls into a password manager
        self._pm_running = False                 # a blocklisted process exists (auto-type risk)
        self._pinned_backend: Optional[str] = None
        # K3: geometry of the first clip; every later clip must match it exactly
        self._pinned_rect: Optional[Tuple[int, int, int, int]] = None
        self._pinned_device: Optional[str] = None
        self.backend_fallbacks: List[str] = []
        self._restart_pending = False
        self._restart_at = 0.0
        self.last_error = ""
        # crash-safe sidecar (raw/segments.json)
        self._sidecar_lock = threading.RLock()
        self._sidecar_cache: Dict[int, str] = {}   # id(seg) -> JSON of a settled clip
        self._settled: set = set()                  # ids of stopped + verified clips
        self._seg_notes: Dict[int, Dict] = {}       # id(seg) -> finalize / probe results
        self._sidecar_due = 0.0                     # time.monotonic() of the next periodic write
        # id(seg) -> (frames encoded, JSON of the last one, JSON of frames[:n]) of a clip
        # that is not settled yet: the periodic write only encodes the new tail
        self._frames_json: Dict[int, Tuple[int, str, str]] = {}
        self._verify_threads: List[threading.Thread] = []   # K1 probes of unfinalized clips (off the lock)
        self._pinned_size: Optional[Tuple[int, int]] = None   # K3: encoded size of the first clip
        self._overlay_error = ""                    # last_error text set by _check_overlays
        self._start_error = ""                      # last_error text of a failed resume / restart
        self._rec_lock: Optional[reclock.RecordingLock] = None   # K11: raw/.recording.lock while recording
        self._keep_rec_lock = False                 # run()'s failure path releases it after "failed"

    # ------------------------------------------------------------------ geometry
    def _choose_app_monitor(self) -> List:
        screens = self.desktop.screens or list_monitors()
        rects = self.masker.target_rects() if self.masker else []
        mon = pick_capture_monitor(screens, rects, self.args.app_monitor)
        if mon is None:
            raise RuntimeError(L("没有检测到显示器", "No display detected"))
        self.capture_monitor = dict(mon)
        self.capture_rect = screen_rect(mon)
        self._set_masker_rect(self.capture_rect)
        return rects

    def _set_masker_rect(self, rect) -> None:
        """Only on a real change: set_capture_rect drops the masker's hold history."""
        if self.masker is not None and rect and tuple(self.masker.capture_rect) != tuple(rect):
            self.masker.set_capture_rect(tuple(rect))

    def _refresh_app_monitor(self) -> None:
        """Re-find the capture monitor (it may have moved/changed between segments)."""
        mon = self.capture_monitor or {}
        screens = list_monitors()
        if not screens:
            return
        self.desktop.screens = screens
        match = next((s for s in screens if mon.get("deviceName") and s.get("deviceName") == mon.get("deviceName")),
                     None)
        pinned = getattr(self, "_pinned_rect", None)
        if pinned and (match is None or screen_rect(match) != tuple(pinned)):
            # \\.\DISPLAYn renumbered (dock / KVM / sleep): a monitor with exactly
            # the pinned rect gives the same picture and geometry
            match = next((s for s in screens if screen_rect(s) == tuple(pinned)), match)
        if match is None:
            try:
                match = pick_capture_monitor(screens, self.masker.target_rects() if self.masker else [], None)
            except ValueError:
                match = None
        if match is None:
            return
        rect = screen_rect(match)
        if pinned and rect == tuple(pinned) and match.get("deviceName"):
            self._pinned_device = match.get("deviceName")
        if pinned and rect != tuple(pinned):
            # K3: every clip of a recording has the first clip's geometry
            print(f"[recorder] WARNING capture monitor is now {rect}; keeping {tuple(pinned)} "
                  "so every clip has the same size")
            return
        if self.capture_rect and rect != tuple(self.capture_rect):
            print(f"[recorder] WARNING capture monitor changed {self.capture_rect} -> {rect}")
        self.capture_monitor = dict(match)
        self.capture_rect = rect
        self._set_masker_rect(rect)

    def _static_capture_rect(self) -> Optional[Tuple[int, int, int, int]]:
        src = str(self.args.source or "desktop")
        mode = capture_mode_of(src)
        screens = self.desktop.screens
        try:
            if mode == "monitor":
                mi = int(src.split(":", 1)[1])
                s = next((x for x in screens if x["index"] == mi), None)
                return screen_rect(s) if s else None
            if mode == "region":
                x, y, w, h = (int(v) for v in src.split(":", 1)[1].split(","))
                return (x, y, w, h)
            if mode == "window":
                return window_client_rect(find_window(src.split(":", 1)[1]))
            # desktop = ONE display, the primary monitor (Markov clips are single-display)
            s = primary_screen(screens)
            if s:
                return screen_rect(s)
            return virtual_screen_rect() or screens_union(screens)
        except Exception:
            return None

    def _static_capture_monitor(self) -> Optional[Dict]:
        """The monitor a desktop / monitor:N recording shows (None for region / window)."""
        src = str(self.args.source or "desktop")
        mode = capture_mode_of(src)
        screens = list(self.desktop.screens or [])
        s = None
        if mode == "desktop":
            s = primary_screen(screens)
        elif mode == "monitor":
            try:
                mi = int(src.split(":", 1)[1])
            except ValueError:
                mi = -1
            s = next((x for x in screens if x.get("index") == mi), None)
        return dict(s) if s else None

    def _masked_display(self) -> int:
        try:
            return int((self.capture_monitor or {}).get("index", 0) or 0)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------ lifecycle
    def _write_capture_if_running(self, status: str = "recording") -> bool:
        """Write capture.json unless stop() already finalized it (never overwrite 'completed')."""
        with self.lock:
            if self.stopped.is_set():
                return False
            self._write_capture(status=status)
            return True

    def _make_hook(self) -> InputHook:
        # input_context also carries the live foreground PID (popped in _on_input)
        context = getattr(self.desktop, "input_context", None) or self.desktop.event_context
        return InputHook(self._on_input, context,
                         hotkeys={self.args.hotkey_pause: self.toggle_pause,
                                  self.args.hotkey_stop: self.stop},
                         enabled=False)

    def _acquire_rec_lock(self) -> None:
        """K11: hold raw/.recording.lock from before the first clip until stop()
        has written everything (the OS frees it if this process dies)."""
        if getattr(self, "_rec_lock", None) is not None:
            return
        try:
            self._rec_lock = reclock.acquire(self.raw)
        except Exception as exc:
            print(f"[recorder] recording lock error: {exc}")
            self._rec_lock = None
        if self._rec_lock is None:
            print("[recorder] WARNING could not take raw/.recording.lock (folder not writable, or another "
                  "process holds it); recording anyway, but a crash of this session cannot be told apart "
                  "from a live recording")

    def _release_rec_lock(self) -> None:
        lock, self._rec_lock = getattr(self, "_rec_lock", None), None
        if lock is not None:
            try:
                lock.release()
            except Exception as exc:
                print(f"[recorder] recording lock release error: {exc}")

    def run(self) -> None:
        try:
            # trial encodes take seconds on the first run (cached): never under the lock
            pick_encoder(self.args.codec, self.args.ffmpeg)
        except Exception:
            pass
        self._refresh_pm_running()           # auto-type guard from the first keystroke
        self._acquire_rec_lock()             # before capture.json says "recording"
        try:
            running = self._write_capture_if_running()
        except Exception:
            self._release_rec_lock()         # nothing started yet: never leave the folder "live"
            raise
        if not running:
            self.stop()                      # stopped before it started: install nothing
            self._release_rec_lock()
            return
        resumed = None
        try:
            hook = self._make_hook()
            hook.start()
            with self.lock:
                late = self.stopped.is_set()
                if not late:
                    self._hook = hook
            if late:
                # stop() finalized while the hook was starting and never saw it:
                # global hooks must not outlive the recording.
                try:
                    hook.stop()
                except Exception as exc:
                    print(f"[recorder] input hook stop error: {exc}")
                self.stop()
                return
            with self.lock:
                reason = None if self.stopped.is_set() else self._hold_reason()
                if self.stopped.is_set():
                    pass
                elif reason:
                    # started from a terminal / another app / a password manager in front
                    now = wall_ms()
                    self.paused = self.auto_paused = True
                    self._auto_reason = reason
                    self._paused_at = now
                    self._enter_auto(now)
                    print("[recorder] " + self._hold_message(reason))
                else:
                    self.start_segment()
                    if self.segment is not None:
                        self._set_capturing(True)
                        resumed = self.segment
            if resumed is not None:
                # snapshot first, so the watcher never emits a duplicate active_app
                self._after_resume(resumed, initial=True)
            if not self.stopped.is_set():
                self._watcher = threading.Thread(target=self._watch, name="watcher", daemon=True)
                self._watcher.start()
        except Exception:
            # Failed startup must not leave global input hooks or a watcher alive.
            self._keep_rec_lock = True       # held until "failed" is on disk
            try:
                try:
                    self.stop()
                except Exception:
                    pass
                self._write_capture(status="failed")
            finally:
                self._keep_rec_lock = False
                self._release_rec_lock()
            raise
        try:
            # advisory: stop() rewrites capture.json; a failure here (disk full,
            # antivirus lock) must not abandon a recording that is already running
            ok = self._write_capture_if_running()
        except Exception as exc:
            self.last_error = str(exc)
            print(f"[recorder] WARNING could not write capture.json: {exc}")
            ok = False
        if ok:
            print(f"[recorder] recording -> {self.out}  (F9 pause/resume, F10 stop)")

        started = time.perf_counter()
        try:
            while not self.stopped.wait(0.5):
                self._status()
                if self.args.max_seconds and time.perf_counter() - started >= self.args.max_seconds:
                    self.stop()
        except KeyboardInterrupt:
            pass
        finally:
            # Waits for an in-flight stop() (F10 comes from the hook thread) so
            # run() only returns once segments.json / capture.json are written.
            self.stop()

    def stop(self) -> None:
        # Idempotent *and blocking*: every caller returns only after the capture
        # is finalized on disk, so the packager can never race the recorder.
        hook = None
        with self.lock:
            if self._stop_done.is_set():
                if self._stop_error:
                    raise self._stop_error
                return
            owner = not self.stopped.is_set()
            if owner:
                self._stopped_at = wall_ms()
                self.stopped.set()
                self._set_capturing(False)
                if getattr(self, "_auto_since", None) is not None:
                    self._leave_auto(self._stopped_at)
                # read under the lock: run() installs its hook only while not stopped
                hook = self._hook
        if not owner:
            if not self._stop_done.wait(timeout=120):
                raise TimeoutError(L("停止录制超时，请保留原始文件并检查录制日志",
                                     "Stopping the recording timed out; keep the raw files and check the log"))
            if self._stop_error:
                raise self._stop_error
            return
        # Never hold the UI snapshot lock while waiting for FFmpeg or hooks.
        try:
            try:
                if hook:
                    hook.request_stop()
                if self.segment:
                    self.segment.request_stop()
                self.stop_segment()
                if hook:
                    hook.stop()
                if self._watcher and self._watcher is not threading.current_thread():
                    self._watcher.join(timeout=3)
                # Close events belong to the end of the last clip, never to a pause gap.
                close_ts = self._paused_at if (self.paused and self._paused_at) else self._stopped_at
                if getattr(self, "_mask_open", None):
                    self._close_mask(self._mask_close_ts(max(close_ts, self._mask_open_ts or close_ts)))
                evt = self.desktop.screen_config_event("close")
                if isinstance(evt, dict):
                    evt = dict(evt, timestamp=close_ts)
                self.log.emit_record(evt)
            finally:
                # K2: the frame sidecar reaches disk even when a step above fails
                # (after the K1 probes of unfinalized clips have settled them)
                self._join_verify()
                self._write_segments_safe()
            total = self._stopped_at - self.started_at
            self._write_capture(status="completed", duration_ms=total)
            print("[recorder] stop: %s wall, %d frames, %d events -> %s" % (
                _human(total), sum(s.frame_count for s in self.segments),
                self.log.count, self.raw))
        except Exception as exc:
            self._stop_error = exc
            self._write_capture(status="failed")
            raise
        finally:
            try:
                self.log.close()
            finally:
                # K11: everything is on disk -- free the lock *before* stop() returns
                # to any caller, so packaging right after Stop never sees it live
                if not getattr(self, "_keep_rec_lock", False):
                    self._release_rec_lock()
                self._stop_done.set()

    # ---------------------------------------------------------------- pause logic
    def toggle_pause(self) -> None:
        """F9 / GUI button.  Manual pauses are never resumed automatically."""
        resumed = None
        with self.lock:
            if self.stopped.is_set():
                return
            now = wall_ms()
            if not self.paused:
                self._pause("manual")
                return
            if self.auto_paused:
                # F9 while auto-paused: hold the pause; coming back to the target
                # will not resume it (press F9 again to continue).
                self._leave_auto(now)
                self.auto_paused = False
                self._auto_reason = None
                print("\n[recorder] auto-pause is now a manual pause (press F9 to continue)")
                return
            reason = self._hold_reason()
            if reason:
                # another app / a password manager is in front: don't record it, wait
                self.auto_paused = True
                self._auto_reason = reason
                self._resume_retry_at = 0.0
                self._enter_auto(now)
                print("\n[recorder] " + self._hold_message(reason))
                return
            resumed = self._resume()
        if resumed is not None:
            self._after_resume(resumed)

    def _hold_message(self, reason: str) -> str:
        if reason == "policy":
            return "a blocklisted app (password manager) is in front: waiting until it leaves ..."
        return f"waiting for {self.target_exe_raw} to come to the front ..."

    def _pause(self, reason: str) -> None:
        """Caller holds the lock."""
        if self.paused:
            return
        now = wall_ms()
        self._set_capturing(False)          # gate first: nothing logged from now on
        self.paused = True
        self.auto_paused = reason == "auto"
        if not self.auto_paused:
            self._auto_reason = None
        self._paused_at = now
        if self.auto_paused:
            self._enter_auto(now)
        self.stop_segment()
        label = "auto-paused (privacy)" if self.auto_paused else "paused"
        print(f"\n[recorder] {label} at {_human(now - self.started_at)}")

    def _mask_info(self, trigger: str, fg: Dict) -> Dict:
        exe = str(fg.get("exe") or "")
        return {"trigger": trigger, "maskTrigger": trigger, "maskDetail": exe.lower(),
                "appName": str(fg.get("name") or ""), "exeName": exe, "windowTitle": "",
                "maskedDisplay": self._masked_display()}

    def _auto_pause(self, info: Dict, reason: str) -> None:
        """Caller holds the lock: privacy mask + time cut (``reason`` 'app' or 'policy').

        The clip is stopped first; ``privacy_mask_start`` is then stamped after
        the last frame that clip really holds, so no video frame ever lies
        inside a [start, end] mask interval (that is how the packager knows
        the masked time was cut from the video).
        """
        was_capturing = self._capturing
        seg = self.segment
        self.auto_pauses += 1
        self._pause("auto")
        self._auto_reason = reason
        self._resume_retry_at = 0.0
        if not was_capturing:
            return
        ts = self._paused_at or wall_ms()
        last = last_frame_wall(seg)
        if last is not None:
            ts = max(ts, round(last, 3) + 0.001)
        if self._mask_open is not None:
            close_ts = self._mask_close_ts(None)
            if close_ts is None:
                return          # no frame since the open mask began: it still covers this pause
            self._close_mask(close_ts)
        self.log.emit("privacy_mask_start", **dict(info, timestamp=ts))
        self._mask_open = info
        self._mask_open_ts = ts
        self._mask_known = {id(s) for s in self.segments}

    def _mask_close_ts(self, default: Optional[float]) -> Optional[float]:
        """``privacy_mask_end`` goes just before the first frame recorded after the
        mask began (``default`` when no such frame exists)."""
        known = self._mask_known
        firsts = [first_frame_wall(s) for s in list(self.segments) if id(s) not in known]
        firsts = [t for t in firsts if t is not None]
        if firsts:
            return round(min(firsts), 3) - 0.001
        return default

    def _close_mask(self, ts: float) -> None:
        mask = self._mask_open
        if mask is None:
            return
        self.log.emit("privacy_mask_end", **dict(mask, timestamp=ts))
        self._mask_open = None
        self._mask_open_ts = None
        self._mask_known = set()

    def _resume(self):
        """Caller holds the lock.  Returns the new segment (emit the snapshot via
        :meth:`_after_resume` *after* releasing the lock) or None."""
        if not self.paused or self.stopped.is_set():
            return None
        now = wall_ms()
        was_auto = self.auto_paused
        reason = self._auto_reason
        if was_auto:
            self._leave_auto(now)
        if self._paused_at:
            self.paused_ms += now - self._paused_at
            self._paused_at = None
        self.paused = False
        self.auto_paused = False
        self._auto_reason = None
        try:
            self.start_segment()
        except Exception as exc:
            # cannot capture: stay paused so nothing lands in a gap
            self.paused = True
            self._paused_at = now
            self.last_error = self._start_error = str(exc)
            if was_auto:
                # an auto-pause stays an auto-pause (only a user action makes a
                # manual one): the watcher retries with a backoff
                self.auto_paused = True
                self._auto_reason = reason
                self._enter_auto(now)
                self._resume_failures += 1
            print(f"\n[recorder] could not resume: {exc}")
            return None
        self._resume_failures = 0
        self._resume_retry_at = 0.0
        self._restart_pending = False
        self._clear_start_error()
        self._set_capturing(True)
        print("[recorder] resumed" + (" (privacy pause over)" if was_auto else ""))
        return self.segment

    def _clear_start_error(self) -> None:
        """Capture runs again: drop the error of the failed (re)start it replaces."""
        err = getattr(self, "_start_error", "")
        if err and getattr(self, "last_error", "") == err:
            self.last_error = ""
        self._start_error = ""

    def _after_resume(self, seg, initial: bool = False) -> None:
        """Open the new clip with Markov's state snapshot (+ close a privacy mask)."""
        t0 = self._wait_first_frame(seg)
        with self.lock:
            if self.segment is not seg or self.paused or self.stopped.is_set():
                return                       # paused/stopped again meanwhile
            # Just before frame 0: the snapshot (cursor position...) is the state
            # the clip's first frame shows (pre-roll / resume head for the packager).
            start = getattr(seg, "start_wall_ms", 0) or 0
            if t0:
                ts = round(t0, 3) - 0.001
            elif start:
                ts = round(float(start), 3) - 0.001
            else:
                ts = wall_ms()
            initial = initial or (bool(self.segments) and self.segments[0] is seg)
            try:
                self._emit_state_snapshot(ts, "initial_cursor" if initial else "resume_snapshot")
            except Exception as exc:
                print(f"[recorder] snapshot error: {exc}")
            if self._mask_open is not None:
                self._close_mask(self._mask_close_ts(ts))

    def _wait_first_frame(self, seg, timeout: float = 3.0) -> Optional[float]:
        """Wall ms of ``seg``'s first frame (None: none within ``timeout``, or the
        clip was paused / replaced / stopped meanwhile).

        K4: this can take seconds (gdigrab) and may run on the watcher thread,
        so the privacy rules are re-checked here: a password manager that comes
        to the front while the clip starts pauses it at once (never resumes)."""
        deadline = time.monotonic() + timeout
        next_check = time.monotonic()
        while True:
            t = getattr(seg, "first_frame_wall", None)
            if t:
                return float(t)
            if time.monotonic() > deadline or self.stopped.is_set() or getattr(seg, "crashed", False):
                return None
            if self.segment is not seg or self.paused:
                return None                  # paused (privacy / F9) or replaced while starting
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + FIRST_FRAME_POLL_S
                try:
                    self._watch_auto_pause(wall_ms(), allow_resume=False)
                except Exception as exc:
                    print(f"[recorder] privacy check error: {exc}")
                continue
            time.sleep(0.01)

    def _enter_auto(self, now: float) -> None:
        if self._auto_since is None:
            self._auto_since = now

    def _leave_auto(self, now: float) -> None:
        if self._auto_since is not None:
            self.auto_paused_ms += max(0.0, now - self._auto_since)
            self._auto_since = None

    def _foreground_now(self) -> Dict:
        """Fresh foreground for the auto-pause rules: ``{"exe", "name", "state"}``."""
        if getattr(self, "app_mode", False) and self.masker is not None:
            fg = self.masker.foreground_state()
            self._fg_info, self._fg_state = fg, fg.get("state", FG_NONE)
            return {"exe": fg.get("exe") or "", "name": fg.get("name") or "", "state": self._fg_state}
        fg = self.desktop.foreground(force=True)
        return {"exe": fg.get("bundleId") or "", "name": fg.get("name") or "", "state": FG_NONE}

    def _is_blocked(self, exe: str) -> bool:
        return bool(exe) and normalize_exe(exe) in self._blocklist_set

    def _hold_reason(self) -> Optional[str]:
        """Why a (re)start must wait right now: "policy" (a blocklisted app is in
        front, any mode), "app" (app mode: another app is in front) or None."""
        try:
            fg = self._foreground_now()
        except Exception:
            return None
        if self._is_blocked(fg.get("exe", "")):
            return "policy"
        if getattr(self, "app_mode", False) and getattr(self, "auto_pause_enabled", False) \
                and fg.get("state") == FG_OTHER:
            return "app"
        return None

    def _should_hold_for_target(self) -> bool:
        return self._hold_reason() is not None

    def _set_capturing(self, flag: bool) -> None:
        self._capturing = bool(flag)
        hook = getattr(self, "_hook", None)
        if hook is not None:
            try:
                hook.set_enabled(flag)
            except Exception:
                pass

    # ------------------------------------------------------------------ input sink
    def _on_input(self, rec: Dict) -> None:
        """Hook writer thread -> log.

        Pause gating happens in the hook at capture time, and the hook swallows
        the F9/F10 presses it consumes as recorder controls (they never get
        here); F9/F10 with a modifier held are ordinary keys and are logged.
        ``bundleId`` (and the private :data:`INPUT_PID_KEY`) describe the live
        foreground app at hook time.
        """
        pid = rec.pop(INPUT_PID_KEY, None)     # never logged
        kind = rec.get("type")
        if kind in ("click", "scroll", "drag") and self._is_blocked(rec.get("bundleId") or ""):
            self.suppressed_inputs += 1        # clicked into a password manager (K4): never logged
            return
        if kind == "key":
            exe = rec.get("bundleId") or ""
            if self._is_blocked(exe):
                self.suppressed_keys += 1      # typed into a password manager: never logged
                return
            if rec.get("synthetic") and self._pm_running:
                # auto-type (KeePass Ctrl+Alt+A, KeePassXC, Bitwarden) injects the user name and
                # password into the foreground app from the background: keyCode / scanCode /
                # characters would all spell the secret, so injected keys are not logged while a
                # password manager runs.  Real keystrokes are never injected.
                self.suppressed_keys += 1
                return
            if self.app_mode and int(rec.get("keyCode", -1)) not in MODIFIER_VKS \
                    and not self._key_goes_to_target(exe, pid):
                self.suppressed_keys += 1      # typed into another app: never logged
                return
        self.log.emit_record(rec)

    def _key_goes_to_target(self, exe: str, pid) -> bool:
        """App mode: a key counts as typed into the target only when the live
        foreground process is the target exe, an exe the user named with
        ``--app-allow``, or a helper process the target launched
        (``masker.is_target_pid``).  The default IME helpers stay *visible* but
        their keys are not the target's: the Win+V clipboard history / emoji
        panel search would leak otherwise."""
        name = normalize_exe(exe)
        if name and (name == self.target_exe or name in getattr(self, "user_allow_exes", ())):
            return True
        masker = self.masker
        check = getattr(masker, "is_target_pid", None) if masker is not None else None
        if not pid or check is None:
            return False
        try:
            return bool(check(int(pid)))
        except Exception:
            return False

    # ------------------------------------------------------------------- segments
    def start_segment(self) -> None:
        if self.stopped.is_set():
            return
        idx = len(self.segments)
        path = os.path.join(self.seg_dir, f"seg_{idx:03d}.mp4")
        args = self.args
        source = str(args.source or "desktop")
        mode = capture_mode_of(source)
        size = offset = rect = None
        monitor_device = None
        pinned = getattr(self, "_pinned_rect", None)
        if mode == "app":
            self._refresh_app_monitor()
            rect = self.capture_rect
            monitor_device = (self.capture_monitor or {}).get("deviceName")
        elif mode in ("desktop", "monitor"):
            # desktop = the primary monitor (one display, every backend)
            s = getattr(self, "capture_monitor", None)
            if s:
                offset = (s["originX"], s["originY"])
                size = (s["width"], s["height"])
                rect = screen_rect(s)
                monitor_device = s.get("deviceName")
            elif mode == "monitor":
                print(f"[recorder] WARNING monitor {source.split(':', 1)[1]} not found; recording the desktop")
        elif mode == "region":
            x, y, w, h = (int(v) for v in source.split(":", 1)[1].split(","))
            offset, size, rect = (x, y), (w, h), (x, y, w, h)
        if pinned and mode != "window":
            # K3: resume / crash restart / fallback reproduce the first clip exactly
            if mode in ("desktop", "monitor"):
                self._check_pinned_display()
            rect = tuple(pinned)
            offset, size = (rect[0], rect[1]), (rect[2], rect[3])
            monitor_device = getattr(self, "_pinned_device", None) or monitor_device

        backend = getattr(self, "_pinned_backend", None) or args.backend
        if backend == "dxcam" and mode == "window":
            print("[recorder] DXGI duplication cannot record a single window; using gdigrab")
            backend = "gdigrab"
        first_clip = not self.segments
        self._begin_mask_segment(mode, first_clip)   # re-arm the mask hold for the clip's first frames

        kwargs = dict(
            index=idx, path=path, fps=args.fps, source=source,
            video_size=size, offset=offset, encoder=args.codec, quality=args.quality,
            audio_device=args.audio_device, draw_mouse=args.draw_mouse_cursor,
            sync_latency_ms=args.sync_latency_ms,
            ffmpeg=args.ffmpeg,
            output_idx=args.dxcam_output,
            capture_rect=rect, monitor_device=monitor_device,
            masker=getattr(self, "masker", None),
        )
        seg = create_segment(backend, **kwargs)
        try:
            seg.start()
        except Exception as exc:
            fb = fallback_backend(source)
            if args.backend != "auto" or seg.backend == fb:
                raise
            note = f"{seg.backend} unavailable ({exc}); falling back to {fb}"
            print(f"[recorder] {note}")
            fallbacks = getattr(self, "backend_fallbacks", None)
            if fallbacks is not None:
                fallbacks.append(note)
            # the failed grabber may already have used up the hold: re-arm it
            self._begin_mask_segment(mode, first_clip)
            seg = create_segment(fb, **kwargs)
            seg.start()
        self._check_geometry(seg, pinned)
        if args.backend == "auto" and getattr(self, "_pinned_backend", None) is None:
            # keep later clips on the same grabber (same geometry / video size)
            self._pinned_backend = seg.backend
        self.active_backend = seg.backend
        self.segments.append(seg)
        self.segment = seg
        if mode != "app" and getattr(seg, "capture_rect", None):
            self.capture_rect = tuple(seg.capture_rect)
        if not pinned and mode != "window" and hasattr(self, "_pinned_rect"):
            got = getattr(seg, "capture_rect", None) or rect
            if got:
                self._pinned_rect = tuple(int(v) for v in got)
                self._pinned_device = monitor_device
                self._pinned_size = _output_size(seg)
        extra = {"gdigrab": "", "dxcam": " [DXGI]", "gdi": " [GDI]"}.get(seg.backend, "")
        if getattr(self, "masker", None) is not None:
            extra += f" [app-only: {self.target_exe_raw}]"
        print(f"[recorder] segment {idx} started ({seg.encoder}, {args.fps}fps, {seg.backend}){extra}")

    def _begin_mask_segment(self, mode: str, first: bool) -> None:
        """App mode: arm the masker's segment-start hold (finding 0) before a
        grabber starts -- the first clip keeps the last known mask, every later
        clip (resume, crash restart) opens fully black for the hold time."""
        masker = getattr(self, "masker", None)
        if mode != "app" or masker is None or not hasattr(masker, "begin_segment"):
            return
        try:
            try:
                masker.begin_segment(first=first)
            except TypeError:                # older masker without ``first``
                masker.begin_segment()
        except Exception as exc:
            print(f"[recorder] mask begin_segment error: {exc}")

    def _check_pinned_display(self) -> None:
        """Desktop / monitor mode: refuse a clip when the recorded display was
        unplugged or changed resolution/position -- the pinned rectangle would
        then show something else (part of another monitor).

        Windows may renumber ``\\\\.\\DISPLAYn`` (dock / KVM reconnect, sleep,
        driver reset): a monitor with exactly the pinned rectangle gives the same
        picture and geometry (K3), so it is accepted under its new name."""
        device = getattr(self, "_pinned_device", None)
        pinned = getattr(self, "_pinned_rect", None)
        if not device or not pinned:
            return
        screens = list_monitors()
        if not screens:
            return                           # cannot tell (not Windows / query failed)
        cur = next((s for s in screens if s.get("deviceName") == device), None)
        if cur is None or screen_rect(cur) != tuple(pinned):
            same = next((s for s in screens if screen_rect(s) == tuple(pinned) and s.get("deviceName")), None)
            if same is not None:
                print(f"[recorder] recorded display is now {same.get('deviceName')} (was {device}); "
                      "same position and size, recording continues")
                self._pinned_device = same.get("deviceName")
                if getattr(self, "capture_monitor", None) is not None:
                    self.capture_monitor = dict(same)
                return
            now = screen_rect(cur) if cur else None
            raise RuntimeError(L("录制的显示器已断开或分辨率/位置已改变（{a} -> {b}），为保证所有片段画面一致暂不继续录制；"
                                 "请恢复显示设置，或停止后重新开始录制",
                                 "The recorded display was disconnected or changed resolution/position ({a} -> {b}); "
                                 "recording does not continue so every clip keeps the same picture. Restore the "
                                 "display settings, or stop and start a new recording",
                                 a=tuple(pinned), b=now))

    def _check_geometry(self, seg, pinned) -> None:
        """K3: a later clip whose capture rectangle or encoded video size differs
        from the first one is discarded (never stream-copied into the same
        clip.mp4) and retried."""
        if not pinned:
            return
        got = getattr(seg, "capture_rect", None)
        got = tuple(int(v) for v in got) if got else None
        want_size = getattr(self, "_pinned_size", None)
        got_size = _output_size(seg)
        rect_ok = got is None or got == tuple(pinned)
        size_ok = not want_size or not got_size or got_size == tuple(want_size)
        if rect_ok and size_ok:
            return
        if rect_ok:                          # same area, other encoded size (odd-size rounding...)
            have, want = "%dx%d" % got_size, "%dx%d" % tuple(want_size)
        else:
            have, want = got, tuple(pinned)
        try:
            seg.stop()
        except Exception:
            pass
        try:
            os.remove(seg.path)
        except OSError:
            pass
        raise RuntimeError(L("新片段的录制区域/画面尺寸 {got} 与之前的片段 {want} 不一致，已丢弃并将重试",
                             "The new clip's capture area / video size {got} differs from the earlier clips "
                             "({want}); it was discarded and will be retried", got=have, want=want))

    def _ffprobe(self) -> str:
        ff = str(getattr(self.args, "ffmpeg", "") or "")
        exe = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
        resolved = shutil.which(ff) if ff else None
        if resolved:
            sibling = os.path.join(os.path.dirname(resolved), exe)
            if os.path.isfile(sibling):
                return sibling
        return ffprobe_path()

    def _verify_segment(self, seg) -> None:
        """K1: a clip whose FFmpeg did not finalize is probed; its frame list is cut
        to what the file really holds, or the clip is marked broken (a gap).

        The probe reads every packet of the clip (seconds for a long one), so it
        never runs under ``self.lock`` (see :meth:`stop_segment`); the results
        are applied under the sidecar lock so a periodic write never sees half."""
        finalized, rc = segment_finalization(seg)
        with self._sidecar_lock:
            note = self._seg_notes.setdefault(id(seg), {})
            note.update(finalized=finalized, returncode=rc)
        if finalized:
            return
        n = probe_frame_count(seg.path, getattr(seg, "fps", None) or self.args.fps, self._ffprobe())
        with self._sidecar_lock:
            if n is None:
                note["probe"] = "unavailable"
                print(f"[recorder] segment {seg.index}: ffmpeg did not finalize (exit {rc}); "
                      "ffprobe could not check it, the packager will check the file")
                return
            note["probed_frames"] = int(n)
            frames = getattr(seg, "frames", None)
            before = len(frames) if frames is not None else int(getattr(seg, "frame_count", 0) or 0)
            if n <= 0:
                note["broken"] = True
                if frames is not None:
                    del frames[:]
                print(f"[recorder] segment {seg.index}: ffmpeg did not finalize (exit {rc}) and the file is "
                      "unreadable; marked broken (its time becomes a gap)")
                return
            if frames is not None:
                kept = [f for f in list(frames) if int(f[0]) < n]
                if len(kept) < before:
                    frames[:] = kept
                    note["truncated_from"] = before
        print(f"[recorder] segment {seg.index}: ffmpeg did not finalize (exit {rc}); "
              f"{min(n, before)} of {before} frames are in the file")

    def _settle_segment(self, seg) -> None:
        """Verify a stopped clip (K1) and mark it settled (its sidecar entry is final)."""
        try:
            self._verify_segment(seg)
        except Exception as exc:
            print(f"[recorder] could not verify segment {seg.index}: {exc}")
        with self._sidecar_lock:
            self._settled.add(id(seg))

    def _start_verify(self, seg) -> None:
        """K1 probe of an unfinalized clip on a worker thread: stop_segment() may run
        under ``self.lock`` (pause, crash restart), and F9/F10, the GUI and the
        watcher must not wait for ffprobe.  Until it settles, the clip is listed
        ``in_progress`` / not finalized (safe: the packager probes such clips).
        stop() joins these workers before its final segments.json write."""
        def work():
            try:
                self._settle_segment(seg)
            finally:
                self._write_segments_safe()
        t = threading.Thread(target=work, name=f"verify-seg{getattr(seg, 'index', '')}", daemon=True)
        self._verify_threads.append(t)
        t.start()

    def _join_verify(self, timeout: float = 150.0) -> None:
        threads = list(getattr(self, "_verify_threads", None) or ())
        deadline = time.monotonic() + timeout
        for t in threads:
            if t is not threading.current_thread():
                t.join(max(0.0, deadline - time.monotonic()))

    def stop_segment(self) -> None:
        if self.segment is not None:
            seg = self.segment
            seg.stop()
            self.segment = None
            if seg.frame_count == 0:
                print("[recorder] segment %d captured 0 frames - ffmpeg said:" % seg.index)
                print(seg.error_tail or "(no stderr output)")
                try:
                    os.remove(seg.path)
                except OSError:
                    pass
                self.segments.remove(seg)
                self._frames_json.pop(id(seg), None)
            elif segment_finalization(seg)[0]:
                self._settle_segment(seg)    # complete file: nothing to probe
            else:
                self._start_verify(seg)      # ffprobe runs off the lock
            self._write_segments_safe()      # K2: every stopped clip reaches disk at once

    def _restart_segment(self):
        """Caller holds the lock: replace a crashed segment (new clip)."""
        try:
            self.start_segment()
        except Exception as exc:
            self._restart_pending = True
            self._restart_at = time.monotonic() + 2.0
            self.last_error = self._start_error = str(exc)
            print(f"[recorder] could not restart capture ({exc}); retrying in 2 s")
            return None
        self._restart_pending = False
        self._clear_start_error()
        self._set_capturing(True)
        return self.segment

    # -------------------------------------------------------------------- watcher
    def _watch(self) -> None:
        ensure_thread_dpi_awareness()
        while not self.stopped.wait(0.1):
            try:
                self._watch_tick(wall_ms())
            except Exception as exc:  # keep recording even if introspection fails
                print(f"[recorder] watcher error: {exc}")

    def _watch_tick(self, now: float) -> None:
        """One 100 ms watcher poll (separate for unit tests)."""
        # Auto-pause rules run first: while paused they decide when to resume.
        self._watch_auto_pause(now)
        with self.lock:
            if self.paused or not self._capturing or self.stopped.is_set():
                return                  # nothing is written while paused
            self._watch_foreground(now)
            if now - self._last["rect_at"] > 250:
                self._last["rect_at"] = now
                self._watch_rect(now)
            if now - self._last["slow_at"] > 2000:
                self._last["slow_at"] = now
                self._watch_slow(now)

    def _watch_auto_pause(self, now: float, allow_resume: bool = True) -> None:
        """Privacy auto-pause / auto-resume.

        * any mode: a blocklisted app (password manager) in front -> mask + pause
          at once; it leaves -> resume (+ mask end).  In app mode the resume
          still waits for the target app, like any app-mode auto-pause.
        * app mode: another app in front for 300 ms -> mask + pause; the target
          is back -> resume.
        A manual pause is never resumed here; a failed resume stays an
        auto-pause and is retried with a backoff.  ``allow_resume=False`` (the
        check :meth:`_wait_first_frame` runs while a clip starts) only pauses.
        """
        fg = self._foreground_now()
        blocked = self._is_blocked(fg.get("exe", ""))
        app_rule = bool(self.app_mode and self.auto_pause_enabled)
        if not blocked and not app_rule and not (self.paused and self.auto_paused):
            return
        resumed = None
        with self.lock:
            if self.stopped.is_set():
                return
            state = "recording" if not self.paused else ("auto" if self.auto_paused else "manual")
            if blocked:
                self._auto.reset()
                if state == "recording":
                    self.policy_pauses += 1
                    self._auto_pause(self._mask_info(POLICY_TRIGGER, fg), "policy")
                return
            action = None
            if app_rule:
                action = self._auto.update(now, fg.get("state", FG_NONE), state)
            elif state == "auto" and self._auto_reason == "policy":
                action = "resume"
            if action == "pause":
                self._auto_pause(self._mask_info(APP_TRIGGER, fg), "app")
            elif action == "resume" and allow_resume and now >= self._resume_retry_at:
                resumed = self._resume()
                if resumed is None and self.paused and self.auto_paused:
                    delay = RESUME_BACKOFF_MS * (2 ** min(max(self._resume_failures - 1, 0), 8))
                    self._resume_retry_at = now + min(delay, RESUME_BACKOFF_MAX_MS)
        if resumed is not None:
            self._after_resume(resumed)

    def _fg_is_target(self, fg: Dict) -> bool:
        if (fg.get("bundleId") or "").lower() == self.target_exe:
            return True
        # out-of-process dialogs owned by the target (owner chain, from the masker)
        return bool(fg.get("hwnd")) and self._fg_info.get("hwnd") == fg.get("hwnd") \
            and self._fg_state == FG_TARGET

    def _fg_payload(self, fg: Dict) -> Dict:
        payload = {k: fg.get(k) for k in _FG_KEYS}
        if self._is_blocked(fg.get("bundleId") or ""):
            payload["windowTitle"] = ""      # a password manager's title is never logged (any mode)
        elif self.app_mode and not self._fg_is_target(fg):
            payload["windowTitle"] = ""      # never log other apps' titles
        return payload

    def _watch_foreground(self, now: float) -> None:
        fg = self.desktop.foreground()
        if self._is_blocked(fg.get("bundleId") or ""):
            # a password manager came to the front after this tick's auto-pause
            # check: it pauses on the next tick, its title is never logged
            self._last["hwnd"] = 0
            return
        if self.app_mode and not self._fg_is_target(fg):
            # other apps are masked: no active_app / title for them; coming back
            # to the target emits a fresh active_app
            self._last["hwnd"] = 0
            return
        if fg["hwnd"] != self._last["hwnd"]:
            self._last["hwnd"] = fg["hwnd"]
            self.log.emit("active_app", **self._fg_payload(fg))
            if fg["pid"] not in self._last["pids"]:
                self._last["pids"].add(fg["pid"])
                self.log.emit("app_launch", name=fg["name"], bundleId=fg["bundleId"],
                              pid=fg["pid"], source="foreground")
            # new foreground window: new baseline for move/resize detection
            self._last["rect"] = self.desktop.window_rect()

    def _watch_rect(self, now: float) -> None:
        rect = self.desktop.window_rect()
        prev = self._last["rect"] or {}
        if not rect or rect == prev:
            return
        fg = self.desktop.foreground()
        if self._is_blocked(fg.get("bundleId") or "") or (self.app_mode and not self._fg_is_target(fg)):
            return
        payload = {"appName": fg["name"], "bundleId": fg["bundleId"],
                   "windowTitle": self._fg_payload(fg)["windowTitle"], **rect}
        if prev and rect["width"] == prev.get("width") and rect["height"] == prev.get("height"):
            self.log.emit("window_moved", **payload)
        else:
            self.log.emit("window_resized", **payload)
        self._last["rect"] = rect

    def _refresh_pm_running(self) -> None:
        """Is any blocklisted (password manager) process running?  Windows only;
        on any error keep the previous answer."""
        if sys.platform != "win32":
            return
        try:
            from .app_mask import win_process_snapshot
            names = {normalize_exe(name) for _, _, name in win_process_snapshot()}
        except Exception:
            return
        self._pm_running = bool(names & self._blocklist_set)

    def _watch_slow(self, now: float) -> None:
        self._refresh_pm_running()
        p = power_status()
        if p and p != self._last["power"]:
            self._last["power"] = p
            self.log.emit("battery_status", **p)
            self.log.emit("power_source_change", **p)
        mem = memory_pressure()
        if mem != self._last["mem"]:
            self._last["mem"] = mem
            self.log.emit("memory_pressure", pressureLevel=mem)
        ap = self._appearance()
        if ap != self._last["appearance"]:
            self._last["appearance"] = ap
            self.log.emit("appearance_change", appearance=ap)
        screens = json.dumps(self.desktop.screens, sort_keys=True)
        if screens != self._last["screens"]:
            self._last["screens"] = screens
            self.log.emit_record(self.desktop.screen_config_event("change"))

    def _emit_state_snapshot(self, ts: float, cursor_source: str) -> None:
        """screen_config(open), active_app, appearance, power, memory, cursor."""
        d = self.desktop
        cfg = d.screen_config_event("open")
        self.log.emit_record(dict(cfg, timestamp=ts))
        fg = d.foreground(force=True)
        self.log.emit("active_app", **dict(self._fg_payload(fg), timestamp=ts))
        ap = self._appearance()
        self.log.emit("appearance_change", appearance=ap, timestamp=ts)
        p = power_status()
        if p:
            self.log.emit("battery_status", **dict(p, timestamp=ts))
            self.log.emit("power_source_change", **dict(p, timestamp=ts))
        mem = memory_pressure()
        self.log.emit("memory_pressure", pressureLevel=mem, timestamp=ts)
        x, y = cursor_pos()
        self.log.emit("mouse_move", **dict(d.event_context(), x=x, y=y, buttons=button_state(),
                                           synthetic=True, source=cursor_source, timestamp=ts))
        self._last.update({"hwnd": fg["hwnd"], "rect": d.window_rect(), "power": p, "mem": mem,
                           "appearance": ap,
                           "screens": json.dumps(d.screens, sort_keys=True)})
        self._last["pids"].add(fg["pid"])

    # -------------------------------------------------------------------- outputs
    def _appearance(self) -> str:
        return appearance()

    def _segment_json(self, seg, current) -> str:
        """One segments.json entry (cached once the clip is stopped and verified)."""
        key = id(seg)
        cached = self._sidecar_cache.get(key)
        if cached is not None:
            return cached
        settled = key in self._settled and seg is not current
        d = dict(seg.to_dict())
        note = self._seg_notes.get(key, {})
        d["in_progress"] = bool(d.get("in_progress")) or not settled
        if d["in_progress"]:
            d["finalized"] = False
        else:
            d.setdefault("finalized", bool(note.get("finalized", True)))
            d.setdefault("returncode", note.get("returncode"))
        for k in ("probed_frames", "probe", "truncated_from"):
            if k in note:
                d[k] = note[k]
        if note.get("broken"):
            d.update(broken=True, frames=[], frame_count=0)
        text = self._dumps_with_frames(key, d)
        if settled:
            self._sidecar_cache[key] = text
            self._frames_json.pop(key, None)
        return text

    def _dumps_with_frames(self, key: int, d: Dict) -> str:
        """``json.dumps(d)``, but ``d["frames"]`` is encoded incrementally.

        A running clip's frame list only grows, so the JSON of the frames already
        written is cached and only the new tail is encoded: the C encoder holds
        the GIL for a whole dump, and re-dumping hours of frames every ~10 s
        stalled the capture threads.  A list that was cut or rebuilt (K1
        truncation, broken clip) is detected and encoded again in full."""
        frames = d.get("frames")
        if not isinstance(frames, (list, tuple)):
            return json.dumps(d)
        rest = {k: v for k, v in d.items() if k != "frames"}
        n = len(frames)
        count, last, text = self._frames_json.get(key, (0, "", ""))
        if count > n or (count and json.dumps(frames[count - 1]) != last):
            count, last, text = 0, "", ""     # not a continuation of what was encoded
        if n > count:
            tail = json.dumps(list(frames[count:]))[1:-1]
            text = text + ", " + tail if text else tail
            count, last = n, json.dumps(frames[n - 1])
        self._frames_json[key] = (count, last, text)
        head = json.dumps(rest)
        return (head[:-1] + ", " if rest else "{") + '"frames": [' + text + "]}"

    def _write_segments(self) -> None:
        """raw/segments.json, atomically: every clip, the running one marked
        ``in_progress`` (contract K2: a crashed session can still be packaged)."""
        with self._sidecar_lock:
            current = self.segment
            parts = [self._segment_json(s, current) for s in list(self.segments)]
            write_text_atomic(os.path.join(self.raw, "segments.json"), "[" + ",".join(parts) + "]")

    def _write_segments_safe(self) -> bool:
        """Never raises (called from finally blocks and the status loop)."""
        started = time.monotonic()
        try:
            self._write_segments()
            ok = True
        except Exception as exc:
            print(f"[recorder] could not write segments.json: {exc}")
            ok = False
        took = time.monotonic() - started
        # a very long single clip makes each write slower: never spend >5% on it
        self._sidecar_due = time.monotonic() + max(SIDECAR_EVERY_S, took * 20.0)
        return ok

    def _cursor_in_video(self) -> bool:
        if not self.args.draw_mouse_cursor:
            return False
        segs = list(self.segments)
        if not segs:
            return True
        return all(bool(getattr(s, "cursor_in_video", True)) for s in segs)

    def _auto_paused_ms_now(self) -> float:
        ms = self.auto_paused_ms
        if self._auto_since is not None:
            ms += max(0.0, (self._stopped_at or wall_ms()) - self._auto_since)
        return ms

    def _write_capture(self, status: str, duration_ms: float = 0.0) -> None:
        segs = list(self.segments)
        first = segs[0] if segs else None

        def info(seg):
            if seg is None or getattr(seg, "backend", "") == "gdigrab":
                return None
            if hasattr(seg, "capture_info"):
                return seg.capture_info()
            return seg.to_dict().get("capture")

        hook = self._hook
        data = {
            "version": 2,
            "status": status,
            "host": socket.gethostname(),
            "platform": "windows",
            "os_version": platform.version(),
            "fps": self.args.fps,
            "source": self.args.source,
            "capture_mode": self.capture_mode,
            "capture_rect": _rect_dict(self.capture_rect),
            "cursor_in_video": self._cursor_in_video(),
            "draw_mouse_cursor": bool(self.args.draw_mouse_cursor),
            "app_target": ({"exe": self.target_exe_raw, "allow": list(self.allow_exes),
                            "overlay_allow": list(getattr(self, "overlay_allow", []) or []),
                            "auto_pause": bool(self.auto_pause_enabled),
                            "monitor": (self.capture_monitor or {}).get("index"),
                            "monitor_device": (self.capture_monitor or {}).get("deviceName")}
                           if self.app_mode else None),
            "mask_stats": {
                "masked_frames": int(sum(getattr(s, "masked_frames", 0) for s in segs)),
                "auto_pauses": int(self.auto_pauses),
                "auto_paused_ms": round(self._auto_paused_ms_now(), 1),
                "auto_pause_debounce_ms": AUTO_PAUSE_DEBOUNCE_MS,
                "policy_pauses": int(getattr(self, "policy_pauses", 0)),
                "suppressed_key_events": int(self.suppressed_keys),
                "suppressed_input_events": int(self.suppressed_inputs),
                "target_hidden_frames": int(sum(getattr(s, "target_hidden_frames", 0) for s in segs)),
                "mask_errors": int(sum(getattr(s, "mask_errors", 0) for s in segs)),
                "masker": self.masker.stats() if self.masker else None,
            },
            "codec": first.encoder if first is not None else pick_encoder(self.args.codec, self.args.ffmpeg),
            "backend": self.active_backend,
            "backend_requested": self.args.backend,
            "backend_detail": info(first),
            "backend_fallbacks": list(self.backend_fallbacks),
            "segment_backends": [getattr(s, "backend", "") for s in segs],
            "dpi_awareness": self.dpi_awareness,
            "quality": self.args.quality,
            "segments": len(segs),
            "paused_ms": round(self.paused_ms + (self._stopped_at - self._paused_at
                                                if self._stopped_at and self.paused and self._paused_at else 0), 1),
            "started_at_ms": round(self.started_at, 3),
            "stopped_at_ms": self._stopped_at,
            "duration_ms": round(duration_ms, 1),
            "screens": self.desktop.screens,
            "sync_latency_ms": self.args.sync_latency_ms,
            "privacy_blocklist": list(getattr(self, "_blocklist", None) or self.args.blocklist or []),
            "ffmpeg": shutil.which(self.args.ffmpeg) or self.args.ffmpeg,
            "session_info": self.session_info,
            "input_health": hook.health() if hook else {},
            "last_error": self.last_error,
            # K11: raw/.recording.lock held by this recorder (False: could not lock)
            "recording_lock": getattr(self, "_rec_lock", None) is not None,
        }
        write_text_atomic(os.path.join(self.raw, "capture.json"),
                          json.dumps(data, ensure_ascii=False, indent=2))

    def snapshot(self) -> Dict:
        """Thread-safe progress for the GUI overlay."""
        with self.lock:
            frames = sum(s.frame_count for s in self.segments)
            enc = self.segments[0].encoder if self.segments else None
            now = self._stopped_at or (self._paused_at if self.paused and self._paused_at else wall_ms())
            paused_ms = self.paused_ms
            if self._stopped_at and self.paused and self._paused_at:
                paused_ms += self._stopped_at - self._paused_at
            elapsed = max(0.0, now - self.started_at - paused_ms)
            if self._stop_done.is_set():
                elapsed = frames * 1000.0 / self.args.fps
            seg = self.segment
            app_mode = getattr(self, "app_mode", False)
            return {
                "elapsed_ms": elapsed,
                "paused": self.paused,
                "segments": len(self.segments),
                "frames": frames,
                "events": self.log.count,
                "backend": self.active_backend,
                "encoder": enc,
                "out": self.out,
                "stopped": self.stopped.is_set(),
                "auto_paused": bool(getattr(self, "auto_paused", False)),
                # "policy" = password manager in front (any mode), "app" = app mode: other app
                "auto_pause_reason": (getattr(self, "_auto_reason", None)
                                      if getattr(self, "auto_paused", False) else None),
                "capture_mode": getattr(self, "capture_mode", "desktop"),
                "target_exe": getattr(self, "target_exe_raw", ""),
                "masked_now": bool(app_mode and seg is not None and not self.paused
                                   and getattr(seg, "masked_now", False)),
                "foreground_state": getattr(self, "_fg_state", FG_NONE),
                "auto_pauses": getattr(self, "auto_pauses", 0),
                "last_error": getattr(self, "last_error", ""),
            }

    def _check_overlays(self) -> None:
        """App mode: an unknown see-through overlay (screen dimmer, DLP watermark
        ...) over at least half of the capture monitor is covered fail-closed, so
        (part of) the video is black.  Tell the user why, and how to allow it,
        while that lasts -- naming the overlays of the masker's *current* frame."""
        masker = getattr(self, "masker", None)
        if masker is None or not hasattr(masker, "stats"):
            return
        try:
            stats = masker.stats() or {}
            covering = bool((stats.get("last_window_reasons") or {}).get("overlay_unknown"))
            current = stats.get("last_unknown_overlays")
            if current is None:              # older masker: only the session-wide set
                current = stats.get("covered_unknown_overlays")
            # "?" = the overlay's process could not be identified: no flag can allow it
            exes = sorted({str(e).strip() for e in (current or []) if str(e or "").strip() not in ("", "?")})
        except Exception:
            return
        if covering and not self.paused and self.segment is not None:
            # (only while a clip is running: an error that stopped capture stays visible)
            if exes:
                names = " ".join(exes[:4])
                msg = L("检测到未知的透明覆盖层（{exe}）遮住了（部分）屏幕，为保护隐私这部分画面被遮黑；"
                        "如确认它不显示隐私内容，可用 --app-overlay-allow {exe} 放行",
                        "An unknown see-through overlay ({exe}) covers (part of) the screen, so that part of "
                        "the video is blacked out for privacy; if it shows nothing private, allow it with "
                        "--app-overlay-allow {exe}", exe=names)
            else:
                msg = L("检测到未知的透明覆盖层遮住了（部分）屏幕，为保护隐私这部分画面被遮黑；无法识别它属于哪个程序，"
                        "因此不能用 --app-overlay-allow 放行，请关闭显示该覆盖层的程序",
                        "An unknown see-through overlay covers (part of) the screen, so that part of the video "
                        "is blacked out for privacy; the program that draws it could not be identified, so it "
                        "cannot be allowed with --app-overlay-allow: close the program that shows it")
            if self.last_error != msg:
                if self._overlay_error != msg:
                    print(f"\n[recorder] WARNING {msg}")
                self.last_error = self._overlay_error = msg
        elif self._overlay_error and self.last_error == self._overlay_error and not covering:
            self.last_error = ""             # the overlay is gone: clear only our own message
            self._overlay_error = ""

    def _check_disk(self) -> bool:
        """Free space on the recording drive, every few seconds while recording.

        FFmpeg dies on a full disk and every restarted clip dies again, while
        the bar keeps showing REC: warn early, and stop cleanly (what was
        recorded stays packageable once space is freed) before that happens.
        Returns True when it stopped the recording.
        """
        now = time.monotonic()
        if self.stopped.is_set() or now < getattr(self, "_disk_due", 0.0):
            return False
        self._disk_due = now + DISK_CHECK_EVERY_S
        try:
            free = shutil.disk_usage(self.out).free
        except OSError:
            return False
        gb = round(free / 1024 ** 3, 1)
        prev = getattr(self, "_disk_error", "")
        if free < DISK_STOP_BYTES:
            msg = L("磁盘空间不足（剩 {gb} GB），已自动停止录制以保护已录内容；请清理磁盘后再打包或录制",
                    "Disk almost full ({gb} GB left): recording stopped to keep what was recorded; "
                    "free some space, then package or record again", gb=gb)
            print(f"\n[recorder] ERROR {msg}")
            self.last_error = self._disk_error = msg
            self.stop()
            return True
        if free < DISK_WARN_BYTES:
            if not prev:
                msg = L("磁盘空间不足（剩 {gb} GB），剩余不足 1 GB 时会自动停止录制",
                        "Low disk space ({gb} GB left): recording stops automatically below 1 GB", gb=gb)
                print(f"\n[recorder] WARNING {msg}")
                if not self.last_error:
                    self.last_error = self._disk_error = msg
        elif prev:
            if self.last_error == prev:
                self.last_error = ""
            self._disk_error = ""
        return False

    def _note_crash(self, tail: str) -> None:
        """A clip crashed (FFmpeg died). A few in a row mean every restart dies
        too (encoder / driver / disk problem): say so in the bar instead of
        silently recording nothing while the timer runs."""
        now = time.monotonic()
        times = [t for t in getattr(self, "_crash_times", []) if now - t < CRASH_LOOP_WINDOW_S]
        times.append(now)
        self._crash_times = times
        if len(times) >= CRASH_LOOP_COUNT and not self.last_error:
            last = next((ln.strip() for ln in reversed(str(tail or "").splitlines()) if ln.strip()), "")
            msg = L("视频采集反复中断，录像可能缺失：{err}", "Video capture keeps failing, the video may have gaps: {err}",
                    err=last[:160] or "?")
            self.last_error = self._crash_error = msg

    def _clear_crash_warning(self) -> None:
        err = getattr(self, "_crash_error", "")
        times = getattr(self, "_crash_times", [])
        if err and (not times or time.monotonic() - times[-1] > 2 * CRASH_LOOP_WINDOW_S):
            if self.last_error == err:
                self.last_error = ""
            self._crash_error = ""

    def _status(self) -> None:
        if not self.stopped.is_set() and time.monotonic() >= self._sidecar_due:
            try:
                self.log.flush()             # the event log is never more than ~10 s behind either
            except Exception as exc:
                print(f"[recorder] could not flush events.jsonl: {exc}")
            self._write_segments_safe()      # K2: crash-safe sidecar, ~every 10 s
        self._check_overlays()
        if self._check_disk():
            return
        seg = self.segment
        resumed = None
        if not (seg is not None and seg.crashed):
            self._clear_crash_warning()
        if seg is not None and seg.crashed:
            print("\n[recorder] WARNING capture segment stopped unexpectedly:")
            print(seg.error_tail[-800:])
            self._note_crash(seg.error_tail)
            with self.lock:
                if self.segment is seg and not self.stopped.is_set():
                    self._set_capturing(False)
                    self.stop_segment()
                    if not self.paused:
                        resumed = self._restart_segment()
        elif (seg is None and self._restart_pending and not self.paused and not self.stopped.is_set()
              and time.monotonic() >= self._restart_at):
            with self.lock:
                if self.segment is None and not self.paused and not self.stopped.is_set():
                    resumed = self._restart_segment()
        if resumed is not None:
            self._after_resume(resumed)
        if self.quiet:
            return
        frames = sum(s.frame_count for s in self.segments)
        x, y = cursor_pos()
        state = ("AUTO-PAUSED" if self.auto_paused else "PAUSED") if self.paused else "REC"
        print(f"\r[{state}] {_human(wall_ms() - self.started_at)} | seg {len(self.segments)} | "
              f"{frames} frames | {self.log.count} events | cursor {x},{y}   ", end="")


# ------------------------------------------------------------------------- CLI
def _default_out() -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(data_dir(create=False), f"rec-{stamp}")


def _fps_arg(value: str) -> int:
    try:
        fps = int(str(value).strip())
    except ValueError:
        fps = 0
    if not 1 <= fps <= MAX_FPS:
        raise argparse.ArgumentTypeError(L("fps 必须是 1 到 {m} 的整数", "fps must be an integer from 1 to {m}",
                                           m=MAX_FPS))
    return fps


def _source_error(source: str) -> Optional[str]:
    mode = capture_mode_of(source)
    value = source.split(":", 1)[1] if ":" in source else ""
    if mode == "desktop" and source != "desktop":
        return L("未知的录制来源 {s}", "Unknown --source {s}", s=source)
    if mode == "monitor":
        try:
            int(value)
        except ValueError:
            return L("显示器编号必须是整数：{s}", "Monitor index must be an integer: {s}", s=source)
    if mode == "region":
        try:
            parts = [int(v) for v in value.split(",")]
        except ValueError:
            parts = []
        if len(parts) != 4 or parts[2] <= 0 or parts[3] <= 0:
            return L("区域格式应为 region:x,y,宽,高", "Region must be region:x,y,width,height")
    if mode == "app" and not app_target_of(source):
        return L("请指定应用：app:<exe>", "Name the application: app:<exe>")
    if mode == "window" and not value.strip():
        return L("请指定窗口标题：window:<标题>", "Name the window: window:<title>")
    return None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="recorder", description="Desktop trajectory recorder")
    p.add_argument("start", nargs="?", default="start", choices=["start"],
                   help=L("start = 命令行录制；gui = 悬浮控制窗（python -m recorder gui）",
                          "start = record from the command line; gui = floating control bar "
                          "(python -m recorder gui)"))
    p.add_argument("--out", required=False, default=None,
                   help="output dataset directory (default: <data dir>/rec-<timestamp>)")
    p.add_argument("--fps", type=_fps_arg, default=30, help="1..120 (Markov samples are 30)")
    p.add_argument("--source", default="desktop",
                   help="desktop | monitor:<idx> | region:x,y,w,h | window:<title substring> | app:<exe>")
    p.add_argument("--app-allow", nargs="*", default=[], metavar="EXE",
                   help="app mode: extra exe names that stay visible (IME helpers %s are always allowed)"
                        % ", ".join(DEFAULT_APP_ALLOW))
    p.add_argument("--app-overlay-allow", nargs="*", default=[], metavar="EXE",
                   help="app mode: full-screen click-through overlay exes that draw nothing private "
                        "(screen dimmer, watermark); unknown ones are blacked out with the whole frame")
    p.add_argument("--app-monitor", type=int, default=None, metavar="N",
                   help="app mode: monitor to record (default: the one holding the app's largest window)")
    p.add_argument("--auto-pause-other-apps", action=argparse.BooleanOptionalAction, default=True,
                   help="app mode: pause while another app is in front (default on)")
    p.add_argument("--backend", default="auto", choices=list(BACKENDS),
                   help="auto = DXGI (bettercam) when usable, else gdi (app mode) / ffmpeg gdigrab")
    p.add_argument("--dxcam-output", type=int, default=0,
                   help="DXGI output index for the dxcam backend, used only when the display list is "
                        "unknown (desktop = the primary monitor; record another one with --source monitor:N)")
    p.add_argument("--codec", default="auto",
                   help="auto | h264_nvenc | hevc_nvenc | h264_amf | libx264 ...")
    p.add_argument("--quality", type=int, default=23, help="CQ/CRF, lower = better")
    p.add_argument("--audio-device", default=None,
                   help='dshow loopback/capture name, e.g. "Stereo Mix (Realtek)"')
    p.add_argument("--draw-mouse-cursor", action=argparse.BooleanOptionalAction, default=True,
                   help="draw the OS cursor into the video like Markov clips (default on)")
    p.add_argument("--sync-latency-ms", type=float, default=0.0,
                   help="constant offset subtracted from frame wall clock for calibration")
    p.add_argument("--blocklist", nargs="*", default=list(_DEFAULT_BLOCKLIST),
                   help="exe names that auto-pause the recording while in front (privacy mask, any mode); "
                        "the password managers %s are always included" % ", ".join(_DEFAULT_BLOCKLIST))
    p.add_argument("--hotkey-pause", type=int, default=VK_F9)
    p.add_argument("--hotkey-stop", type=int, default=VK_F10)
    p.add_argument("--max-seconds", type=float, default=0,
                   help="auto-stop after N seconds (0 = run until F10/Ctrl+C)")
    p.add_argument("--ffmpeg", default=None, help="ffmpeg executable (default: bundled / PATH)")
    p.add_argument("--session-info", default=None, help="CAD session information JSON")
    p.add_argument("--markov-delivery", action="store_true", help="enable Markov CAD export in the recorder window")
    args = p.parse_args(argv)
    args.source = str(args.source or "desktop").strip() or "desktop"
    problem = _source_error(args.source)
    if problem:
        p.error(problem)
    if args.app_monitor is not None and capture_mode_of(args.source) != "app":
        print("[recorder] note: --app-monitor only applies to --source app:<exe>")
    if not args.ffmpeg:
        args.ffmpeg = ffmpeg_path()
    if not args.out:
        args.out = _default_out()
    return args


def main(argv: Optional[List[str]] = None) -> int:
    safe_stdio()                             # bilingual messages on a cp1252 pipe / console never raise
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "gui":
        from .gui import main as gui_main
        return gui_main(argv[1:])
    args = parse_args(argv)
    if not shutil.which(args.ffmpeg):
        print(L("[recorder] 找不到 ffmpeg（{p}）；请重新安装 D Studio 或用 --ffmpeg 指定路径",
                "[recorder] ffmpeg not found ({p}); reinstall D Studio or pass --ffmpeg PATH", p=args.ffmpeg))
        return 2
    try:
        rec = Recorder(args)
    except ValueError as exc:
        print(f"[recorder] {exc}")
        return 2
    rec.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
