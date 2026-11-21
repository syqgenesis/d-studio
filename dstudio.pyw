"""D Studio launcher - the target of the Start-menu / desktop shortcuts.

    pythonw dstudio.pyw                 recorder bar (GUI); any other option is
                                        passed to ``python -m recorder gui``
    pythonw dstudio.pyw --viewer        annotator: local web server + browser
                                        (?lang=<current language>), with a small
                                        window to stop it
    pythonw dstudio.pyw --open-data     open the recordings folder (settings
                                        data_dir) in Explorer and exit
    python  dstudio.pyw --selftest      environment report; exit 0 = ready,
                                        1 = something needs fixing
            [--json OUT.json] [--quick] [--no-pause] [--no-dialog]
    python  dstudio.pyw --version

Runs the app in-process with this folder on ``sys.path`` (works from the
installed folder, the portable zip and a dev checkout). Under ``pythonw.exe``
there is no console: stdout/stderr go to ``<data dir>/dstudio.log``, fatal
errors are shown in a message box, and child processes (ffmpeg, ffprobe) are
started without console windows.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

IS_WIN = sys.platform == "win32"
WINDOWED = sys.stdout is None or sys.stderr is None      # pythonw.exe: no console
BUNDLED_PYTHON_DIR = os.path.join(HERE, "python")
SELFTEST_LOG = "dstudio-selftest.log"
APP_LOG = "dstudio.log"
DEFAULT_PORT = 8000
TCL_ENV_VARS = ("TCL_LIBRARY", "TK_LIBRARY", "TIX_LIBRARY")
_LOG_PATH: Optional[str] = None
_TCL_REPLACED: Dict[str, str] = {}   # foreign Tcl/Tk variables replaced at start (self-test shows them)


# --------------------------------------------------------------------------- basics
def L(zh: str, en: str, **fmt: Any) -> str:
    """Bilingual text via recorder.i18n; plain English if the app cannot be imported."""
    try:
        from recorder.i18n import L as _L

        return _L(zh, en, **fmt)
    except Exception:
        return en.format(**fmt) if fmt else en


def app_version() -> str:
    try:
        with open(os.path.join(HERE, "VERSION.txt"), encoding="utf-8-sig") as fh:
            head = fh.read(400)
    except OSError:
        return "0.0.0"
    m = re.search(r"\d+\.\d+\.\d+(?:[-.+][0-9A-Za-z.-]+)?", head)
    return m.group(0) if m else "0.0.0"


def is_bundled_interpreter() -> bool:
    """True when running on the python\\ folder shipped by the installer / portable zip."""
    try:
        return os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(os.path.abspath(BUNDLED_PYTHON_DIR))
    except Exception:
        return False


def is_installed() -> bool:
    return os.path.isfile(os.path.join(HERE, "INSTALLED"))


def layout_name() -> str:
    if is_installed():
        return "installed"
    return "portable" if is_bundled_interpreter() else "dev"


def safe_data_dir() -> str:
    """Recordings folder (recorder.paths.data_dir), or %TEMP%\\DStudio if that fails."""
    try:
        from recorder import paths

        return paths.data_dir()
    except Exception:
        fallback = os.path.join(tempfile.gettempdir(), "DStudio")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _rotate(path: str, limit: int = 5 << 20) -> None:
    try:
        if os.path.getsize(path) > limit:
            os.replace(path, path + ".1")
    except OSError:
        pass


def sanitize_sys_path() -> None:
    """On the bundled interpreter keep only our own folders on sys.path.

    Users' machines often carry PYTHONPATH / user site-packages from other
    Python installs (CAD suites set them globally); mixing those with the
    bundled numpy or tkinter breaks in confusing ways. The shortcuts also pass
    ``-E -s``; this is the belt to those braces.
    """
    if not is_bundled_interpreter():
        return
    root = os.path.normcase(os.path.abspath(HERE))
    kept = []
    for entry in sys.path:
        if not entry:
            continue
        full = os.path.normcase(os.path.abspath(entry))
        if full == root or full.startswith(root + os.sep):
            kept.append(entry)
    sys.path[:] = kept


def _bundled_tcl_dir(prefix: str, stem: str, marker: str) -> Optional[str]:
    """``<prefix>\\tcl\\<stem><version>`` holding ``marker`` (e.g. tcl8.6\\init.tcl), newest first."""
    base = os.path.join(prefix, "tcl")
    try:
        names = sorted(os.listdir(base), reverse=True)
    except OSError:
        return None
    for name in names:
        if re.fullmatch(re.escape(stem) + r"\d+(?:\.\d+)*", name) and os.path.isfile(os.path.join(base, name, marker)):
            return os.path.join(base, name)
    return None


def pin_bundled_tcl(env: Optional[Dict[str, str]] = None, prefix: Optional[str] = None) -> Dict[str, str]:
    """On the bundled interpreter, make Tcl/Tk load the bundled libraries.

    ``-E`` only ignores PYTHON* variables. A machine-wide TCL_LIBRARY /
    TK_LIBRARY (some EDA/CAE/GIS tools and old Python installers set them)
    would make the bundled Tk read a foreign init.tcl and fail ("Can't find a
    usable init.tcl"), so the bar and the annotator would never open. Must run
    before tkinter is imported. Returns the foreign values that were replaced.
    """
    env = os.environ if env is None else env
    if prefix is None:
        if not is_bundled_interpreter():
            return {}
        prefix = sys.prefix
    wanted = {"TCL_LIBRARY": _bundled_tcl_dir(prefix, "tcl", "init.tcl"),
              "TK_LIBRARY": _bundled_tcl_dir(prefix, "tk", "tk.tcl"),
              "TIX_LIBRARY": None}
    replaced: Dict[str, str] = {}
    for var in TCL_ENV_VARS:
        old, new = env.get(var), wanted.get(var)
        if old is not None and (not new or os.path.normcase(os.path.abspath(old)) != os.path.normcase(new)):
            replaced[var] = old
        if new:
            env[var] = new
        else:
            env.pop(var, None)   # let _tkinter find the bundled library next to sys.prefix
    return replaced


def setup_windowed_io() -> None:
    """pythonw.exe: stdout/stderr are None, so route them to a log file."""
    global _LOG_PATH
    handle = None
    for base in (None, os.path.join(tempfile.gettempdir(), "DStudio")):
        try:
            folder = base or safe_data_dir()
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, APP_LOG)
            _rotate(path)
            handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
            _LOG_PATH = path
            break
        except OSError:
            continue
    if handle is None:
        handle = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle
    try:
        handle.write(f"\n===== D Studio {app_version()} | {dt.datetime.now():%Y-%m-%d %H:%M:%S} | pid {os.getpid()}"
                     f" | {sys.executable} {' '.join(sys.argv[1:])}\n")
        import faulthandler

        faulthandler.enable(handle)
    except Exception:
        pass


def setup_console_io() -> None:
    """Never crash on a character the console/pipe encoding cannot show."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or not hasattr(stream, "reconfigure"):
                continue
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:   # CI logs / redirected output: always UTF-8
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def hide_child_consoles() -> None:
    """Under pythonw every console child (ffmpeg, ffprobe...) would flash a black
    window - and could even end up in the recording. Default CREATE_NO_WINDOW."""
    if not IS_WIN or getattr(subprocess.Popen, "_dstudio_no_window", False):
        return
    create_no_window, create_new_console, detached = 0x08000000, 0x00000010, 0x00000008
    base = subprocess.Popen

    class NoWindowPopen(base):  # type: ignore[misc, valid-type]
        _dstudio_no_window = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if len(args) < 14:   # creationflags is Popen's 14th positional parameter
                flags = int(kwargs.get("creationflags") or 0)
                if not flags & (create_new_console | detached):
                    kwargs["creationflags"] = flags | create_no_window
            super().__init__(*args, **kwargs)

    subprocess.Popen = NoWindowPopen  # type: ignore[misc]


def set_app_identity(app_id: str) -> None:
    """Own taskbar group + our icon on every Tk window (instead of Python's)."""
    if not IS_WIN:
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
    except Exception:
        pass
    ico = os.path.join(HERE, "dstudio.ico")
    if not os.path.isfile(ico):
        return
    try:
        import tkinter

        original = tkinter.Tk.__init__
        if getattr(original, "_dstudio_icon", False):
            return

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            original(self, *args, **kwargs)
            try:
                self.iconbitmap(default=ico)
            except Exception:
                pass

        __init__._dstudio_icon = True  # type: ignore[attr-defined]
        tkinter.Tk.__init__ = __init__  # type: ignore[method-assign]
    except Exception:
        pass


def message_box(title: str, text: str, kind: str = "info") -> None:
    """Tk message box, falling back to the Win32 one (Tk may be what is broken)."""
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        {"error": messagebox.showerror, "warning": messagebox.showwarning}.get(kind, messagebox.showinfo)(
            title, text, parent=root)
        root.destroy()
        return
    except Exception:
        pass
    if IS_WIN:
        try:
            import ctypes

            icon = {"error": 0x10, "warning": 0x30}.get(kind, 0x40)
            ctypes.windll.user32.MessageBoxW(None, text, title, icon | 0x00010000)  # MB_SETFOREGROUND
            return
        except Exception:
            pass
    print(f"{title}: {text}", file=sys.stderr)


def open_path(path: str) -> bool:
    try:
        if IS_WIN:
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as exc:
        print(f"[dstudio] could not open {path}: {exc}", file=sys.stderr)
        return False


def current_lang() -> str:
    """The app's UI language (recorder.i18n), 'zh' or 'en'."""
    try:
        from recorder.i18n import get_language

        lang = str(get_language() or "")
    except Exception:
        lang = ""
    return lang if lang in ("zh", "en") else "en"


def with_lang(url: str, lang: Optional[str] = None) -> str:
    """Annotator URL carrying ``?lang=<zh|en>`` (replacing any lang, before a #fragment)."""
    lang = lang if lang in ("zh", "en") else current_lang()
    base, hash_sep, fragment = url.partition("#")
    path, _q, query = base.partition("?")
    params = [p for p in query.split("&") if p and p.split("=", 1)[0] != "lang"]
    params.append("lang=" + lang)
    return f"{path}?{'&'.join(params)}{hash_sep}{fragment}"


def run_open_data() -> int:
    """Start-menu "D Studio Recordings": open the current recordings folder (settings data_dir)."""
    # The configured folder itself - never safe_data_dir()'s %TEMP% fallback, which
    # would show an empty folder while the recordings sit on an unplugged drive.
    try:
        from recorder import paths
        folder = paths.data_dir(create=False)
    except Exception as exc:
        message_box("D Studio", L("无法确定录制文件夹：{e}", "Cannot determine the recordings folder: {e}", e=exc), "error")
        return 1
    print(f"[dstudio] recordings folder: {folder}")
    if not os.path.isdir(folder):
        try:
            os.makedirs(folder, exist_ok=True)   # first run, or a folder chosen but not created yet
        except OSError:
            # e.g. the recordings were moved to an external drive that is not connected
            message_box("D Studio", L("录制文件夹不存在或无法访问（例如移动硬盘未连接）：\n{p}",
                                      "The recordings folder does not exist or cannot be reached "
                                      "(e.g. an external drive is not connected):\n{p}", p=folder), "error")
            return 1
    if open_path(folder):
        return 0
    message_box("D Studio", L("无法打开录制文件夹：\n{p}", "Could not open the recordings folder:\n{p}", p=folder), "error")
    return 1


# --------------------------------------------------------------------------- args
def parse_launcher_args(argv: List[str]) -> Tuple[argparse.Namespace, List[str]]:
    p = argparse.ArgumentParser(
        prog="dstudio.pyw", allow_abbrev=False, add_help=True,
        description="D Studio launcher. Without --viewer/--selftest it starts the recorder bar; "
                    "unknown options are passed to `python -m recorder gui`.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--viewer", action="store_true", help="start the annotator (web server + browser)")
    mode.add_argument("--selftest", action="store_true", help="print an environment report and exit 0/1")
    mode.add_argument("--version", action="store_true", help="print the version and exit")
    mode.add_argument("--open-data", action="store_true",
                      help="open the recordings folder (settings data_dir) in Explorer and exit")
    p.add_argument("--lang", choices=["zh", "en"], help="UI language for this run")
    p.add_argument("--json", metavar="OUT.json", help="--selftest: also write the report as JSON")
    p.add_argument("--quick", action="store_true", help="--selftest: skip slow probes (hardware encoders, DXGI grab)")
    p.add_argument("--no-pause", action="store_true", help="--selftest: never wait for Enter")
    p.add_argument("--no-dialog", action="store_true", help="--selftest under pythonw: no message box (CI)")
    p.add_argument("--no-tk-window", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--port", type=int, default=0, help="--viewer: first port to try (default from settings, 8000)")
    p.add_argument("--no-browser", action="store_true", help="--viewer: do not open the browser")
    p.add_argument("--no-window", action="store_true", help="--viewer: no control window; run until Ctrl+C")
    return p.parse_known_args(argv)


# --------------------------------------------------------------------------- GUI
def run_gui(rest: List[str], lang: Optional[str]) -> int:
    if lang and "--lang" not in rest:
        rest = ["--lang", lang] + rest
    try:
        gui = importlib.import_module("recorder.gui")
    except Exception as exc:
        traceback.print_exc()
        message_box("D Studio", L(
            "D Studio 无法启动（程序文件缺失或损坏）：\n{e}\n\n请运行开始菜单中的“D Studio 自检”，或重新安装。\n日志：{log}",
            "D Studio could not start (program files missing or damaged):\n{e}\n\n"
            "Run \"D Studio Self-test\" from the Start menu, or reinstall.\nLog: {log}",
            e=f"{type(exc).__name__}: {exc}", log=_LOG_PATH or "-"), "error")
        return 1
    return int(gui.main(rest) or 0)


# --------------------------------------------------------------------------- viewer
def load_serve_module():
    """tools/serve.py loaded by file path (no reliance on a 'tools' package name)."""
    mod = sys.modules.get("dstudio_tools_serve")
    if mod is not None:
        return mod
    path = os.path.join(HERE, "tools", "serve.py")
    spec = importlib.util.spec_from_file_location("dstudio_tools_serve", path)
    if spec is None or spec.loader is None:
        raise ImportError(L("找不到标注台服务 {p}", "Annotator server not found: {p}", p=path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dstudio_tools_serve"] = mod
    spec.loader.exec_module(mod)
    return mod


def _ping_viewer(port: int, timeout: float = 0.8) -> Optional[Dict[str, Any]]:
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never via a system proxy
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) and data.get("app") == "dstudio-viewer" else None
    except Exception:
        return None


def _same_dir(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _settings_port() -> int:
    try:
        from recorder.settings import load_settings

        return int(load_settings().get("viewer_port") or DEFAULT_PORT)
    except Exception:
        return DEFAULT_PORT


def run_viewer(opts: argparse.Namespace) -> int:
    import webbrowser

    port = int(opts.port or _settings_port())
    data_dir = safe_data_dir()
    # An annotator for the same recordings folder is already running (e.g. opened
    # from the recorder bar): just show it instead of starting a second server.
    for candidate in range(port, port + 10):
        info = _ping_viewer(candidate)
        if info and _same_dir(info.get("data_dir"), data_dir):
            url = with_lang(f"http://127.0.0.1:{candidate}/viewer/")
            print(f"[dstudio] annotator already running: {url}")
            if not opts.no_browser:
                webbrowser.open(url)
            return 0
    try:
        serve = load_serve_module()
        if hasattr(serve, "start_server"):
            server, thread = serve.start_server(port=port, data_dir=None, open_browser=False)
        else:   # older tools/serve.py without the in-process API
            httpd = serve.ThreadingHTTPServer(("127.0.0.1", port), serve.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            server = httpd
    except Exception as exc:
        traceback.print_exc()
        message_box("D Studio", L("标注台无法启动：\n{e}", "The annotator could not start:\n{e}", e=exc), "error")
        return 1
    url = getattr(server, "url", None) or f"http://127.0.0.1:{server.server_address[1]}/viewer/"
    print(f"[dstudio] annotator: {with_lang(url)}  (data: {data_dir})")
    if not opts.no_browser:
        try:
            webbrowser.open(with_lang(url))
        except Exception:
            pass

    def stop() -> None:
        try:
            if hasattr(serve, "stop_server") and hasattr(serve, "start_server"):
                serve.stop_server(server)
            else:
                server.shutdown()
                server.server_close()
        except Exception:
            pass

    use_window = not opts.no_window and (WINDOWED or not sys.stdin or not sys.stdin.isatty())
    if use_window:
        try:
            ViewerWindow(url, data_dir, stop).run()
            return 0
        except Exception:
            traceback.print_exc()   # no Tk -> fall through to the blocking loop
    print(L("标注台运行中，按 Ctrl+C 停止。", "Annotator running - press Ctrl+C to stop."))
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        pass
    stop()
    return 0


class ViewerWindow:
    """Tiny always-available window: the annotator runs while it is open.

    ``url`` is the server's base URL; every link shown/opened carries the
    current ``?lang=`` (the 中文/EN toggle reopens the page in the new language).
    """

    BG, PANEL, TEXT, MUTED, ACCENT = "#0d1117", "#151b23", "#e6edf3", "#8b98a9", "#4da3ff"

    def __init__(self, url: str, data_dir: str, on_stop: Callable[[], None]):
        import tkinter as tk

        if IS_WIN:   # crisp text on scaled displays (same awareness as the recorder bar)
            try:
                import ctypes

                ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
        self.tk = tk
        self.url, self.data_dir, self.on_stop = url, data_dir, on_stop
        self.root = tk.Tk()
        self.root.configure(bg=self.BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        frame = tk.Frame(self.root, bg=self.BG, padx=18, pady=14)
        frame.pack(fill="both", expand=True)
        self.title_lbl = tk.Label(frame, bg=self.BG, fg=self.TEXT, font=("Segoe UI", 12, "bold"), anchor="w")
        self.title_lbl.pack(fill="x")
        self.url_lbl = tk.Label(frame, text=with_lang(url), bg=self.BG, fg=self.ACCENT, cursor="hand2",
                                font=("Segoe UI", 10, "underline"), anchor="w")
        self.url_lbl.pack(fill="x", pady=(6, 2))
        self.url_lbl.bind("<Button-1>", lambda _e: self.open_browser())
        self.dir_lbl = tk.Label(frame, bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9), anchor="w",
                                justify="left", wraplength=440)
        self.dir_lbl.pack(fill="x")
        row = tk.Frame(frame, bg=self.BG)
        row.pack(fill="x", pady=(12, 0))
        style = dict(bg=self.PANEL, fg=self.TEXT, activebackground="#1f2733", activeforeground=self.TEXT,
                     relief="flat", padx=12, pady=5, font=("Segoe UI", 10), bd=0, highlightthickness=0)
        self.btn_open = tk.Button(row, command=self.open_browser, **style)
        self.btn_open.pack(side="left")
        self.btn_folder = tk.Button(row, command=lambda: open_path(self.data_dir), **style)
        self.btn_folder.pack(side="left", padx=8)
        self.btn_lang = tk.Button(row, command=self.toggle_lang, **style)
        self.btn_lang.pack(side="left")
        self.btn_stop = tk.Button(row, command=self.close, **dict(style, fg="#ffd9d9"))
        self.btn_stop.pack(side="right")
        self.note_lbl = tk.Label(frame, bg=self.BG, fg=self.MUTED, font=("Segoe UI", 9), anchor="w")
        self.note_lbl.pack(fill="x", pady=(10, 0))
        self.render()

    def render(self) -> None:
        self.root.title(L("D Studio 标注台", "D Studio Annotator"))
        self.title_lbl.configure(text=L("标注台已启动", "The annotator is running"))
        self.url_lbl.configure(text=with_lang(self.url))
        self.dir_lbl.configure(text=L("录制文件夹：{d}", "Recordings: {d}", d=self.data_dir))
        self.btn_open.configure(text=L("打开浏览器", "Open in browser"))
        self.btn_folder.configure(text=L("录制文件夹", "Recordings folder"))
        self.btn_lang.configure(text="EN" if L("zh", "en") == "zh" else "中文")
        self.btn_stop.configure(text=L("停止", "Stop"))
        self.note_lbl.configure(text=L("关闭此窗口即停止标注台。", "Closing this window stops the annotator."))

    def toggle_lang(self) -> None:
        try:
            from recorder.i18n import toggle_language

            toggle_language()
        except Exception:
            pass
        self.render()
        # the open page cannot be switched from here: show it again in the new language
        self.open_browser()

    def open_browser(self) -> None:
        import webbrowser

        try:
            webbrowser.open(with_lang(self.url))
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.on_stop()
        finally:
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# --------------------------------------------------------------------------- self-test
OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


class SelfTest:
    def __init__(self, quick: bool = False, tk_window: bool = True):
        self.quick = quick
        self.tk_window = tk_window
        self.checks: List[Dict[str, str]] = []
        self.bundled = is_bundled_interpreter()
        self.ffmpeg: Optional[str] = None

    def add(self, cid: str, name: str, status: str, detail: str = "") -> None:
        self.checks.append({"id": cid, "name": name, "status": status, "detail": str(detail)})

    def guard(self, cid: str, name: str, fn: Callable[[], Tuple[str, str]]) -> None:
        try:
            status, detail = fn()
        except Exception as exc:
            status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        self.add(cid, name, status, detail)

    # individual checks ---------------------------------------------------------
    def c_python(self) -> Tuple[str, str]:
        bits = 64 if sys.maxsize > 2 ** 32 else 32
        detail = f"{platform.python_version()} {bits}-bit, {sys.executable}"
        if sys.version_info < (3, 10) or bits != 64:
            return FAIL, detail + " - " + L("需要 64 位 Python 3.10+", "64-bit Python 3.10+ required")
        return OK, detail

    def c_app(self) -> Tuple[str, str]:
        names = {"installed": L("已安装", "installed"), "portable": L("便携版", "portable"),
                 "dev": L("开发目录", "dev folder")}
        return OK, f"{app_version()} ({names[layout_name()]}) {HERE}"

    def c_os(self) -> Tuple[str, str]:
        detail = f"{platform.system()} {platform.release()} ({platform.version()}), {platform.machine()}"
        if IS_WIN:
            build = sys.getwindowsversion().build  # type: ignore[attr-defined]
            if build < 19041:
                return WARN, detail + " - " + L(
                    "Windows 10 2004 以下无法把录制条排除出画面", "below Windows 10 2004 the recorder bar cannot be excluded from capture")
        elif not self.bundled:
            return WARN, detail + " - " + L("录制只支持 Windows", "recording needs Windows")
        return OK, detail

    def c_tk(self) -> Tuple[str, str]:
        import tkinter

        note = ""
        if _TCL_REPLACED:
            note = "; " + L("已忽略系统变量 {v}（改用内置 Tcl/Tk）", "ignored system {v} (bundled Tcl/Tk used instead)",
                            v=", ".join(f"{k}={v}" for k, v in sorted(_TCL_REPLACED.items())))
        if not self.tk_window:
            return OK, f"Tcl {tkinter.Tcl().eval('info patchlevel')} (no window test)" + note
        root = tkinter.Tk()
        try:
            root.withdraw()
            level = root.tk.call("info", "patchlevel")
            root.update_idletasks()
        finally:
            root.destroy()
        return OK, f"Tk {level}" + note

    def c_module(self, name: str, required: bool) -> Tuple[str, str]:
        try:
            mod = importlib.import_module(name)
        except Exception as exc:
            return (FAIL if required else WARN), f"{type(exc).__name__}: {exc}"
        return OK, str(getattr(mod, "__version__", "") or "ok")

    def c_dxgi(self) -> Tuple[str, str]:
        if not IS_WIN:
            return SKIP, L("仅 Windows", "Windows only")
        name = next((n for n in ("bettercam", "dxcam") if importlib.util.find_spec(n)), None)
        if name is None:
            status = FAIL if self.bundled else WARN
            return status, L("未安装 bettercam：将使用 GDI / gdigrab 采集", "bettercam not installed: GDI / gdigrab capture will be used")
        if self.quick:
            return OK, f"{name} " + L("已安装（快速模式未试采）", "installed (grab skipped in quick mode)")
        result: Dict[str, Any] = {}

        def probe() -> None:
            try:
                mod = importlib.import_module(name)
                # BGRA = the recorder's own format; any other colour needs OpenCV
                cam = mod.create(output_color="BGRA")
                try:
                    frame = None
                    deadline = time.time() + 2.5
                    while frame is None and time.time() < deadline:
                        frame = cam.grab()
                        if frame is None:
                            time.sleep(0.05)
                    result["shape"] = None if frame is None else tuple(int(v) for v in frame.shape)
                finally:
                    try:
                        cam.release()
                    except Exception:
                        pass
                result["ok"] = True
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"

        th = threading.Thread(target=probe, name="dxgi-probe", daemon=True)
        th.start()
        th.join(10)
        if th.is_alive():
            return WARN, L("DXGI 测试超时：将使用 GDI 采集", "DXGI probe timed out: GDI capture will be used")
        if "error" in result:
            return WARN, L("DXGI 桌面复制不可用（{e}）：将使用 GDI 采集",
                           "DXGI desktop duplication unavailable ({e}): GDI capture will be used", e=result["error"])
        shape = result.get("shape")
        if shape:
            return OK, L("DXGI 桌面复制可用（{s}）", "DXGI desktop duplication works ({s})", s=f"{shape[1]}x{shape[0]}")
        return OK, L("DXGI 已创建，桌面静止未取到新帧", "DXGI created; no new frame on a static desktop")

    def c_recorder(self) -> Tuple[str, str]:
        if not IS_WIN:
            importlib.import_module("recorder.paths")
            return SKIP, L("录制模块只在 Windows 上加载", "recorder modules load on Windows only")
        for mod in ("recorder.recorder", "recorder.gui"):
            importlib.import_module(mod)
        detail = "recorder, gui"
        try:
            app_mask = importlib.import_module("recorder.app_mask")
            apps = app_mask.list_apps()
            detail += ", " + L("可选应用 {n} 个", "{n} apps to pick from", n=len(apps))
        except Exception as exc:
            return WARN, detail + f"; app_mask: {type(exc).__name__}: {exc}"
        return OK, detail

    def _tool_version(self, exe: Optional[str]) -> Tuple[bool, str]:
        if not exe:
            return False, L("未找到", "not found")
        resolved = exe if os.path.isfile(exe) else shutil.which(exe)
        if not resolved:
            return False, L("未找到：{p}", "not found: {p}", p=exe)
        proc = subprocess.run([resolved, "-hide_banner", "-version"], capture_output=True, timeout=30)
        first = (proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()[:1]
        if proc.returncode != 0:
            return False, f"exit {proc.returncode}: {resolved}"
        where = L("内置", "bundled") if os.path.normcase(resolved).startswith(os.path.normcase(HERE)) else L("系统", "system")
        return True, f"{(first or ['?'])[0][:60]} ({where}: {resolved})"

    def c_ffmpeg(self) -> Tuple[str, str]:
        from recorder import paths

        self.ffmpeg = paths.ffmpeg_path()
        ok, detail = self._tool_version(self.ffmpeg)
        if ok:
            self.ffmpeg = self.ffmpeg if os.path.isfile(self.ffmpeg) else (shutil.which(self.ffmpeg) or self.ffmpeg)
        else:
            self.ffmpeg = None
        return (OK if ok else FAIL), detail

    def c_ffprobe(self) -> Tuple[str, str]:
        from recorder import paths

        ok, detail = self._tool_version(paths.ffprobe_path())
        return (OK if ok else FAIL), detail

    def c_encoder(self) -> Tuple[str, str]:
        if not self.ffmpeg:
            return FAIL, L("没有可用的 FFmpeg", "no working FFmpeg")
        videocap = importlib.import_module("recorder.videocap")

        def call(fn: Callable[..., Any], *args: Any) -> Any:
            try:
                return fn(*args, self.ffmpeg)
            except TypeError:   # older signature without the ffmpeg argument
                return fn(*args)

        if not call(videocap.probe_encoder, "libx264"):
            return FAIL, L("libx264 无法编码（Markov 格式需要 CRF 23 的 libx264）",
                           "libx264 cannot encode (the Markov format needs libx264 CRF 23)")
        hw = []
        try:
            listed = call(videocap.available_encoders) or set()
            hw = [e for e in ("h264_nvenc", "h264_amf", "h264_qsv") if e in listed]
        except Exception:
            pass
        if self.quick:
            return OK, "libx264 ok" + (f"; {L('已编译', 'compiled in')}: {', '.join(hw)}" if hw else "")
        picked = call(videocap.pick_encoder, "auto")
        works = picked == "libx264" or call(videocap.probe_encoder, picked)
        return (OK if works else WARN), L("libx264 正常；自动选择：{p}", "libx264 ok; auto picks: {p}", p=picked) + (
            f"; {L('已编译', 'compiled in')}: {', '.join(hw)}" if hw else "")

    def c_gdigrab(self) -> Tuple[str, str]:
        if not IS_WIN:
            return SKIP, L("仅 Windows", "Windows only")
        if not self.ffmpeg:
            return FAIL, L("没有可用的 FFmpeg", "no working FFmpeg")
        proc = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "gdigrab", "-framerate", "5",
                               "-i", "desktop", "-frames:v", "1", "-f", "null", "-"],
                              capture_output=True, timeout=30)
        if proc.returncode == 0:
            return OK, L("可以截取桌面", "desktop capture works")
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-200:]
        return WARN, L("gdigrab 截屏失败：{e}", "gdigrab capture failed: {e}", e=tail)

    def c_displays(self) -> Tuple[str, str]:
        if not IS_WIN:
            return SKIP, L("仅 Windows", "Windows only")
        import ctypes

        user32 = ctypes.windll.user32
        monitors = user32.GetSystemMetrics(80)                        # SM_CMONITORS
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)  # virtual screen
        pw, ph = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        dpi = 96
        try:
            dpi = int(user32.GetDpiForSystem())
        except Exception:
            pass
        return OK, L("{m} 个显示器，主屏 {pw}x{ph}，虚拟桌面 {vw}x{vh}，缩放 {s}%",
                     "{m} monitor(s), primary {pw}x{ph}, virtual desktop {vw}x{vh}, scaling {s}%",
                     m=monitors, pw=pw, ph=ph, vw=vw, vh=vh, s=round(dpi * 100 / 96))

    def c_data_dir(self) -> Tuple[str, str]:
        from recorder import paths

        folder = paths.data_dir()
        probe = os.path.join(folder, f".dstudio-write-test-{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        with open(probe, encoding="utf-8") as fh:
            assert fh.read() == "ok"
        os.remove(probe)
        free_gb = shutil.disk_usage(folder).free / 1e9
        detail = L("{d}（可写，剩余 {g:.1f} GB）", "{d} (writable, {g:.1f} GB free)", d=folder, g=free_gb)
        if free_gb < 2:
            return WARN, detail + " - " + L("磁盘空间不足，长时间录制可能失败", "low disk space; long recordings may fail")
        return OK, detail

    def c_settings(self) -> Tuple[str, str]:
        from recorder.settings import settings_path

        path = settings_path()
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        probe = os.path.join(folder, f".dstudio-write-test-{os.getpid()}")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return OK, path

    def c_viewer(self) -> Tuple[str, str]:
        serve = load_serve_module()
        if not hasattr(serve, "start_server"):
            return WARN, L("tools/serve.py 版本过旧（缺少 start_server）", "tools/serve.py is too old (no start_server)")
        if not os.path.isfile(os.path.join(HERE, "viewer", "index.html")):
            return FAIL, L("缺少 viewer/index.html", "viewer/index.html is missing")
        return OK, "tools/serve.py, viewer/"

    # driver ------------------------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        if IS_WIN:   # real pixel sizes in the display check (the self-test opens no layout-sensitive UI)
            try:
                import ctypes

                ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
        req = self.bundled
        steps = [
            ("python", L("Python", "Python"), self.c_python),
            ("app", L("D Studio", "D Studio"), self.c_app),
            ("os", L("操作系统", "Operating system"), self.c_os),
            ("tkinter", L("界面 (tkinter)", "UI (tkinter)"), self.c_tk),
            ("numpy", "numpy", lambda: self.c_module("numpy", req)),
            ("pillow", "Pillow", lambda: self.c_module("PIL", False)),
            ("dxgi", L("DXGI 屏幕采集", "DXGI screen capture"), self.c_dxgi),
            ("recorder", L("录制模块", "Recorder modules"), self.c_recorder),
            ("ffmpeg", "FFmpeg", self.c_ffmpeg),
            ("ffprobe", "FFprobe", self.c_ffprobe),
            ("encoder", L("视频编码", "Video encoder"), self.c_encoder),
            ("gdigrab", L("GDI 截屏", "GDI screen grab"), self.c_gdigrab),
            ("displays", L("显示器", "Displays"), self.c_displays),
            ("data_dir", L("录制文件夹", "Recordings folder"), self.c_data_dir),
            ("settings", L("设置文件", "Settings file"), self.c_settings),
            ("viewer", L("标注台", "Annotator"), self.c_viewer),
        ]
        for cid, name, fn in steps:
            self.guard(cid, name, fn)
        failed = [c for c in self.checks if c["status"] == FAIL]
        return {
            "app": "D Studio",
            "version": app_version(),
            "layout": layout_name(),
            "bundled_python": self.bundled,
            "time": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "python": sys.executable,
            "platform": platform.platform(),
            "ok": not failed,
            "exit_code": 1 if failed else 0,
            "checks": self.checks,
        }


def format_report(report: Dict[str, Any]) -> str:
    tags = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}
    lines = [L("D Studio {v} 自检", "D Studio {v} self-test", v=report["version"]),
             f"{report['time']}  {report['platform']}", ""]
    width = max(len(c["name"]) for c in report["checks"]) if report["checks"] else 10
    for c in report["checks"]:
        lines.append(f"{tags.get(c['status'], '[ ?? ]')} {c['name'].ljust(width)}  {c['detail']}")
    counts = {s: sum(1 for c in report["checks"] if c["status"] == s) for s in (OK, WARN, FAIL)}
    lines.append("")
    if counts[FAIL]:
        lines.append(L("结果：{f} 项失败，{w} 项警告。请处理 [FAIL] 项后再录制。",
                       "Result: {f} failed, {w} warning(s). Fix the [FAIL] items before recording.",
                       f=counts[FAIL], w=counts[WARN]))
    else:
        lines.append(L("结果：可以录制（{w} 项警告）。", "Result: ready to record ({w} warning(s)).", w=counts[WARN]))
    return "\n".join(lines)


def _console_owned_by_us() -> bool:
    """True when this process got a fresh console (started from a shortcut), so
    the window would vanish before the user can read the report."""
    if not IS_WIN:
        return False
    try:
        import ctypes

        arr = (ctypes.c_uint32 * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(arr, 8)
        return count == 1
    except Exception:
        return False


def run_selftest(opts: argparse.Namespace) -> int:
    report = SelfTest(quick=opts.quick, tk_window=not opts.no_tk_window).run()
    text = format_report(report)
    log_path = None
    try:
        log_path = os.path.join(safe_data_dir(), SELFTEST_LOG)
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        report["log"] = log_path
    except OSError:
        log_path = None
    if opts.json:
        out = os.path.abspath(opts.json)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    footer = L("报告已保存：{p}", "Report saved to: {p}", p=log_path) if log_path else ""
    if WINDOWED:
        print(text + "\n" + footer)
        if not opts.no_dialog:
            kind = "error" if not report["ok"] else ("warning" if any(c["status"] == WARN for c in report["checks"]) else "info")
            body = text if len(text) < 3000 else text[:3000] + "\n..."
            message_box(L("D Studio 自检", "D Studio self-test"), body + "\n\n" + footer, kind)
    else:
        print(text)
        if footer:
            print(footer)
        ci = any(os.environ.get(k) for k in ("CI", "GITHUB_ACTIONS", "TF_BUILD"))
        if not opts.no_pause and not ci and sys.stdin is not None and sys.stdin.isatty() and _console_owned_by_us():
            try:
                input("\n" + L("按回车键关闭…", "Press Enter to close..."))
            except (EOFError, KeyboardInterrupt):
                pass
    return int(report["exit_code"])


# --------------------------------------------------------------------------- main
def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sanitize_sys_path()             # before importing anything from the app
    _TCL_REPLACED.update(pin_bundled_tcl())   # before anything imports tkinter
    if WINDOWED:
        setup_windowed_io()         # also gives argparse somewhere to print --help
        hide_child_consoles()
    else:
        setup_console_io()
    opts, rest = parse_launcher_args(argv)
    if opts.version:
        print(app_version())
        return 0
    if opts.lang and (opts.selftest or opts.viewer or opts.open_data):
        try:
            from recorder.i18n import set_language

            set_language(opts.lang, persist=False)
        except Exception:
            pass
    if opts.open_data:
        return run_open_data()
    if opts.selftest:
        return run_selftest(opts)
    if opts.viewer:
        set_app_identity("DStudio.Annotator")
        return run_viewer(opts)
    set_app_identity("DStudio.Recorder")
    return run_gui(rest, opts.lang)


def _entry() -> int:
    try:
        return main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:  # last resort: never die silently under pythonw
        traceback.print_exc()
        if WINDOWED:
            message_box("D Studio", L("D Studio 出错：\n{e}\n\n日志：{log}", "D Studio error:\n{e}\n\nLog: {log}",
                                      e=f"{type(exc).__name__}: {exc}", log=_LOG_PATH or "-"), "error")
        return 1


if __name__ == "__main__":
    sys.exit(_entry())
