"""CAD session information, supplied by the operator rather than fabricated.

``edit_session(parent, path, on_saved, markov=True)`` opens a small dark form
(bilingual, follows the 中/EN switch live) that edits one ``session_info``
JSON file: the template used for new recordings, or the ``session_info.json``
of the recording that just finished.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict

from . import i18n
from .i18n import L

# key, 中文, English[, "file" | "dir"]
FIELDS = [
    ("engineer_id", "工程师编号", "Engineer ID"),
    ("part_id", "原创案例／零件编号", "Original case / part ID"),
    ("software_name", "CAD 软件名称", "CAD software"),
    ("software_version", "软件版本", "Software version"),
    ("interface_language", "软件界面语言 zh / en", "Software UI language (zh / en)"),
    ("license_reference", "正版许可证明文件", "Software licence proof", "file"),
    ("authorization_reference", "工程师授权文件", "Engineer authorisation", "file"),
    ("task_set", "任务集目录", "Task set folder", "dir"),
]
CHECKS = [
    ("originality_confirmed", "已核验为原创案例，非公开样例或受限图纸",
     "Verified as an original case, not a public sample or restricted drawing"),
    ("task_completed", "本次 CAD 任务已完成，成品文件已保存",
     "The CAD task in this recording is finished and its output files are saved"),
    ("sync_verified", "已实测检查画面与键鼠日志同步",
     "Checked that the video and the keyboard/mouse log are in sync"),
    ("quality_reviewed", "已回看成片，确认无长时间空窗或中断任务",
     "Reviewed the clip: no long idle gaps and no abandoned task"),
]


def field_label(item) -> str:
    return L(item[1], item[2])


def hint_text(markov: bool = True) -> str:
    """Form hint; ``markov`` = Markov CAD export (default) vs. the legacy layout (⚙ → DELIVERY)."""
    text = L("缺项可先试录。录制后请核验当前录像，再生成交付包。",
             "You can make a test recording with fields missing. After recording, review that recording "
             "here before building the delivery.")
    if markov:
        text += "\n" + L("任务集目录需有 task.pdf、task_desc.json 和 rubrics.json。"
                         "同一目录可放 references.zip 和 gold.zip，也可放 task_files/ 和 output_files/。",
                         "The task set folder needs task.pdf, task_desc.json and rubrics.json. It may also "
                         "contain references.zip and gold.zip, or task_files/ and output_files/.")
    else:
        text += "\n" + L("旧版交付格式：任务集目录需有 task.pdf、task_desc.json、rubrics.json、"
                         "references.zip 和 gold.zip（不会自动生成 ZIP）。",
                         "Legacy delivery format: the task set folder needs task.pdf, task_desc.json, "
                         "rubrics.json, references.zip and gold.zip (ZIPs are not built for you).")
    return text


def scope_text(path: str) -> str:
    folder = os.path.basename(os.path.dirname(os.path.abspath(path)))
    if folder.lower().startswith("rec-"):
        return L("当前录制：{name}", "This recording: {name}", name=folder)
    return L("默认信息（用于之后的新录制）", "Defaults (used for new recordings)")


def load_info(path: str) -> Dict[str, Any]:
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {}


def save_info(path: str, info: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """Merge form ``values`` into ``info`` (unknown keys kept) and write atomically."""
    result = dict(info)
    result.update(values)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    # Windows: a reader (antivirus, the annotator) holding the old file makes
    # os.replace fail for a moment -- retry briefly.
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)
    return result


def edit_session(parent, path: str, on_saved: Callable[[Dict[str, Any]], None], *, markov: bool = True):
    import tkinter as tk
    from tkinter import filedialog, messagebox
    from tkinter import font as tkfont

    from .gui import (ACCENT, FIELD, LINE, MUTED, PANEL, TEXT, CheckBox, FlatButton,
                      exclude_from_capture)

    info = load_info(path)
    try:
        scale = max(1.0, float(parent.winfo_fpixels("1i")) / 96.0)
    except Exception:
        scale = 1.0

    def s(n: float) -> int:
        return int(round(n * scale))

    base = tkfont.nametofont("TkDefaultFont")
    font = base.copy()
    bold = base.copy()
    bold.configure(weight="bold")

    window = tk.Toplevel(parent)
    window.configure(bg=PANEL)
    window.attributes("-topmost", True)
    window.resizable(False, False)

    texts = []  # (widget, callable) re-rendered when the language flips

    def tr(widget, fn):
        texts.append((widget, fn))
        widget.configure(text=fn())
        return widget

    head = tk.Frame(window, bg=PANEL)
    head.grid(row=0, column=0, columnspan=3, sticky="we", padx=s(14), pady=(s(12), s(4)))
    tr(tk.Label(head, bg=PANEL, fg=TEXT, font=bold, anchor="w"), lambda: L("CAD 交付信息", "CAD delivery info")).pack(fill="x")
    tr(tk.Label(head, bg=PANEL, fg=ACCENT, font=font, anchor="w"), lambda: scope_text(path)).pack(fill="x")
    tr(tk.Label(head, bg=PANEL, fg=MUTED, font=font, anchor="w", justify="left", wraplength=s(560)),
       lambda: hint_text(markov)).pack(fill="x", pady=(s(4), 0))

    entries: Dict[str, Any] = {}
    row = 1
    for item in FIELDS:
        key = item[0]
        tr(tk.Label(window, bg=PANEL, fg=MUTED, font=font, anchor="e"), lambda it=item: field_label(it)).grid(
            row=row, column=0, padx=(s(14), s(8)), pady=s(3), sticky="e")
        variable = tk.StringVar(value=str(info.get(key) or ""))
        entries[key] = variable
        entry = tk.Entry(window, width=46, textvariable=variable, bg=FIELD, fg=TEXT, relief="flat",
                         insertbackground=TEXT, highlightthickness=1, highlightbackground=LINE,
                         highlightcolor=ACCENT, font=font, bd=s(3))
        entry.grid(row=row, column=1, padx=0, pady=s(3), sticky="we")
        if len(item) == 4:
            def choose(v=variable, mode=item[3]):
                current = v.get().strip()
                start = current if os.path.isdir(current) else (os.path.dirname(current) if current else "")
                if mode == "dir":
                    selected = filedialog.askdirectory(parent=window, initialdir=start or None)
                else:
                    selected = filedialog.askopenfilename(parent=window, initialdir=start or None)
                if selected:
                    v.set(os.path.normpath(selected))
            btn = FlatButton(window, "", choose, font=font, padx=s(9), pady=s(2))
            tr(btn, lambda: L("选择…", "Browse…"))
            btn.grid(row=row, column=2, padx=(s(6), s(14)), pady=s(3), sticky="w")
        row += 1

    checks = tk.Frame(window, bg=PANEL)
    checks.grid(row=row, column=0, columnspan=3, sticky="w", padx=s(14), pady=(s(8), 0))
    check_boxes = {}
    for key, zh, en in CHECKS:
        box = CheckBox(checks, (lambda z=zh, e=en: L(z, e)), info.get(key) is True, font=font)
        box.pack(anchor="w", pady=s(2))
        check_boxes[key] = box
    row += 1

    status = tk.Label(window, bg=PANEL, fg=MUTED, font=font, anchor="w")
    status.grid(row=row, column=0, columnspan=3, sticky="we", padx=s(14))
    row += 1

    buttons = tk.Frame(window, bg=PANEL)
    buttons.grid(row=row, column=0, columnspan=3, sticky="e", padx=s(14), pady=(s(8), s(14)))

    def close():
        i18n.off_change(on_lang)
        try:
            window.destroy()
        except Exception:
            pass

    def save():
        values: Dict[str, Any] = {key: var.get().strip() for key, var in entries.items()}
        values.update({key: box.get() for key, box in check_boxes.items()})
        try:
            result = save_info(path, info, values)
        except (OSError, ValueError) as exc:
            messagebox.showerror(L("保存失败", "Save failed"), str(exc), parent=window)
            return
        close()
        on_saved(result)

    save_btn = FlatButton(buttons, "", save, style="primary", font=bold, padx=s(14), pady=s(4))
    tr(save_btn, lambda: L("保存信息", "Save"))
    save_btn.pack(side="right")
    cancel_btn = FlatButton(buttons, "", close, font=font, padx=s(12), pady=s(4))
    tr(cancel_btn, lambda: L("取消", "Cancel"))
    cancel_btn.pack(side="right", padx=(0, s(8)))

    def on_lang(_lang=None):
        try:
            window.title(L("CAD 交付信息", "CAD delivery info"))
            for widget, fn in texts:
                widget.configure(text=fn())
            for box in check_boxes.values():
                box.retranslate()
        except Exception:
            pass

    i18n.on_change(on_lang)
    window.title(L("CAD 交付信息", "CAD delivery info"))
    window.protocol("WM_DELETE_WINDOW", close)
    window.bind("<Escape>", lambda _e: close())
    try:
        x = parent.winfo_rootx()
        y = parent.winfo_rooty() + parent.winfo_height() + s(8)
        window.geometry(f"+{max(0, x)}+{max(0, y)}")
    except Exception:
        pass
    window.update_idletasks()
    exclude_from_capture(window)
    window.lift()
    return window
