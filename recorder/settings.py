"""Per-user settings (language, fps, last app, data dir...) as one JSON file.

Location: ``%APPDATA%\\DStudio\\settings.json`` on Windows,
``~/.config/dstudio/settings.json`` elsewhere; override with
``DSTUDIO_SETTINGS`` (tests point this at a temp file).

Values read from disk are sanitised against :data:`DEFAULTS` (a hand-edited or
half-written file can never hand the GUI a string fps or an unknown backend).
Writes are atomic (temp file + ``os.replace``) and serialised in-process.
Standard library only.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import time
from typing import Any, Dict

_lock = threading.RLock()

DEFAULTS: Dict[str, Any] = {
    "language": "",          # "" = follow Windows UI language
    "fps": 30,               # Markov samples are 30 fps CFR; 60 is allowed
    "quality": 23,           # libx264 CRF 23 = Markov sample encoder setting
    "capture_mode": "desktop",   # desktop | app
    "app_exe": "",           # e.g. "SLDWORKS.exe" when capture_mode == "app"
    "app_name": "",          # friendly name shown on the bar, e.g. "SOLIDWORKS"
    "app_allow": [],         # extra exe names shown in app-only mode (IME, helpers)
    # app-only mode: full-screen see-through overlays (screen dimmer, watermark) that
    # show nothing private; unknown ones black out the whole video (--app-overlay-allow)
    "app_overlay_allow": [],
    "auto_pause_other_apps": True,
    "draw_cursor": True,     # Markov clips have the OS cursor drawn in
    "backend": "auto",
    "autopack": True,
    # CAD export format: True = the Markov CAD sample (10 files; references.zip /
    # gold.zip can be built from task_files/ + output_files/), False = the legacy
    # task_set / clip_set / evidence layout (settings panel -> DELIVERY).
    "markov_delivery": True,
    "data_dir": "",          # "" = recorder.paths default
    "viewer_port": 8000,
    "title": "",             # last recording title (pre-filled next time)
    "export_dir": "",        # last folder chosen for exports
    "bar_pos": [],           # [x, y] of the floating bar, [] = top centre
    # rec-* folder names whose crash-recovery offer was answered "No" (never asked again)
    "recovery_dismissed": [],
}

LANGUAGES = ("", "zh", "en")
CAPTURE_MODES = ("desktop", "app")
BACKENDS = ("auto", "dxcam", "gdi", "gdigrab")


def settings_path() -> str:
    override = os.environ.get("DSTUDIO_SETTINGS")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "DStudio", "settings.json")
    return os.path.join(os.path.expanduser("~"), ".config", "dstudio", "settings.json")


# ------------------------------------------------------------------ sanitising
_INVALID = object()


def _bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
    return _INVALID


def _int(lo: int, hi: int):
    def check(value: Any) -> Any:
        if isinstance(value, bool) or value is None:
            return _INVALID
        try:
            number = int(float(value))
        except (TypeError, ValueError, OverflowError):
            return _INVALID
        return number if lo <= number <= hi else _INVALID
    return check


def _text(limit: int):
    def check(value: Any) -> Any:
        if value is None:
            return ""
        return value.strip()[:limit] if isinstance(value, str) else _INVALID
    return check


def _choice(options: tuple):
    def check(value: Any) -> Any:
        if not isinstance(value, str):
            return _INVALID
        v = value.strip().lower()
        return v if v in options else _INVALID
    return check


def _exe_list(value: Any) -> Any:
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    if not isinstance(value, (list, tuple)):
        return _INVALID
    out = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip())
    return out


def _pos(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and len(value) == 0:
        return []
    if (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
            and all(abs(v) < 100000 for v in value)):
        return [int(value[0]), int(value[1])]
    return _INVALID


_SANITIZERS = {
    "language": _choice(LANGUAGES),
    "fps": _int(1, 120),
    "quality": _int(0, 51),
    "capture_mode": _choice(CAPTURE_MODES),
    "app_exe": _text(260),
    "app_name": _text(260),
    "app_allow": _exe_list,
    "app_overlay_allow": _exe_list,
    "auto_pause_other_apps": _bool,
    "draw_cursor": _bool,
    "backend": _choice(BACKENDS),
    "autopack": _bool,
    "markov_delivery": _bool,
    "data_dir": _text(4096),
    "viewer_port": _int(1, 65535),
    "title": _text(300),
    "export_dir": _text(4096),
    "bar_pos": _pos,
    "recovery_dismissed": _exe_list,     # plain names: same clean-up as an exe list
}


def sanitize_value(key: str, value: Any) -> Any:
    """``value`` coerced for ``key``; raises ValueError if it is unusable."""
    if key not in DEFAULTS:
        raise KeyError(key)
    check = _SANITIZERS.get(key)
    result = check(value) if check else value
    if result is _INVALID:
        raise ValueError(f"invalid value for setting {key!r}: {value!r}")
    return result


def sanitize_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a fresh dict with every DEFAULTS key, each of a valid type/range
    (invalid or missing values fall back to the default)."""
    src = data if isinstance(data, dict) else {}
    out = copy.deepcopy(DEFAULTS)
    for key in DEFAULTS:
        if key in src:
            try:
                out[key] = sanitize_value(key, src[key])
            except ValueError:
                pass
    return out


# ------------------------------------------------------------------- file I/O
def _read_raw() -> Dict[str, Any]:
    try:
        with open(settings_path(), encoding="utf-8-sig") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            return saved
    except (OSError, ValueError):
        pass
    return {}


def load_settings() -> Dict[str, Any]:
    """All settings (defaults filled in, values sanitised). Never raises."""
    with _lock:
        saved = _read_raw()
    merged = copy.deepcopy(DEFAULTS)
    merged.update({k: v for k, v in saved.items() if k in DEFAULTS})
    return sanitize_settings(merged)


def save_settings(data: Dict[str, Any]) -> None:
    path = settings_path()
    folder = os.path.dirname(os.path.abspath(path))
    clean = sanitize_settings({k: data.get(k, v) for k, v in DEFAULTS.items()})
    with _lock:
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(clean, fh, ensure_ascii=False, indent=2)
                # a power cut right after os.replace must not leave an empty
                # settings.json (that would silently drop a custom data_dir)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            # Windows: os.replace fails while another process (antivirus,
            # a second D Studio window) has the file open -- retry briefly.
            for attempt in range(10):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.05)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def update_settings(**changes: Any) -> Dict[str, Any]:
    """Merge ``changes`` into the saved settings and return the result.

    Unknown keys and invalid values are ignored (the previous value is kept),
    so a bad value can never wipe a good one.
    """
    with _lock:
        data = load_settings()
        for key, value in changes.items():
            if key not in DEFAULTS:
                continue
            try:
                data[key] = sanitize_value(key, value)
            except ValueError:
                pass
        save_settings(data)
        return copy.deepcopy(data)
