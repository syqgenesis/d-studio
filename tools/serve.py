"""Local web server for the annotation viewer: static files, Range requests
(video seeking) and a tiny save API.

Command line (unchanged)::

    python tools/serve.py --port 8000 --open
    python tools/serve.py --data-dir D:\\recordings --port 8010

In-process (the recorder GUI's "open annotator" button)::

    from tools.serve import start_server
    server, thread = start_server(port=8000, data_dir=None, open_browser=True)
    port = server.server_address[1]          # may differ if 8000 was taken
    url = server.url                          # http://127.0.0.1:<port>/viewer/

URL layout:

* ``/viewer/...``          the static single-page app (``<repo>/viewer``)
* ``/data/<id>/...``       files of one recording, served from the data dir
                           (default ``recorder.paths.data_dir()``)
* ``GET  /api/datasets``   recordings that have a ``metadata.json``
* ``GET  /api/dataset?id=<id>``  the same summary for one recording
* ``GET  /api/ping``       liveness probe
* ``POST /api/save?dataset=<id>&file=narration.json|draft.json``

Safety: binds to 127.0.0.1 by default, dataset ids are a single safe path
component, no ``..``/backslash/drive/NUL in paths, only ``narration.json`` and
``draft.json`` are writable, Host/Origin checks stop DNS-rebinding and
cross-site form posts from other web pages.
Standard library only.
"""
from __future__ import annotations

import argparse
import errno
import json
import math
import mimetypes
import os
import re
import socket
import socketserver
import stat
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:  # `python tools/serve.py` puts tools/ (not the repo) on sys.path
    sys.path.insert(0, ROOT)

try:  # bilingual user-facing messages (OSError text shown by the GUI)
    from recorder.i18n import L
except Exception:  # pragma: no cover - keep the server usable stand-alone
    def L(zh: str, en: str, **fmt) -> str:  # type: ignore[misc]
        return en.format(**fmt) if fmt else en

# the static app; DSTUDIO_VIEWER_DIR lets an installer/frozen build relocate it
VIEWER_DIR = os.environ.get("DSTUDIO_VIEWER_DIR", "").strip() or os.path.join(ROOT, "viewer")
DATA_DIR = os.path.join(ROOT, "data")   # legacy default; see default_data_dir()
ALLOWED_FILES = {"narration.json", "draft.json"}
MAX_BODY = 32 << 20
CHUNK = 1 << 20
DEFAULT_PORT = 8000
DEFAULT_PORT_TRIES = 10

# One path component: letters/digits (any script), _ - . ( ) [ ] + @ ~ and
# inner spaces ("rec-1 - Copy"); never starts with "." (so never "." / "..").
_DATASET_ID = re.compile(r"[\w\-()\[\]+@~][\w\-. ()\[\]+@~]{0,199}")
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | \
    {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
# kept for backwards compatibility with code that imported it
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# Explicit types: on Windows `mimetypes` reads the registry, which is often
# wrong (e.g. .js -> text/plain), so never rely on it for our own files.
MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}


# ----------------------------------------------------------------- utilities
def _log(msg: str) -> None:
    """stderr is None under pythonw.exe (GUI without console): never crash."""
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(msg + "\n")
        stream.flush()
    except Exception:
        pass


def _say(msg: str) -> None:
    """print() that survives consoles/pipes which cannot encode the text."""
    stream = sys.stdout
    if stream is None:
        return
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "ascii"
        print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)
    except Exception:
        pass


def default_data_dir() -> str:
    """The recordings folder (``recorder.paths.data_dir()``), created if missing."""
    try:
        from recorder.paths import data_dir

        return data_dir()
    except Exception:
        os.makedirs(DATA_DIR, exist_ok=True)
        return DATA_DIR


def resolve_data_dir(data_dir: Optional[str] = None) -> str:
    if not data_dir:
        return default_data_dir()
    path = os.path.abspath(os.path.expanduser(str(data_dir)))
    os.makedirs(path, exist_ok=True)
    return path


def is_safe_dataset_id(name: str) -> bool:
    if not isinstance(name, str) or not _DATASET_ID.fullmatch(name):
        return False
    if name.endswith((".", " ")):        # Windows silently strips these
        return False
    if name.split(".")[0].strip().upper() in _WIN_RESERVED:
        return False
    return True


def safe_join(base: str, rel: str) -> Optional[str]:
    """``base/rel`` for a URL-style relative path, or None if it could escape
    ``base`` or is malformed (``..``, backslashes, drive letters, NUL, ADS)."""
    if not isinstance(rel, str) or "\x00" in rel or "\\" in rel:
        return None
    parts = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or ":" in part:
            return None
        parts.append(part)
    base_abs = os.path.abspath(base)
    full = os.path.abspath(os.path.join(base_abs, *parts)) if parts else base_abs
    try:
        common = os.path.commonpath([os.path.normcase(base_abs), os.path.normcase(full)])
    except ValueError:  # different drives on Windows
        return None
    if common != os.path.normcase(base_abs):
        return None
    return full


def _read_json(path: str):
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def _num(value, default=0):
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return default


def _resolve_video(folder: str, meta: Dict) -> str:
    """File name of the playable video: metadata.video_file, else clip.mp4,
    else the legacy video.mp4. "" if none exists."""
    names: List[str] = []
    vf = meta.get("video_file") if isinstance(meta, dict) else None
    if isinstance(vf, str) and vf and "/" not in vf and "\\" not in vf and ":" not in vf \
            and not vf.startswith("."):
        names.append(vf)
    names += [n for n in ("clip.mp4", "video.mp4") if n not in names]
    for name in names:
        if os.path.isfile(os.path.join(folder, name)):
            return name
    return ""


def _capture_info(folder: str) -> Dict:
    for rel in (("raw", "capture.json"), ("capture.json",)):
        try:
            data = _read_json(os.path.join(folder, *rel))
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def dataset_info(data_dir: str, name: str) -> Optional[Dict]:
    """Summary of one recording folder, or None if it has no usable metadata."""
    if not is_safe_dataset_id(name):
        return None
    folder = os.path.join(data_dir, name)
    meta_path = os.path.join(folder, "metadata.json")
    if not os.path.isfile(meta_path):
        return None
    try:
        meta = _read_json(meta_path)
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    annotations = 0
    try:
        narration = _read_json(os.path.join(folder, "narration.json"))
        annotations = len(narration.get("annotations") or []) if isinstance(narration, dict) else 0
    except Exception:
        pass
    capture: Dict = {}
    cursor = meta.get("cursor_in_video")
    mode = meta.get("capture_mode")
    if not isinstance(cursor, bool) or not mode:
        capture = _capture_info(folder)
    if not isinstance(cursor, bool):
        cursor = capture.get("cursor_in_video") is True
    fps = _num(meta.get("fps"), 30) or 30
    total_frames = int(_num(meta.get("total_frames"), 0))
    try:
        mtime = os.path.getmtime(meta_path)
    except OSError:
        mtime = 0.0
    video_file = _resolve_video(folder, meta)
    title = meta.get("title")

    def _mtime(fname: str) -> float:
        try:
            return os.path.getmtime(os.path.join(folder, fname))
        except OSError:
            return 0.0
    return {
        "id": name,
        "title": title if isinstance(title, str) and title.strip() else name,
        "total_frames": total_frames,
        "fps": fps,
        "duration_ms": _num(meta.get("total_duration_ms"), 0) or int(round(total_frames * 1000.0 / fps)),
        "event_count": int(_num(meta.get("event_count"), 0)),
        "annotations": annotations,
        "has_video": bool(video_file),
        "video_file": video_file,
        "cursor_in_video": bool(cursor),
        "capture_mode": str(mode or capture.get("capture_mode") or ""),
        "mtime": mtime,
        # the viewer loads whichever of narration.json / draft.json is newer
        "narration_mtime": _mtime("narration.json"),
        "draft_mtime": _mtime("draft.json"),
    }


def list_datasets(data_dir: Optional[str] = None) -> list:
    """Every packaged recording in the data dir, newest first."""
    data_dir = data_dir or default_data_dir()
    try:
        names = os.listdir(data_dir)
    except OSError:
        return []
    out = []
    for name in names:
        info = dataset_info(data_dir, name)
        if info is not None:
            out.append(info)
    out.sort(key=lambda d: (-d["mtime"], d["id"]))
    return out


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def validate_narration(payload, strict: bool) -> Optional[str]:
    """None if ``payload`` looks like narration.json, else an error string.
    ``strict`` (narration.json) also requires numeric times and text fields."""
    if not isinstance(payload, dict):
        return "body must be a JSON object"
    anns = payload.get("annotations")
    if not isinstance(anns, list):
        return "missing annotations[]"
    if "summary" in payload and not isinstance(payload["summary"], str):
        return "summary must be a string"
    for i, a in enumerate(anns):
        if not isinstance(a, dict):
            return f"annotations[{i}] must be an object"
        if not strict:
            continue
        for key in ("start_s", "end_s"):
            v = a.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return f"annotations[{i}].{key} must be a number"
        for key in ("action", "intent", "app", "details"):
            if key in a and not isinstance(a[key], str):
                return f"annotations[{i}].{key} must be a string"
    return None


def atomic_write_json(path: str, obj) -> None:
    """Write via a temp file + os.replace. On Windows the replace fails while
    another handle (antivirus, indexer, a concurrent GET) has the file open,
    so retry briefly instead of losing the save."""
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        if os.name != "nt":  # mkstemp creates 0600; keep the usual 0644 / previous mode
            try:
                mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o644
                os.chmod(tmp, mode)
            except OSError:
                pass
        for attempt in range(40):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "dstudio/0.2"
    protocol_version = "HTTP/1.1"   # keep-alive + proper streaming for <video>
    timeout = 120                   # drop idle keep-alive / stalled media sockets

    # ------------------------------------------------------------------ helpers
    @property
    def data_dir(self) -> str:
        return getattr(self.server, "data_dir", None) or default_data_dir()

    @property
    def viewer_dir(self) -> str:
        return getattr(self.server, "viewer_dir", None) or VIEWER_DIR

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except OSError:
                self.close_connection = True

    def _not_found(self, msg: str = "not found") -> None:
        self._json(404, {"error": msg})

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):  # keep the console readable
        if getattr(self.server, "quiet", False):
            return
        if "/api/" in (self.path or ""):
            _log("[serve] %s" % (fmt % args))

    def _host_ok(self) -> bool:
        """DNS-rebinding guard: a loopback-bound server only answers requests
        addressed to a loopback name."""
        bound = str(self.server.server_address[0])
        if bound not in ("127.0.0.1", "::1", "localhost"):
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True
        if host.startswith("["):
            name = host.split("]", 1)[0] + "]"
        else:
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return name in _LOOPBACK_NAMES or name.endswith(".localhost")

    def _origin_ok(self) -> bool:
        """Writes only from our own pages (or non-browser clients)."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            o = urlparse(origin)
        except ValueError:
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        return o.scheme in ("http", "https") and bool(o.netloc) and o.netloc.lower() == host

    # --------------------------------------------------------------------- GET
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        try:
            parsed = urlparse(self.path)
        except ValueError:
            return self._json(400, {"error": "bad request"})
        path = parsed.path
        if path == "/api/datasets":
            data_dir = self.data_dir
            return self._json(200, {"datasets": list_datasets(data_dir), "data_dir": data_dir})
        if path == "/api/dataset":
            name = (parse_qs(parsed.query).get("id") or [""])[0]
            info = dataset_info(self.data_dir, name) if is_safe_dataset_id(name) else None
            if info is None:
                return self._not_found("unknown dataset")
            return self._json(200, info)
        if path == "/api/ping":
            return self._json(200, {"ok": True, "app": "dstudio-viewer", "data_dir": self.data_dir})
        if path in ("/", "/index.html", "/viewer"):
            return self._redirect("/viewer/" + (("?" + parsed.query) if parsed.query else ""))
        decoded = unquote(path)
        if decoded.startswith("/viewer/"):
            full = safe_join(self.viewer_dir, decoded[len("/viewer/"):])
            if full and os.path.isdir(full):
                full = os.path.join(full, "index.html")
            return self._serve_file(full)
        if decoded.startswith("/data/"):
            rest = decoded[len("/data/"):]
            dataset, _, rel = rest.partition("/")
            if not is_safe_dataset_id(dataset) or not rel:
                return self._not_found()
            full = safe_join(os.path.join(self.data_dir, dataset), rel)
            if full and os.path.isdir(full):
                return self._not_found()
            return self._serve_file(full)
        return self._not_found()

    def _serve_file(self, full: Optional[str]):
        if not full or not os.path.isfile(full):
            return self._not_found()
        try:
            fh = open(full, "rb")
        except OSError:
            return self._not_found()
        with fh:
            try:
                size = os.fstat(fh.fileno()).st_size
            except OSError:
                return self._not_found()
            ext = os.path.splitext(full)[1].lower()
            ctype = MIME.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"

            rng = self.headers.get("Range")
            start, end = 0, size - 1
            partial = False
            if rng and size:
                m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng)
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else size - 1
                    else:  # suffix range: last N bytes
                        start = max(0, size - int(m.group(2)))
                        end = size - 1 if int(m.group(2)) > 0 else -1
                    end = min(end, size - 1)
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    partial = True
                # malformed / multi-range: ignore the header, send the whole file

            length = max(0, end - start + 1)
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            norm = full.replace("\\", "/")
            no_store = "/view/" in norm or ext in (".json", ".jsonl", ".html", ".js", ".css")
            self.send_header("Cache-Control", "no-store" if no_store else "no-cache")
            self.end_headers()
            if self.command == "HEAD":
                return
            fh.seek(start)
            remaining = length
            while remaining > 0:
                buf = fh.read(min(CHUNK, remaining))
                if not buf:          # file shrank while streaming
                    self.close_connection = True
                    break
                try:
                    self.wfile.write(buf)
                except (ConnectionError, socket.timeout, OSError):
                    # browser cancelled a range request (seek) - normal for <video>
                    self.close_connection = True
                    return
                remaining -= len(buf)

    def do_HEAD(self):
        return self.do_GET()

    # -------------------------------------------------------------------- POST
    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        try:
            parsed = urlparse(self.path)
        except ValueError:
            return self._json(400, {"error": "bad request"})
        if parsed.path != "/api/save":
            return self._not_found()
        if not self._origin_ok():
            return self._json(403, {"error": "cross-origin save refused"})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        # application/json cannot be sent cross-site without a CORS pre-flight
        # (which we refuse). text/plain is accepted only from our own pages
        # (matching Origin), for navigator.sendBeacon() when the tab closes.
        if ctype != "application/json" and not (
                ctype == "text/plain" and self.headers.get("Origin") is not None):
            return self._json(415, {"error": "Content-Type must be application/json"})
        qs = parse_qs(parsed.query)
        dataset = (qs.get("dataset") or [""])[0]
        fname = (qs.get("file") or [""])[0]
        if not is_safe_dataset_id(dataset):
            return self._json(400, {"error": "bad dataset name"})
        if fname not in ALLOWED_FILES:
            return self._json(400, {"error": f"file must be one of {sorted(ALLOWED_FILES)}"})
        folder = os.path.join(self.data_dir, dataset)
        if not os.path.isdir(folder):
            return self._json(404, {"error": "unknown dataset"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            self.close_connection = True
            return self._json(400 if length <= MAX_BODY else 413, {"error": "bad body size"})
        try:
            raw = self.rfile.read(length)
        except OSError:
            self.close_connection = True
            return
        try:
            payload = json.loads(raw.decode("utf-8-sig"), parse_constant=_reject_constant)
        except Exception as exc:
            return self._json(400, {"error": f"invalid JSON: {exc}"})
        problem = validate_narration(payload, strict=(fname == "narration.json"))
        if problem:
            return self._json(400, {"error": f"invalid narration: {problem}"})

        out = os.path.join(folder, fname)
        lock = getattr(self.server, "save_lock", None) or threading.Lock()
        try:
            with lock:
                atomic_write_json(out, payload)
        except OSError as exc:
            return self._json(500, {"error": f"could not write {fname}: {exc}"})
        return self._json(200, {"ok": True, "dataset": dataset, "file": fname, "path": out,
                                "annotations": len(payload["annotations"])})

    def do_OPTIONS(self):
        # no CORS: answer pre-flights without Access-Control-* headers
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD, POST")
        self.send_header("Content-Length", "0")
        self.end_headers()


# -------------------------------------------------------------------- server
class ViewerServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that knows its data dir and fails loudly when the
    port is taken (also on Windows, where SO_REUSEADDR would let two servers
    share a port silently)."""

    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, address, handler, data_dir: str, viewer_dir: str = VIEWER_DIR,
                 quiet: bool = False):
        self.data_dir = data_dir
        self.viewer_dir = viewer_dir
        self.quiet = quiet
        self.save_lock = threading.Lock()
        self.requested_host = address[0]
        self.requested_port = address[1]
        self.url = ""
        super().__init__(address, handler)

    def server_bind(self):
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        # HTTPServer.server_bind() also calls socket.getfqdn(), a reverse DNS
        # lookup that can stall for seconds on badly configured machines.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, socket.timeout, TimeoutError)):
            return  # browsers drop media connections all the time
        if not self.quiet:
            import traceback

            _log("[serve] error while handling %s:\n%s" % (client_address, traceback.format_exc()))


class ViewerServer6(ViewerServer):
    address_family = socket.AF_INET6


class ServerStartError(OSError):
    """Raised by :func:`start_server`; ``str(exc)`` is a bilingual message
    ready to show to the user (``exc.errno`` keeps the OS error code)."""

    def __str__(self) -> str:
        return str(self.strerror or super().__str__())


def _port_unavailable(exc: OSError) -> bool:
    codes = {errno.EADDRINUSE, errno.EACCES, 10048, 10013}  # WSAEADDRINUSE / WSAEACCES
    return exc.errno in codes or getattr(exc, "winerror", None) in (10048, 10013)


def _url_host(host: str) -> str:
    if host in ("", "0.0.0.0", "::"):
        return "127.0.0.1"
    return f"[{host}]" if ":" in host else host


def _bind(host: str, port: int, data_dir: str, port_tries: int, quiet: bool) -> ViewerServer:
    cls = ViewerServer6 if ":" in host else ViewerServer
    port = int(port)
    if not 0 <= port <= 65535:
        raise ServerStartError(errno.EINVAL, L("端口号无效：{port}", "Invalid port number: {port}", port=port))
    tries = 1 if port == 0 else max(1, int(port_tries or 1))
    last: Optional[OSError] = None
    for candidate in range(port, min(65535, port + tries - 1) + 1):
        try:
            server = cls((host, candidate), Handler, data_dir=data_dir, quiet=quiet)
        except OSError as exc:
            if _port_unavailable(exc):
                last = exc
                continue
            raise ServerStartError(exc.errno or errno.EIO, L(
                "无法启动标注台服务（{host}:{port}）：{err}",
                "Could not start the annotator server on {host}:{port}: {err}",
                host=host, port=candidate, err=exc.strerror or exc)) from exc
        server.url = f"http://{_url_host(host)}:{server.server_address[1]}/viewer/"
        return server
    last_port = min(65535, port + tries - 1)
    span = str(port) if last_port == port else f"{port}-{last_port}"
    raise ServerStartError(errno.EADDRINUSE, L(
        "端口 {span} 已被占用（可能标注台已在运行，或被其他程序占用）。"
        "请关闭占用端口的程序，或在设置中换一个端口后重试。({err})",
        "Port {span} is already in use (the annotator may already be running, or another "
        "program holds it). Close that program or choose another port in Settings and "
        "try again. ({err})",
        span=span, err=(last.strerror if last is not None and last.strerror else last)))


_RUNNING: List[Tuple[ViewerServer, threading.Thread]] = []
_RUNNING_LOCK = threading.Lock()


def start_server(port: int = DEFAULT_PORT, data_dir: Optional[str] = None,
                 open_browser: bool = False, host: str = "127.0.0.1",
                 port_tries: int = DEFAULT_PORT_TRIES, quiet: bool = False):
    """Start the viewer server on a daemon thread; return ``(server, thread)``.

    * ``data_dir`` defaults to ``recorder.paths.data_dir()``.
    * If ``port`` is busy the next ``port_tries - 1`` ports are tried; the port
      actually used is ``server.server_address[1]`` and the viewer URL is
      ``server.url``. Pass ``port_tries=1`` to insist on ``port``. ``port=0``
      picks any free port (tests).
    * If none is free an ``OSError`` with a bilingual, user-readable message
      is raised (show ``str(exc)`` to the user).
    * Calling it again in the same process with the same host/port/data dir
      returns the server that is already running instead of a second one.
    * Stop with :func:`stop_server`.
    """
    data_dir = resolve_data_dir(data_dir)
    key_dir = os.path.normcase(os.path.abspath(data_dir))
    with _RUNNING_LOCK:
        for srv, th in list(_RUNNING):
            if not th.is_alive():
                _RUNNING.remove((srv, th))
                continue
            if (srv.requested_port == int(port) and srv.requested_host == host
                    and os.path.normcase(os.path.abspath(srv.data_dir)) == key_dir):
                if open_browser:
                    _open_browser(srv.url)
                return srv, th
        server = _bind(host, port, data_dir, port_tries, quiet)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25},
                                  name=f"dstudio-viewer-{server.server_address[1]}", daemon=True)
        thread.start()
        _RUNNING.append((server, thread))
    if open_browser:
        _open_browser(server.url)
    return server, thread


def stop_server(server: ViewerServer, timeout: float = 5.0) -> None:
    """Stop a server returned by :func:`start_server` (idempotent)."""
    with _RUNNING_LOCK:
        entries = [e for e in _RUNNING if e[0] is server]
        for e in entries:
            _RUNNING.remove(e)
    # shutdown() waits for serve_forever(); only call it when a loop is running
    if any(th.is_alive() for _, th in entries):
        try:
            server.shutdown()
        except Exception:
            pass
    try:
        server.server_close()
    except Exception:
        pass
    for _, th in entries:
        if th is not threading.current_thread():
            th.join(timeout)


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as exc:  # no browser registered, etc.
        _log(f"[serve] could not open a browser: {exc}")


# ----------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    try:
        from recorder.i18n import safe_stdio
        safe_stdio()
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="D Studio annotation viewer server")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="open the viewer in the browser")
    ap.add_argument("--data-dir", default=None,
                    help="folder holding the recordings (default: recorder.paths.data_dir())")
    ap.add_argument("--port-tries", type=int, default=DEFAULT_PORT_TRIES,
                    help="if --port is busy, try this many consecutive ports (1 = no fallback)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    try:
        data_dir = resolve_data_dir(args.data_dir)
        httpd = _bind(args.host, args.port, data_dir, args.port_tries, args.quiet)
    except OSError as exc:
        _say(f"[serve] {exc}")
        return 1
    _say(f"[serve] data={data_dir}\n[serve] {httpd.url}")
    if args.open:
        _open_browser(httpd.url)
    try:
        httpd.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        _say("\n[serve] bye")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
