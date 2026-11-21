"""Shared helpers for the Windows end-to-end scripts (``tests_windows/e2e_*.py``).

Everything here drives the *real* desktop: SendInput for mouse/keyboard,
Win32 window management, the recorder / packager CLIs as subprocesses, and
ffmpeg/ffprobe to look at the produced video. Import-safe on macOS/Linux (the
pure helpers are unit-tested there); the Win32 parts load lazily.

Each scenario writes ``<out>/report.json``::

    {"name", "ok", "checks": [{"name", "ok", "soft", "detail"}], "info": {...}, "error"}

and exits 0 (all hard checks passed) or 1.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from fractions import Fraction
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
INPUT_KEYS = {"mouse_move", "click", "key", "scroll", "drag"}

Box = Tuple[int, int, int, int]   # x0, y0, x1, y1 (half-open)


# =========================================================================== report
class Report:
    def __init__(self, name: str, out_dir: str):
        self.name = name
        self.out_dir = os.path.abspath(out_dir)
        os.makedirs(self.out_dir, exist_ok=True)
        self.checks: List[Dict[str, Any]] = []
        self.info: Dict[str, Any] = {}
        self.error: Optional[str] = None
        self.started = time.time()

    def check(self, name: str, ok: bool, detail: Any = "", soft: bool = False) -> bool:
        ok = bool(ok)
        self.checks.append({"name": name, "ok": ok, "soft": soft, "detail": detail})
        tag = "PASS" if ok else ("WARN" if soft else "FAIL")
        print(f"[{self.name}] {tag:4} {name}" + (f" -- {detail}" if detail not in ("", None) else ""), flush=True)
        return ok

    def note(self, key: str, value: Any) -> None:
        self.info[key] = value
        print(f"[{self.name}] info {key} = {json.dumps(value, ensure_ascii=False, default=str)[:400]}", flush=True)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.checks) and all(c["ok"] or c["soft"] for c in self.checks)

    def write(self) -> str:
        path = os.path.join(self.out_dir, "report.json")
        data = {"name": self.name, "ok": self.ok, "seconds": round(time.time() - self.started, 1),
                "checks": self.checks, "info": self.info, "error": self.error}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
        hard_fail = [c["name"] for c in self.checks if not c["ok"] and not c["soft"]]
        print(f"[{self.name}] {'OK' if self.ok else 'FAILED'} -> {path}" +
              (f" (failed: {', '.join(hard_fail)})" if hard_fail else "") +
              (f" (error: {self.error.splitlines()[-1]})" if self.error else ""), flush=True)
        return path


def warn_takeover(seconds: int = 5) -> None:
    """These scripts drive the real mouse/keyboard. On a developer PC give the
    person a moment to let go of the mouse (or Ctrl+C); CI needs no pause."""
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS") or os.environ.get("DSTUDIO_E2E_CONFIRMED"):
        return
    print(f"[e2e] This test moves the mouse, types and opens windows. Starting in {seconds}s - "
          "do not touch the mouse/keyboard (Ctrl+C to abort).", flush=True)
    time.sleep(seconds)
    os.environ["DSTUDIO_E2E_CONFIRMED"] = "1"


def scenario_main(name: str, body: Callable[[Report, argparse.Namespace, "Procs"], None],
                  add_args: Optional[Callable[[argparse.ArgumentParser], None]] = None,
                  argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=f"D Studio Windows E2E: {name}")
    ap.add_argument("--out", default=os.path.join(ROOT, "artifacts", "e2e", name), help="output folder")
    if add_args:
        add_args(ap)
    args = ap.parse_args(argv)
    rep = Report(name, args.out)
    if not IS_WIN:
        rep.check("runs on Windows", True, "skipped: not Windows", soft=True)
        rep.note("skipped", True)
        rep.write()
        return 0
    warn_takeover()
    procs = Procs()
    try:
        win().prepare()
        body(rep, args, procs)
    except Exception:
        rep.error = traceback.format_exc()
        print(rep.error, file=sys.stderr, flush=True)
    finally:
        try:
            procs.cleanup()
        except Exception:
            traceback.print_exc()
        try:
            win().release_all_input()
        except Exception:
            pass
    rep.write()
    return 0 if rep.ok else 1


# =========================================================================== pure helpers
def parse_rate(text: Any) -> float:
    """'30/1' -> 30.0, '30000/1001' -> 29.97, '0/0' -> 0."""
    try:
        s = str(text).strip()
        if "/" in s:
            num, den = s.split("/", 1)
            return float(Fraction(int(num), int(den))) if int(den) else 0.0
        return float(s)
    except (ValueError, ZeroDivisionError):
        return 0.0


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{n} is not a JSON object")
            rows.append(obj)
    return rows


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def clip_box(box: Box, w: int, h: int) -> Box:
    x0, y0, x1, y1 = box
    return max(0, x0), max(0, y0), min(w, x1), min(h, y1)


def inset(box: Box, d: int) -> Box:
    x0, y0, x1, y1 = box
    return x0 + d, y0 + d, x1 - d, y1 - d


def region_stats(rgb: bytes, w: int, h: int, box: Box, dark_max: int = 16) -> Dict[str, Any]:
    """Pixel statistics of an RGB24 frame region (pure Python; regions are small).

    dark_frac : share of pixels whose brightest channel <= dark_max (masked black)
    red_frac  : share of strongly red pixels (the fake notification colour)
    mean      : mean of all channels (0-255)
    """
    x0, y0, x1, y1 = clip_box(box, w, h)
    n = dark = red = 0
    total = 0
    step = 1 if (x1 - x0) * (y1 - y0) <= 120_000 else 2
    for y in range(y0, y1, step):
        row = y * w * 3
        for x in range(x0, x1, step):
            i = row + x * 3
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            n += 1
            total += r + g + b
            if max(r, g, b) <= dark_max:
                dark += 1
            if r >= 170 and g <= 90 and b <= 90:
                red += 1
    if not n:
        return {"n": 0, "dark_frac": 0.0, "red_frac": 0.0, "mean": 0.0, "box": [x0, y0, x1, y1]}
    return {"n": n, "dark_frac": round(dark / n, 4), "red_frac": round(red / n, 4),
            "mean": round(total / (3 * n), 1), "box": [x0, y0, x1, y1]}


def count_differing(rgb: bytes, w: int, h: int, box: Box, ref: Tuple[int, int, int], tol: int = 60) -> int:
    x0, y0, x1, y1 = clip_box(box, w, h)
    n = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            i = (y * w + x) * 3
            if max(abs(rgb[i] - ref[0]), abs(rgb[i + 1] - ref[1]), abs(rgb[i + 2] - ref[2])) > tol:
                n += 1
    return n


def screen_to_video(box: Box, origin: Tuple[int, int], scale: Tuple[float, float] = (1.0, 1.0)) -> Box:
    ox, oy = origin
    sx, sy = scale
    x0, y0, x1, y1 = box
    return (int(round((x0 - ox) * sx)), int(round((y0 - oy) * sy)),
            int(round((x1 - ox) * sx)), int(round((y1 - oy) * sy)))


def event_types(events: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for e in events:
        t = str(e.get("type"))
        out[t] = out.get(t, 0) + 1
    return out


def keys_with_codes(events: Iterable[Dict[str, Any]], codes: Iterable[int]) -> List[Dict[str, Any]]:
    wanted = set(int(c) for c in codes)
    hits = []
    for e in events:
        if e.get("type") != "key":
            continue
        try:
            code = int(e.get("keyCode", -1))
        except (TypeError, ValueError):
            code = -1
        if code in wanted:
            hits.append(e)
    return hits


def input_events_between(events: Iterable[Dict[str, Any]], t0_ms: float, t1_ms: float) -> List[Dict[str, Any]]:
    out = []
    for e in events:
        if e.get("type") in INPUT_KEYS and not e.get("synthetic_snapshot"):
            try:
                t = float(e.get("timestamp"))
            except (TypeError, ValueError):
                continue
            if t0_ms < t < t1_ms:
                out.append(e)
    return out


# ---------------------------------------------------------------- CI environment (env_report.py)
def env_report_path() -> str:
    return os.environ.get("DSTUDIO_ENV_REPORT") or os.path.join(ROOT, "artifacts", "env", "env.json")


def load_env_report(path: Optional[str] = None) -> Dict[str, Any]:
    """artifacts/env/env.json written by env_report.py before the tests ({} if missing)."""
    path = path or env_report_path()
    try:
        data = load_json(path)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def env_dxgi_works(env: Dict[str, Any]) -> bool:
    """env_report showed that DXGI desktop duplication delivers frames on this machine."""
    return bool(env.get("dxgi_duplication_works"))


def env_cursor_showing(env: Dict[str, Any]) -> bool:
    return bool(((env.get("windows") or {}).get("cursor") or {}).get("showing"))


# ---------------------------------------------------------------- raw/segments.json + capture.json
def segments_list(data: Any) -> List[Dict[str, Any]]:
    """raw/segments.json is a list of segment dicts (older/other writers: {"segments": [...]})."""
    if isinstance(data, dict):
        data = data.get("segments")
    return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []


def load_segments(out: str) -> List[Dict[str, Any]]:
    path = os.path.join(out, "raw", "segments.json")
    try:
        return segments_list(load_json(path))
    except (OSError, ValueError):
        return []


def rect_tuple(rect: Any) -> Optional[Tuple[int, int, int, int]]:
    """{x, y, width, height} (or [x, y, w, h]) -> (x, y, w, h)."""
    try:
        if isinstance(rect, dict):
            return int(rect["x"]), int(rect["y"]), int(rect["width"]), int(rect["height"])
        if isinstance(rect, (list, tuple)) and len(rect) == 4:
            return tuple(int(v) for v in rect)  # type: ignore[return-value]
    except (KeyError, TypeError, ValueError):
        pass
    return None


def summarize_segments(segs: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Real-capture statistics over all segments (pure; unit-tested).

    frames      CFR slots written (what ffprobe counts)
    captures    frames really grabbed (None when no segment reports it, e.g. gdigrab)
    real_ratio  captures / frames of the segments that report captures: CFR slots
                padded with duplicates do not count as captured
    """
    frames = captures = cursor_frames = 0
    frames_with_stats = 0
    have_captures = have_cursor = False
    capture_fps: List[float] = []
    rects: List[Optional[Tuple[int, int, int, int]]] = []
    for s in segs:
        n = len(s.get("frames") or []) or int(s.get("frame_count") or 0)
        frames += n
        cap = s.get("capture") or {}
        if isinstance(cap.get("captures"), (int, float)):
            have_captures = True
            captures += int(cap["captures"])
            frames_with_stats += n
        if isinstance(cap.get("capture_fps"), (int, float)):
            capture_fps.append(float(cap["capture_fps"]))
        if isinstance(cap.get("cursor_frames"), (int, float)):
            have_cursor = True
            cursor_frames += int(cap["cursor_frames"])
        rects.append(rect_tuple(cap.get("capture_rect")))
    return {
        "segments": len(segs),
        "frames": frames,
        "captures": captures if have_captures else None,
        "real_ratio": round(captures / frames_with_stats, 3) if have_captures and frames_with_stats else None,
        "capture_fps": capture_fps,
        "cursor_frames": cursor_frames if have_cursor else None,
        "backends": [s.get("backend") for s in segs],
        "encoders": sorted({str(s.get("encoder")) for s in segs if s.get("encoder")}),
        "capture_rects": rects,
        "in_progress": [bool(s.get("in_progress")) for s in segs],
        "finalized": [s.get("finalized") for s in segs],
        "broken": [bool(s.get("broken")) for s in segs],
    }


def real_capture_ok(summary: Dict[str, Any], min_ratio: float = 0.8) -> Optional[bool]:
    """True/False when the backend reports real captures, None when it cannot (gdigrab)."""
    ratio = summary.get("real_ratio")
    return None if ratio is None else ratio >= min_ratio


# ---------------------------------------------------------------- MP4 / H.264 bytes
def mp4_top_level_boxes(path: str, limit: int = 64) -> List[str]:
    """Types of the top-level MP4 boxes in file order (ftyp, moov, mdat / moof...)."""
    boxes: List[str] = []
    size_total = os.path.getsize(path)
    with open(path, "rb") as fh:
        pos = 0
        while pos + 8 <= size_total and len(boxes) < limit:
            fh.seek(pos)
            head = fh.read(16)
            if len(head) < 8:
                break
            size = int.from_bytes(head[:4], "big")
            kind = head[4:8].decode("latin-1")
            if size == 1 and len(head) >= 16:
                size = int.from_bytes(head[8:16], "big")
            elif size == 0:
                size = size_total - pos
            boxes.append(kind)
            if size < 8:
                break
            pos += size
    return boxes


def is_faststart_mp4(boxes: Sequence[str]) -> bool:
    """A normal (non-fragmented) MP4 with the index first: moov before mdat, no moof."""
    return "moov" in boxes and "mdat" in boxes and "moof" not in boxes \
        and boxes.index("moov") < boxes.index("mdat")


def has_x264_sei(path: str, max_bytes: int = 8 << 20) -> bool:
    """libx264 writes an 'x264 - core NNN' SEI into the first frames (a -c copy remux keeps it)."""
    with open(path, "rb") as fh:
        data = fh.read(max_bytes)
    return b"x264 - core" in data


def red_pixel_count(rgb: bytes, w: int, h: int, r_min: int = 160, gb_max: int = 70) -> int:
    """Saturated red pixels (the canary window's colour) in an RGB24 frame."""
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        a = np.frombuffer(rgb, dtype=np.uint8, count=w * h * 3).reshape(h, w, 3)
        return int(np.count_nonzero((a[..., 0] >= r_min) & (a[..., 1] <= gb_max) & (a[..., 2] <= gb_max)))
    n = 0
    for i in range(0, w * h * 3, 3):
        if rgb[i] >= r_min and rgb[i + 1] <= gb_max and rgb[i + 2] <= gb_max:
            n += 1
    return n


def bright_fraction(rgb: bytes, w: int, h: int, level: int = 64) -> float:
    """Share of pixels whose brightest channel exceeds ``level`` (not masked black)."""
    try:
        import numpy as np
    except ImportError:
        np = None
    if np is not None:
        a = np.frombuffer(rgb, dtype=np.uint8, count=w * h * 3).reshape(h, w, 3)
        return float(np.count_nonzero(a.max(axis=2) > level)) / max(1, w * h)
    n = sum(1 for i in range(0, w * h * 3, 3) if max(rgb[i], rgb[i + 1], rgb[i + 2]) > level)
    return n / max(1, w * h)


# =========================================================================== tools / processes
def ffmpeg_path() -> str:
    from recorder import paths

    ff = paths.ffmpeg_path()
    resolved = ff if os.path.isfile(ff) else shutil.which(ff)
    if not resolved:
        raise RuntimeError(f"ffmpeg not found ({ff}); set DSTUDIO_FFMPEG")
    return resolved


def ffprobe_path() -> str:
    from recorder import paths

    fp = paths.ffprobe_path()
    resolved = fp if os.path.isfile(fp) else shutil.which(fp)
    if not resolved:
        raise RuntimeError(f"ffprobe not found ({fp})")
    return resolved


def child_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("DSTUDIO_LANG", "en")
    if extra:
        env.update(extra)
    return env


def run(cmd: Sequence[str], timeout: float = 300, log: Optional[str] = None, **kw: Any) -> subprocess.CompletedProcess:
    print("$ " + " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd), flush=True)
    proc = subprocess.run(list(cmd), cwd=kw.pop("cwd", ROOT), env=kw.pop("env", child_env()),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                          creationflags=CREATE_NO_WINDOW if IS_WIN else 0, **kw)
    text = (proc.stdout or b"").decode("utf-8", "replace")
    if log:
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(text)
    tail = "\n".join(text.strip().splitlines()[-15:])
    if tail:
        print("    " + tail.replace("\n", "\n    "), flush=True)
    proc.text = text  # type: ignore[attr-defined]
    return proc


class Procs:
    """Every process a scenario starts; all of them die in cleanup()."""

    def __init__(self) -> None:
        self.popens: List[subprocess.Popen] = []
        self.pids: List[int] = []

    def add(self, p: subprocess.Popen) -> subprocess.Popen:
        self.popens.append(p)
        return p

    def add_pid(self, pid: int) -> None:
        if pid and pid not in self.pids:
            self.pids.append(pid)

    def cleanup(self) -> None:
        for p in reversed(self.popens):
            if p.poll() is None:
                kill_tree(p.pid)
                try:
                    p.wait(10)
                except Exception:
                    pass
        for pid in self.pids:
            kill_tree(pid)


def kill_tree(pid: int) -> None:
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def process_ids(image: str) -> List[int]:
    """PIDs of running processes with this exe name (tasklist)."""
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {image}"],
                         capture_output=True, creationflags=CREATE_NO_WINDOW).stdout.decode("utf-8", "replace")
    pids = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == image.lower():
            try:
                pids.append(int(parts[1].strip('"')))
            except ValueError:
                pass
    return pids


# =========================================================================== recorder / packager
def start_recorder(out: str, extra: Sequence[str], procs: Procs, tag: str) -> Tuple[subprocess.Popen, str]:
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    log = os.path.join(os.path.dirname(os.path.abspath(out)), f"{tag}.recorder.log")
    cmd = [sys.executable, "-m", "recorder", "start", "--out", out, "--ffmpeg", ffmpeg_path(), *extra]
    print("$ " + " ".join(cmd), flush=True)
    fh = open(log, "w", encoding="utf-8")
    p = subprocess.Popen(cmd, cwd=ROOT, env=child_env(), stdin=subprocess.DEVNULL, stdout=fh,
                         stderr=subprocess.STDOUT,
                         creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if IS_WIN else 0)
    fh.close()
    procs.add(p)
    return p, log


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def wait_recording(p: subprocess.Popen, log: str, out: str, timeout: float = 30.0) -> bool:
    """True once the recorder reports that capture is running."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = read_text(log)
        if "recording ->" in text or re.search(r"segment \d+ started", text):
            time.sleep(0.8)   # let ffmpeg / the capture thread deliver the first frames
            return True
        if p.poll() is not None:
            return False
        time.sleep(0.2)
    return False


def wait_exit(p: subprocess.Popen, timeout: float, stop_with_f10: bool = True) -> Optional[int]:
    try:
        return p.wait(timeout)
    except subprocess.TimeoutExpired:
        pass
    if stop_with_f10 and IS_WIN:
        print("recorder still running - pressing F10", flush=True)
        win().tap(win().VK_F10)
        try:
            return p.wait(30)
        except subprocess.TimeoutExpired:
            pass
    kill_tree(p.pid)
    try:
        p.wait(10)
    except Exception:
        pass
    return None


def package(out: str, log_dir: str, tag: str) -> subprocess.CompletedProcess:
    return run([sys.executable, "-m", "packager.build", "--raw", os.path.join(out, "raw"), "--out", out,
                "--id", os.path.basename(out)], timeout=600, log=os.path.join(log_dir, f"{tag}.packager.log"))


def validate(out: str, log_dir: str, tag: str) -> Dict[str, Any]:
    run([sys.executable, "-m", "packager.validate", "--dataset", out, "--decode"], timeout=600,
        log=os.path.join(log_dir, f"{tag}.validate.log"))
    path = os.path.join(out, "validation.json")
    return load_json(path) if os.path.isfile(path) else {}


def video_file(out: str) -> str:
    meta = os.path.join(out, "metadata.json")
    name = "clip.mp4"
    if os.path.isfile(meta):
        name = load_json(meta).get("video_file") or name
    return os.path.join(out, name)


def ffprobe(video: str, count_frames: bool = True) -> Dict[str, Any]:
    cmd = [ffprobe_path(), "-v", "error", "-select_streams", "v:0"]
    if count_frames:
        cmd.append("-count_frames")
    cmd += ["-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,nb_read_frames,codec_name,pix_fmt"
            ":format=duration", "-of", "json", video]
    proc = subprocess.run(cmd, capture_output=True, timeout=300, creationflags=CREATE_NO_WINDOW if IS_WIN else 0)
    if proc.returncode != 0:
        raise RuntimeError("ffprobe failed: " + proc.stderr.decode("utf-8", "replace")[-400:])
    data = json.loads(proc.stdout.decode("utf-8"))
    st = (data.get("streams") or [{}])[0]
    info = {k: st.get(k) for k in ("width", "height", "avg_frame_rate", "r_frame_rate", "codec_name", "pix_fmt")}
    for k in ("nb_frames", "nb_read_frames"):
        try:
            info[k] = int(st.get(k))
        except (TypeError, ValueError):
            info[k] = None
    try:
        info["duration_s"] = float((data.get("format") or {}).get("duration"))
    except (TypeError, ValueError):
        info["duration_s"] = None
    info["avg_fps"] = parse_rate(info.get("avg_frame_rate"))
    return info


def frame_rgb(video: str, t: float, png: Optional[str] = None) -> Tuple[int, int, bytes]:
    """Decode one frame at t seconds -> (w, h, rgb24 bytes); optionally save a PNG."""
    probe = ffprobe(video, count_frames=False)
    w, h = int(probe["width"]), int(probe["height"])
    t = max(0.0, t)
    base = [ffmpeg_path(), "-v", "error", "-ss", f"{t:.3f}", "-i", video, "-frames:v", "1"]
    flags = CREATE_NO_WINDOW if IS_WIN else 0
    if png:
        os.makedirs(os.path.dirname(os.path.abspath(png)), exist_ok=True)
        subprocess.run(base + ["-y", png], capture_output=True, timeout=120, creationflags=flags)
    proc = subprocess.run(base + ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=120,
                          creationflags=flags)
    data = proc.stdout
    if proc.returncode != 0 or len(data) < w * h * 3:
        raise RuntimeError(f"could not decode a frame at {t:.2f}s of {video}: "
                           + proc.stderr.decode("utf-8", "replace")[-300:])
    return w, h, data[: w * h * 3]


def iter_frames_rgb(video: str, scale_div: int = 1) -> Iterable[Tuple[int, int, int, bytes]]:
    """Decode EVERY frame -> (index, w, h, rgb24). ``scale_div`` > 1 samples every
    n-th pixel (nearest neighbour: saturated colours are not blended away)."""
    probe = ffprobe(video, count_frames=False)
    w, h = int(probe["width"]), int(probe["height"])
    cmd = [ffmpeg_path(), "-v", "error", "-i", video, "-map", "0:v:0", "-fps_mode", "passthrough"]
    if scale_div > 1:
        w, h = max(2, w // scale_div), max(2, h // scale_div)
        cmd += ["-vf", f"scale={w}:{h}:flags=neighbor"]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    size = w * h * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            creationflags=CREATE_NO_WINDOW if IS_WIN else 0)
    assert proc.stdout is not None
    n = 0
    try:
        while True:
            chunks, got = [], 0
            while got < size:
                part = proc.stdout.read(size - got)
                if not part:
                    break
                chunks.append(part)
                got += len(part)
            if got < size:
                break
            yield n, w, h, b"".join(chunks)
            n += 1
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.wait(60)
        except Exception:
            kill_tree(proc.pid)


def prune_videos(out: str) -> List[str]:
    """Delete the .mp4 files of a full-desktop recording so CI artifacts keep only
    the reports (the runner desktop is not part of what we publish)."""
    removed = []
    for d, _, fs in os.walk(out):
        for f in fs:
            if f.lower().endswith(".mp4"):
                try:
                    os.remove(os.path.join(d, f))
                    removed.append(os.path.relpath(os.path.join(d, f), out))
                except OSError:
                    pass
    return removed


def cursor_pixel_check(out: str, video: str, screen_pt: Tuple[int, int], parked_ms: float, bg_rgb: Tuple[int, int, int],
                       png: Optional[str] = None) -> Dict[str, Any]:
    """Look for the drawn cursor where it was parked over a uniform ``bg_rgb`` area.

    Compares the pixels at the cursor tip with a reference patch of the same
    size 120 px to the left (same background). ``ok`` when the cursor is there.
    """
    probe = ffprobe(video, count_frames=False)
    origin, scale, cap = capture_geometry(out)
    t0 = first_frame_wall_ms(out)
    dur = probe["duration_s"] or 1.0
    t = ((parked_ms + 600 - t0) / 1000.0) if t0 else dur - 0.5
    t = min(max(0.0, t), max(0.0, dur - 0.2))
    w, h, rgb = frame_rgb(video, t, png)
    cx, cy = screen_to_video((screen_pt[0], screen_pt[1], screen_pt[0] + 1, screen_pt[1] + 1), origin, scale)[:2]
    changed = count_differing(rgb, w, h, (cx - 2, cy - 2, cx + 24, cy + 30), bg_rgb)
    ref = count_differing(rgb, w, h, (cx - 120, cy - 2, cx - 94, cy + 30), bg_rgb)
    return {"ok": changed >= 12 and changed > ref + 8, "at_cursor": changed, "reference": ref, "t": round(t, 2),
            "video_xy": [cx, cy], "cursor_in_video": cap.get("cursor_in_video")}


def capture_geometry(out: str) -> Tuple[Tuple[int, int], Tuple[float, float], Dict[str, Any]]:
    """Screen origin of the video and screen->video scale, from raw/capture.json."""
    cap_path = os.path.join(out, "raw", "capture.json")
    cap = load_json(cap_path) if os.path.isfile(cap_path) else {}
    rect = cap.get("capture_rect") or {}
    if rect and all(k in rect for k in ("x", "y", "width", "height")):
        origin = (int(rect["x"]), int(rect["y"]))
        size = (int(rect["width"]), int(rect["height"]))
    else:
        g = win().virtual_screen()
        origin, size = (g[0], g[1]), (g[2], g[3])
    probe = ffprobe(video_file(out), count_frames=False)
    sx = (probe["width"] or size[0]) / max(1, size[0])
    sy = (probe["height"] or size[1]) / max(1, size[1])
    return origin, (sx, sy), cap


def first_frame_wall_ms(out: str) -> Optional[float]:
    meta_path = os.path.join(out, "metadata.json")
    if not os.path.isfile(meta_path):
        return None
    clips = load_json(meta_path).get("clips") or []
    try:
        return float(clips[0]["source_start_ms"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def copy_small_dataset(src: str, dst_root: str, max_mb: float = 40.0) -> Optional[str]:
    """Keep the produced dataset as a CI artifact when it is small enough."""
    if not os.path.isdir(src):
        return None
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(src) for f in fs)
    if size > max_mb * 1e6:
        return None
    dst = os.path.join(dst_root, os.path.basename(src))
    if os.path.abspath(dst) != os.path.abspath(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    return dst


# =========================================================================== Win32
_WIN = None


def win() -> "_Win32":
    global _WIN
    if _WIN is None:
        _WIN = _Win32()
    return _WIN


class _Win32:
    VK_F9, VK_F10, VK_RETURN, VK_MENU, VK_SHIFT, VK_CONTROL = 0x78, 0x79, 0x0D, 0x12, 0x10, 0x11

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ct, self.wt = ctypes, wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            self.dwmapi = ctypes.WinDLL("dwmapi")
        except OSError:
            self.dwmapi = None
        u, k = self.user32, self.kernel32
        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        class CURSORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hCursor", wintypes.HANDLE),
                        ("ptScreenPos", wintypes.POINT)]

        self.INPUT, self.MOUSEINPUT, self.KEYBDINPUT, self.CURSORINFO = INPUT, MOUSEINPUT, KEYBDINPUT, CURSORINFO
        self.WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        u.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        u.SendInput.restype = wintypes.UINT
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        u.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        u.MapVirtualKeyW.restype = wintypes.UINT
        u.VkKeyScanW.argtypes = [wintypes.WCHAR]
        u.VkKeyScanW.restype = ctypes.c_short
        u.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        u.GetForegroundWindow.restype = wintypes.HWND
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.BringWindowToTop.argtypes = [wintypes.HWND]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.UINT]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.EnumWindows.argtypes = [self.WNDENUMPROC, wintypes.LPARAM]
        u.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        k.GetCurrentThreadId.restype = wintypes.DWORD
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateProcess.restype = wintypes.BOOL
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ULONG_PTR), ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                        ("szExeFile", wintypes.WCHAR * 260)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        self.PROCESSENTRY32W, self.MONITORINFO = PROCESSENTRY32W, MONITORINFO
        k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        u.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        u.MonitorFromPoint.restype = wintypes.HMONITOR
        u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]

    # ------------------------------------------------------------------ setup
    def prepare(self) -> None:
        """Per-monitor DPI awareness: SendInput / window rects in physical pixels,
        the same coordinate space the recorder logs and captures."""
        try:
            if self.user32.SetProcessDpiAwarenessContext(self.ct.c_void_p(-4)):
                return
        except Exception:
            pass
        try:
            self.ct.WinDLL("shcore").SetProcessDpiAwareness(2)
        except Exception:
            pass

    # ------------------------------------------------------------------ geometry
    def metric(self, i: int) -> int:
        return int(self.user32.GetSystemMetrics(i))

    def virtual_screen(self) -> Tuple[int, int, int, int]:
        return self.metric(76), self.metric(77), self.metric(78), self.metric(79)

    def primary_size(self) -> Tuple[int, int]:
        return self.metric(0), self.metric(1)

    def primary_monitor_rect(self) -> Tuple[int, int, int, int]:
        """(x, y, w, h) of the primary monitor in physical pixels (always at 0,0)."""
        mi = self.MONITORINFO()
        mi.cbSize = self.ct.sizeof(self.MONITORINFO)
        hmon = self.user32.MonitorFromPoint(self.wt.POINT(0, 0), 1)   # MONITOR_DEFAULTTOPRIMARY
        if hmon and self.user32.GetMonitorInfoW(hmon, self.ct.byref(mi)):
            r = mi.rcMonitor
            return int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)
        pw, ph = self.primary_size()
        return 0, 0, pw, ph

    def monitor_count(self) -> int:
        return self.metric(80)   # SM_CMONITORS

    def cursor_pos(self) -> Tuple[int, int]:
        pt = self.wt.POINT()
        self.user32.GetCursorPos(self.ct.byref(pt))
        return pt.x, pt.y

    def cursor_info(self) -> Dict[str, Any]:
        ci = self.CURSORINFO()
        ci.cbSize = self.ct.sizeof(self.CURSORINFO)
        ok = bool(self.user32.GetCursorInfo(self.ct.byref(ci)))
        return {"ok": ok, "flags": int(ci.flags), "showing": bool(ci.flags & 1), "suppressed": bool(ci.flags & 2),
                "pos": [int(ci.ptScreenPos.x), int(ci.ptScreenPos.y)]}

    # ------------------------------------------------------------------ input
    def _send(self, inputs: List[Any]) -> None:
        arr = (self.INPUT * len(inputs))(*inputs)
        sent = self.user32.SendInput(len(inputs), arr, self.ct.sizeof(self.INPUT))
        if sent != len(inputs):
            raise OSError(f"SendInput sent {sent}/{len(inputs)} (error {self.ct.get_last_error()}); "
                          "is the desktop locked or the target elevated?")

    def _mouse(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> Any:
        inp = self.INPUT()
        inp.type = 0
        inp.mi = self.MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
        return inp

    def _key(self, vk: int, up: bool) -> Any:
        scan = self.user32.MapVirtualKeyW(vk, 0)
        flags = 0x0002 if up else 0
        if vk in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0xA3, 0xA5):
            flags |= 0x0001   # KEYEVENTF_EXTENDEDKEY
        inp = self.INPUT()
        inp.type = 1
        inp.ki = self.KEYBDINPUT(vk, scan, flags, 0, 0)
        return inp

    def move_abs(self, x: int, y: int) -> None:
        vx, vy, vw, vh = self.virtual_screen()
        nx = int(round((x - vx) * 65535 / max(1, vw - 1)))
        ny = int(round((y - vy) * 65535 / max(1, vh - 1)))
        self._send([self._mouse(0x0001 | 0x8000 | 0x4000, nx, ny)])   # MOVE | ABSOLUTE | VIRTUALDESK
        cx, cy = self.cursor_pos()
        if abs(cx - x) > 2 or abs(cy - y) > 2:   # rounding at odd virtual-desktop sizes
            self.user32.SetCursorPos(int(x), int(y))

    def move_to(self, x: int, y: int, duration: float = 0.25, steps: int = 12) -> None:
        sx, sy = self.cursor_pos()
        steps = max(1, steps)
        for i in range(1, steps + 1):
            self.move_abs(int(sx + (x - sx) * i / steps), int(sy + (y - sy) * i / steps))
            time.sleep(duration / steps)

    def click(self, button: str = "left", hold: float = 0.06) -> None:
        down, up = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}[button]
        self._send([self._mouse(down)])
        time.sleep(hold)
        self._send([self._mouse(up)])
        time.sleep(0.05)

    def wheel(self, notches: int = -1) -> None:
        self._send([self._mouse(0x0800, data=120 * notches)])
        time.sleep(0.05)

    def key_down(self, vk: int) -> None:
        self._send([self._key(vk, False)])

    def key_up(self, vk: int) -> None:
        self._send([self._key(vk, True)])

    def tap(self, vk: int, hold: float = 0.04) -> None:
        self.key_down(vk)
        time.sleep(hold)
        self.key_up(vk)
        time.sleep(0.03)

    def vk_for_char(self, ch: str) -> Tuple[int, bool]:
        res = self.user32.VkKeyScanW(ch)
        if res == -1:
            raise ValueError(f"no key for {ch!r} in the current layout")
        return res & 0xFF, bool(res & 0x100)

    def type_text(self, text: str, delay: float = 0.04) -> List[int]:
        """Type with real virtual-key codes (what the low-level hook sees for a person)."""
        vks = []
        for ch in text:
            vk, shift = self.vk_for_char(ch)
            if shift:
                self.key_down(self.VK_SHIFT)
            self.tap(vk)
            if shift:
                self.key_up(self.VK_SHIFT)
            vks.append(vk)
            time.sleep(delay)
        return vks

    def release_all_input(self) -> None:
        """Never leave a modifier or the left button logically held after a scenario."""
        for vk in (self.VK_SHIFT, self.VK_CONTROL, self.VK_MENU, 0x5B):
            if self.user32.GetAsyncKeyState(vk) & 0x8000:
                self.key_up(vk)
        if self.user32.GetAsyncKeyState(0x01) & 0x8000:
            self._send([self._mouse(0x0004)])

    # ------------------------------------------------------------------ windows
    def exe_of_pid(self, pid: int) -> str:
        h = self.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = self.ct.create_unicode_buffer(32768)
            size = self.wt.DWORD(32768)
            if self.kernel32.QueryFullProcessImageNameW(h, 0, buf, self.ct.byref(size)):
                return os.path.basename(buf.value)
            return ""
        finally:
            self.kernel32.CloseHandle(h)

    def pid_of(self, hwnd: int) -> int:
        pid = self.wt.DWORD(0)
        self.user32.GetWindowThreadProcessId(hwnd, self.ct.byref(pid))
        return int(pid.value)

    def title_of(self, hwnd: int) -> str:
        buf = self.ct.create_unicode_buffer(512)
        self.user32.GetWindowTextW(hwnd, buf, 512)
        return buf.value

    def class_of(self, hwnd: int) -> str:
        buf = self.ct.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def top_windows(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []

        def cb(hwnd: int, _lp: int) -> bool:
            if self.user32.IsWindowVisible(hwnd):
                pid = self.pid_of(hwnd)
                out.append({"hwnd": int(hwnd), "pid": pid, "title": self.title_of(hwnd),
                            "class": self.class_of(hwnd)})
            return True

        self.user32.EnumWindows(self.WNDENUMPROC(cb), 0)
        return out

    def find_window(self, predicate: Callable[[Dict[str, Any]], bool], timeout: float = 15.0) -> Optional[Dict[str, Any]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for w in self.top_windows():
                if w["title"] and predicate(w):
                    return w
            time.sleep(0.25)
        return None

    def window_rect(self, hwnd: int) -> Box:
        """Visible bounds (DWM extended frame, without the invisible resize border)."""
        rect = self.wt.RECT()
        if self.dwmapi is not None:
            hr = self.dwmapi.DwmGetWindowAttribute(self.wt.HWND(hwnd), 9, self.ct.byref(rect), self.ct.sizeof(rect))
            if hr == 0 and rect.right > rect.left:
                return rect.left, rect.top, rect.right, rect.bottom
        self.user32.GetWindowRect(hwnd, self.ct.byref(rect))
        return rect.left, rect.top, rect.right, rect.bottom

    def client_rect(self, hwnd: int) -> Box:
        rect = self.wt.RECT()
        self.user32.GetClientRect(hwnd, self.ct.byref(rect))
        pt = self.wt.POINT(0, 0)
        self.user32.ClientToScreen(hwnd, self.ct.byref(pt))
        return pt.x, pt.y, pt.x + rect.right, pt.y + rect.bottom

    def place(self, hwnd: int, x: int, y: int, w: int, h: int) -> None:
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
        self.user32.ShowWindow(hwnd, 1)       # SW_SHOWNORMAL (un-maximize)
        self.user32.SetWindowPos(hwnd, None, x, y, w, h, 0x0004 | 0x0010)   # NOZORDER | NOACTIVATE

    def foreground(self) -> int:
        return int(self.user32.GetForegroundWindow() or 0)

    def force_foreground(self, hwnd: int, click_at: Optional[Tuple[int, int]] = None, tries: int = 6) -> bool:
        """SetForegroundWindow with the usual workarounds for the foreground lock:
        AttachThreadInput to the current foreground thread, then (last resort) a
        real click on a harmless point of the window."""
        u = self.user32
        for attempt in range(tries):
            if self.foreground() == hwnd:
                return True
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 9)
            me = self.kernel32.GetCurrentThreadId()
            fg = self.foreground()
            fg_tid = u.GetWindowThreadProcessId(fg, None) if fg else 0
            tgt_tid = u.GetWindowThreadProcessId(hwnd, None)
            attached = []
            try:
                for tid in {fg_tid, tgt_tid}:
                    if tid and tid != me and u.AttachThreadInput(me, tid, True):
                        attached.append(tid)
                u.BringWindowToTop(hwnd)
                u.SetForegroundWindow(hwnd)
            finally:
                for tid in attached:
                    u.AttachThreadInput(me, tid, False)
            time.sleep(0.2 + 0.1 * attempt)
            if self.foreground() == hwnd:
                return True
            if click_at is not None and attempt >= 1:
                self.move_to(*click_at, duration=0.1, steps=4)
                self.click()
                time.sleep(0.3)
        return self.foreground() == hwnd

    def close_window(self, hwnd: int) -> None:
        self.user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE

    SW_MAXIMIZE, SW_MINIMIZE, SW_RESTORE = 3, 6, 9

    def show(self, hwnd: int, cmd: int) -> None:
        self.user32.ShowWindow(hwnd, cmd)

    # ------------------------------------------------------------------ processes
    def processes(self) -> List[Tuple[int, int, str]]:
        """(pid, parent pid, exe) of every process (Toolhelp snapshot)."""
        snap = self.kernel32.CreateToolhelp32Snapshot(0x00000002, 0)   # TH32CS_SNAPPROCESS
        if not snap or snap == self.ct.c_void_p(-1).value:
            return []
        out: List[Tuple[int, int, str]] = []
        try:
            pe = self.PROCESSENTRY32W()
            pe.dwSize = self.ct.sizeof(self.PROCESSENTRY32W)
            ok = self.kernel32.Process32FirstW(snap, self.ct.byref(pe))
            while ok:
                out.append((int(pe.th32ProcessID), int(pe.th32ParentProcessID), pe.szExeFile))
                ok = self.kernel32.Process32NextW(snap, self.ct.byref(pe))
        finally:
            self.kernel32.CloseHandle(snap)
        return out

    def descendants(self, pid: int) -> List[Tuple[int, str]]:
        table = self.processes()
        found: List[Tuple[int, str]] = []
        parents = {pid}
        changed = True
        while changed:
            changed = False
            for child, parent, exe in table:
                if parent in parents and child not in parents and child != parent:
                    parents.add(child)
                    found.append((child, exe))
                    changed = True
        return found

    def terminate(self, pid: int, code: int = 1) -> bool:
        """TerminateProcess: no cleanup, no atexit, no finally - a hard crash."""
        h = self.kernel32.OpenProcess(0x0001 | 0x1000, False, pid)   # PROCESS_TERMINATE | QUERY_LIMITED
        if not h:
            return False
        try:
            return bool(self.kernel32.TerminateProcess(h, code))
        finally:
            self.kernel32.CloseHandle(h)


# =========================================================================== helper windows
class HelperWindow:
    """A Tk window in a separate process (tests_windows/helper_window.py)."""

    def __init__(self, info: Dict[str, Any], proc: subprocess.Popen):
        self.info, self.proc = info, proc
        self.hwnd = int(info["hwnd"])
        self.pid = int(info["pid"])

    @property
    def rect(self) -> Box:
        return win().window_rect(self.hwnd)

    def center(self) -> Tuple[int, int]:
        x0, y0, x1, y1 = self.rect
        return (x0 + x1) // 2, (y0 + y1) // 2


def spawn_helper(procs: Procs, out_dir: str, name: str, *, color: str, x: int, y: int, w: int, h: int,
                 title: str, topmost: bool = False, popup: bool = False, text: bool = False,
                 label: str = "", timeout: float = 600, pythonw: bool = False) -> HelperWindow:
    info_file = os.path.join(out_dir, f"{name}.window.json")
    if os.path.exists(info_file):
        os.remove(info_file)
    exe = sys.executable
    if pythonw:
        cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = cand if os.path.isfile(cand) else exe
    cmd = [exe, os.path.join(HERE, "helper_window.py"), "--title", title, "--color", color,
           "--x", str(x), "--y", str(y), "--w", str(w), "--h", str(h), "--info-file", info_file,
           "--timeout", str(int(timeout))]
    if topmost:
        cmd.append("--topmost")
    if popup:
        cmd.append("--popup")
    if text:
        cmd.append("--text")
    if label:
        cmd += ["--label", label]
    p = procs.add(subprocess.Popen(cmd, cwd=ROOT, env=child_env(), stdin=subprocess.DEVNULL,
                                   creationflags=CREATE_NO_WINDOW if IS_WIN else 0))
    deadline = time.time() + 30
    while time.time() < deadline:
        if os.path.isfile(info_file):
            try:
                info = load_json(info_file)
                if info.get("hwnd"):
                    time.sleep(0.3)
                    return HelperWindow(info, p)
            except (ValueError, OSError):
                pass
        if p.poll() is not None:
            raise RuntimeError(f"helper window {name} exited with {p.returncode}")
        time.sleep(0.2)
    raise RuntimeError(f"helper window {name} did not report its handle")
