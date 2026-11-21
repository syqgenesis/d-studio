"""“App only” capture: pick the application to record.

A dark Toplevel listing :func:`recorder.app_mask.list_apps` (friendly name,
exe, window title).  Double-click / Enter picks, "Refresh" re-scans, and a
program name can be typed when the app is not running yet.  The list is
loaded on a worker thread so a slow window enumeration never freezes the bar.

The pure helpers (:func:`normalize_app`, :func:`filter_apps`,
:func:`manual_app`, :func:`row_values`) are unit-tested on any OS; Windows
APIs are only touched lazily.
"""
from __future__ import annotations

import os
import re
import sys
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import i18n
from .i18n import L

_BAD_EXE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ------------------------------------------------------------------ pure logic
def normalize_app(app: Any) -> Optional[Dict[str, Any]]:
    """Coerce one ``list_apps()`` entry into the contract shape (or None)."""
    if not isinstance(app, dict):
        return None
    exe = os.path.basename(str(app.get("exe") or "").replace("\\", "/")).strip()
    if not exe:
        return None

    def as_list(value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    titles = [str(t) for t in as_list(app.get("titles")) if str(t or "").strip()]
    name = str(app.get("name") or "").strip() or exe_stem(exe)
    return {"exe": exe, "name": name, "pids": as_list(app.get("pids")),
            "titles": titles, "hwnds": as_list(app.get("hwnds")),
            "elevated": app.get("elevated")}


def exe_stem(exe: str) -> str:
    base = os.path.basename(str(exe or "").replace("\\", "/"))
    return base[:-4] if base.lower().endswith(".exe") else base


def first_title(app: Dict[str, Any], limit: int = 70) -> str:
    titles = [t for t in app.get("titles") or [] if t]
    if not titles:
        return ""
    text = titles[0] if len(titles[0]) <= limit else titles[0][: limit - 1] + "…"
    return text + (f"  (+{len(titles) - 1})" if len(titles) > 1 else "")


def filter_apps(apps: Sequence[Dict[str, Any]], query: str) -> List[Dict[str, Any]]:
    """Case-insensitive match on name, exe or any window title (all words)."""
    words = [w for w in (query or "").lower().split() if w]
    if not words:
        return list(apps)
    out = []
    for app in apps:
        hay = " ".join([app.get("name", ""), app.get("exe", ""), *(app.get("titles") or [])]).lower()
        if all(w in hay for w in words):
            out.append(app)
    return out


def manual_app(text: str) -> Optional[Dict[str, Any]]:
    """App entry for a typed program name / path (``SLDWORKS`` -> ``SLDWORKS.exe``)."""
    raw = (text or "").strip().strip('"').strip("'").strip()
    if not raw:
        return None
    base = os.path.basename(raw.replace("\\", "/")).strip()
    if not base or _BAD_EXE_CHARS.search(base) or base in (".", ".."):
        return None
    if not os.path.splitext(base)[1]:
        base += ".exe"
    return {"exe": base, "name": exe_stem(base), "pids": [], "titles": [], "hwnds": [], "elevated": None}


def sort_apps(apps: Sequence[Dict[str, Any]], current_exe: str = "") -> List[Dict[str, Any]]:
    cur = (current_exe or "").lower()
    return sorted(apps, key=lambda a: (a.get("exe", "").lower() != cur, a.get("name", "").lower(),
                                       a.get("exe", "").lower()))


def row_values(app: Dict[str, Any], *, warn_elevated: bool = False) -> Tuple[str, str, str]:
    name = app.get("name") or exe_stem(app.get("exe", ""))
    if warn_elevated and app.get("elevated"):
        name = L("{n}（管理员）", "{n} (admin)", n=name)
    return name, app.get("exe", ""), first_title(app)


def load_apps() -> Tuple[List[Dict[str, Any]], str]:
    """(apps, error text).  Never raises."""
    try:
        from .app_mask import list_apps
    except Exception as exc:  # module missing / not importable on this OS
        return [], L("无法读取应用列表：{e}", "Cannot list apps: {e}", e=exc)
    try:
        raw = list_apps() or []
    except Exception as exc:
        return [], L("无法读取应用列表：{e}", "Cannot list apps: {e}", e=exc)
    apps = [a for a in (normalize_app(x) for x in raw) if a]
    for app in apps:
        if app.get("elevated") is None and app.get("pids"):
            app["elevated"] = process_elevated(app["pids"][0])
    return apps, ""


# ------------------------------------------------------------------ elevation
def process_elevated(pid: Any) -> Optional[bool]:
    """True/False if the process token is elevated, None if unknown/non-Windows.

    Low-level input hooks of a normal process do not see input sent to an
    elevated (administrator) window, so the picker warns about those apps.
    """
    if sys.platform != "win32":
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        a = ctypes.WinDLL("advapi32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a.OpenProcessToken.restype = wintypes.BOOL
        a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        a.GetTokenInformation.restype = wintypes.BOOL
        proc = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not proc:
            return None
        try:
            token = wintypes.HANDLE()
            if not a.OpenProcessToken(proc, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
                return None
            try:
                value, size = wintypes.DWORD(0), wintypes.DWORD(0)
                if not a.GetTokenInformation(token, 20, ctypes.byref(value),  # TokenElevation
                                             ctypes.sizeof(value), ctypes.byref(size)):
                    return None
                return bool(value.value)
            finally:
                k.CloseHandle(token)
        finally:
            k.CloseHandle(proc)
    except Exception:
        return None


# ------------------------------------------------------------------- window
class AppPicker:
    """Modeless picker window; calls ``on_pick(app_dict)`` and closes."""

    def __init__(self, parent, on_pick: Callable[[Dict[str, Any]], None], current_exe: str = "",
                 scale: float = 1.0, fonts: Optional[Dict[str, Any]] = None,
                 anchor: Optional[Tuple[int, int]] = None):
        import tkinter as tk
        from tkinter import ttk
        from tkinter import font as tkfont

        from .gui import (ACCENT, BG, DIM, FIELD, HOVER, LINE, MUTED, PANEL, SELECTED, TEXT, WARN,
                          FlatButton, exclude_from_capture)

        self.tk, self.ttk = tk, ttk
        self.colors = {"BG": BG, "PANEL": PANEL, "FIELD": FIELD, "LINE": LINE, "TEXT": TEXT,
                       "MUTED": MUTED, "DIM": DIM, "ACCENT": ACCENT, "WARN": WARN}
        self.on_pick = on_pick
        self.current_exe = current_exe or ""
        self.scale = max(1.0, float(scale or 1.0))
        self.apps: List[Dict[str, Any]] = []
        self.visible: List[Dict[str, Any]] = []
        self._result: Optional[Tuple[List[Dict[str, Any]], str]] = None
        self._loading = False
        self._error = ""
        self._own_elevated = process_elevated(os.getpid())
        self._closed = False

        if fonts is None:
            fonts = {"ui": tkfont.nametofont("TkDefaultFont"), "small": tkfont.nametofont("TkDefaultFont"),
                     "bold": tkfont.nametofont("TkDefaultFont"), "head": tkfont.nametofont("TkDefaultFont")}
        self.fonts = fonts
        s = self.px

        self.win = tk.Toplevel(parent)
        self.win.configure(bg=PANEL)
        self.win.attributes("-topmost", True)
        self.win.minsize(s(520), s(340))
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Escape>", lambda _e: self.close())

        self.title_lbl = tk.Label(self.win, bg=PANEL, fg=TEXT, font=fonts["bold"], anchor="w")
        self.title_lbl.pack(fill="x", padx=s(14), pady=(s(12), s(2)))
        self.hint_lbl = tk.Label(self.win, bg=PANEL, fg=MUTED, font=fonts["small"], anchor="w",
                                 justify="left", wraplength=s(600))
        self.hint_lbl.pack(fill="x", padx=s(14), pady=(0, s(8)))

        top = tk.Frame(self.win, bg=PANEL)
        top.pack(fill="x", padx=s(14), pady=(0, s(6)))
        self.search_lbl = tk.Label(top, bg=PANEL, fg=MUTED, font=fonts["small"])
        self.search_lbl.pack(side="left")
        self.search_var = tk.StringVar()
        self.search = tk.Entry(top, textvariable=self.search_var, bg=FIELD, fg=TEXT, relief="flat",
                               insertbackground=TEXT, highlightthickness=1, highlightbackground=LINE,
                               highlightcolor=ACCENT, font=fonts["ui"], bd=s(3))
        self.search.pack(side="left", fill="x", expand=True, padx=(s(6), s(8)))
        self.search_var.trace_add("write", lambda *_: self._render_rows())
        self.btn_refresh = FlatButton(top, "", self.refresh, font=fonts["small"], padx=s(10), pady=s(3))
        self.btn_refresh.pack(side="right")

        # list -----------------------------------------------------------
        style = ttk.Style(self.win)
        try:
            if style.theme_use() != "clam":
                style.theme_use("clam")
        except Exception:
            pass
        try:
            rowh = int(fonts["ui"].metrics("linespace")) + s(8)
        except Exception:
            rowh = s(24)
        style.configure("DStudio.Treeview", background=FIELD, fieldbackground=FIELD, foreground=TEXT,
                        rowheight=rowh, borderwidth=0, relief="flat", font=fonts["ui"])
        style.map("DStudio.Treeview", background=[("selected", SELECTED)], foreground=[("selected", TEXT)])
        style.configure("DStudio.Treeview.Heading", background=PANEL, foreground=MUTED, relief="flat",
                        borderwidth=0, font=fonts["small"])
        style.map("DStudio.Treeview.Heading", background=[("active", HOVER)])
        style.configure("DStudio.Vertical.TScrollbar", background=PANEL, troughcolor=FIELD,
                        bordercolor=FIELD, arrowcolor=MUTED, lightcolor=PANEL, darkcolor=PANEL)

        box = tk.Frame(self.win, bg=LINE)
        box.pack(fill="both", expand=True, padx=s(14))
        self.tree = ttk.Treeview(box, columns=("name", "exe", "title"), show="headings",
                                 selectmode="browse", style="DStudio.Treeview", height=9)
        self.tree.column("name", width=s(170), minwidth=s(90), stretch=False, anchor="w")
        self.tree.column("exe", width=s(150), minwidth=s(80), stretch=False, anchor="w")
        self.tree.column("title", width=s(300), minwidth=s(120), stretch=True, anchor="w")
        self.tree.tag_configure("elevated", foreground=WARN)
        self.tree.tag_configure("current", foreground=ACCENT)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview,
                               style="DStudio.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        scroll.pack(side="right", fill="y", padx=(0, 1), pady=1)
        self.tree.bind("<Double-1>", lambda _e: self.pick_selected())
        self.tree.bind("<Return>", lambda _e: self.pick_selected())
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._update_details())

        self.status_lbl = tk.Label(self.win, bg=PANEL, fg=MUTED, font=fonts["small"], anchor="w",
                                   justify="left", wraplength=s(600))
        self.status_lbl.pack(fill="x", padx=s(14), pady=(s(6), 0))

        manual = tk.Frame(self.win, bg=PANEL)
        manual.pack(fill="x", padx=s(14), pady=(s(8), 0))
        self.manual_lbl = tk.Label(manual, bg=PANEL, fg=MUTED, font=fonts["small"])
        self.manual_lbl.pack(side="left")
        self.manual_var = tk.StringVar()
        self.manual = tk.Entry(manual, textvariable=self.manual_var, width=22, bg=FIELD, fg=TEXT,
                               relief="flat", insertbackground=TEXT, highlightthickness=1,
                               highlightbackground=LINE, highlightcolor=ACCENT, font=fonts["ui"], bd=s(3))
        self.manual.pack(side="left", padx=(s(6), s(6)))
        self.manual.bind("<Return>", lambda _e: self.pick_manual())
        self.btn_manual = FlatButton(manual, "", self.pick_manual, font=fonts["small"], padx=s(10), pady=s(3))
        self.btn_manual.pack(side="left")

        bottom = tk.Frame(self.win, bg=PANEL)
        bottom.pack(fill="x", padx=s(14), pady=(s(12), s(12)))
        self.btn_pick = FlatButton(bottom, "", self.pick_selected, style="primary", font=fonts["bold"],
                                   padx=s(14), pady=s(4))
        self.btn_pick.pack(side="right")
        self.btn_cancel = FlatButton(bottom, "", self.close, font=fonts["ui"], padx=s(12), pady=s(4))
        self.btn_cancel.pack(side="right", padx=(0, s(8)))

        self._lang_cb = i18n.on_change(self._on_language)
        self._retranslate()
        self._place(anchor)
        self.win.update_idletasks()
        exclude_from_capture(self.win)
        self.win.lift()
        self.search.focus_set()
        self.refresh()

    # ------------------------------------------------------------- helpers
    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    def alive(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.win.winfo_exists())
        except Exception:
            return False

    def focus(self) -> None:
        try:
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
        except Exception:
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        i18n.off_change(self._lang_cb)
        try:
            self.win.destroy()
        except Exception:
            pass

    def _place(self, anchor: Optional[Tuple[int, int]]) -> None:
        w, h = self.px(690), self.px(480)
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        if anchor:
            x, y = int(anchor[0]), int(anchor[1])
        else:
            x, y = (sw - w) // 2, (sh - h) // 3
        if 0 <= x < sw:
            x = max(0, min(x, sw - w))
        if 0 <= y < sh:
            y = max(0, min(y, sh - h - self.px(40)))
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _on_language(self, _lang: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self._retranslate()

    def _retranslate(self) -> None:
        if not self.alive():
            return
        self.win.title(L("选择要录制的应用", "Choose the app to record"))
        self.title_lbl.configure(text=L("选择要录制的应用", "Choose the app to record"))
        self.hint_lbl.configure(text=L(
            "“仅应用”模式：视频仍是整个屏幕的分辨率，但不属于该应用的窗口、弹出通知、聊天弹窗和任务栏都会被涂黑。"
            "该应用自己的菜单和对话框照常显示。切换到其他应用时录制自动暂停，切回后自动继续。",
            "App-only mode: the video keeps the full screen resolution, but every window, notification, chat "
            "pop-up and the taskbar that does not belong to this app is painted black. The app's own menus and "
            "dialogs stay visible. Switching to another app pauses the recording automatically; it resumes "
            "when you switch back."))
        self.search_lbl.configure(text=L("搜索", "Search"))
        self.btn_refresh.configure(text=L("↻ 刷新", "↻ Refresh"))
        self.tree.heading("name", text=L("应用", "App"))
        self.tree.heading("exe", text=L("程序", "Program"))
        self.tree.heading("title", text=L("窗口标题", "Window title"))
        self.manual_lbl.configure(text=L("不在列表中？输入程序名：", "Not listed? Type the program name:"))
        self.btn_manual.configure(text=L("使用", "Use"))
        self.btn_pick.configure(text=L("录制此应用", "Record this app"))
        self.btn_cancel.configure(text=L("取消", "Cancel"))
        self._render_rows()

    # --------------------------------------------------------------- data
    def refresh(self) -> None:
        if self._loading or not self.alive():
            return
        self._loading = True
        self._result = None
        self.btn_refresh.configure(state="disabled")
        self.status_lbl.configure(text=L("正在读取打开的应用…", "Looking for open apps…"),
                                  fg=self.colors["MUTED"])

        def work():
            self._result = load_apps()

        threading.Thread(target=work, name="dstudio-list-apps", daemon=True).start()
        self.win.after(80, self._poll)

    def _poll(self) -> None:
        if not self.alive():
            return
        if self._result is None:
            self.win.after(80, self._poll)
            return
        apps, error = self._result
        self._loading = False
        self.apps = sort_apps(apps, self.current_exe)
        self._error = error
        self.btn_refresh.configure(state="normal")
        self._render_rows()

    def _warn_elevated(self) -> bool:
        return self._own_elevated is not True

    def _render_rows(self) -> None:
        if not self.alive():
            return
        selected = self._selected_app()
        keep = (selected or {}).get("exe", "").lower() or self.current_exe.lower()
        self.tree.delete(*self.tree.get_children())
        self.visible = filter_apps(self.apps, self.search_var.get())
        focus_iid = None
        for i, app in enumerate(self.visible):
            tags = []
            if app.get("elevated") and self._warn_elevated():
                tags.append("elevated")
            if app.get("exe", "").lower() == self.current_exe.lower():
                tags.append("current")
            iid = str(i)
            self.tree.insert("", "end", iid=iid, values=row_values(app, warn_elevated=self._warn_elevated()),
                             tags=tags)
            if keep and app.get("exe", "").lower() == keep and focus_iid is None:
                focus_iid = iid
        if focus_iid is None and self.visible:
            focus_iid = "0"
        if focus_iid is not None:
            self.tree.selection_set(focus_iid)
            self.tree.focus(focus_iid)
            self.tree.see(focus_iid)
        self._update_details()

    def _selected_app(self) -> Optional[Dict[str, Any]]:
        try:
            sel = self.tree.selection()
            if sel:
                idx = int(sel[0])
                if 0 <= idx < len(self.visible):
                    return self.visible[idx]
        except Exception:
            pass
        return None

    def _update_details(self) -> None:
        if self._loading:
            return
        app = self._selected_app()
        warn = self.colors["WARN"]
        if self._error:
            self.status_lbl.configure(text=self._error, fg=warn)
        elif not self.apps:
            self.status_lbl.configure(text=L(
                "没有找到带可见窗口的应用。先打开要录制的软件再点“刷新”，或在下方输入程序名。",
                "No apps with visible windows found. Open the program you want to record and press "
                "Refresh, or type its program name below."), fg=warn)
        elif app and app.get("elevated") and self._warn_elevated():
            self.status_lbl.configure(text=L(
                "{n} 以管理员身份运行：请也以管理员身份运行 D Studio，否则无法记录在它里面的键盘和鼠标操作。",
                "{n} runs as administrator: run D Studio as administrator too, otherwise keyboard and mouse "
                "input inside it cannot be recorded.", n=app.get("name")), fg=warn)
        elif not self.visible:
            self.status_lbl.configure(text=L("没有匹配的应用。", "No matching apps."), fg=self.colors["MUTED"])
        else:
            self.status_lbl.configure(text=L(
                "找到 {n} 个应用 · 双击即可选择", "{n} apps found · double-click to choose", n=len(self.apps)),
                fg=self.colors["MUTED"])
        self.btn_pick.configure(state="normal" if app else "disabled")

    # --------------------------------------------------------------- pick
    def _finish(self, app: Dict[str, Any]) -> None:
        clean = {k: app.get(k) for k in ("exe", "name", "pids", "titles", "hwnds")}
        self.close()
        try:
            self.on_pick(clean)
        except Exception:
            pass

    def pick_selected(self) -> None:
        app = self._selected_app()
        if app:
            self._finish(app)

    def pick_manual(self) -> None:
        app = manual_app(self.manual_var.get())
        if not app:
            self.status_lbl.configure(text=L("请输入程序文件名，例如 SLDWORKS.exe", "Type a program file name, e.g. SLDWORKS.exe"),
                                      fg=self.colors["WARN"])
            return
        known = next((a for a in self.apps if a.get("exe", "").lower() == app["exe"].lower()), None)
        self._finish(known or app)
