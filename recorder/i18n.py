"""Tiny bilingual (中文 / English) helper shared by recorder, packager and GUI.

Usage -- keep both languages next to each other at the call site, so nobody
has to maintain a separate key catalogue and parallel edits never collide::

    from recorder.i18n import L
    label = L("停止并打包", "Stop & package")
    msg = L("已录制 {n} 帧", "Recorded {n} frames", n=42)

The active language is process-wide.  It is chosen, in order, from:
``DSTUDIO_LANG`` env var -> settings.json ``language`` -> Windows UI language
(Chinese UI -> ``zh``) -> ``en``.  The GUI calls :func:`set_language` when the
user flips the 中/EN switch and re-renders its labels via :func:`on_change`
(windows that close again unsubscribe with :func:`off_change`).

Standard library only; safe to import on any OS.
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Callable, List

SUPPORTED = ("zh", "en")
_lang: str = ""
_listeners: List[Callable[[str], None]] = []
_listeners_lock = threading.Lock()


def _system_language() -> str:
    if sys.platform == "win32":
        try:
            import ctypes

            # LANGID: low 10 bits = primary language; LANG_CHINESE = 0x04
            # (covers zh-CN, zh-TW, zh-HK, zh-SG ...).
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "zh" if (lang_id & 0x3FF) == 0x04 else "en"
        except Exception:
            return "en"
    for var in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        env = (os.environ.get(var) or "").strip().lower()
        if env and env not in ("c", "posix", "c.utf-8"):
            return "zh" if env.startswith("zh") else "en"
    return "en"


def _initial_language() -> str:
    env = (os.environ.get("DSTUDIO_LANG") or "").strip().lower()
    if env in SUPPORTED:
        return env
    try:
        from .settings import load_settings

        saved = str(load_settings().get("language") or "").lower()
        if saved in SUPPORTED:
            return saved
    except Exception:
        pass
    return _system_language()


def get_language() -> str:
    global _lang
    if not _lang:
        _lang = _initial_language()
    return _lang


def reset() -> None:
    """Forget the cached language so the next call re-reads env/settings/OS.

    Used by tests and after settings were replaced on disk; listeners stay.
    """
    global _lang
    _lang = ""


def set_language(lang: str, persist: bool = True) -> str:
    """Switch language for the whole process; notifies listeners.

    ``persist=False`` is for one-off overrides (``--lang`` on the command line,
    CI screenshots) that must not change the user's saved preference.
    """
    global _lang
    lang = (lang or "").strip().lower()
    if lang not in SUPPORTED:
        raise ValueError(f"unsupported language {lang!r}; choose one of {SUPPORTED}")
    _lang = lang
    if persist:
        try:
            from .settings import update_settings

            update_settings(language=lang)
        except Exception:
            pass
    with _listeners_lock:
        listeners = list(_listeners)
    for cb in listeners:
        try:
            cb(lang)
        except Exception:
            pass
    return lang


def toggle_language() -> str:
    return set_language("en" if get_language() == "zh" else "zh")


def on_change(callback: Callable[[str], None]) -> Callable[[str], None]:
    """Call ``callback(lang)`` after every :func:`set_language`.

    Returns the callback so it can be handed to :func:`off_change` later.
    """
    with _listeners_lock:
        _listeners.append(callback)
    return callback


def off_change(callback: Callable[[str], None]) -> None:
    """Unsubscribe a callback registered with :func:`on_change` (no-op if absent)."""
    with _listeners_lock:
        try:
            _listeners.remove(callback)
        except ValueError:
            pass


def _format(text: str, fmt: dict) -> str:
    if not fmt:
        return text
    try:
        return text.format(**fmt)
    except (KeyError, IndexError, ValueError):
        # A typo in one translation must never crash the UI; show it raw.
        return text


def L(zh: str, en: str, **fmt) -> str:
    """Return the string for the active language, ``str.format``-ed with fmt."""
    return _format(zh if get_language() == "zh" else en, fmt)


def both(zh: str, en: str, **fmt) -> dict:
    """Both renderings, for reports (validation.json) read by either audience."""
    return {"zh": _format(zh, fmt), "en": _format(en, fmt)}


def safe_stdio() -> None:
    """Make print() of Chinese text safe on any console / pipe.

    The installed app starts Python with ``-E`` (PYTHONIOENCODING is ignored),
    so on an English-locale Windows a pipe is cp1252 and printing a bilingual
    report raised UnicodeEncodeError after the work was done.  Pipes and files
    get UTF-8; a real console keeps its code page but never raises.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            interactive = stream.isatty()
        except Exception:
            interactive = False
        try:
            if interactive:
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
