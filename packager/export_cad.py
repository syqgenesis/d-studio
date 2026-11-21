"""Export a recording in Markov's clip-set format, with fail-closed validation.

* :func:`export_cad` - reviewed CAD task set + clip set (``--markov``: the 10
  files of the Markov CAD sample; otherwise the legacy task_set/clip_set layout).
* :func:`export_recording` - just the Markov clip set (clip.mp4, events.json,
  frame_events.json, metadata.json, narration.json) for when the narration or
  task rubric is not ready yet.  Only the technical checks must pass.

Both write into a temporary folder next to the destination and rename it into
place, so a failed export never leaves a half-written delivery behind, and
neither ever overwrites an existing folder.

Coordinates: the local dataset keeps the hook's virtual-screen pixels (all
monitors share one space; a monitor left of / above the primary has negative
coordinates) and maps them through ``view/geometry.json``.  The delivered
Markov clip set uses *video* pixels instead - (0, 0) is the top-left pixel of
clip.mp4 - for every mouse_move / click / drag / scroll (raw and per-frame
synthetic) and window_moved / window_resized event, like Markov's single-display
``screen_px_top_left``.  ``screen_config`` keeps the monitor layout as is.
A position outside the captured area (e.g. the cursor on another monitor while
only the primary one is recorded) is kept as is - outside [0, width) x
[0, height) of clip.mp4, never clamped or flagged per event; validation warns
with the number of such frames (``offscreen_cursor_frames``).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from recorder.i18n import L, safe_stdio

from .build import dumps, stream_jsonl, to_video_px, video_transform
from .markov_task import BUNDLES, TASK_FILES, task_directory, write_bundle
from .validate import message_text, validate_dataset


CLIP_FILES = ("clip.mp4", "events.json", "frame_events.json", "narration.json", "metadata.json")
MARKOV_METADATA = ("platform", "total_duration_ms", "total_frames", "event_count", "fps")
EMPTY_NARRATION = {"summary": "", "annotations": []}
#: Fields of our local logs that Markov's clip set does not carry.
LOCAL_EVENT_FIELDS = ("seq", "osTime")
#: Events whose x/y are screen positions (translated to video pixels on export).
POSITION_EVENTS = frozenset({"mouse_move", "click", "drag", "scroll", "window_moved", "window_resized"})


# --------------------------------------------------------------------- helpers
def _paths(dataset, destination):
    source, target = Path(dataset).resolve(), Path(destination).resolve()
    if not source.is_dir():
        raise ValueError(L("找不到录制目录：{path}", "Recording folder not found: {path}", path=source))
    if target == source or target in source.parents:
        raise ValueError(L("导出目录不能是录制目录本身或其上级目录",
                           "The export folder cannot be the recording folder or one of its parents"))
    if target.exists():
        raise ValueError(L("导出目录已存在，请选择新的目录，避免覆盖已有交付：{path}",
                           "The export folder already exists; choose a new one so nothing is overwritten: {path}",
                           path=target))
    return source, target


def _write_validation(source, report):
    (source / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _publish(package, target, attempts=25):
    """Rename the finished temp folder into place without overwriting anything."""
    for attempt in range(attempts):
        if target.exists():
            raise ValueError(L("导出目录已存在，请选择新的目录，避免覆盖已有交付：{path}",
                               "The export folder already exists; choose a new one so nothing is overwritten: {path}",
                               path=target))
        try:
            package.rename(target)
            return
        except PermissionError:
            # Windows: antivirus/indexer briefly holding a freshly written file
            if attempt == attempts - 1:
                raise
            time.sleep(0.2)


def _staged(target, writer, prefix):
    """Run ``writer(package_dir)`` in a temp folder beside ``target``, then publish it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=prefix, dir=target.parent) as temporary:
        package = Path(temporary) / "package"
        package.mkdir()
        writer(package)
        _publish(package, target)
    return str(target)


def _strip_local_fields(event):
    for key in LOCAL_EVENT_FIELDS:
        event.pop(key, None)
    return event


def _to_video_px(event, transform):
    """Translate (and scale) one event's screen position into video pixels, in place."""
    ox, oy, sx, sy = transform
    if event.get("type") not in POSITION_EVENTS or (ox, oy, sx, sy) == (0.0, 0.0, 1.0, 1.0):
        return event
    for key, origin, scale in (("x", ox, sx), ("y", oy, sy)):
        value = event.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            event[key] = to_video_px(value, origin, scale)
    if event.get("type") == "window_resized" or event.get("type") == "window_moved":
        for key, scale in (("width", sx), ("height", sy)):
            value = event.get(key)
            if scale != 1.0 and isinstance(value, (int, float)) and not isinstance(value, bool):
                event[key] = int(round(value * scale))
    return event


def _write_markov_clip(source, package, *, category="cad", narration=None):
    """The 5 clip-set files exactly as Markov ships them.

    * clip.mp4 copied as-is;
    * metadata.json reduced to the sample's 6 fields;
    * events.json / frame_events.json as JSONL without our local seq/osTime,
      with screen positions in video pixels (see the module docstring);
    * narration.json copied, or ``narration`` written when given.
    """
    transform = video_transform(source)
    shutil.copyfile(source / "clip.mp4", package / "clip.mp4")
    if narration is None:
        shutil.copyfile(source / "narration.json", package / "narration.json")
    else:
        (package / "narration.json").write_text(json.dumps(narration, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8", newline="\n")
    metadata = json.loads((source / "metadata.json").read_text(encoding="utf-8-sig"))
    result = {"category": category, **{key: metadata[key] for key in MARKOV_METADATA}}
    (package / "metadata.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name in ("events.json", "frame_events.json"):
        with (package / name).open("w", encoding="utf-8", newline="\n") as output:
            for row in stream_jsonl(source / name):
                for event in row["events"] if name == "frame_events.json" else (row,):
                    _to_video_px(_strip_local_fields(event), transform)
                output.write(dumps(row) + "\n")


# ---------------------------------------------------------------- CAD delivery
def export_cad(dataset, destination, task_set=None, *, markov=False):
    source, target = Path(dataset).resolve(), Path(destination).resolve()
    if target == source or source in target.parents or target in source.parents:
        raise ValueError(L("交付目录必须在录制目录之外", "The delivery folder must be outside the recording folder"))
    if target.exists():
        raise ValueError(L("交付目录已存在，请选择新的目录，避免覆盖已有交付",
                           "The delivery folder already exists; choose a new one so nothing is overwritten"))
    report = validate_dataset(source, decode=True, task_set=task_set, markov=markov)
    _write_validation(source, report)
    if not report["ready_for_delivery"]:
        raise ValueError(L("未达到交付条件：", "Delivery conditions are not met:") + "\n"
                         + "\n".join(message_text(m) for m in report["technical_errors"] + report["delivery_missing"]))
    capture = json.loads((source / "capture.json").read_text(encoding="utf-8-sig"))
    task = task_directory(source, capture, task_set)
    if markov:
        return _staged(target, lambda package: _write_markov(source, task, package), ".cad-export-")
    return _staged(target, lambda package: _write_legacy(source, task, capture, package), ".cad-export-")


def _write_markov(source, task, package):
    package.mkdir(exist_ok=True)
    for name in TASK_FILES:
        if name in BUNDLES and not (task / name).is_file():
            write_bundle(task, name, package / name)
        else:
            shutil.copyfile(task / name, package / name)
    _write_markov_clip(source, package, category="cad")


def _write_legacy(source, task, capture, package):
    clip = package / "clip_set" / source.name
    tasks = package / "task_set"
    clip.mkdir(parents=True)
    tasks.mkdir()
    for name in TASK_FILES:
        shutil.copyfile(task / name, tasks / name)
    for name in (*CLIP_FILES, "validation.json"):
        shutil.copyfile(source / name, clip / name)
    metadata = json.loads((clip / "metadata.json").read_text(encoding="utf-8"))
    evidence = package / "evidence"
    evidence.mkdir()
    for label, reference in (("license", metadata["software"]["license_reference"]),
                             ("authorization", metadata["authorization_reference"])):
        original = Path(reference)
        if not original.is_absolute():
            original = source / original
        name = label + original.suffix
        shutil.copyfile(original, evidence / name)
        relative = Path(os.path.relpath(evidence / name, clip)).as_posix()
        if label == "license":
            metadata["software"]["license_reference"] = relative
        else:
            metadata["authorization_reference"] = relative
    (clip / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    exported_capture = dict(capture)
    exported_capture["session_info"] = dict(capture.get("session_info") or {})
    exported_capture["session_info"]["task_set"] = "../../task_set"
    (clip / "capture.json").write_text(json.dumps(exported_capture, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {"part_id": metadata["part_id"], "engineer_id": metadata["engineer_id"],
                "workflow_id": metadata["workflow_id"], "files": []}
    for path in sorted(package.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as fh:
                for block in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(block)
            manifest["files"].append({"path": path.relative_to(package).as_posix(),
                                       "bytes": path.stat().st_size, "sha256": digest.hexdigest()})
    (package / "delivery_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------- recording only
def export_recording(dataset, destination, *, decode=True):
    """Write the Markov clip set (5 files) for ``dataset`` into ``destination``.

    No task set, session info or narration is required - only the technical
    checks (media vs. logs) must pass, and no privacy mask may still be in the
    video (old recordings that only marked a blocklisted app are refused).
    ``narration.json`` is copied when the dataset has one, else
    ``{"summary": "", "annotations": []}`` is written.
    Refuses to overwrite; raises :class:`ValueError` with a message in the
    active UI language.  Returns the destination path.
    """
    source, target = _paths(dataset, destination)
    report = validate_dataset(source, decode=decode)
    _write_validation(source, report)
    if not report["technical_ok"]:
        raise ValueError(L("技术检查未通过，不能导出录制：", "Technical checks failed; the recording cannot be exported:")
                         + "\n" + "\n".join(message_text(m) for m in report["technical_errors"]))
    if report.get("uncut_privacy_masks"):
        # Older recorders only *marked* a password manager in front; its window
        # and keystrokes are still in the video and logs (contract K4).
        raise ValueError(L("录制中有 {n} 段隐私标记时段（如密码管理器在前台）仍留在视频和日志中，不能导出；"
                           "请人工剪除后重新打包，或重新录制",
                           "The recording has {n} privacy-marked interval(s) (e.g. a password manager in front) that "
                           "are still in the video and logs, so it cannot be exported; cut them manually and repackage, "
                           "or record again", n=report["uncut_privacy_masks"]))
    metadata = json.loads((source / "metadata.json").read_text(encoding="utf-8-sig"))
    category = str(metadata.get("category") or "").strip() or "cad"
    narration = None if (source / "narration.json").is_file() else dict(EMPTY_NARRATION)
    return _staged(target, lambda package: _write_markov_clip(source, package, category=category,
                                                              narration=narration), ".rec-export-")


# ------------------------------------------------------------------------- CLI
def main(argv=None):
    safe_stdio()   # bilingual messages on a cp1252 pipe (installed app runs python -E)
    ap = argparse.ArgumentParser(description=L("核验并导出 CAD 交付包或 Markov 格式录制",
                                               "Validate and export a CAD delivery or a Markov-format recording"))
    ap.add_argument("--dataset", required=True, help=L("录制目录（data/rec-…）", "recording folder (data/rec-…)"))
    ap.add_argument("--out", required=True, help=L("新的导出目录（不能已存在）", "new export folder (must not exist)"))
    ap.add_argument("--task-set", help=L("任务集目录", "task set folder"))
    ap.add_argument("--markov", action="store_true", help=L("按 Markov CAD 样例导出 10 个文件",
                                                            "export the 10 files of the Markov CAD sample"))
    ap.add_argument("--recording-only", action="store_true",
                    help=L("只导出 Markov 剪辑集 5 个文件（无需任务集与标注）",
                           "export only the 5 Markov clip-set files (no task set or narration needed)"))
    args = ap.parse_args(argv)
    try:
        if args.recording_only:
            print(export_recording(args.dataset, args.out))
        else:
            print(export_cad(args.dataset, args.out, args.task_set, markov=args.markov))
        return 0
    except (ValueError, OSError) as exc:
        print(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
