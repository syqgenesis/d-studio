"""Offline packager: raw capture -> training-ready deliverable.

    raw/events.jsonl + raw/segments.json + raw/segments/*.mp4
        -> clip.mp4, metadata.json, events.json, frame_events.json
        -> view/mouse.jsonl, view/events.jsonl, view/geometry.json
           (low-bandwidth indexes for the annotator)

Everything here is pure function of the raw capture, so schema/alignment changes
never require re-recording: just re-run the packager.

Positions in the package (events.json, frame_events.json, view/) stay in the
input hook's Windows virtual-screen pixels; view/geometry.json maps them onto
the video.  The delivered Markov clip set converts them to video pixels
(:mod:`packager.export_cad`).  Segments whose FFmpeg did not finish normally
are repaired or skipped first (:func:`prepare_segments`, contracts K1/K2).  A raw
folder whose recording lock is still held (:mod:`recorder.reclock`, K11) is
refused: the recorder is still writing it.
"""
from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from recorder import paths as _paths
from recorder import reclock
from recorder.i18n import L, safe_stdio

EVENTS_SCHEMA = 2
DEFAULT_MAX_GAP_MS = 30_000
DEFAULT_FPS = 30

_SYSTEM_TYPES = {
    "screen_config", "active_app", "app_launch", "app_terminate", "app_hidden",
    "app_unhidden", "window_moved", "window_resized", "window_title_changed",
    "modifier_change", "privacy_mask_start", "privacy_mask_end", "appearance_change",
    "battery_status", "power_source_change", "network_change", "wifi_change",
    "memory_pressure",
}
_DISCRETE_TYPES = _SYSTEM_TYPES | {"click", "key", "scroll"}

_BUTTON_BITS = {"left": 1, "right": 2, "middle": 4, "other_3": 8, "other_4": 16}

#: capture_mode values written by the recorder (contract section 4).
CAPTURE_MODES = ("desktop", "monitor", "region", "window", "app")
#: Modes whose video shows a whole display (Markov samples are full-screen).
FULL_SCREEN_MODES = frozenset({"desktop", "monitor", "app"})
#: maskTrigger of the app-only auto-pause (time is cut from the video).
AUTO_PAUSE_TRIGGER = "non_target_app"


# --------------------------------------------------------------------------- io
def load_json(path: str) -> Dict:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def stream_jsonl(path, errors: Optional[List[int]] = None) -> Iterator[Dict]:
    """Yield one JSON object per non-empty line.

    ``errors=None`` is strict (a bad line raises).  With a list, unreadable or
    non-object lines are skipped and their 1-based line numbers appended - the
    raw recorder log can end in a torn line if the PC lost power mid-write.
    """
    if errors is None:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
        return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                errors.append(number)
                continue
            if isinstance(rec, dict):
                yield rec
            else:
                errors.append(number)


def trailing_bad_lines(path, bad_lines: List[int]) -> int:
    """How many of ``bad_lines`` form the tail of the file's non-empty lines.

    A power cut leaves a torn or NUL-filled END of the log (the writer flushes
    but never fsyncs); corruption anywhere else is not explained by it.
    Returns len(bad_lines) when every bad line comes after the last good one, else 0.
    """
    if not bad_lines:
        return 0
    bad = set(bad_lines)
    last_good = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for number, line in enumerate(fh, 1):
                if line.strip() and number not in bad:
                    last_good = number
    except OSError:
        return 0
    return len(bad_lines) if min(bad_lines) > last_good else 0


def dumps(obj: Dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def publish_file(tmp: str, dst: str, attempts: int = 25) -> None:
    """Move a finished temp file over ``dst``.

    On Windows a replace fails while another process (the annotator server,
    an antivirus scan, Explorer preview) holds ``dst`` open without
    FILE_SHARE_DELETE; retry briefly, then overwrite in place, which a
    reader's share mode does allow.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if os.name != "nt" or attempt == attempts - 1:
                break
            time.sleep(0.1)
    shutil.copyfile(tmp, dst)
    try:
        os.remove(tmp)
    except OSError:
        pass


def _num(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return default


def _sort_key(rec: Dict, order: int) -> Tuple[float, int, int]:
    return (_num(rec.get("timestamp")), _int(rec.get("seq")), order)


def sorted_events(path: str, chunk_size: int = 200_000,
                  errors: Optional[List[int]] = None) -> Iterator[Dict]:
    """Stable, memory-bounded sort of the raw log by (timestamp, seq).

    The raw log is written by several threads, so it is *almost* ordered; a
    chunked external merge keeps memory flat for 1M+ event sessions.
    Unreadable lines are skipped and reported through ``errors``.
    """
    chunk: List[Tuple[float, int, int, Dict]] = []
    tmp_paths: List[str] = []
    order = 0
    bad = errors if errors is not None else []
    try:
        for rec in stream_jsonl(path, bad):
            key = _sort_key(rec, order)
            order += 1
            chunk.append((key[0], key[1], key[2], rec))
            if len(chunk) >= chunk_size:
                tmp_paths.append(_spill(chunk))
                chunk = []
        if tmp_paths:
            if chunk:
                tmp_paths.append(_spill(chunk))
            chunk = []
            iters = [_read_spill(p) for p in tmp_paths]
            for item in heapq.merge(*iters, key=lambda x: (x[0], x[1], x[2])):
                yield item[3]
        else:
            chunk.sort(key=lambda x: (x[0], x[1], x[2]))
            for item in chunk:
                yield item[3]
    finally:
        for p in tmp_paths:
            try:
                os.remove(p)
            except OSError:
                pass


def _spill(chunk: List[Tuple[float, int, int, Dict]]) -> str:
    chunk.sort(key=lambda x: (x[0], x[1], x[2]))
    fd, path = tempfile.mkstemp(prefix="destudio-sort-", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for ts, seq, order, rec in chunk:
            # keep the original arrival order so the merge stays stable
            fh.write(dumps({"o": order, "r": rec}) + "\n")
    return path


def _read_spill(path: str) -> Iterator[Tuple[float, int, int, Dict]]:
    for item in stream_jsonl(path):
        rec = item["r"]
        yield (_num(rec.get("timestamp")), _int(rec.get("seq")), int(item["o"]), rec)


# ------------------------------------------------------------------------ tools
def resolve_tools(capture: Optional[Dict] = None) -> Tuple[str, str]:
    """(ffmpeg, ffprobe) for a recording.

    Prefers the FFmpeg saved in capture.json (and its sibling ffprobe), so a
    recording made with the bundled FFmpeg is packaged with the same build even
    when nothing is on PATH; falls back to :mod:`recorder.paths` when the saved
    executable no longer exists (recording moved to another PC, app reinstalled).
    """
    capture = capture or {}
    exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    saved = str(capture.get("ffmpeg") or "").strip()

    def sibling_probe(ffmpeg_exe: str) -> Optional[str]:
        if not ffmpeg_exe:
            return None
        located = ffmpeg_exe if os.path.isfile(ffmpeg_exe) else (shutil.which(ffmpeg_exe) or ffmpeg_exe)
        folder = os.path.dirname(located)
        candidate = os.path.join(folder, exe) if folder else ""
        return candidate if candidate and os.path.isfile(candidate) else None

    ffmpeg: Optional[str] = None
    if saved and (os.path.isfile(saved) or shutil.which(saved)):
        ffmpeg = saved
    if ffmpeg is None:
        fallback = _paths.ffmpeg_path()
        if os.path.isfile(fallback) or shutil.which(fallback) or not saved:
            ffmpeg = fallback
        else:
            ffmpeg = saved  # nothing better known; let the error name the saved path
    ffprobe = sibling_probe(saved) or sibling_probe(ffmpeg) or _paths.ffprobe_path()
    return ffmpeg, ffprobe


# --------------------------------------------------------------- segment checks
#: Re-encode fallback when stream copy fails; Markov clips are libx264 CRF 23.
REENCODE_ARGS = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p")
CONCAT_COPY_TIMEOUT_S = 1800
_DISK_FULL_HINTS = ("no space left", "disk full", "not enough space", "enospc", "error number -28")


def is_disk_full(ffmpeg_stderr: str) -> bool:
    """FFmpeg's message for a full disk (POSIX ENOSPC / Windows ERROR_DISK_FULL)."""
    text = str(ffmpeg_stderr or "").lower()
    return any(h in text for h in _DISK_FULL_HINTS)


def segment_needs_probe(seg: Dict) -> bool:
    """True for a segment whose FFmpeg did not finish normally (contract K1/K2).

    ``in_progress``: still recording when raw/segments.json was last written
    (the recorder crashed).  ``finalized: false``: FFmpeg did not exit 0 after
    EOF/'q'.  Older recordings have neither key and are trusted as before.
    """
    return seg.get("in_progress") is True or seg.get("finalized") is False


def _segment_path(raw_dir: str, seg: Dict) -> str:
    return os.path.join(raw_dir, "segments", str(seg.get("file") or ""))


def probe_stream(path: str, ffprobe: str = "ffprobe", packets: bool = False) -> Optional[Dict]:
    """First video stream of ``path``: width/height/codec (+ packets when asked).

    ``packets=True`` lists every packet (pts, dts, flags); that reads the whole
    file, so it is only used for segments that did not finalize.  Returns
    ``None`` when the file cannot be read; raises :class:`OSError` when FFprobe
    itself cannot run.
    """
    entries = "stream=width,height,codec_name,time_base"
    if packets:
        entries += ":packet=pts,dts,flags"
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", entries, "-of", "json", path]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=3600 if packets else 120)
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        return None
    streams = data.get("streams") or []
    if not streams or not isinstance(streams[0], dict):
        return None
    st = streams[0]
    out: Dict = {"width": _int(st.get("width")) or None, "height": _int(st.get("height")) or None,
                 "codec": st.get("codec_name"), "time_base": st.get("time_base"),
                 "damaged": bool((r.stderr or "").strip()) or r.returncode != 0}
    if packets:
        out["packets"] = [p for p in (data.get("packets") or []) if isinstance(p, dict)]
    return out


def usable_prefix(packets: Sequence[Dict], damaged: bool = False) -> Tuple[int, Optional[int]]:
    """(frames, cut_dts) for the decodable start of a segment that did not finalize.

    ``packets`` are in decode order.  The answer is the longest decode-order
    prefix whose frames are exactly the first N frames in presentation order
    (B-frames make a decode-order prefix end in a hole otherwise), so it can be
    stream-copied without a gap.  ``cut_dts`` is the DTS of the first packet to
    drop (None = keep the whole file).  When FFprobe reported damage (a file cut
    off mid-write) the last packet is assumed partial and never kept.
    """
    def is_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    pts_list: List[int] = []
    for p in packets:
        if not is_int(p.get("pts")):
            break                          # nothing after an untimed packet is trusted
        pts_list.append(p["pts"])
    if damaged and pts_list:
        pts_list = pts_list[:-1]
    if not pts_list:
        return 0, None
    ordered = sorted(pts_list)
    steps = sorted(b - a for a, b in zip(ordered, ordered[1:]) if b > a)
    step = steps[len(steps) // 2] if steps else 1
    first = ordered[0]
    best, highest, seen = 0, -1, set()
    for i, pts in enumerate(pts_list):
        index = int(round((pts - first) / step))
        if index < 0 or index in seen:
            break
        seen.add(index)
        highest = max(highest, index)
        if highest == i:
            best = i + 1
    if best == len(packets):
        return best, None
    cut = packets[best].get("dts")
    if best == 0 or not is_int(cut):
        return 0, None                     # nowhere safe to cut: treat as unreadable
    return best, cut


def prepare_segments(segments: Sequence[Dict], raw_dir: str, fps: int,
                     ffprobe: str = "ffprobe") -> Tuple[List[Dict], List[Dict]]:
    """Apply the recorder's finalization flags (contracts K1/K2) before packaging.

    * ``broken: true`` -> skipped; its time becomes a gap between clips.
    * ``in_progress`` / ``finalized: false`` -> the mp4 is probed and the frame
      list is fitted to the decodable frames: truncated when the file holds
      fewer, extended at the nominal frame interval when the sidecar was saved
      before the last frames were encoded (crash), dropped when nothing is
      decodable.  A cut needed inside the file is passed to :func:`build_video`
      as ``_cut_s`` (concat ``outpoint``).

    Returns (segments to package, notes for capture.json).  Input dicts are not
    modified.
    """
    dt = 1000.0 / max(1, fps)
    kept: List[Dict] = []
    notes: List[Dict] = []
    for position, seg in enumerate(segments):
        frames = list(seg.get("frames") or [])
        note = {"position": position, "index": seg.get("index"), "file": seg.get("file"),
                "frames_listed": len(frames)}
        if seg.get("broken") is True:
            notes.append(dict(note, reason="broken", frames_kept=0))
            continue
        if not segment_needs_probe(seg):
            kept.append(seg)
            continue
        path = _segment_path(raw_dir, seg)
        info = None
        if seg.get("file") and os.path.isfile(path) and os.path.getsize(path) > 0:
            try:
                info = probe_stream(path, ffprobe, packets=True)
            except OSError as exc:
                raise RuntimeError(L("无法运行 FFprobe：{path}（{err}），无法检查未正常结束的录制片段",
                                     "Cannot run FFprobe: {path} ({err}); a segment that did not finish "
                                     "normally cannot be checked", path=ffprobe, err=exc)) from exc
            except subprocess.SubprocessError:
                info = None
        usable, cut = usable_prefix(info["packets"], info["damaged"]) if info else (0, None)
        if usable <= 0:
            notes.append(dict(note, reason="unreadable", frames_kept=0))
            continue
        out = dict(seg)
        out.pop("_cut_s", None)
        if usable < len(frames):
            out["frames"] = frames[:usable]
            reason = "truncated"
        elif usable > len(frames):
            # Sidecar saved before the last frames were encoded (crash): those
            # frames are in the file; place them at the nominal frame interval.
            walls = _frame_walls({"frames": frames})
            # gdigrab's start_wall_ms is the ffmpeg launch, not its first frame (the first
            # showinfo line can come 0.3-3 s later): with no frame walls it cannot be placed
            gdigrab_launch_only = not walls and str(seg.get("backend") or "") == "gdigrab"
            if len(walls) != len(frames) or gdigrab_launch_only \
                    or not (walls or _num(seg.get("start_wall_ms")) > 0):
                notes.append(dict(note, reason="untimed", frames_kept=0))
                continue
            base = walls[-1] if walls else _num(seg.get("start_wall_ms")) - dt
            extra = [round(base + k * dt, 3) for k in range(1, usable - len(frames) + 1)]
            if not frames or isinstance(frames[-1], (list, tuple)):
                last_n = _int(frames[-1][0], len(frames) - 1) if frames else -1
                extra = [[last_n + k, wall] for k, wall in enumerate(extra, 1)]
            out["frames"] = frames + extra
            reason = "extended"
        else:
            reason = "trimmed" if cut is not None else "complete"
        out["frame_count"] = usable
        if cut is not None:
            tb = str(info.get("time_base") or "1/1000")
            try:
                num, _, den = tb.partition("/")
                seconds = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                seconds = 0.001
            # half a frame before the first dropped packet: concat drops dts >= outpoint
            out["_cut_s"] = cut * seconds - 0.5 / max(1, fps)
        out["_size"] = (info.get("width"), info.get("height"))
        kept.append(out)
        # "complete": probed and used as listed; still noted, since FFmpeg did
        # not finish it normally (validation recognizes a recovered session).
        notes.append(dict(note, reason=reason, frames_kept=usable))
    return kept, notes


def _video_frame_count(seg: Dict) -> int:
    """Frames of ``seg`` that belong in the video (same rule as :func:`build_clips`)."""
    if "frames" in seg:
        return len(_frame_walls(seg))
    return _int(seg.get("frame_count"), 1)


def _concat_entry(path: str) -> str:
    return "file '" + os.path.abspath(path).replace("'", "'\\''") + "'\n"


# ------------------------------------------------------------------ video/concat
def build_video(segments: Sequence[Dict], out_path: str, ffmpeg: str = "ffmpeg", raw_dir: Optional[str] = None,
                *, ffprobe: Optional[str] = None, fps: Optional[int] = None) -> bool:
    """Join the segment mp4s into ``out_path`` (stream copy, ``+faststart``).

    Segments are written as fragmented MP4 (crash-safe, contract K10); the
    delivered clip is always remuxed into a normal MP4 with the index at the
    front, also for a single segment.  Every concat entry gets an exact
    ``duration`` (frames / fps): the container durations are rounded to 1 ms
    and would add timestamp jitter at each boundary.  Segments of different
    resolutions are refused (they cannot be stream-copied into one video).
    ``broken`` segments are skipped (see :func:`prepare_segments`).
    """
    raw_dir = raw_dir or os.path.join(os.path.dirname(out_path), "raw")
    entries = [s for s in segments if s.get("broken") is not True and s.get("file") and _video_frame_count(s) > 0]
    files = [(s, _segment_path(raw_dir, s)) for s in entries]
    valid = [(s, f) for s, f in files if os.path.exists(f) and os.path.getsize(f) > 0]
    if valid and len(valid) != len(files):
        raise ValueError(L("部分录制片段缺失，不能跳过片段后继续生成交付视频",
                           "Some recorded segments are missing; the video cannot be built by skipping them"))
    files = valid
    if not files:
        print("[packager] no segment files found; skipping video build")
        return False
    if ffprobe:
        check_segment_sizes(files, ffprobe)
    fps = fps or _int(files[0][0].get("fps"), DEFAULT_FPS) or DEFAULT_FPS
    list_path = out_path + ".concat.txt"
    tmp_out = out_path + ".partial.mp4"
    with open(list_path, "w", encoding="utf-8", newline="\n") as fh:
        frames_so_far = 0
        for seg, path in files:
            start_us = round(frames_so_far * 1_000_000 / fps)
            frames_so_far += _video_frame_count(seg)
            end_us = round(frames_so_far * 1_000_000 / fps)
            fh.write(_concat_entry(path))
            fh.write("duration %.6f\n" % ((end_us - start_us) / 1_000_000))
            if seg.get("_cut_s") is not None:
                fh.write("outpoint %.6f\n" % float(seg["_cut_s"]))
    try:
        for extra in (["-c", "copy"], list(REENCODE_ARGS)):
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "concat", "-safe", "0", "-i", list_path,
                   # Markov clips are video only; a --audio-device track would also make the
                   # container longer than the video stream (duration checks) - keep it in raw/
                   "-map", "0:v:0", "-an", *extra,
                   "-movflags", "+faststart", tmp_out]
            copy = extra[1] == "copy"
            try:
                # a stream copy of even a 90-minute clip takes well under a minute;
                # the re-encode fallback is legitimately slow and has no limit
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=CONCAT_COPY_TIMEOUT_S if copy else None)
            except subprocess.TimeoutExpired:
                print(f"[packager] concat (copy) timed out after {CONCAT_COPY_TIMEOUT_S} s")
                continue
            except OSError as exc:
                raise RuntimeError(L("无法运行 FFmpeg：{path}（{err}）", "Cannot run FFmpeg: {path} ({err})",
                                     path=ffmpeg, err=exc)) from exc
            if r.returncode == 0 and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 0:
                publish_file(tmp_out, out_path)
                return True
            err = (r.stderr or "").strip()
            print(f"[packager] concat failed ({'copy' if copy else 're-encode'}): {err[:300]}")
            if is_disk_full(err):
                # a re-encode would run for a long time and hit the same full disk
                raise RuntimeError(L("磁盘已满，无法拼接视频；原始文件已保留，请清理磁盘后重新打包",
                                     "The disk is full, so the video could not be joined; raw files are kept - "
                                     "free some space and package again"))
    finally:
        for leftover in (list_path, tmp_out):
            try:
                os.remove(leftover)
            except OSError:
                pass
    raise RuntimeError(L("视频片段拼接失败，保留原始文件；当前数据不能交付",
                         "Joining the video segments failed; raw files are kept and this recording cannot be delivered"))


def check_segment_sizes(files: Sequence[Tuple[Dict, str]], ffprobe: str) -> None:
    """Refuse segments of different resolutions (finding: backend fallback on resume).

    Stream copy would put frames of another size into one H.264 stream whose
    header announces the first size.  Unreadable files are left to the concat
    step; when FFprobe cannot run at all the check is skipped with a warning.
    """
    sizes: Dict[Tuple[int, int], List[str]] = {}
    for seg, path in files:
        size = seg.get("_size")
        if not (isinstance(size, (list, tuple)) and len(size) == 2 and all(size)):
            try:
                info = probe_stream(path, ffprobe)
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"[packager] WARN cannot run FFprobe ({exc}); segment resolutions not checked")
                return
            size = (info.get("width"), info.get("height")) if info else None
        if size and all(size):
            sizes.setdefault((int(size[0]), int(size[1])), []).append(os.path.basename(path))
    if len(sizes) > 1:
        listing = ", ".join(f"{w}x{h} ({', '.join(names)})" for (w, h), names in sorted(sizes.items()))
        raise ValueError(L("录制片段的分辨率不一致：{sizes}；不能拼接成一个视频（录制中途显示器或采集方式发生了变化）",
                           "The recorded segments have different resolutions: {sizes}; they cannot be joined into "
                           "one video (the display or capture method changed during recording)",
                           sizes=listing))


def probe_video(path: str, ffprobe: str = "ffprobe") -> Dict:
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,avg_frame_rate,nb_frames,duration",
           "-show_entries", "format=duration", "-of", "json", path]
    try:
        out = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout or "{}")
        st = (out.get("streams") or [{}])[0]
        return {"width": st.get("width"), "height": st.get("height"),
                "fps": st.get("avg_frame_rate"),
                "frames": int(st["nb_frames"]) if str(st.get("nb_frames", "")).isdigit() else None,
                "duration_s": float((out.get("format") or {}).get("duration") or 0.0)}
    except Exception:
        return {}


# ---------------------------------------------------------------------- geometry
def capture_mode_of(capture: Dict) -> str:
    """capture.json ``capture_mode``; older recordings derive it from ``source``."""
    mode = str(capture.get("capture_mode") or "").strip().lower()
    if mode in CAPTURE_MODES:
        return mode
    source = str(capture.get("source") or "desktop").strip()
    prefix = source.split(":", 1)[0].strip().lower()
    return prefix if prefix in CAPTURE_MODES else "desktop"


def cursor_in_video_of(capture: Dict) -> bool:
    """True only when the recorder says it drew the OS cursor into the video."""
    return capture.get("cursor_in_video") is True


def _rect_tuple(value) -> Optional[Tuple[int, int, int, int]]:
    """{x,y,width,height} (or a 4-sequence) -> (x, y, w, h) with w, h > 0."""
    try:
        if isinstance(value, dict):
            x, y = value.get("x", value.get("originX")), value.get("y", value.get("originY"))
            w, h = value.get("width", value.get("w")), value.get("height", value.get("h"))
        elif isinstance(value, (list, tuple)) and len(value) == 4:
            x, y, w, h = value
        else:
            return None
        rect = tuple(int(round(float(v))) for v in (x, y, w, h))
    except (TypeError, ValueError, OverflowError):
        return None
    return rect if rect[2] > 0 and rect[3] > 0 else None  # type: ignore[return-value]


def _screen_rect(screen: Dict) -> Optional[Tuple[int, int, int, int]]:
    return _rect_tuple({"x": screen.get("originX"), "y": screen.get("originY"),
                        "width": screen.get("width"), "height": screen.get("height")})


def capture_rect_of(capture: Dict) -> Optional[Tuple[int, int, int, int]]:
    """Virtual-screen rectangle the video shows, as (x, y, width, height).

    Uses ``capture_rect`` when the recorder wrote one; older recordings derive
    it from ``source`` + ``screens``.  App mode captures one monitor (whose
    origin can be negative on multi-monitor desktops), never the union.
    ``None`` means unknown (e.g. a legacy window capture).
    """
    rect = _rect_tuple(capture.get("capture_rect"))
    if rect:
        return rect
    screens = [s for s in (capture.get("screens") or []) if isinstance(s, dict) and _screen_rect(s)]
    source = str(capture.get("source") or "desktop")
    mode = capture_mode_of(capture)
    if mode == "region" and source.startswith("region:"):
        parts = source.split(":", 1)[1].split(",")
        return _rect_tuple(parts) if len(parts) == 4 else None
    if not screens:
        return None
    if mode == "monitor":
        try:
            idx = int(source.split(":", 1)[1])
        except (IndexError, ValueError):
            idx = 0
        screen = next((s for s in screens if s.get("index") == idx), screens[0])
        return _screen_rect(screen)
    if mode == "app":
        target = capture.get("app_target") if isinstance(capture.get("app_target"), dict) else {}
        idx = target.get("monitor", capture.get("app_monitor"))
        screen = next((s for s in screens if idx is not None and s.get("index") == idx), None)
        screen = screen or next((s for s in screens if s.get("primary")), screens[0])
        return _screen_rect(screen)
    if mode == "desktop":
        rects = [_screen_rect(s) for s in screens]
        x0 = min(r[0] for r in rects)
        y0 = min(r[1] for r in rects)
        x1 = max(r[0] + r[2] for r in rects)
        y1 = max(r[1] + r[3] for r in rects)
        return (x0, y0, x1 - x0, y1 - y0)
    return None


def view_geometry(capture: Dict, probe: Dict) -> Dict:
    """Screen-pixel -> video-pixel mapping used by the annotator / burn-in tool.

    ``video_px = (screen_px - origin) * videoSize / size``.
    """
    mode = capture_mode_of(capture)
    rect = capture_rect_of(capture)
    vw = _int(probe.get("width")) or (rect[2] if rect else 0) or 1920
    vh = _int(probe.get("height")) or (rect[3] if rect else 0) or 1080
    x0, y0, w, h = rect if rect else (0, 0, vw, vh)
    return {"originX": x0, "originY": y0, "width": w, "height": h,
            "videoWidth": vw, "videoHeight": vh,
            "coordinateSpace": "screen_px_top_left",
            "source": str(capture.get("source") or "desktop"),
            "captureMode": mode,
            "cursorInVideo": cursor_in_video_of(capture)}


def _axis_scale(video_px, screen_px) -> float:
    """video px per screen px; a 1 px difference is even-size padding, not scaling."""
    try:
        video, screen = float(video_px), float(screen_px)
    except (TypeError, ValueError):
        return 1.0
    if video <= 0 or screen <= 0 or abs(video - screen) <= 1:
        return 1.0
    return video / screen


def package_geometry(package_dir) -> Dict:
    """Screen-pixel -> clip.mp4 mapping of a built package (:func:`view_geometry`).

    From ``view/geometry.json``; falls back to capture.json's capture_rect and
    metadata's recording_resolution.  ``{}`` means the geometry is unknown.
    """
    root = str(package_dir)

    def read(*parts):
        try:
            data = load_json(os.path.join(root, *parts))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    geometry = read("view", "geometry.json")
    if "originX" in geometry:
        return geometry
    rect = capture_rect_of(read("capture.json"))
    if not rect:
        return {}
    size = read("metadata.json").get("recording_resolution")
    size = size if isinstance(size, dict) else {}
    return {"originX": rect[0], "originY": rect[1], "width": rect[2], "height": rect[3],
            "videoWidth": size.get("width") or rect[2], "videoHeight": size.get("height") or rect[3]}


def geometry_transform(geometry: Dict) -> Tuple[float, float, float, float]:
    """(origin_x, origin_y, scale_x, scale_y) of a geometry dict; ``{}`` is the identity."""
    try:
        ox, oy = float(geometry.get("originX") or 0), float(geometry.get("originY") or 0)
    except (TypeError, ValueError):
        ox = oy = 0.0
    return (ox, oy, _axis_scale(geometry.get("videoWidth"), geometry.get("width")),
            _axis_scale(geometry.get("videoHeight"), geometry.get("height")))


def to_video_px(value: float, origin: float, scale: float) -> float:
    """One screen coordinate in video pixels, exactly as the Markov export writes it.

    ``(value - origin) * scale``; integers stay integers and scaled values are
    rounded to whole pixels.  The result can lie outside the video (e.g. the
    cursor on another monitor); it is not clamped.
    """
    moved = (value - origin) * scale
    return int(round(moved)) if isinstance(value, int) or scale != 1.0 else moved


def video_transform(package_dir) -> Tuple[float, float, float, float]:
    """(origin_x, origin_y, scale_x, scale_y): virtual-screen px -> clip.mp4 px.

    ``video_px = (screen_px - origin) * scale``; unknown geometry is the
    identity (the video shows the screen at 0,0).
    """
    return geometry_transform(package_geometry(package_dir))


# ------------------------------------------------------------------------ clips
class Clip:
    __slots__ = ("index", "frame_start", "frame_count", "wall0", "frames", "gap_before_ms",
                 "gap_before_clamped", "segment_index")

    def __init__(self, index: int, frame_start: int, wall0: float, frames: List[float],
                 gap_before_ms: int, gap_before_clamped: bool, segment_index: Optional[int] = None):
        self.index = index
        self.frame_start = frame_start
        self.frame_count = len(frames)
        self.wall0 = wall0
        self.frames = frames
        self.gap_before_ms = gap_before_ms
        self.gap_before_clamped = gap_before_clamped
        self.segment_index = index if segment_index is None else segment_index


def _frame_walls(seg: Dict) -> List[float]:
    walls: List[float] = []
    for item in seg.get("frames") or []:
        try:
            value = float(item[1] if isinstance(item, (list, tuple)) else item)
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(value):
            walls.append(value)
    return walls


def build_clips(segments: Sequence[Dict], fps: int, max_gap_ms: int) -> List[Clip]:
    dt = 1000.0 / fps
    clips: List[Clip] = []
    frame_start = 0
    prev_end: Optional[float] = None
    for seg in segments:
        frames = _frame_walls(seg)
        if not frames:
            continue
        wall0 = frames[0]
        raw_gap = 0.0 if prev_end is None else wall0 - prev_end
        clamped = raw_gap > max_gap_ms
        gap = max(0.0, min(raw_gap, float(max_gap_ms)))
        clips.append(Clip(len(clips), frame_start, wall0, frames,
                          int(round(gap)), clamped, _int(seg.get("index"), len(clips))))
        frame_start += len(frames)
        prev_end = wall0 + len(frames) * dt
    return clips


def clip_index_for(clip_starts: List[float], ts: float) -> int:
    """Binary search: last clip whose start <= ts (clamped to valid range)."""
    lo, hi = 0, len(clip_starts) - 1
    if ts <= clip_starts[0]:
        return 0
    if ts >= clip_starts[hi]:
        return hi
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if clip_starts[mid] <= ts:
            lo = mid
        else:
            hi = mid - 1
    return lo


def map_event(ts: float, clips: List[Clip], clip_starts: List[float], fps: int) -> Tuple[float, int, float]:
    """wall clock -> (video_t_ms, frame_index, t_in_frame_ms)."""
    dt = 1000.0 / fps
    ci = clip_index_for(clip_starts, ts)
    c = clips[ci]
    video_t = c.frame_start * dt + (ts - c.wall0)
    lo = c.frame_start * dt
    hi = (c.frame_start + c.frame_count) * dt
    if video_t < lo:
        video_t = lo
    elif video_t >= hi:
        video_t = hi - 0.001
    frame = int(video_t * fps / 1000.0)
    frame = max(0, min(frame, c.frame_start + c.frame_count - 1))
    return video_t, frame, video_t - frame * dt


# ------------------------------------------------------------------ gap handling
def mask_trigger(rec: Dict) -> str:
    return str(rec.get("maskTrigger") or rec.get("trigger") or "").strip().lower()


def is_resume_marker(rec: Dict) -> bool:
    """First event of the state snapshot the recorder writes on every resume.

    v0.2 recorders open each segment like Markov does: ``screen_config``
    (reason "open") plus app/appearance/power/memory/cursor state, and an
    app-mode auto-resume adds ``privacy_mask_end`` (trigger non_target_app).
    Legacy recordings only ever wrote reason "open" before the first frame.
    """
    etype = rec.get("type")
    if etype == "screen_config":
        return str(rec.get("reason") or "").strip().lower() in ("open", "resume")
    if etype == "privacy_mask_end":
        return mask_trigger(rec) == AUTO_PAUSE_TRIGGER
    return False


_INPUT_EVENT_TYPES = frozenset({"click", "key", "scroll"})


def split_gap(gap: Sequence[Dict], input_gated: bool = False) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """Split events logged between two clips into (tail, head, dropped).

    * head: from the resume snapshot that follows the last
      ``privacy_mask_start`` -> clamped into the next clip's first frame
      (exactly like the pre-roll of the first clip);
    * tail: ``privacy_mask_start`` before it -> the previous clip's last
      frame (it explains why that clip ended);
    * dropped: everything else.  v0.2 recorders log nothing while paused, but
      legacy recordings kept logging input during a pause; those events are
      not in the video, so they only update the carried-over cursor/app state.

    The head starts at the *last* resume ``screen_config`` of the gap: an
    earlier one opened a segment that is not in the video (it captured no
    frames, or was skipped as broken), so input logged after it is not either.

    ``input_gated`` (capture version >= 2): the recorder logs no input while
    paused and re-opens the gate when the next segment starts, *before* its
    first frame (where the snapshot is stamped).  Clicks / keys / scrolls after
    the last ``privacy_mask_start`` therefore belong to that start-up and join
    the head (after the snapshot, so the clip still opens with it) instead of
    being dropped - otherwise a key-up or click-up would lose its down.
    """
    last_start = -1
    for i, rec in enumerate(gap):
        if rec.get("type") == "privacy_mask_start":
            last_start = i
    head_at = len(gap)
    for i in range(last_start + 1, len(gap)):
        if gap[i].get("type") == "screen_config" and is_resume_marker(gap[i]):
            head_at = i
    if head_at == len(gap):
        for i in range(last_start + 1, len(gap)):
            if is_resume_marker(gap[i]):
                head_at = i
                break
    before = list(gap[:head_at])
    tail = [r for r in before if r.get("type") == "privacy_mask_start"]
    startup = ([r for r in gap[last_start + 1:head_at] if r.get("type") in _INPUT_EVENT_TYPES]
               if input_gated and head_at < len(gap) else [])
    moved = {id(r) for r in startup}
    dropped = [r for r in before if r.get("type") != "privacy_mask_start" and id(r) not in moved]
    return tail, list(gap[head_at:]) + startup, dropped


# ----------------------------------------------------------------------- outputs
def write_events(events_iter: Iterable[Dict], out_path: str) -> int:
    count = 0
    tmp = out_path + ".partial"
    with open(tmp, "w", encoding="utf-8", newline="\n", buffering=1 << 20) as fh:
        for rec in events_iter:
            fh.write(dumps(rec) + "\n")
            count += 1
    publish_file(tmp, out_path)
    return count


def write_frame_events(
    clips: List[Clip],
    events_iter: Iterator[Dict],
    fps: int,
    frame_path: str,
    view_dir: str,
    *,
    preroll_is_gap: bool = False,
    input_gated: bool = False,
) -> Dict:
    """Bucket events per frame; synthesize per-frame cursor/delta/drag state.

    Bucketing uses the per-frame wall-clock sidecar (not the clip math), so the
    synthetic cursor state and the events in the same frame always agree.
    Events that happened before the first frame are clamped into frame 0 (this
    matches the reference data, where screen_config/active_app land on frame 0).
    Between clips (pause / auto-pause / stall) see :func:`split_gap`: the
    resume snapshot opens the next clip's first frame, ``privacy_mask_start``
    closes the previous clip's last frame, and anything else is dropped from
    the frame log (it is not in the video) but still updates the carried-over
    cursor/app state.  Every event stays in events.json.

    ``preroll_is_gap``: segments before the first clip were skipped (broken /
    unreadable), so the time before the first frame is a gap too; only its
    last resume snapshot (plus ``privacy_mask_start``) opens frame 0.
    """
    dt = 1000.0 / fps
    os.makedirs(view_dir, exist_ok=True)
    targets = (frame_path, os.path.join(view_dir, "mouse.jsonl"), os.path.join(view_dir, "events.jsonl"))
    handles = [open(p + ".partial", "w", encoding="utf-8", newline="\n", buffering=1 << 20) for p in targets]
    frame_fh, mouse_fh, view_fh = handles

    cur = next(events_iter, None)
    state = {"x": 0, "y": 0, "buttons": {k: False for k in _BUTTON_BITS},
             "appName": "", "bundleId": ""}
    stats = {"frames": 0, "discrete_events": 0, "dropped_in_gaps": 0,
             "kept_from_gaps": 0, "dropped_after_end": 0, "dropped_before_first_clip": 0,
             "preroll_from_ms": None}

    def ts_of(rec: Optional[Dict]) -> float:
        return _num(rec.get("timestamp")) if rec is not None else float("inf")

    def apply_state(rec: Dict) -> None:
        t = rec.get("type")
        if t in ("mouse_move", "drag"):
            if "x" in rec and "y" in rec:
                state["x"], state["y"] = _int(rec["x"], state["x"]), _int(rec["y"], state["y"])
            if t == "drag" and isinstance(rec.get("buttons"), dict):
                for k, v in rec["buttons"].items():
                    if k in state["buttons"]:
                        state["buttons"][k] = bool(v)
        elif t == "click":
            state["x"], state["y"] = _int(rec.get("x"), state["x"]), _int(rec.get("y"), state["y"])
            btn = rec.get("button")
            if btn in state["buttons"]:
                state["buttons"][btn] = bool(rec.get("isDown"))
        elif t in ("active_app", "app_launch"):
            state["appName"] = rec.get("name") or state["appName"]
            state["bundleId"] = rec.get("bundleId") or state["bundleId"]
        elif t in ("privacy_mask_start", "privacy_mask_end"):
            pass  # names the *other* (masked) app, not the one being recorded
        elif rec.get("appName"):
            state["appName"] = rec["appName"]
            state["bundleId"] = rec.get("bundleId", state["bundleId"])

    try:
        # pre-roll (before the first frame) -> frame 0
        pending: List[Dict] = []
        if clips:
            while cur is not None and ts_of(cur) < clips[0].frames[0]:
                pending.append(cur)
                cur = next(events_iter, None)
            if preroll_is_gap:
                tail, head, dropped = split_gap(pending, input_gated)
                for rec in dropped:
                    apply_state(rec)
                pending = tail + head
                stats["dropped_before_first_clip"] = len(dropped)
                stats["preroll_from_ms"] = ts_of(head[0]) if head else clips[0].frames[0]

        for ci, clip in enumerate(clips):
            has_next = ci + 1 < len(clips)
            next_start = clips[ci + 1].frames[0] if has_next else float("inf")
            # Nominal end; a sidecar stretched past it still owns its last frame.
            clip_end = max(clip.wall0 + clip.frame_count * dt, clip.frames[-1] + dt)
            clip_end = min(clip_end, next_start) if has_next else clip_end

            for i in range(clip.frame_count):
                wall = clip.frames[i]
                nxt_wall = clip.frames[i + 1] if i + 1 < clip.frame_count else clip_end
                frame_index = clip.frame_start + i
                frame_t = frame_index * 1000.0 / fps
                span = max(0.001, nxt_wall - wall)
                last = i == clip.frame_count - 1

                deltas = {"dx": 0.0, "dy": 0.0, "packets": 0, "n": 0}
                drag_count = 0
                drag_last: Optional[Dict] = None
                bucket: List[Dict] = []

                def to_bucket(rec: Dict) -> None:
                    # Map within this actual frame's capture interval. Arithmetic from
                    # clip.wall0 alone mis-buckets irregular/duplicated DXGI frames.
                    t_in = min(max((ts_of(rec) - wall) / span * dt, 0.0), dt - 0.001)
                    out = dict(rec)
                    out["video_t_ms"] = frame_t + t_in
                    out["t_in_frame_ms"] = t_in
                    bucket.append(out)
                    stats["discrete_events"] += 1

                # 1) events strictly before this frame: update state, keep discrete ones
                pre: List[Dict] = pending if i == 0 else []
                pending = []
                while cur is not None and ts_of(cur) < wall:
                    pre.append(cur)
                    cur = next(events_iter, None)
                for rec in pre:
                    apply_state(rec)
                    if rec.get("type") in _DISCRETE_TYPES:
                        to_bucket(rec)

                # 2) cursor state at the frame's first instant (what the viewer shows)
                cur_x, cur_y = state["x"], state["y"]
                cur_buttons = dict(state["buttons"])

                # 3) events inside this frame's window (last frame also takes the
                #    boundary sample, since there is no following frame to own it)
                in_frame: List[Dict] = []
                while cur is not None and (ts_of(cur) < nxt_wall or (last and ts_of(cur) <= nxt_wall)):
                    in_frame.append(cur)
                    cur = next(events_iter, None)
                for rec in in_frame:
                    apply_state(rec)
                    etype = rec.get("type")
                    if etype == "mouse_delta":
                        deltas["dx"] += _num(rec.get("dx"))
                        deltas["dy"] += _num(rec.get("dy"))
                        deltas["packets"] += _int(rec.get("packets"), 1) or 1
                        deltas["n"] += 1
                    elif etype == "drag":
                        drag_count += 1
                        drag_last = rec
                    elif etype in _DISCRETE_TYPES:
                        to_bucket(rec)

                # 4) the gap after this clip (pause / auto-pause / end of recording)
                if last:
                    gap: List[Dict] = []
                    while cur is not None and ts_of(cur) < next_start:
                        gap.append(cur)
                        cur = next(events_iter, None)
                    tail, head, dropped = split_gap(gap, input_gated)
                    head_ids = {id(r) for r in head}
                    for rec in gap:
                        if id(rec) not in head_ids:
                            apply_state(rec)
                    for rec in tail:
                        to_bucket(rec)
                    if has_next:
                        pending = head  # applied + bucketed as the next clip's pre-roll
                        stats["dropped_in_gaps"] += len(dropped)
                        stats["kept_from_gaps"] += len(tail) + len(head)
                    else:
                        stats["dropped_after_end"] += len(dropped) + len(head)
                        stats["kept_from_gaps"] += len(tail)

                synthetic_move = {
                    "type": "mouse_move", "x": cur_x, "y": cur_y, "screen": 0,
                    "buttons": cur_buttons,
                    "coordinate_space": "screen_px_top_left", "synthetic": True,
                    "source": "frame_timestamps_sidecar", "source_wall_ms": int(round(wall)),
                    "sidecar_frame": i, "timestamp": frame_t, "video_t_ms": frame_t,
                    "t_in_frame_ms": 0,
                }
                events = [synthetic_move]
                if deltas["n"]:
                    events.append({
                        "type": "mouse_delta", "dx": deltas["dx"], "dy": deltas["dy"],
                        "packets": deltas["packets"], "aggregated_count": deltas["n"],
                        "screen": 0, "sourcePid": 0, "timestamp": frame_t,
                        "video_t_ms": frame_t, "t_in_frame_ms": 0,
                        "source": "wm_input_frame_sum", "coordinate_space": "raw_hid_relative",
                    })
                if drag_count and drag_last is not None:
                    events.append({
                        "type": "drag", "x": state["x"], "y": state["y"], "screen": 0,
                        "buttons": dict(state["buttons"]), "button": drag_last.get("button", "left"),
                        "pressure": drag_last.get("pressure", 1),
                        "aggregated_count": drag_count, "sourcePid": 0, "timestamp": frame_t,
                        "video_t_ms": frame_t, "t_in_frame_ms": 0,
                        "source": "raw_drag_frame_state",
                        "coordinate_space": "screen_px_top_left",
                        "appName": drag_last.get("appName", state["appName"]),
                        "bundleId": drag_last.get("bundleId", state["bundleId"]),
                    })
                events.extend(bucket)
                frame_fh.write(dumps({"frame": frame_index, "video_t_ms": frame_t,
                                      "events": events}) + "\n")
                stats["frames"] += 1

                bits = 0
                for k, v in cur_buttons.items():
                    if v:
                        bits |= _BUTTON_BITS[k]
                mouse_fh.write(dumps({"f": frame_index, "x": cur_x, "y": cur_y,
                                      "b": bits}) + "\n")
                for rec in bucket:
                    view_fh.write(dumps(_view_record(rec, v_t_ms=rec.get("video_t_ms", frame_t))) + "\n")
        if not clips:
            while cur is not None:
                cur = next(events_iter, None)
    except BaseException:
        for fh in handles:
            fh.close()
        for p in targets:
            try:
                os.remove(p + ".partial")
            except OSError:
                pass
        raise
    for fh in handles:
        fh.close()
    for p in targets:
        publish_file(p + ".partial", p)
    return stats


def _view_record(rec: Dict, v_t_ms: float) -> Dict:
    t = rec.get("type")
    out: Dict = {"t": round(float(v_t_ms), 2), "type": t}
    if t == "click":
        out.update({k: rec[k] for k in ("x", "y", "button", "isDown", "clickCount") if k in rec})
    elif t == "key":
        out.update({k: rec[k] for k in ("keyCode", "characters", "isDown", "modifiers") if k in rec})
    elif t == "scroll":
        out.update({k: rec[k] for k in ("x", "y", "deltaX", "deltaY") if k in rec})
    elif t in ("active_app", "app_launch"):
        out.update({k: rec[k] for k in ("name", "bundleId", "windowTitle", "pid") if k in rec})
    elif t in ("privacy_mask_start", "privacy_mask_end"):
        out.update({k: rec[k] for k in ("maskTrigger", "trigger", "maskDetail", "appName", "exeName")
                    if k in rec})
    else:
        for k in ("appName", "windowTitle", "x", "y", "reason", "pressureLevel"):
            if k in rec:
                out[k] = rec[k]
    return out


def write_metadata(path: str, meta: Dict) -> None:
    tmp = path + ".partial"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    publish_file(tmp, path)


def _capture_version(capture: Dict) -> int:
    """capture.json ``version`` (1 = v0.1 recorder, which logged input while paused)."""
    try:
        return int((capture or {}).get("version") or 1)
    except (TypeError, ValueError):
        return 1


def _fps_of(capture: Dict, segments: Sequence[Dict]) -> int:
    for value in [capture.get("fps")] + [s.get("fps") for s in segments if isinstance(s, dict)]:
        fps = _int(value)
        if 1 <= fps <= 1000:
            return fps
    return DEFAULT_FPS


# -------------------------------------------------------------------------- main
def build(
    raw_dir: str,
    out_dir: str,
    dataset_id: Optional[str] = None,
    title: Optional[str] = None,
    description: str = "",
    category: str = "general",
    max_gap_ms: int = DEFAULT_MAX_GAP_MS,
    skip_video: bool = False,
    session_info: Optional[Dict] = None,
    *,
    markov: bool = False,
) -> Dict:
    raw_dir = os.path.abspath(raw_dir)
    out_dir = os.path.abspath(out_dir)
    if reclock.is_live(raw_dir):
        # K11: the recorder holds raw/.recording.lock until stop() has written everything
        raise SystemExit(L("录制仍在进行中（录制程序仍在写入 {path}），请先停止录制再打包",
                           "The recording is still in progress (the recorder is still writing {path}); "
                           "stop it before packaging", path=raw_dir))
    os.makedirs(out_dir, exist_ok=True)
    capture_path = os.path.join(raw_dir, "capture.json")
    try:
        capture = load_json(capture_path) if os.path.exists(capture_path) else {}
    except (OSError, ValueError):
        capture = {}
    if not isinstance(capture, dict):
        capture = {}
    segments_path = os.path.join(raw_dir, "segments.json")
    try:
        segments = load_json(segments_path)
    except (OSError, ValueError) as exc:
        raise SystemExit(L("找不到或无法读取 segments.json（{path}），录制可能未正常结束",
                           "segments.json is missing or unreadable ({path}); the recording may not have finished",
                           path=segments_path)) from exc
    if not isinstance(segments, list):
        segments = []
    segments = [s for s in segments if isinstance(s, dict)]
    fps = _fps_of(capture, segments)
    dataset_id = dataset_id or os.path.basename(out_dir) or str(uuid.uuid4())
    title = title or dataset_id
    platform_name = capture.get("platform", "windows")
    capture_mode = capture_mode_of(capture)
    cursor_in_video = cursor_in_video_of(capture)

    video_path = os.path.join(out_dir, "clip.mp4")
    ffmpeg, ffprobe = resolve_tools(capture)
    raw_segments = segments
    segments, segment_notes = prepare_segments(raw_segments, raw_dir, fps, ffprobe)
    for note in segment_notes:
        print(f"[packager] WARN segment {note.get('file')}: {note['reason']} "
              f"({note['frames_listed']} -> {note['frames_kept']} frames)")
    if not skip_video or not os.path.exists(video_path):
        build_video(segments, video_path, ffmpeg=ffmpeg, raw_dir=raw_dir, ffprobe=ffprobe, fps=fps)
    probe = probe_video(video_path, ffprobe=ffprobe) if os.path.exists(video_path) else {}

    clips = build_clips(segments, fps, max_gap_ms)
    # Segments skipped before the first clip: their time is a gap, not pre-roll.
    skipped = {n["position"] for n in segment_notes if n["frames_kept"] == 0}
    kept_positions = [i for i in range(len(raw_segments)) if i not in skipped]
    first_pos = next((p for p, s in zip(kept_positions, segments) if _frame_walls(s)), None)
    preroll_is_gap = first_pos is not None and any(p < first_pos for p in skipped)
    if not clips:
        raise SystemExit(L("segments.json 中没有任何视频帧，无法打包",
                           "segments.json contains no video frames; nothing to package"))
    total_frames = sum(c.frame_count for c in clips)
    total_duration_ms = int(round(total_frames * 1000.0 / fps))

    events_in = os.path.join(raw_dir, "events.jsonl")
    events_out = os.path.join(out_dir, "events.json")

    def raw_events(errors: List[int]) -> Iterator[Dict]:
        if not os.path.exists(events_in):
            print(f"[packager] WARN {events_in} is missing; packaging without input events")
            return iter(())
        return sorted_events(events_in, errors=errors)

    # two passes over a sorted stream: one for events.json, one for bucketing
    bad_lines: List[int] = []
    event_count = write_events(raw_events(bad_lines), events_out)
    frame_stats = write_frame_events(
        clips, raw_events([]), fps,
        os.path.join(out_dir, "frame_events.json"),
        os.path.join(out_dir, "view"),
        preroll_is_gap=preroll_is_gap,
        input_gated=_capture_version(capture) >= 2,
    )
    if bad_lines:
        print(f"[packager] WARN skipped {len(bad_lines)} unreadable raw event line(s): {bad_lines[:10]}")

    clip_starts = [c.wall0 for c in clips]
    os.makedirs(os.path.join(out_dir, "view"), exist_ok=True)
    write_metadata(os.path.join(out_dir, "view", "geometry.json"),
                   view_geometry(capture, probe))
    meta = {
        "workflow_id": dataset_id,
        "category": category,
        "platform": platform_name,
        "title": title,
        "description": description,
        "total_duration_ms": total_duration_ms,
        "total_frames": total_frames,
        "event_count": event_count,
        "fps": fps,
        "events_schema": EVENTS_SCHEMA,
        "video_file": "clip.mp4",
        "capture_mode": capture_mode,
        "cursor_in_video": cursor_in_video,
        "clips": [
            {"clip_index": c.index, "segment_index": c.segment_index,
             "frame_start": c.frame_start, "frame_count": c.frame_count,
             "source_start_ms": int(round(c.wall0)),
             "source_start_precise_ms": c.wall0,
             "source_end_ms": c.wall0 + c.frame_count * 1000.0 / fps,
             "gap_before_ms": c.gap_before_ms,
             "gap_before_clamped": c.gap_before_clamped}
            for c in clips
        ],
    }
    info = dict(capture.get("session_info") or {}) if isinstance(capture.get("session_info"), dict) else {}
    if session_info is not None:
        info.update(session_info)
    meta.update({
        "recording_resolution": {"width": probe.get("width"), "height": probe.get("height")},
        "software": {"name": info.get("software_name", ""), "version": info.get("software_version", ""),
                     "license_reference": info.get("license_reference", ""),
                     "interface_language": info.get("interface_language", "")},
        "engineer_id": info.get("engineer_id", ""), "part_id": info.get("part_id", ""),
        "authorization_reference": info.get("authorization_reference", ""),
        "review": {key: info.get(key, False) is True for key in
                   ("originality_confirmed", "task_completed", "sync_verified", "quality_reviewed")},
    })
    write_metadata(os.path.join(out_dir, "metadata.json"), meta)

    narration_path = os.path.join(out_dir, "narration.json")
    if not os.path.exists(narration_path):
        write_metadata(narration_path, {"summary": "", "annotations": []})

    capture_out = dict(capture)
    capture_out.update({
        "capture_mode": capture_mode,
        "cursor_in_video": cursor_in_video,
        "session_info": info,
        "packaged_at": os.path.getmtime(video_path) if os.path.exists(video_path) else None,
        "packager": {"schema": EVENTS_SCHEMA, "max_gap_ms": max_gap_ms,
                     "frames": frame_stats["frames"],
                     "dropped_events_in_gaps": frame_stats["dropped_in_gaps"],
                     "kept_events_from_gaps": frame_stats["kept_from_gaps"],
                     "dropped_events_after_end": frame_stats["dropped_after_end"],
                     "dropped_events_before_first_clip": frame_stats["dropped_before_first_clip"],
                     "preroll_from_ms": frame_stats["preroll_from_ms"],
                     "discrete_events": frame_stats["discrete_events"],
                     "invalid_raw_lines": len(bad_lines),
                     "invalid_raw_tail_lines": trailing_bad_lines(events_in, bad_lines),
                     "segment_repairs": [{k: v for k, v in n.items() if k != "position"}
                                         for n in segment_notes]},
        "video_probe": probe,
        "clip_starts_ms": [int(round(s)) for s in clip_starts],
    })
    write_metadata(os.path.join(out_dir, "capture.json"), capture_out)
    from .validate import validate_dataset
    report = validate_dataset(out_dir, markov=markov, raw_dir=raw_dir)
    write_metadata(os.path.join(out_dir, "validation.json"), report)

    if probe.get("duration_s"):
        drift = abs(probe["duration_s"] * 1000.0 - total_duration_ms)
        if drift > 1000.0 / fps * 2:
            print(f"[packager] WARN video duration {probe['duration_s']:.3f}s != "
                  f"frames/fps {total_duration_ms / 1000.0:.3f}s (drift {drift:.0f}ms)")

    print(f"[packager] {out_dir}\n"
          f"  video      {probe.get('width')}x{probe.get('height')} "
          f"{probe.get('duration_s', 0):.2f}s ({probe.get('fps', '?')})\n"
          f"  capture    {capture_mode}  cursor_in_video={cursor_in_video}\n"
          f"  clips      {len(clips)}  frames {total_frames}  fps {fps}\n"
          f"  events     {event_count} (frame-bucketed discrete {frame_stats['discrete_events']}, "
          f"dropped in gaps {frame_stats['dropped_in_gaps']}, kept from gaps {frame_stats['kept_from_gaps']})\n"
          f"  files      clip.mp4 events.json frame_events.json metadata.json narration.json view/")
    return meta


def main(argv: Optional[List[str]] = None) -> int:
    safe_stdio()   # bilingual messages on a cp1252 pipe (installed app runs python -E)
    ap = argparse.ArgumentParser(prog="packager", description=L("把原始录制打包成训练数据包",
                                                                "Build a training package from a raw capture"))
    ap.add_argument("--raw", required=True, help=L("录制端生成的 <out>/raw 目录", "<out>/raw produced by the recorder"))
    ap.add_argument("--out", required=True, help=L("数据包目录（标注台读取）", "package directory (served by the annotator)"))
    ap.add_argument("--id", default=None, help="workflow_id / dataset id")
    ap.add_argument("--title", default=None)
    ap.add_argument("--description", default="")
    ap.add_argument("--category", default="general")
    ap.add_argument("--max-gap-ms", type=int, default=DEFAULT_MAX_GAP_MS)
    ap.add_argument("--skip-video", action="store_true", help=L("复用已有 clip.mp4", "reuse existing clip.mp4"))
    ap.add_argument("--session-info", help=L("CAD 会话信息 JSON", "CAD session information JSON"))
    ap.add_argument("--markov", action="store_true", help=L("按 Markov CAD 样例核对任务集",
                                                            "check the task set like the Markov CAD sample"))
    args = ap.parse_args(argv)
    build(args.raw, args.out, args.id, args.title, args.description,
          args.category, args.max_gap_ms, args.skip_video,
          session_info=load_json(args.session_info) if args.session_info else None,
          markov=args.markov)
    return 0


if __name__ == "__main__":
    sys.exit(main())
