"""Validate the manifest and the two file bundles in a Markov CAD task set."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import zipfile

from recorder.i18n import both, get_language


class TaskError(ValueError):
    """A task-set problem carrying both languages (``message`` is a both() dict)."""

    def __init__(self, message):
        self.message = message
        super().__init__(message["en"])

    def __str__(self):
        return self.message.get(get_language()) or self.message["en"]


def _texts(error):
    """(zh, en) renderings of an exception raised while checking the task set."""
    if isinstance(error, TaskError):
        return error.message["zh"], error.message["en"]
    return str(error), str(error)


def _issue(zh, en, error=None, **fmt):
    """both() message whose ``{err}`` is the matching language of ``error``."""
    if error is None:
        return both(zh, en, **fmt)
    err_zh, err_en = _texts(error)
    return {"zh": both(zh, zh, err=err_zh, **fmt)["zh"], "en": both(en, en, err=err_en, **fmt)["en"]}


TASK_FILES = ("task.pdf", "task_desc.json", "rubrics.json", "references.zip", "gold.zip")
MODEL_EXTENSIONS = {
    ".dwg", ".dxf", ".sldprt", ".sldasm", ".slddrw", ".f3d", ".f3z", ".catpart",
    ".catproduct", ".catdrawing", ".prt", ".asm", ".drw",
}
BUNDLES = {
    "references.zip": ("task_files", "input_files"),
    "gold.zip": ("output_files", "deliverables"),
}


def task_directory(dataset, capture, override=None):
    path = override or (capture.get("session_info") or {}).get("task_set") or "task_set"
    return (Path(dataset) / path).resolve()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _bundle_path(value, prefix):
    if not isinstance(value, str) or "\\" in value or ":" in value:
        raise TaskError(both("路径须使用 / 分隔：{path}", "Paths must use / separators: {path}", path=value))
    parts = value.split("/")
    if len(parts) < 2 or parts[0] != prefix or any(p in ("", ".", "..") for p in parts):
        raise TaskError(both("路径须位于 {prefix}/ 下：{path}", "Path must be under {prefix}/: {path}",
                           prefix=prefix, path=value))
    return value


def _check_unique(paths):
    if len({path.casefold() for path in paths}) != len(paths):
        raise TaskError(both("文件路径重复（包含只有大小写不同的路径）",
                           "Duplicate file paths (including paths that differ only in case)"))


def _input_files(task_root, prefix):
    folder = task_root / prefix
    if not folder.is_dir():
        raise TaskError(both("找不到 {prefix}/ 目录", "Folder {prefix}/ not found", prefix=prefix))
    if folder.resolve() != folder:
        raise TaskError(both("{prefix}/ 不能指向其他目录", "{prefix}/ must not link to another folder", prefix=prefix))
    for path in sorted(folder.rglob("*")):
        if path.resolve() != path:
            raise TaskError(both("{prefix}/ 中含有链接：{name}", "{prefix}/ contains a link: {name}",
                               prefix=prefix, name=path.name))
        if path.is_file():
            if not path.stat().st_size:
                raise TaskError(both("文件为空：{name}", "Empty file: {name}", name=path.name))
            yield path.relative_to(task_root).as_posix(), path


def _bundle_members(task_root, archive_name, prefix):
    archive_path = task_root / archive_name
    if not archive_path.exists():
        return [name for name, _ in _input_files(task_root, prefix)]
    with zipfile.ZipFile(archive_path) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if not members or any(not item.file_size for item in members):
            raise TaskError(both("压缩包为空或包含空文件", "The archive is empty or contains empty files"))
        names = [_bundle_path(item.filename, prefix) for item in members]
        _check_unique(names)
        bad = archive.testzip()
        if bad is not None:
            raise TaskError(both("CRC 校验失败：{name}", "CRC check failed: {name}", name=bad))
        return names


def _check_manifest(task_root):
    spec = json.loads((task_root / "task_desc.json").read_text(encoding="utf-8-sig"))
    if not isinstance(spec, dict):
        raise TaskError(both("顶层必须是 JSON 对象", "The top level must be a JSON object"))
    if not all(_text(spec.get(k)) for k in ("task", "domain", "application", "description")):
        raise TaskError(both("请填写 task、domain、application 和 description",
                           "Fill in task, domain, application and description"))
    if spec["domain"].strip().upper() != "CAD":
        raise TaskError(both("domain 应填写 CAD", "domain must be CAD"))
    if not isinstance(spec.get("input_files"), list) or not spec["input_files"]:
        raise TaskError(both("input_files 必须列出参考文件路径", "input_files must list the reference file paths"))
    if not isinstance(spec.get("deliverables"), list) or not spec["deliverables"]:
        raise TaskError(both("deliverables 必须列出要求交付的文件", "deliverables must list the required output files"))
    inputs = [_bundle_path(item, "task_files") for item in spec["input_files"]]
    outputs = []
    for item in spec["deliverables"]:
        if not isinstance(item, dict) or not all(_text(item.get(k)) for k in ("path", "type", "format", "description")):
            raise TaskError(both("每项 deliverables 需要 path、type、format 和 description",
                               "Every deliverable needs path, type, format and description"))
        path = _bundle_path(item["path"], "output_files")
        if PurePosixPath(path).suffix.casefold() != item["format"].strip().casefold():
            raise TaskError(both("format 与文件扩展名不一致：{path}", "format does not match the file extension: {path}",
                               path=path))
        outputs.append(path)
    _check_unique(inputs)
    _check_unique(outputs)
    return {"input_files": inputs, "deliverables": outputs}


def _check_rubrics(task_root):
    data = json.loads((task_root / "rubrics.json").read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or not _text(data.get("schema_version")):
        raise TaskError(both("请填写 schema_version", "Fill in schema_version"))
    rubrics = data.get("rubrics")
    if not isinstance(rubrics, list) or not rubrics:
        raise TaskError(both("rubrics 至少需要一条验收标准", "rubrics needs at least one acceptance criterion"))
    if any(not isinstance(item, dict) or not _text(item.get("id")) or not _text(item.get("requirement"))
           for item in rubrics):
        raise TaskError(both("每条验收标准需要 id 和 requirement", "Every rubric needs an id and a requirement"))
    if len({item["id"].strip() for item in rubrics}) != len(rubrics):
        raise TaskError(both("rubrics 的 id 重复", "Duplicate rubric ids"))


def validate_task(task_root):
    """Problems in a Markov task set, as both() messages (empty list = complete)."""
    issues = []
    try:
        with (task_root / "task.pdf").open("rb") as pdf:
            if pdf.read(5) != b"%PDF-":
                raise TaskError(both("文件不是 PDF", "not a PDF file"))
    except (OSError, ValueError) as error:
        issues.append(_issue("任务集 task.pdf 缺失或无效：{err}", "Task set task.pdf is missing or invalid: {err}", error))
    manifest = {}
    for name, check in (("task_desc.json", _check_manifest), ("rubrics.json", _check_rubrics)):
        try:
            result = check(task_root)
            if result is not None:
                manifest = result
        except (OSError, ValueError) as error:
            issues.append(_issue("任务集 {name} 缺失或无效：{err}", "Task set {name} is missing or invalid: {err}",
                                 error, name=name))
    for archive, (prefix, field) in BUNDLES.items():
        try:
            members = _bundle_members(task_root, archive, prefix)
            _check_unique(members)
            if not members:
                raise TaskError(both("没有文件", "no files"))
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
            issues.append(_issue("任务集 {name} 无法读取：{err}", "Task set {name} cannot be read: {err}",
                                 error, name=archive))
            continue
        if field in manifest:
            for difference, zh, en in ((set(manifest[field]) - set(members),
                                        "{archive} 缺少清单中的文件：{path}", "{archive} is missing a listed file: {path}"),
                                       (set(members) - set(manifest[field]),
                                        "{archive} 包含清单外文件：{path}", "{archive} contains an unlisted file: {path}")):
                issues.extend(both(zh, en, archive=archive, path=path) for path in sorted(difference))
        if archive == "gold.zip":
            extensions = {PurePosixPath(name).suffix.casefold() for name in members}
            if not (extensions & MODEL_EXTENSIONS and extensions & {".step", ".stp"}):
                issues.append(both("gold.zip 需要 CAD 原生工程文件和 STEP 文件",
                                   "gold.zip needs native CAD project files and a STEP file"))
    return issues


def write_bundle(task_root, archive_name, destination):
    """Pack expanded task/output folders when no prepared ZIP was supplied."""
    prefix, _ = BUNDLES[archive_name]
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in _input_files(task_root, prefix):
            archive.write(path, name)
