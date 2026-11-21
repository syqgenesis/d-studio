"""Where things live: app root, recordings (data dir), bundled FFmpeg.

Two layouts are supported:

* **dev / unzipped folder** (``python -m recorder gui`` from the repo):
  recordings go to ``<root>/data`` like before.
* **installed** (Setup.exe writes an ``INSTALLED`` marker next to the code):
  the install folder may be read-only/hidden, so recordings default to
  ``Documents\\DStudio`` and FFmpeg comes from ``<root>/ffmpeg/``.

Every lookup can be overridden with an env var (tests and CI use these):
``DSTUDIO_DATA_DIR``, ``DSTUDIO_FFMPEG``.
Standard library only.
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_installed() -> bool:
    return os.path.isfile(os.path.join(ROOT, "INSTALLED"))


def _documents_dir() -> str:
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_PERSONAL = 5 (Documents), honours OneDrive/folder redirection
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                return buf.value
        except Exception:
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def data_dir(create: bool = True) -> str:
    """Folder holding ``rec-*`` recording folders (one dataset per folder)."""
    path = os.environ.get("DSTUDIO_DATA_DIR", "").strip()
    if not path:
        try:
            from .settings import load_settings

            path = str(load_settings().get("data_dir") or "").strip()
        except Exception:
            path = ""
    if not path:
        path = (os.path.join(_documents_dir(), "DStudio") if is_installed()
                else os.path.join(ROOT, "data"))
    path = os.path.abspath(path)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _first_existing(candidates):
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def ffmpeg_path() -> str:
    """Absolute path to ffmpeg if found, else the bare name ``ffmpeg``."""
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    found = _first_existing([
        os.environ.get("DSTUDIO_FFMPEG", "").strip(),
        os.path.join(ROOT, "ffmpeg", exe),
        os.path.join(ROOT, "ffmpeg", "bin", exe),
        r"D:\ffmpeg\bin\ffmpeg.exe" if sys.platform == "win32" else "",
    ])
    return found or shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_path() -> str:
    ff = ffmpeg_path()
    exe = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
    sibling = os.path.join(os.path.dirname(ff), exe) if os.path.dirname(ff) else ""
    return _first_existing([sibling]) or shutil.which("ffprobe") or "ffprobe"
