"""Check actual media and both JSONL logs; never equate packaging with acceptance.

Report (validation.json):
    technical_ok        media + logs are internally consistent (gate for any export)
    ready_for_delivery  technical_ok and no delivery item is missing (CAD delivery gate)
    technical_errors    list[msg]  blocking problems in the recording itself
    delivery_missing    list[msg]  items the engineer/reviewer still has to supply
    warnings            list[msg]  informational, never blocking
    privacy_masks       int        privacy masks other than the app-only auto-pause
    uncut_privacy_masks int        of those, masks whose time is still in the video or
                                   whose input is still in the raw log
                                   (``export_recording`` refuses these)
    offscreen_cursor_frames int    frames whose cursor lies outside the delivered video
A capture whose status is not "completed" is still recording while its
``raw/.recording.lock`` is held (:mod:`recorder.reclock`, contract K11) and
counts as recovered after an abnormal stop once the lock is free.
Every message is a ``{"zh": ..., "en": ...}`` dict (:func:`recorder.i18n.both`),
so the report reads the same after the UI language changes; render one with
:func:`message_text`.  ``language`` only records the UI language at check time.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import subprocess
from fractions import Fraction
from pathlib import Path
import sqlite3
import zipfile

from recorder import reclock
from recorder.i18n import L, both, get_language, safe_stdio

from .build import (AUTO_PAUSE_TRIGGER, FULL_SCREEN_MODES, _DISCRETE_TYPES, capture_mode_of,
                    capture_rect_of, cursor_in_video_of, geometry_transform, mask_trigger,
                    package_geometry, resolve_tools, to_video_px)
from .markov_task import MODEL_EXTENSIONS, TASK_FILES, task_directory, validate_task

MARKOV_FPS = 30
MARKOV_CODEC = "h264"
#: Shift/Ctrl/Alt/CapsLock/Win virtual-key codes (same set as recorder.recorder.MODIFIER_VKS).
MODIFIER_VKS = frozenset({0x10, 0x11, 0x12, 0x14, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5})
#: Same defaults as recorder.recorder._DEFAULT_BLOCKLIST (password managers, contract K4).
DEFAULT_PRIVACY_BLOCKLIST = ("1password.exe", "keepass.exe", "keepassxc.exe", "bitwarden.exe")


def message_text(message, lang=None):
    """Render a report message (``both()`` dict or legacy plain string)."""
    if isinstance(message, dict):
        lang = lang or get_language()
        return str(message.get(lang) or message.get("en") or message.get("zh") or "")
    return str(message)


def _size_matches(video, expected):
    """(exact, acceptable): encoders need even sizes, so a 1 px crop (or older pad) is tolerated."""
    vw, vh = video
    w, h = expected
    if (vw, vh) == (w, h):
        return True, True
    close = abs(vw - w) <= 1 and abs(vh - h) <= 1 and vw % 2 == 0 and vh % 2 == 0
    return False, close


def _screens_in(capture, rect):
    """How many monitors overlap the captured rectangle."""
    x, y, w, h = rect
    count = 0
    for screen in capture.get("screens") or []:
        try:
            sx, sy = int(screen["originX"]), int(screen["originY"])
            sw, sh = int(screen["width"]), int(screen["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if sx < x + w and x < sx + sw and sy < y + h and y < sy + sh:
            count += 1
    return count


def privacy_intervals(marks):
    """Pair ``privacy_mask_start``/``end`` (ts, type, trigger) into (start, end, trigger).

    Pairs by trigger (older recorders put the *new* foreground exe in the end
    event's maskDetail).  A start without an end lasts to the end of the
    recording (``end = inf``); an end without a start is ignored.
    """
    open_at, intervals = {}, []
    for ts, kind, trigger in sorted(marks, key=lambda m: m[0]):
        if kind == "privacy_mask_start":
            open_at.setdefault(trigger, ts)
        elif trigger in open_at:
            intervals.append((open_at.pop(trigger), ts, trigger))
    intervals.extend((start, math.inf, trigger) for trigger, start in open_at.items())
    return sorted(intervals)


def _cursor_outside(event, transform, width, height):
    """Whether a synthetic cursor sample (screen px) exports outside [0,width) x [0,height).

    Uses the export's own mapping (:func:`packager.build.to_video_px`), so the
    count matches what the delivered clip set contains.
    """
    ox, oy, sx, sy = transform
    x, y = event.get("x"), event.get("y")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (x, y)):
        return False
    vx, vy = to_video_px(x, ox, sx), to_video_px(y, oy, sy)
    return not (0 <= vx < width and 0 <= vy < height)


def clip_timeline(clips, dt, total=None):
    """``[(start_ms, frame_count)]`` from metadata's clip table, or None if malformed.

    Output frame ``k`` of a clip plays at its NOMINAL time ``start + k * dt``
    (``source_start_precise_ms``, else the rounded ``source_start_ms``).
    With ``total`` (metadata ``total_frames``) the frame counts must add up to
    it, so an empty table for a non-empty video is malformed too.
    """
    timeline = []
    try:
        for c in clips or []:
            start = float(c.get("source_start_precise_ms", c["source_start_ms"]))
            count = int(c["frame_count"])
            if not math.isfinite(start) or count < 0:
                return None
            timeline.append((start, count))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None
    if total is not None and sum(count for _, count in timeline) != total:
        return None
    return timeline


def _key_code(event):
    try:
        return int(event.get("keyCode"))
    except (TypeError, ValueError, OverflowError):
        return None


def input_inside(start, end, times):
    """Whether a timestamp of the sorted list ``times`` lies strictly inside (start, end)."""
    i = bisect.bisect_right(times, start)
    return i < len(times) and times[i] < end


def _inside(start, end, clip_start, count, dt):
    """Indices (lo, hi) of a clip's frames whose nominal time lies strictly in (start, end)."""
    lo = 0 if start < clip_start else max(0, math.floor((start - clip_start) / dt) - 1)
    while lo < count and not clip_start + lo * dt > start:
        lo += 1
    if math.isinf(end):
        hi = count - 1
    else:
        hi = min(count - 1, max(lo, math.ceil((end - clip_start) / dt) + 1))
        while hi >= lo and not clip_start + hi * dt < end:
            hi -= 1
    return lo, hi


def mask_was_cut(start, end, timeline, dt):
    """Whether the video leaves out the masked time (contract K4).

    ``timeline`` is :func:`clip_timeline`: every output frame is judged by its
    nominal time, i.e. what the video shows at that moment.  (The per-frame
    sidecar wall is not used: v0.2 DXGI recordings stamped a duplicate-padded
    slot with the *previous* capture's wall, so a static screen during the mask
    looked like no frame inside it.)

    Strict rule: no output frame lies strictly inside ``(start, end)``, with at
    most one frame of slack per boundary for a capture placed in its slot by
    rounding - the stopped clip's LAST frame within ``dt`` after ``start`` and
    the resumed clip's FIRST frame within ``dt`` before ``end`` (so at most
    ``2 * dt`` in total).  Frames of a clip that runs on through the mask are
    never slack, however short the mask.
    """
    inside, first, last = 0, None, None        # (time, index, count) of the earliest / latest
    for clip_start, count in timeline:
        if count <= 0:
            continue
        lo, hi = _inside(start, end, clip_start, count, dt)
        if hi < lo:
            continue
        inside += hi - lo + 1
        if first is None or clip_start + lo * dt < first[0]:
            first = (clip_start + lo * dt, lo, count)
        if last is None or clip_start + hi * dt > last[0]:
            last = (clip_start + hi * dt, hi, count)
    if not inside:
        return True
    slack = 0
    if first[1] == first[2] - 1 and first[0] - start <= dt + 1e-6:
        slack += 1
    if inside > slack and last[1] == 0 and end - last[0] <= dt + 1e-6:
        slack += 1
    return inside == slack


def validate_dataset(directory, decode=False, task_set=None, *, markov=False, raw_dir=None):
    """Validation report of the package in ``directory`` (see the module docstring).

    ``raw_dir``: the recorder's raw folder (default ``<directory>/raw``), whose
    recording lock tells a live session from a crashed one (K11).
    """
    root = Path(directory).resolve()
    errors, missing, warnings = [], [], []

    def error(message):
        if message not in errors:
            errors.append(message)

    def warn(message):
        if message not in warnings:
            warnings.append(message)

    def read(name, required=True):
        path = root / name
        if not required and not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError(name)
            return data
        except (OSError, ValueError):
            error(both("缺失或损坏的文件：{name}", "Missing or corrupt file: {name}", name=name))
            return {}

    meta, capture = read("metadata.json"), read("capture.json")
    total = meta.get("total_frames", 0)
    fps = meta.get("fps", 0)
    if isinstance(fps, bool) or not isinstance(fps, (float, int)) or not math.isfinite(fps) or fps <= 0:
        error(both("metadata 帧率无效", "metadata fps is invalid"))
        fps = MARKOV_FPS
    elif fps != MARKOV_FPS:
        warn(both("帧率为 {fps} fps；Markov 样例为 30 fps，交付前请与客户确认",
                  "Frame rate is {fps} fps; Markov samples are 30 fps - confirm with the customer before delivery",
                  fps=fps))
    dt = 1000.0 / fps
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        error(both("metadata 总帧数无效", "metadata total_frames is invalid"))
        total = 0
    duration = total * dt
    if not isinstance(meta.get("total_duration_ms"), (int, float)) or abs(meta.get("total_duration_ms") - duration) > 1:
        error(both("metadata 时长与帧数不一致", "metadata duration does not match the frame count"))

    mode = capture_mode_of(capture)
    rect = capture_rect_of(capture)
    full_screen = mode in FULL_SCREEN_MODES
    if not full_screen:
        missing.append(both("客户要求全屏录制，当前会话为 {mode} 录制（区域或窗口）",
                            "The customer requires full-screen recording; this session is a {mode} capture (region or window)",
                            mode=mode))
    elif mode == "desktop" and rect:
        spanned = _screens_in(capture, rect)
        if spanned > 1:
            warn(both("整个桌面跨 {n} 个显示器；Markov 样例为单显示器，建议选择单个显示器或应用模式",
                      "The desktop spans {n} monitors; Markov samples are single-display - prefer one monitor or app mode",
                      n=spanned))
    if not cursor_in_video_of(capture):
        warn(both("视频中未绘制鼠标光标（Markov 样例带光标）；可用 tools/burn_overlay.py 烧录",
                  "The mouse cursor is not drawn into the video (Markov samples include it); tools/burn_overlay.py can burn it in"))
    mask_stats = capture.get("mask_stats") if isinstance(capture.get("mask_stats"), dict) else {}
    if mode == "app" and mask_stats.get("auto_pauses"):
        try:
            cut_s = float(mask_stats.get("auto_paused_ms") or 0) / 1000.0
        except (TypeError, ValueError):
            cut_s = 0.0
        warn(both("应用模式自动暂停 {n} 次，共剪去 {s:.1f} 秒（其他应用不在成片中）",
                  "App-only mode auto-paused {n} time(s); {s:.1f} s were cut (other apps are not in the video)",
                  n=mask_stats.get("auto_pauses"), s=cut_s))
    packager_stats = capture.get("packager") if isinstance(capture.get("packager"), dict) else {}
    repairs = [n for n in packager_stats.get("segment_repairs") or [] if isinstance(n, dict)]
    for note in repairs:
        name, kept = note.get("file") or note.get("index"), note.get("frames_kept", 0)
        reason = note.get("reason")
        if reason in ("broken", "unreadable", "untimed"):
            warn(both("录制片段 {name} 已损坏（录制或 FFmpeg 异常结束），未放入成片；这段时间按暂停处理",
                      "Segment {name} is damaged (the recorder or FFmpeg stopped abnormally) and was left out; "
                      "its time is treated like a pause", name=name))
        elif reason in ("truncated", "trimmed"):
            warn(both("录制片段 {name} 异常结束，只使用前 {n} 帧可解码画面",
                      "Segment {name} ended abnormally; only its first {n} decodable frames are used",
                      name=name, n=kept))
        elif reason == "extended":
            warn(both("录制片段 {name} 最后 {n} 帧的时间戳未及保存（录制异常结束），已按标准帧间隔补齐",
                      "Segment {name}: the recorder stopped before saving the times of its last {n} frames; "
                      "they were placed at the nominal frame interval",
                      name=name, n=max(0, kept - int(note.get("frames_listed") or 0))))

    media = root / str(meta.get("video_file") or "clip.mp4")
    clip_decodes = False          # FFprobe read the clip and its frame count matches the log
    video_size = None
    if not media.is_file() or media.stat().st_size == 0:
        error(both("缺少可播放的 {name}", "Missing playable {name}", name=media.name))
    else:
        _, probe = resolve_tools(capture)
        cmd = [probe, "-v", "error", "-select_streams", "v:0"]
        if decode:
            cmd += ["-count_frames"]
        cmd += ["-show_entries", "stream=codec_name,width,height,avg_frame_rate,nb_frames,nb_read_frames"
                ":stream_tags=encoder:format=duration", "-of", "json", str(media)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=3600 if decode else 120)
            data = json.loads(result.stdout)
            stream = data["streams"][0]
            count = stream.get("nb_read_frames" if decode else "nb_frames")
            if count is None or int(count) != total:
                error(both("视频实际帧数与日志帧数不一致", "The video's frame count does not match the frame log"))
            else:
                clip_decodes = total > 0
            if abs(float(Fraction(stream["avg_frame_rate"])) - fps) > 0.001:
                error(both("视频实际帧率与 metadata 不一致", "The video's frame rate does not match metadata"))
            if abs(float(data["format"]["duration"]) * 1000 - duration) > dt + 1:
                error(both("视频时长与帧级日志不一致", "The video duration does not match the frame log"))
            codec = str(stream.get("codec_name") or "").strip().lower()
            if codec != MARKOV_CODEC:
                error(both("成片编码为 {codec}；Markov 要求 H.264（libx264 CRF 23）",
                           "The clip is encoded as {codec}; Markov requires H.264 (libx264 CRF 23)",
                           codec=codec or "?"))
            else:
                encoder = str((stream.get("tags") or {}).get("encoder") or "").strip()
                if "x264" not in encoder.lower():
                    warn(both("视频编码器为 {enc}；Markov 样例使用 libx264 CRF 23，交付前请确认",
                              "The video encoder is {enc}; Markov samples use libx264 CRF 23 - confirm before delivery",
                              enc=encoder or "?"))
            video_size = (int(stream["width"]), int(stream["height"]))
            if full_screen and rect:
                exact, acceptable = _size_matches(video_size, rect[2:])
                if not acceptable:
                    error(both("成片分辨率 {vw}x{vh} 与采集区域 {w}x{h} 不一致（请检查显示器缩放/DPI 设置）",
                               "Video size {vw}x{vh} does not match the captured area {w}x{h} (check display scaling/DPI)",
                               vw=video_size[0], vh=video_size[1], w=rect[2], h=rect[3]))
                elif not exact:
                    warn(both("成片分辨率 {vw}x{vh} 为采集区域 {w}x{h} 取整到偶数后的尺寸",
                              "Video size {vw}x{vh} is the captured area {w}x{h} rounded to an even size",
                              vw=video_size[0], vh=video_size[1], w=rect[2], h=rect[3]))
        except (OSError, ValueError, KeyError, IndexError, TypeError, ZeroDivisionError, subprocess.SubprocessError):
            clip_decodes = False
            error(both("无法核验视频，请检查 FFprobe 或视频文件", "Cannot verify the video; check FFprobe or the video file"))
    recovered_session = False
    if capture.get("status") != "completed":
        # K11: the recorder holds raw/.recording.lock until stop() has written
        # everything; the OS frees it when the process dies (crash, power loss).
        recovered_session = not reclock.is_live(str(raw_dir) if raw_dir else str(root / "raw"))
        if reclock.is_live(str(raw_dir) if raw_dir else str(root / "raw")):
            error(both("录制仍在进行中（录制程序仍在写入 raw 目录），请先停止录制再打包或导出",
                       "The recording is still in progress (the recorder is still writing its raw folder); "
                       "stop it before packaging or exporting"))
        elif clip_decodes and repairs:
            # segments.json was saved while recording (K2) and the packager checked /
            # repaired the unfinished segments; the clip decodes.
            warn(both("录制异常结束后已恢复：检查并修复了 {n} 个未正常结束的录制片段，成片可以正常解码；交付前请检查视频结尾",
                      "The recording was recovered after an abnormal stop: {n} segment(s) that did not finish "
                      "normally were checked or repaired and the clip decodes; check the end of the video "
                      "before delivery", n=len(repairs)))
        elif clip_decodes:
            # e.g. a crash during a pause or between segments: every segment was saved
            warn(both("录制异常结束后已恢复：所有录制片段均已正常保存，成片可以正常解码；交付前请检查视频结尾",
                      "The recording was recovered after an abnormal stop: every segment was saved normally "
                      "and the clip decodes; check the end of the video before delivery"))
        else:
            error(both("录制未正常完成", "The recording did not finish normally"))

    # (2) Cursor outside the delivered video (e.g. on another monitor, K3): the
    # export keeps the values (video pixels), so only count and report them.
    geometry = package_geometry(root)
    bounds = None
    if "originX" in geometry:
        try:
            vw = int(geometry.get("videoWidth") or (video_size or (0, 0))[0])
            vh = int(geometry.get("videoHeight") or (video_size or (0, 0))[1])
        except (TypeError, ValueError):
            vw = vh = 0
        if vw > 0 and vh > 0:
            bounds = (geometry_transform(geometry), vw, vh)
    offscreen = 0

    # SQLite's temporary database bounds memory for long sessions and permits
    # checking that frame logs contain original discrete inputs exactly once.
    db = sqlite3.connect("")
    db.execute("CREATE TABLE raw (seq INTEGER PRIMARY KEY, kind TEXT, ts REAL, mapped INTEGER DEFAULT 0)")
    counts = {}
    marks = []
    inputs = []          # times of clicks and non-modifier keys (typed input, K4)
    # Only input that went to a blocklisted app can leak what a privacy mask
    # hides.  v0.2+ recorders gate input during every pause, so a click into
    # the target while the resumed clip is starting (it lies inside the mask's
    # end mark) is fine; input with no app attribution counts only for
    # pre-v0.2 recorders, which logged everything during a pause.
    try:
        capture_version = int(capture.get("version") or 1)
    except (TypeError, ValueError):
        capture_version = 1
    blocked_exes = {str(x).strip().lower() for x in (capture.get("privacy_blocklist") or []) if str(x).strip()}
    blocked_exes |= set(DEFAULT_PRIVACY_BLOCKLIST)
    raw_count = 0
    previous = -float("inf")
    try:
        with (root / "events.json").open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                event = json.loads(line)
                ts, seq, kind = float(event["timestamp"]), event["seq"], event["type"]
                counts[kind] = counts.get(kind, 0) + 1
                raw_count += 1
                if not math.isfinite(ts) or ts < previous:
                    error(both("原始事件时间戳无效或未按时间排序", "Raw event timestamps are invalid or out of order"))
                if not math.isfinite(ts):
                    continue      # JSON NaN / Infinity: reported above, kept out of every check
                previous = ts
                try:
                    db.execute("INSERT INTO raw(seq,kind,ts) VALUES(?,?,?)", (seq, kind, ts))
                except sqlite3.IntegrityError:
                    error(both("原始事件序号重复", "Duplicate raw event sequence numbers"))
                if kind in ("privacy_mask_start", "privacy_mask_end"):
                    marks.append((ts, kind, mask_trigger(event)))
                    if kind == "privacy_mask_start" and mask_trigger(event) != AUTO_PAUSE_TRIGGER:
                        detail = str(event.get("maskDetail") or event.get("exeName") or "").strip().lower()
                        if detail:
                            blocked_exes.add(detail)
                elif kind == "click" or (kind == "key" and _key_code(event) not in MODIFIER_VKS):
                    app = str(event.get("bundleId") or "").strip().lower()
                    inputs.append((ts, app, kind))
        if raw_count != meta.get("event_count"):
            error(both("原始事件条数与 metadata 不一致", "The raw event count does not match metadata"))
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        error(both("events.json 缺失或格式错误，须为一行一条 JSON",
                   "events.json is missing or malformed; it must be one JSON object per line"))

    frames = 0
    try:
        with (root / "frame_events.json").open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                frame = json.loads(line)
                if frame["frame"] != frames or abs(frame["video_t_ms"] - frames * dt) > 0.01:
                    error(both("帧级日志序号或时间轴不连续", "Frame log indices or timeline are not continuous"))
                events = frame["events"]
                if not events or events[0].get("source") != "frame_timestamps_sidecar":
                    error(both("帧级日志缺少逐帧光标状态", "Frame log is missing the per-frame cursor state"))
                elif bounds is not None and _cursor_outside(events[0], *bounds):
                    offscreen += 1
                for event in events:
                    t, within = float(event["video_t_ms"]), float(event["t_in_frame_ms"])
                    if not math.isfinite(t) or not math.isfinite(within) or not (-0.001 <= within < dt + 0.001):
                        error(both("事件在帧内的时间偏移无效", "An event's in-frame time offset is invalid"))
                    if abs(t - (frames * dt + within)) > 0.01:
                        error(both("事件被放入错误的视频帧", "An event was placed in the wrong video frame"))
                    if "seq" in event:
                        row = db.execute("SELECT kind,ts,mapped FROM raw WHERE seq=?", (event["seq"],)).fetchone()
                        if not row or row[0] != event["type"] or abs(row[1] - event["timestamp"]) > 0.001:
                            error(both("帧级事件无法追溯到原始日志", "A frame-log event cannot be traced to the raw log"))
                        elif row[2]:
                            error(both("同一个离散事件被重复写入多帧", "The same discrete event was written to several frames"))
                        else:
                            db.execute("UPDATE raw SET mapped=1 WHERE seq=?", (event["seq"],))
                frames += 1
        if frames != total:
            error(both("帧级日志行数与视频总帧数不一致", "Frame log line count does not match the total frame count"))
    except (OSError, ValueError, KeyError, TypeError, IndexError, sqlite3.Error):
        error(both("frame_events.json 缺失或格式错误，须为一行一帧 JSON",
                   "frame_events.json is missing or malformed; it must be one JSON object per frame"))
    finally:
        ranges = []
        try:
            for c in meta.get("clips") or []:
                start = float(c.get("source_start_precise_ms", c["source_start_ms"]))
                ranges.append((start, float(c.get("source_end_ms", start + c["frame_count"] * dt))))
        except (KeyError, TypeError, ValueError, AttributeError):
            error(both("metadata 的 clip 表格式错误", "metadata clips table is malformed"))
        # Segments skipped before the first clip (crash): their input is a gap.
        preroll_from = packager_stats.get("preroll_from_ms")
        preroll_from = float(preroll_from) if isinstance(preroll_from, (int, float)) else -math.inf
        try:
            for kind, timestamp in db.execute("SELECT kind,ts FROM raw WHERE mapped=0"):
                before_first = not ranges or (preroll_from <= timestamp < ranges[0][0])
                if kind in _DISCRETE_TYPES and (before_first or
                                               any(start <= timestamp <= end for start, end in ranges)):
                    error(both("视频有效时段内的离散输入未完整写入帧级日志",
                               "Discrete inputs inside the recorded video time are missing from the frame log"))
                    break
        finally:
            db.close()

    if offscreen:
        warn(both("有 {n} 帧（占 {pct:.1f}%）的鼠标位置在成片画面之外（例如在另一台显示器上）；"
                  "导出时这些坐标原样保留，超出视频像素范围，交付前请确认",
                  "The mouse cursor is outside the video in {n} frame(s) ({pct:.1f}% of the clip), e.g. on "
                  "another monitor; these positions are exported as they are, outside the clip's pixel area - "
                  "check before delivery", n=offscreen, pct=100.0 * offscreen / max(1, frames)))

    # Privacy masks (contract K4): app-only auto-pauses always cut the time;
    # any other mask (password-manager blocklist) must have been cut as well,
    # older recorders only marked it.  Judged on the clip table's nominal frame
    # times; without a usable clip table every such mask counts as uncut.  A
    # click or non-modifier key logged inside the mask is uncut too: v0.1
    # recorders kept logging keys during a pause, and events.json ships them.
    timeline = clip_timeline(meta.get("clips"), dt, total=total)
    if not timeline:
        if total > 0:
            error(both("metadata 的 clip 表格式错误", "metadata clips table is malformed"))
        timeline = None
    raw_inputs = inputs
    inputs = sorted(ts for ts, app, _ in raw_inputs
                    if app in blocked_exes or (not app and capture_version < 2))
    policy = [m for m in privacy_intervals(marks) if m[2] != AUTO_PAUSE_TRIGGER]
    uncut = [m for m in policy if timeline is None or not mask_was_cut(m[0], m[1], timeline, dt)
             or input_inside(m[0], m[1], inputs)]
    # Input that leaks without any privacy mark: keys typed INTO a password
    # manager (any recorder version; v0.2.1 never logs them), clicks into one
    # from pre-v0.2 recorders, and - pre-v0.2 only, which kept logging during a
    # manual pause - any key or click typed while paused (between clips).
    leaked = [ts for ts, app, kind in raw_inputs
              if app in blocked_exes and (kind == "key" or capture_version < 2)]
    if capture_version < 2 and timeline:
        spans = [(start, start + count * dt) for start, count in timeline if count > 0]
        for ts, _, _ in raw_inputs:
            if spans and spans[0][0] < ts < spans[-1][1] and not any(a <= ts <= b for a, b in spans):
                leaked.append(ts)
    # input inside an interval already reported as uncut is not counted twice
    leaked = sorted({ts for ts in leaked if not any(m[0] < ts < m[1] for m in uncut)})
    if leaked:
        missing.append(both("键鼠日志中有 {n} 条输入发往密码管理器或在暂停期间录入（旧版录制），可能包含密码，须人工处理后重新核验",
                            "The input log has {n} input event(s) sent to a password manager or typed while paused "
                            "(older recordings) that may contain passwords; handle them manually and re-validate",
                            n=len(leaked)))
    if uncut:
        missing.append(both("包含 {n} 段未剪除的隐私标记时段（旧版录制只做了标记，视频或键鼠日志中仍有这段时间的内容），"
                            "须人工处理后重新核验",
                            "Contains {n} privacy-marked interval(s) still in the video or the input log (older "
                            "recordings only marked them); handle them manually and re-validate", n=len(uncut)))
    elif policy:
        warn(both("隐私名单中的应用（如密码管理器）在前台 {n} 次，相应时段已从视频中剪去",
                  "A blocklisted app (e.g. a password manager) was in front {n} time(s); that time was cut from the video",
                  n=len(policy)))

    health = capture.get("input_health") if isinstance(capture.get("input_health"), dict) else {}
    bad_raw = packager_stats.get("invalid_raw_lines") or 0
    if bad_raw and recovered_session and packager_stats.get("invalid_raw_tail_lines") == bad_raw:
        # power loss: the unsynced end of the log is torn / NUL-filled; everything before it is intact
        warn(both("异常结束截断了键鼠日志的最后 {n} 行（已跳过）；之前的事件完整",
                  "The abnormal stop cut off the last {n} line(s) of the input log (skipped); "
                  "everything before them is intact", n=bad_raw))
    elif bad_raw:
        error(both("原始事件日志有 {n} 行无法读取（日志损坏）", "The raw event log has {n} unreadable line(s) (log corrupted)",
                   n=bad_raw))
    if any(health.get(k, 0) for k in ("dropped_events", "write_errors", "callback_errors", "raw_mouse_errors")) or health.get("hook_error"):
        error(both("录制时存在键鼠监听错误或日志丢失", "Keyboard/mouse capture errors or lost log entries during recording"))
    if not health.get("raw_mouse_available"):
        missing.append(both("缺少原始鼠标输入采集验证，旧录制无法补造原始输入",
                            "Raw mouse input capture is not verified; older recordings cannot recreate raw input"))
    for kind in ("mouse_move", "click", "key"):
        if not counts.get(kind):
            missing.append(both("本次录制未包含 {kind}，需确认已完整录制 CAD 操作",
                                "This recording has no {kind} events; confirm the CAD work was fully recorded", kind=kind))
    software = meta.get("software") if isinstance(meta.get("software"), dict) else {}
    for key, label in (("name", both("软件名称", "Software name")), ("version", both("软件版本", "Software version")),
                       ("license_reference", both("正版许可材料", "Software license evidence")),
                       ("interface_language", both("软件界面语言", "Software interface language"))):
        if not str(software.get(key) or "").strip():
            missing.append(label)
    for key, label in (("engineer_id", both("工程师编号", "Engineer ID")),
                       ("part_id", both("原创零件或案例编号", "Original part or case ID")),
                       ("authorization_reference", both("工程师版权授权材料", "Engineer copyright authorization"))):
        if not str(meta.get(key) or "").strip():
            missing.append(label)
    review = meta.get("review") if isinstance(meta.get("review"), dict) else {}
    for key, label in (("originality_confirmed", both("案例原创核验", "Originality check")),
                       ("task_completed", both("任务已完成确认", "Task completion confirmed")),
                       ("sync_verified", both("画面与输入同步实测确认", "Video/input sync verified by test")),
                       ("quality_reviewed", both("成片质量人工复核", "Manual video quality review"))):
        if review.get(key) is not True:
            missing.append(label)

    narration_text = both("narration.json 缺少已审核的操作描述与原因",
                          "narration.json lacks reviewed action descriptions and reasons")
    narration = read("narration.json", required=False)
    if narration is None:
        narration = {}
        missing.append(narration_text)
    last = 0.0
    covered = 0.0
    annotations = narration.get("annotations") or []
    if not isinstance(annotations, list):
        error(both("narration.json 的 annotations 必须是列表", "narration.json annotations must be a list"))
        annotations = []
    if not str(narration.get("summary") or "").strip() or not annotations:
        missing.append(narration_text)
    for annotation in annotations:
        try:
            start, end = float(annotation["start_s"]), float(annotation["end_s"])
            if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration / 1000 + 0.001 and start >= last - 0.001):
                raise ValueError()
            if not all(str(annotation.get(k) or "").strip() for k in ("action", "intent", "app")) or not isinstance(annotation.get("details"), str):
                raise ValueError()
            covered += end - start
            last = end
        except (KeyError, ValueError, TypeError, AttributeError):
            missing.append(both("narration.json 存在空描述、非法区间或重叠",
                                "narration.json has empty descriptions, invalid ranges or overlaps"))
            break
    if annotations and covered < duration / 1000 * 0.95:
        missing.append(both("操作描述覆盖不足 95%，需人工复核", "Annotations cover less than 95% of the video; review manually"))
    if not decode:
        warn(both("本次核验视频头部帧数；正式导出另做全片解码检查",
                  "This check read the frame count from the video header; exports also decode the full video"))
    task_root = task_directory(root, capture, task_set)
    if markov:
        missing.extend(validate_task(task_root))
    for name in (() if markov else TASK_FILES):
        path = task_root / name
        if not path.is_file() or not path.stat().st_size:
            missing.append(both("任务集缺少 {name}", "Task set is missing {name}", name=name))
            continue
        try:
            if name.endswith(".json"):
                if not json.loads(path.read_text(encoding="utf-8-sig")):
                    raise ValueError()
            elif name.endswith(".zip"):
                with zipfile.ZipFile(path) as archive:
                    names = [n.filename for n in archive.infolist() if not n.is_dir() and n.file_size > 0]
                    if not names or archive.testzip() is not None:
                        raise ValueError()
                    if name == "gold.zip":
                        extensions = {Path(n).suffix.lower() for n in names}
                        if not extensions & MODEL_EXTENSIONS or not extensions & {".step", ".stp"}:
                            missing.append(both("gold.zip 需要CAD原生文件和 STEP 文件；二维任务如需豁免须先与客户确认",
                                                "gold.zip needs native CAD files and a STEP file; 2D tasks need a customer-approved exemption"))
            elif not path.read_bytes()[:5] == b"%PDF-":
                raise ValueError()
        except (ValueError, OSError, RuntimeError, zipfile.BadZipFile):
            missing.append(both("任务集文件损坏或为空：{name}", "Task set file is corrupt or empty: {name}", name=name))
    for reference in (software.get("license_reference"), meta.get("authorization_reference")):
        if reference:
            path = Path(str(reference))
            if not path.is_absolute():
                path = root / path
            if not path.is_file():
                missing.append(both("许可或授权证明文件不存在：{ref}", "License or authorization file not found: {ref}",
                                    ref=reference))
    warn(both("同步字段校验不等于物理同步精度认证，仍需点击画面与日志实测",
              "Sync field checks are not a physical sync-accuracy certification; still test clicks against the video"))
    unique_missing = []
    for item in missing:
        if item not in unique_missing:
            unique_missing.append(item)
    return {"technical_ok": not errors, "ready_for_delivery": not errors and not unique_missing,
            "technical_errors": errors, "delivery_missing": unique_missing,
            "warnings": warnings, "checked_frames": frames, "checked_events": raw_count,
            "decode_verified": decode, "capture_mode": mode, "full_screen": full_screen,
            "privacy_masks": len(policy),
            # export_recording / the GUI refuse when this is > 0: uncut intervals, plus 1 for leaked input
            "uncut_privacy_masks": len(uncut) + (1 if leaked else 0),
            "privacy_input_events": len(leaked),
            "offscreen_cursor_frames": offscreen,
            "language": get_language()}


def main(argv=None):
    safe_stdio()   # the report holds Chinese text; the installed app's pipes may be cp1252
    ap = argparse.ArgumentParser(description=L("检查 CAD 剪辑集交付条件", "Check CAD clip-set delivery conditions"))
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--decode", action="store_true", help=L("解码全片核对帧数", "decode the full video to count frames"))
    ap.add_argument("--task-set")
    ap.add_argument("--markov", action="store_true", help=L("核对 Markov CAD 样例的任务清单",
                                                            "check the task set like the Markov CAD sample"))
    args = ap.parse_args(argv)
    report = validate_dataset(args.dataset, args.decode, args.task_set, markov=args.markov)
    (Path(args.dataset) / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready_for_delivery"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
