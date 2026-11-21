"""App-only capture: keep the chosen application's pixels, paint the rest black.

The video stays the full monitor (Markov clips are full-screen), but every
pixel that does not belong to the target application -- other apps, chat
pop-ups (WeChat / QQ / DingTalk are separate processes), Windows toasts
(``Windows.UI.Core.CoreWindow`` in ShellExperienceHost), taskbar, desktop --
is painted black *before* the frame reaches the encoder.

How the mask is built (all coordinates are physical px, virtual screen):

1. enumerate top-level windows in z-order (``EnumWindows`` = top -> bottom);
   skip invisible, minimized and DWM-cloaked ones (other virtual desktops,
   suspended UWP apps); window rectangles come from
   ``DWMWA_EXTENDED_FRAME_BOUNDS`` (no invisible resize borders, not
   DPI-virtualized);
2. classify each window: target exe, a process descended from the target
   (parent-PID tree, creation-time checked against PID reuse, image under the
   target's install root), owned (owner chain) by a target window, or a user
   allow-listed exe -> *reveal*; IME helpers (TextInputHost, ctfmon...) ->
   *reveal* only while the target is in front and only candidate-sized
   windows (absolute, DPI-scaled caps: Win+V clipboard history and the emoji
   panel stay black on any display); our own window excluded from capture
   (``WDA_EXCLUDEFROMCAPTURE``) or a full-screen click-through overlay
   (``WS_EX_LAYERED|WS_EX_TRANSPARENT``) of a known-benign exe (GPU / FPS
   HUDs) -> *skip* (not occluding pixels); anything else -> *cover* --
   unknown full-screen click-through windows included (fail closed);
3. walk bottom -> top: reveals add to the visible region, covers subtract;
4. black = capture_rect minus visible region, as a short list of rectangles
   (canonical horizontal bands) painted by numpy slice assignment or GDI
   ``PatBlt``.

While a taskbar thumbnail / task-switcher ("Peek") host is visible, or desktop
peek is active (Win+, or the Show-desktop button), DWM can draw another app's
window -- or the desktop -- over the target with no change to the window list
or the foreground, so the whole frame is black.

Window rects are *logical*: DWM keeps animating maximize / snap / restore /
minimize / fades for ~150-300 ms after they change.  The hold is therefore
time-based and asymmetric: newly covered pixels are black at once, a pixel
that goes from black to revealed stays black for ``hold_ms`` (350 ms).  A
covering window that is minimized or restored animates between its rect and
its taskbar button -- outside every rect we know -- so that blacks the whole
frame for ``hold_ms``.  Every segment start (:meth:`AppMasker.begin_segment`)
re-arms the hold: the first clip with the last mask (or a black frame), every
later one (resume, crash restart, fallback) with a black frame.  The geometry
is recomputed only when the layout
signature (z-order, rects, roles) changes.  If window enumeration fails the
masker fails *closed* (whole frame black).

Everything above the ``Windows`` section is pure Python and unit-tested on
macOS/Linux with fake window lists; Win32 bindings are created lazily.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from .i18n import L

Rect = Tuple[int, int, int, int]          # (x0, y0, x1, y1), half-open
XYWH = Tuple[int, int, int, int]          # (x, y, width, height)

# IME / text-input helpers stay visible in app mode (candidate windows etc.),
# but only candidate-sized windows and only while the target is in front:
# TextInputHost also hosts Win+V clipboard history, the emoji panel and (on
# Windows 11) the candidate window of IME input typed into *other* apps.
DEFAULT_APP_ALLOW: Tuple[str, ...] = ("textinputhost.exe", "ctfmon.exe", "chsime.exe", "inputapp.exe")
IME_HELPER_EXES: FrozenSet[str] = frozenset(DEFAULT_APP_ALLOW)
# Candidate-sized = a candidate bar / composition window.  Absolute caps at
# 96 DPI (100 %), scaled by the monitor's DPI scale (area by its square): the
# Win+V clipboard-history / emoji panels have a fixed physical size at a given
# scale (~360x460 px at 100 %), so a share of the capture area would let them
# through on 4K / ultra-wide displays.  Unknown DPI -> 96 (smaller caps: fails
# closed on high-DPI displays).
IME_MAX_HEIGHT_PX = 240          # height cap ...
IME_MAX_AREA_PX = 62_000         # ... or area cap (~3 % of 1080p)
MAX_DPI_SCALE = 5.0              # Windows tops out at 500 %
DPI_REFRESH_MS = 2000.0
AUTO_PAUSE_DEBOUNCE_MS = 300.0
# A pixel that goes from black to revealed stays black this long: DWM
# maximize / snap / restore / minimize / fade animations run ~150-300 ms
# behind the window rects we read.
DEFAULT_HOLD_MS = 350.0
MAX_HELD_MASKS = 32

# Full-screen click-through windows of these exes are transparent overlays
# that are always present (GPU / FPS HUDs): covering them would black out every
# frame.  Every *other* full-screen click-through window is covered (fail
# closed): Game Bar widgets, meeting annotation layers, danmaku / lyrics
# overlays draw real content.  Extend with ``AppMasker(overlay_exes=...)``.
KNOWN_OVERLAY_EXES: Tuple[str, ...] = (
    "nvidia share.exe", "nvidia overlay.exe", "nvcontainer.exe", "nvsphelper64.exe",   # NVIDIA GeForce / App
    "amdow.exe", "radeonsoftware.exe", "amdrsserv.exe",                                # AMD Adrenalin overlay
    "rtss.exe",                                                                        # RivaTuner statistics
)

# Taskbar thumbnail / task-switcher ("Peek") hosts.  While one is visible DWM
# may draw another app's window (or the desktop) at full size over the target
# -- no window-list or foreground change -- so the whole frame is blacked out.
# ``(exe or "" for any exe, window class)``, compared lower-case.  Extend here
# or with ``AppMasker(peek_classes=...)``.  The Windows 11 XAML entries are the
# best-known candidates; confirm them with Spy++ on each new Windows build.
PEEK_WINDOW_CLASSES: Tuple[Tuple[str, str], ...] = (
    ("explorer.exe", "tasklistthumbnailwnd"),          # taskbar thumbnails, Windows 7 - 11
    ("explorer.exe", "multitaskingviewframe"),         # Windows 10 Alt+Tab / Task View (peeks on hover)
    ("explorer.exe", "taskswitcherwnd"),               # classic Alt+Tab
    ("explorer.exe", "xamlexplorerhostislandwindow"),  # Windows 11 Alt+Tab / Task View / XAML thumbnails
    ("explorer.exe", "xaml_windowedpopupclass"),       # Windows 11 XAML taskbar pop-ups (new thumbnails)
)

# A process the target launched inherits target status only when its image
# lies under the target's install root (:func:`install_root`).  On top of that,
# processes the target merely *launches* (a help link opens the browser, "open
# folder" starts Explorer, "open after export" starts Excel / a PDF reader, a
# share button opens a chat app) are general-purpose, often single-instance
# apps whose later windows have nothing to do with the target: they and their
# subtrees never inherit, wherever they are installed.
DESCENDANT_DENY_EXES: Tuple[str, ...] = (
    "explorer.exe", "chrome.exe", "msedge.exe", "firefox.exe", "iexplore.exe", "opera.exe", "brave.exe",
    "vivaldi.exe", "360se.exe", "360chrome.exe", "qqbrowser.exe", "sogouexplorer.exe", "2345explorer.exe",
    "wechat.exe", "weixin.exe", "wxwork.exe", "qq.exe", "tim.exe", "dingtalk.exe", "feishu.exe", "lark.exe",
    "teams.exe", "ms-teams.exe", "slack.exe", "telegram.exe", "discord.exe", "whatsapp.exe", "skype.exe",
    "zoom.exe", "wemeetapp.exe", "outlook.exe", "olk.exe", "thunderbird.exe", "foxmail.exe",
    "1password.exe", "keepass.exe", "keepassxc.exe", "bitwarden.exe", "mstsc.exe",
    # shells / terminals / editors
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wt.exe", "openconsole.exe",
    "notepad.exe", "notepad++.exe", "code.exe", "wordpad.exe", "write.exe", "mspaint.exe",
    # office suites / PDF readers / viewers
    "winword.exe", "excel.exe", "powerpnt.exe", "onenote.exe", "msaccess.exe", "mspub.exe", "visio.exe",
    "wps.exe", "et.exe", "wpp.exe", "wpspdf.exe", "acrobat.exe", "acrord32.exe", "foxitpdfreader.exe",
    "foxitreader.exe", "foxitphantompdf.exe", "sumatrapdf.exe", "applicationframehost.exe",
)
# A console host is a leaf: it shows its (qualifying) parent's console, so it
# is allowed when its parent qualifies, wherever its image lies (System32).
CONSOLE_HOST_EXES: FrozenSet[str] = frozenset({"conhost.exe"})
# Folders that hold many vendors' programs: an install root starts one level
# below them (C:\Program Files\<Vendor>, ...\AppData\Local\Programs\<App>,
# C:\Program Files\WindowsApps\<Package>).
_SHARED_PROGRAM_DIRS: FrozenSet[str] = frozenset({
    "program files", "program files (x86)", "programdata", "programs", "common files", "windowsapps"})
# Never an install root by itself (the grandparent fallback steps past them).
_BROAD_DIRS: FrozenSet[str] = _SHARED_PROGRAM_DIRS | frozenset({
    "windows", "system32", "syswow64", "users", "public", "desktop", "downloads", "documents",
    "appdata", "local", "locallow", "roaming", "temp", "tmp"})

WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
_CLICK_THROUGH = WS_EX_LAYERED | WS_EX_TRANSPARENT
WDA_EXCLUDEFROMCAPTURE = 0x11
# A click-through window covering at least this share of the capture area is
# treated as a transparent overlay (not occluding).  Small click-through
# windows (OSD pop-ups...) are still masked: privacy wins over cosmetics.
OVERLAY_MIN_SHARE = 0.5

ROLE_REVEAL, ROLE_COVER, ROLE_SKIP = "reveal", "cover", "skip"

FG_TARGET, FG_ALLOW, FG_OWN, FG_OTHER, FG_NONE = "target", "allow", "own", "other", "none"


# =========================================================================== geometry
def _valid(r: Rect) -> bool:
    return r[0] < r[2] and r[1] < r[3]


def xywh_to_rect(r: XYWH) -> Rect:
    x, y, w, h = (int(v) for v in r)
    return (x, y, x + w, y + h)


def rect_area(r: Rect) -> int:
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def total_area(rects: Iterable[Rect]) -> int:
    return sum(rect_area(r) for r in rects)


def intersect(a: Rect, b: Rect) -> Optional[Rect]:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None


def subtract_rect(r: Rect, cut: Rect) -> List[Rect]:
    """``r`` minus ``cut`` as at most 4 disjoint rectangles."""
    if not _valid(r):
        return []
    i = intersect(r, cut)
    if i is None:
        return [r]
    x0, y0, x1, y1 = r
    ix0, iy0, ix1, iy1 = i
    out: List[Rect] = []
    if y0 < iy0:
        out.append((x0, y0, x1, iy0))           # band above the cut
    if iy1 < y1:
        out.append((x0, iy1, x1, y1))           # band below the cut
    if x0 < ix0:
        out.append((x0, iy0, ix0, iy1))         # left of the cut
    if ix1 < x1:
        out.append((ix1, iy0, x1, iy1))         # right of the cut
    return out


def subtract_all(rects: Iterable[Rect], cut: Rect) -> List[Rect]:
    out: List[Rect] = []
    for r in rects:
        out.extend(subtract_rect(r, cut))
    return out


def add_rect(region: List[Rect], r: Rect) -> List[Rect]:
    """Union keeping the region a list of *disjoint* rectangles."""
    if not _valid(r):
        return region
    pieces = [r]
    for q in region:
        pieces = subtract_all(pieces, q)
        if not pieces:
            return region
    return region + pieces


def normalize(rects: Iterable[Rect]) -> List[Rect]:
    """Canonical, minimal-ish form: horizontal bands of merged x-intervals,
    vertically adjacent bands with identical intervals merged.  Accepts
    overlapping input; output is disjoint and sorted (top-left first)."""
    rs = [r for r in rects if _valid(r)]
    if not rs:
        return []
    ys = sorted({y for r in rs for y in (r[1], r[3])})
    bands: List[Tuple[int, int, Tuple[Tuple[int, int], ...]]] = []
    for ya, yb in zip(ys, ys[1:]):
        spans = sorted((r[0], r[2]) for r in rs if r[1] <= ya and r[3] >= yb)
        merged: List[List[int]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                if b > merged[-1][1]:
                    merged[-1][1] = b
            else:
                merged.append([a, b])
        key = tuple((a, b) for a, b in merged)
        if not key:
            continue
        if bands and bands[-1][1] == ya and bands[-1][2] == key:
            bands[-1] = (bands[-1][0], yb, key)
        else:
            bands.append((ya, yb, key))
    # Second pass: extend a rectangle downwards while the next band has the
    # exact same x-span (bands that differ elsewhere no longer split it).
    out: List[List[int]] = []
    open_spans: Dict[Tuple[int, int], int] = {}
    for y0, y1, key in bands:
        nxt: Dict[Tuple[int, int], int] = {}
        for a, b in key:
            i = open_spans.get((a, b))
            if i is not None and out[i][3] == y0:
                out[i][3] = y1
            else:
                out.append([a, y0, b, y1])
                i = len(out) - 1
            nxt[(a, b)] = i
        open_spans = nxt
    out.sort(key=lambda r: (r[1], r[0]))
    return [(r[0], r[1], r[2], r[3]) for r in out]


def complement(rects: Iterable[Rect], bounds: Rect) -> List[Rect]:
    """``bounds`` minus the union of ``rects``."""
    out = [bounds] if _valid(bounds) else []
    for r in rects:
        out = subtract_all(out, r)
        if not out:
            break
    return normalize(out)


def visible_region(layers: Sequence[Tuple[Rect, str]], bounds: Rect) -> List[Rect]:
    """Visible part of the revealed windows inside ``bounds``.

    ``layers`` is ``[(rect, role), ...]`` in z-order **top to bottom** (the
    EnumWindows order).  Walking bottom -> top, a *reveal* window adds its
    rectangle, a *cover* window removes it, a *skip* window does nothing.
    """
    vis: List[Rect] = []
    for rect, role in reversed(list(layers)):
        if role == ROLE_SKIP:
            continue
        r = intersect(rect, bounds)
        if r is None:
            continue
        if role == ROLE_REVEAL:
            vis = add_rect(vis, r)
        else:
            vis = subtract_all(vis, r)
        if len(vis) > 96:            # keep fragmentation bounded
            vis = normalize(vis)
    return normalize(vis)


def translate(rects: Iterable[Rect], dx: int, dy: int) -> List[Rect]:
    return [(x0 + dx, y0 + dy, x1 + dx, y1 + dy) for x0, y0, x1, y1 in rects]


# ======================================================================= window model
def normalize_exe(name: str) -> str:
    """``'C:\\X\\SLDWORKS.exe'`` / ``'"sldworks"'`` -> ``'sldworks.exe'``."""
    n = (name or "").strip().strip('"').strip()
    n = n.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    if n and "." not in n:
        n += ".exe"
    return n


class WinInfo:
    """One top-level window as the masker sees it (plain data, fakeable in tests)."""

    __slots__ = ("hwnd", "pid", "exe", "rect", "visible", "cloaked", "minimized",
                 "ex_style", "owner_exes", "affinity", "class_name", "title")

    def __init__(self, hwnd: int = 0, pid: int = 0, exe: str = "", rect: Rect = (0, 0, 0, 0),
                 visible: bool = True, cloaked: bool = False, minimized: bool = False,
                 ex_style: int = 0, owner_exes: Sequence[str] = (), affinity: int = 0,
                 class_name: str = "", title: str = ""):
        self.hwnd = int(hwnd or 0)
        self.pid = int(pid or 0)
        self.exe = exe or ""
        self.rect = tuple(int(v) for v in rect)  # type: ignore[assignment]
        self.visible = bool(visible)
        self.cloaked = bool(cloaked)
        self.minimized = bool(minimized)
        self.ex_style = int(ex_style or 0)
        self.owner_exes = tuple(owner_exes or ())
        self.affinity = int(affinity or 0)
        self.class_name = class_name or ""
        self.title = title or ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"WinInfo({self.hwnd:#x}, {self.exe!r}, pid={self.pid}, rect={self.rect})"


def _lower_set(items: Iterable[str]) -> FrozenSet[str]:
    """Sets are taken as already lower-case (hot path); anything else is lowered."""
    if isinstance(items, (set, frozenset)):
        return items  # type: ignore[return-value]
    return frozenset((a or "").lower() for a in items if a)


_KNOWN_OVERLAYS: FrozenSet[str] = frozenset(KNOWN_OVERLAY_EXES)


def dpi_to_scale(dpi) -> float:
    """Monitor DPI (96 = 100 %) -> scale; unknown / invalid -> 1.0, capped at 500 %."""
    try:
        s = float(dpi) / 96.0
    except (TypeError, ValueError):
        return 1.0
    if not s > 0 or s != s:                 # <= 0 or NaN
        return 1.0
    return min(s, MAX_DPI_SCALE)


def ime_candidate_sized(rect: Rect, scale: float = 1.0) -> bool:
    """An IME candidate bar / composition window -- not a clipboard-history,
    emoji or touch-keyboard panel (those stay black).  ``scale`` is the
    monitor's DPI scale (1.0 = 96 DPI); the caps do not depend on the size of
    the capture area."""
    if not _valid(rect):
        return False
    try:
        s = dpi_to_scale(float(scale) * 96.0) if scale else 1.0
    except (TypeError, ValueError):
        s = 1.0
    if rect[3] - rect[1] <= IME_MAX_HEIGHT_PX * s:
        return True
    return rect_area(rect) <= IME_MAX_AREA_PX * s * s


def _ime_cover(w: WinInfo, fg_state: Optional[str], dpi_scale: float) -> Optional[Tuple[str, str]]:
    """``(ROLE_COVER, reason)`` when an IME helper window must stay black, else ``None``."""
    if fg_state != FG_TARGET:
        return ROLE_COVER, "ime_background"   # e.g. candidates for text typed into WeChat
    if not ime_candidate_sized(w.rect, dpi_scale):
        return ROLE_COVER, "ime_panel"        # clipboard history / emoji / touch keyboard
    return None


def classify_window(w: WinInfo, target: str, allow: Iterable[str] = (), own_pid: int = 0,
                    bounds: Optional[Rect] = None, *, fg_state: Optional[str] = None,
                    target_pids: Iterable[int] = (), overlay_exes: Optional[Iterable[str]] = None,
                    ime_exes: Optional[Iterable[str]] = None, dpi_scale: float = 1.0) -> Tuple[str, str]:
    """-> ``(role, reason)``; ``target``/``allow`` are normalized lower-case exe names.

    ``fg_state`` is the foreground classification (:func:`classify_foreground`):
    IME helper windows -- click-through ones included -- are revealed (or, when
    click-through, skipped) only while it is ``FG_TARGET`` (``None`` = unknown
    -> covered) and only when :func:`ime_candidate_sized` at ``dpi_scale``.
    ``target_pids`` are processes descended from the target
    (:func:`descendant_pids`); they count as the target.  ``overlay_exes``
    (default :data:`KNOWN_OVERLAY_EXES`) are the only exes whose full-screen
    click-through windows are skipped; ``ime_exes`` defaults to
    :data:`IME_HELPER_EXES`.
    """
    if not w.visible:
        return ROLE_SKIP, "hidden"
    if w.cloaked:
        return ROLE_SKIP, "cloaked"
    if w.minimized:
        return ROLE_SKIP, "minimized"
    if not _valid(w.rect):
        return ROLE_SKIP, "empty"
    if own_pid and w.pid == own_pid:
        # An excluded window is not in the captured pixels at all: masking it
        # would leave a black box where the overlay bar floats.
        if w.affinity == WDA_EXCLUDEFROMCAPTURE:
            return ROLE_SKIP, "own_excluded"
        return ROLE_COVER, "own"
    exe = (w.exe or "").lower()
    allow_set = _lower_set(allow)
    is_target = bool(exe) and exe == target
    is_child = not is_target and bool(w.pid) and w.pid in target_pids
    owned = bool(target) and any((o or "").lower() == target for o in w.owner_exes)
    is_ime = bool(exe) and exe in allow_set and exe in (IME_HELPER_EXES if ime_exes is None
                                                        else _lower_set(ime_exes))
    if (w.ex_style & _CLICK_THROUGH) == _CLICK_THROUGH:
        # never reveal what lies *under* a see-through window
        if is_target or is_child:
            return ROLE_SKIP, "target_click_through"
        if is_ime:
            cover = _ime_cover(w, fg_state, dpi_scale)   # IME rule first: a see-through
            if cover is not None:                       # clipboard / emoji layer is not skipped
                return cover
        if exe and exe in allow_set:
            return ROLE_SKIP, "allow_click_through"
        if owned:
            return ROLE_SKIP, "owned_click_through"
        if bounds is not None:
            inter = intersect(w.rect, bounds)
            share = rect_area(inter) / float(max(1, rect_area(bounds))) if inter else 0.0
        else:
            share = 1.0
        if share >= OVERLAY_MIN_SHARE:
            overlays = _KNOWN_OVERLAYS if overlay_exes is None else _lower_set(overlay_exes)
            if exe and exe in overlays:
                return ROLE_SKIP, "overlay"
            return ROLE_COVER, "overlay_unknown"      # may draw chat / annotations: fail closed
        return ROLE_COVER, "other"
    if is_target:
        return ROLE_REVEAL, "target"
    if is_child:
        return ROLE_REVEAL, "target_descendant"
    if exe and exe in allow_set:
        if is_ime:
            cover = _ime_cover(w, fg_state, dpi_scale)
            if cover is not None:
                return cover
        return ROLE_REVEAL, "allow"
    if owned:
        return ROLE_REVEAL, "owned_by_target"
    return ROLE_COVER, "other"


def classify_foreground(pid: int, exe: str, target: str, allow: Iterable[str] = (),
                        own_pid: int = 0, owner_exes: Sequence[str] = (),
                        target_pids: Iterable[int] = ()) -> str:
    """Foreground window -> ``target | allow | own | other | none``.

    ``target_pids``: processes descended from the target (they count as it)."""
    if not pid and not exe:
        return FG_NONE
    if own_pid and pid == own_pid:
        return FG_OWN
    e = (exe or "").lower()
    if e and e == target:
        return FG_TARGET
    if pid and pid in target_pids:
        return FG_TARGET      # a helper process the target launched (e.g. Ansys Mechanical)
    if target and any((o or "").lower() == target for o in owner_exes):
        return FG_TARGET      # e.g. an out-of-process dialog owned by the target
    if e and e in _lower_set(allow):
        return FG_ALLOW
    return FG_OTHER


def _peek_pairs(peek_classes: Optional[Iterable]) -> Tuple[Tuple[str, str], ...]:
    """``None`` -> :data:`PEEK_WINDOW_CLASSES`; else ``(exe, class)`` pairs or bare
    class names (any exe), lower-cased."""
    if peek_classes is None:
        return PEEK_WINDOW_CLASSES
    out: List[Tuple[str, str]] = []
    for item in peek_classes:
        exe, cls = ("", item) if isinstance(item, str) else (item[0], item[1])
        cls = (cls or "").strip().lower()
        if cls:
            out.append((normalize_exe(exe) if exe else "", cls))
    return tuple(out)


def find_peek_window(windows: Sequence[WinInfo], peek_classes: Optional[Iterable] = None) -> Optional[WinInfo]:
    """The first visible, uncloaked thumbnail / task-switcher host (anywhere on
    the desktop: a thumbnail on another monitor's taskbar peeks here too)."""
    pairs = _peek_pairs(peek_classes)
    if not pairs:
        return None
    for w in windows:
        if not w.visible or w.cloaked or w.minimized or not _valid(w.rect):
            continue
        cls = (w.class_name or "").lower()
        if not cls:
            continue
        exe = (w.exe or "").lower()
        for pexe, pcls in pairs:
            # unreadable exe -> assume the worst (fail closed)
            if cls == pcls and (not pexe or not exe or exe == pexe):
                return w
    return None


# ================================================================== process tree
ProcInfo = Tuple[Optional[int], str]      # (creation time, full image path); path "" = unknown


def _path_parts(path: str) -> Tuple[List[str], int]:
    """Image path -> (lower-case components, number of leading "drive" components:
    1 for ``C:``, 2 for ``\\\\server\\share``)."""
    p = (path or "").strip().strip('"').strip().replace("/", "\\")
    if p.startswith("\\\\?\\") or p.startswith("\\\\.\\"):
        p = p[4:]
        if p.lower().startswith("unc\\"):
            p = "\\\\" + p[4:]
    unc = p.startswith("\\\\")
    parts = [x.strip().lower() for x in p.split("\\") if x.strip() and x.strip() != "."]
    return parts, (2 if unc else 1)


def _join(parts: Sequence[str]) -> str:
    return "\\".join(parts)


def install_root(exe_path: str) -> str:
    """Lower-case install root of an exe (``""`` = unknown -> nothing inherits).

    The vendor / package directory one level below a shared program folder
    (``C:\\Program Files\\<Vendor>``, ``...\\Programs\\<App>``,
    ``...\\WindowsApps\\<Package>``), else the exe's grandparent directory --
    or its parent when the grandparent is a drive root, a user-profile root or
    a broad folder (Windows, Users, Desktop, Downloads...).
    """
    parts, drive = _path_parts(exe_path)
    dirs = parts[:-1]                     # drop the file name
    if len(dirs) <= drive:
        return ""
    for i in range(drive, len(dirs)):
        if dirs[i] in _SHARED_PROGRAM_DIRS:
            j = i
            while j + 1 < len(dirs) and dirs[j + 1] in _SHARED_PROGRAM_DIRS:
                j += 1                    # Program Files\Common Files\<Vendor>
            return _join(dirs[:j + 2]) if j + 1 < len(dirs) else ""
    for n in (len(dirs) - 1, len(dirs)):  # grandparent, else parent
        cand = dirs[:n]
        if len(cand) <= drive or cand[-1] in _BROAD_DIRS:
            continue
        if len(cand) >= 2 and cand[-2] == "users":
            continue                      # C:\Users\<name>: a whole profile
        return _join(cand)
    return ""


def path_under(path: str, root: str) -> bool:
    """Does image ``path`` lie inside directory ``root`` (from :func:`install_root`)?"""
    if not root:
        return False
    parts, _ = _path_parts(path)
    return _join(parts).startswith(root + "\\")


def _split_info(v) -> ProcInfo:
    """``proc_info`` result -> ``(ctime or None, path)``; a bare number is a
    creation time with an unknown path (never inherits; fail closed)."""
    if v is None:
        return None, ""
    if isinstance(v, (tuple, list)):
        t = v[0] if len(v) > 0 else None
        path = v[1] if len(v) > 1 else ""
        return (None if t is None else int(t)), str(path or "")
    return int(v), ""


def descendant_pids(processes: Iterable[Tuple[int, int, str]], target_exe: str,
                    proc_info: Callable[[int], Optional[ProcInfo]],
                    deny_exes: Iterable[str] = DESCENDANT_DENY_EXES, own_pid: int = 0) -> Set[int]:
    """PIDs descended from the processes running ``target_exe`` (roots excluded)
    that count as the target.

    ``processes`` is ``[(pid, parent_pid, exe_name), ...]`` (a Toolhelp32
    snapshot); ``proc_info(pid)`` is ``(creation_time, image_path)`` (any
    monotonic time unit, e.g. FILETIME) or ``None`` when unknown.  A child
    inherits only when

    * it was created no earlier than its parent -- Windows reuses PIDs, so a
      "parent" PID now held by a younger process is not the real parent;
    * its image lies under the :func:`install_root` of the target process at
      the top of its chain (a console host, ``conhost.exe``, is allowed as a
      leaf because its parent qualifies);
    * its exe is not in ``deny_exes`` (browsers, chat, office, shells...: their
      subtrees never inherit, wherever they are installed).

    Anything unknown (time, path, root) -> not inherited (fail closed).
    ``proc_info`` is only called along the target's subtree.
    """
    target = normalize_exe(target_exe)
    deny = {normalize_exe(e) for e in deny_exes if e}
    exe_of: Dict[int, str] = {}
    children: Dict[int, List[int]] = {}
    for pid, ppid, exe in processes:
        pid, ppid = int(pid or 0), int(ppid or 0)
        if pid <= 4:                      # System Idle / System
            continue
        exe_of[pid] = normalize_exe(exe)
        if ppid and ppid != pid:
            children.setdefault(ppid, []).append(pid)
    if not target:
        return set()
    roots = [p for p, e in exe_of.items() if e == target]
    infos: Dict[int, ProcInfo] = {}

    def info(pid: int) -> ProcInfo:
        if pid not in infos:
            try:
                infos[pid] = _split_info(proc_info(pid))
            except Exception:
                infos[pid] = (None, "")
        return infos[pid]

    out: Set[int] = set()
    seen = set(roots)
    for root in roots:
        rt, rpath = info(root)
        base = install_root(rpath)
        if rt is None or not base:
            continue                      # unknown target image: nothing inherits
        queue = [root]
        while queue:
            parent = queue.pop()
            kids = [c for c in children.get(parent, ()) if c not in seen]
            if not kids:
                continue
            pt = info(parent)[0]
            for child in kids:
                seen.add(child)
                exe = exe_of.get(child, "")
                if child == own_pid or exe in deny:
                    continue
                ct, cpath = info(child)
                if pt is None or ct is None or ct < pt:
                    continue              # unknown, or the parent PID was reused after the child started
                if exe in CONSOLE_HOST_EXES:
                    out.add(child)        # its parent's console window; a leaf
                    continue
                if not path_under(cpath, base):
                    continue              # launched app installed elsewhere (Excel, a PDF reader...)
                out.add(child)
                queue.append(child)
    return out


def _perf_ms() -> float:
    return time.perf_counter() * 1000.0


class ProcessTreeTracker:
    """Keeps :func:`descendant_pids` of the target fresh (pure; the snapshot and
    ``info_fn(pid) -> (creation_time, image_path)`` functions are injected --
    Win32 by default in :class:`AppMasker`).  Refreshes at most every
    ``refresh_ms``, or after ``retry_ms`` when asked about a PID it does not
    know yet (a helper window that just came to the front).  Thread-safe; a
    caller never waits for another thread's refresh.  A failed snapshot clears
    the set (fail closed).
    """

    def __init__(self, target_exe: str, snapshot_fn: Callable[[], Iterable[Tuple[int, int, str]]],
                 info_fn: Callable[[int], Optional[ProcInfo]], *,
                 deny_exes: Iterable[str] = DESCENDANT_DENY_EXES, own_pid: int = 0,
                 refresh_ms: float = 1000.0, retry_ms: float = 250.0,
                 clock_fn: Optional[Callable[[], float]] = None):
        self.target = normalize_exe(target_exe)
        self._snapshot = snapshot_fn
        self._info = info_fn
        self.deny = frozenset(normalize_exe(e) for e in deny_exes if e)
        self.own_pid = int(own_pid or 0)
        self.refresh_ms = float(refresh_ms)
        self.retry_ms = float(retry_ms)
        self._clock = clock_fn or _perf_ms
        self._lock = threading.Lock()
        self._pids: FrozenSet[int] = frozenset()
        self._at: Optional[float] = None
        self._infos: Dict[Tuple[int, int, str], Tuple[int, str]] = {}
        self.refreshes = 0
        self.errors = 0
        self.last_error = ""

    @property
    def current(self) -> FrozenSet[int]:
        return self._pids

    def pids(self, now: Optional[float] = None, want: int = 0) -> FrozenSet[int]:
        now = self._clock() if now is None else float(now)
        at = self._at
        due = (at is None or now < at or now - at >= self.refresh_ms
               or (bool(want) and want not in self._pids and now - at >= self.retry_ms))
        if due and self._lock.acquire(blocking=False):
            try:
                self._refresh(now)
            finally:
                self._lock.release()
        return self._pids

    def _refresh(self, now: float) -> None:
        self._at = now
        self.refreshes += 1
        try:
            procs = [(int(p or 0), int(pp or 0), normalize_exe(e or "")) for p, pp, e in self._snapshot()]
        except Exception as exc:
            self.errors += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._pids = frozenset()
            return
        rows = {p: (pp, e) for p, pp, e in procs}
        cache = self._infos
        for key in [k for k in cache if rows.get(k[0]) != (k[1], k[2])]:
            del cache[key]                # process gone (its PID may be reused)

        def info(pid: int) -> ProcInfo:
            pp, e = rows.get(pid, (0, ""))
            key = (pid, pp, e)
            hit = cache.get(key)
            if hit is None:
                ct, path = _split_info(self._info(pid))
                if ct is None or not path:
                    return ct, path       # not cached: asked again next refresh
                hit = cache[key] = (ct, path)
            return hit

        self._pids = frozenset(descendant_pids(procs, self.target, info, self.deny, self.own_pid))


class MaskResult:
    """What one frame needs: black rectangles (capture-local, half-open)."""

    __slots__ = ("black_rects", "target_visible", "foreground_is_target", "foreground_exe",
                 "foreground_state", "visible_area", "recomputed", "failed", "peek")

    def __init__(self, black_rects: List[Rect], target_visible: bool,
                 foreground_is_target: bool = False, foreground_exe: str = "",
                 foreground_state: str = FG_NONE, visible_area: int = 0,
                 recomputed: bool = False, failed: bool = False, peek: str = ""):
        self.black_rects = black_rects
        self.target_visible = target_visible
        self.foreground_is_target = foreground_is_target
        self.foreground_exe = foreground_exe
        self.foreground_state = foreground_state
        self.visible_area = visible_area
        self.recomputed = recomputed
        self.failed = failed
        self.peek = peek              # why the whole frame is black (thumbnail / desktop peek), or ""

    @property
    def masked(self) -> bool:
        return bool(self.black_rects)


def build_layers(windows: Sequence[WinInfo], target: str, allow: Iterable[str], own_pid: int,
                 bounds: Rect, stats: Optional[Dict[str, int]] = None, *, fg_state: Optional[str] = None,
                 target_pids: Iterable[int] = (), overlay_exes: Optional[Iterable[str]] = None,
                 ime_exes: Optional[Iterable[str]] = None,
                 reason_exes: Optional[Dict[str, Set[str]]] = None, dpi_scale: float = 1.0,
                 window_roles: Optional[Dict[int, Tuple[str, str, Rect]]] = None) -> List[Tuple[Rect, str]]:
    """``[(rect, role)]`` top -> bottom for :func:`visible_region` (skips dropped).
    ``reason_exes`` (``{reason: set()}``) collects the exe names seen per reason;
    ``window_roles`` collects ``{hwnd: (role, reason, rect)}``."""
    allow_set = _lower_set(allow)
    overlays = None if overlay_exes is None else _lower_set(overlay_exes)
    imes = None if ime_exes is None else _lower_set(ime_exes)
    layers: List[Tuple[Rect, str]] = []
    for w in windows:
        role, reason = classify_window(w, target, allow_set, own_pid, bounds, fg_state=fg_state,
                                       target_pids=target_pids, overlay_exes=overlays, ime_exes=imes,
                                       dpi_scale=dpi_scale)
        if stats is not None:
            stats[reason] = stats.get(reason, 0) + 1
        if reason_exes is not None and reason in reason_exes:
            reason_exes[reason].add(w.exe or "?")
        if window_roles is not None and w.hwnd:
            window_roles[w.hwnd] = (role, reason, w.rect)  # type: ignore[assignment]
        if role == ROLE_SKIP:
            continue
        r = intersect(w.rect, bounds)  # type: ignore[arg-type]
        if r is not None:
            layers.append((r, role))
    return layers


def mask_from_layers(layers: Sequence[Tuple[Rect, str]], bounds: Rect) -> Tuple[List[Rect], int]:
    """-> (black rects in *capture-local* px, visible area)."""
    vis = visible_region(layers, bounds)
    black = complement(vis, bounds)
    return translate(black, -bounds[0], -bounds[1]), total_area(vis)


def compute_mask(windows: Sequence[WinInfo], target_exe: str, allow_exes: Iterable[str] = (),
                 own_pid: int = 0, capture_rect: XYWH = (0, 0, 0, 0), *,
                 foreground_state: Optional[str] = None, target_pids: Iterable[int] = (),
                 overlay_exes: Optional[Iterable[str]] = None,
                 peek_classes: Optional[Iterable] = None, dpi_scale: float = 1.0) -> MaskResult:
    """Pure one-shot mask (no caching, no hold) -- what :class:`AppMasker` does per frame.

    ``foreground_state`` (``FG_*``) decides whether IME helper windows may be
    revealed (``None`` = unknown -> not revealed); ``dpi_scale`` is the capture
    monitor's DPI scale (1.0 = 96 DPI) for :func:`ime_candidate_sized`."""
    bounds = xywh_to_rect(capture_rect)
    target = normalize_exe(target_exe)
    allow = frozenset(normalize_exe(a) for a in allow_exes if a)
    peek = find_peek_window(windows, peek_classes)
    if peek is not None:
        w, h = bounds[2] - bounds[0], bounds[3] - bounds[1]
        return MaskResult([(0, 0, w, h)] if w > 0 and h > 0 else [], False, recomputed=True,
                          peek=peek.class_name or "peek")
    layers = build_layers(windows, target, allow, own_pid, bounds, fg_state=foreground_state,
                          target_pids=target_pids, overlay_exes=overlay_exes, dpi_scale=dpi_scale)
    black, vis_area = mask_from_layers(layers, bounds)
    return MaskResult(black, vis_area > 0, visible_area=vis_area, recomputed=True)


def apply_mask(frame, rects: Iterable[Rect]):
    """Paint ``rects`` black in a numpy ``HxWx4`` (BGRA) frame, in place.

    Pure slice assignment: one memset per rectangle, no per-pixel Python.
    """
    for x0, y0, x1, y1 in rects:
        x0 = x0 if x0 > 0 else 0
        y0 = y0 if y0 > 0 else 0
        if x1 > x0 and y1 > y0:
            frame[y0:y1, x0:x1] = 0
    return frame


def pick_capture_monitor(screens: Sequence[Dict], target_rects: Sequence[Rect] = (),
                         app_monitor: Optional[int] = None) -> Optional[Dict]:
    """``--app-monitor`` if given, else the monitor holding the target's largest
    visible window, else the primary monitor."""
    if not screens:
        return None
    if app_monitor is not None:
        for s in screens:
            if int(s.get("index", -1)) == int(app_monitor):
                return s
        raise ValueError(L("找不到显示器 {n}（可用：{ids}）", "Monitor {n} not found (available: {ids})",
                           n=app_monitor, ids=", ".join(str(s.get("index")) for s in screens)))
    rects = [r for r in target_rects if _valid(r)]
    if rects:
        largest = max(rects, key=rect_area)
        best, best_area = None, 0
        for s in screens:
            inter = intersect(largest, xywh_to_rect((s["originX"], s["originY"], s["width"], s["height"])))
            a = rect_area(inter) if inter else 0
            if a > best_area:
                best, best_area = s, a
        if best is not None:
            return best
    return next((s for s in screens if s.get("primary")), screens[0])


def screen_rect(screen: Dict) -> XYWH:
    return (int(screen["originX"]), int(screen["originY"]), int(screen["width"]), int(screen["height"]))


# ======================================================================= auto-pause
class AutoPauseController:
    """Debounce for app-mode auto-pause (pure; driven with a fake clock in tests).

    The recorder owns the pause state and passes it in on every poll:
    ``state`` is ``"recording"``, ``"auto"`` (auto-paused) or ``"manual"``.
    ``fg`` is the foreground classification (:func:`classify_foreground`).

    * recording and a non-target app (``other``) stays in front for
      ``debounce_ms`` -> ``"pause"``;
    * auto-paused and the target is in front again -> ``"resume"``;
    * own process / allow-listed / no foreground window are neutral: they
      neither pause nor resume, and they reset the debounce;
    * a manual pause is never resumed automatically.
    """

    def __init__(self, debounce_ms: float = AUTO_PAUSE_DEBOUNCE_MS):
        self.debounce_ms = float(debounce_ms)
        self._other_since: Optional[float] = None

    def reset(self) -> None:
        self._other_since = None

    @property
    def pending(self) -> bool:
        return self._other_since is not None

    def update(self, now_ms: float, fg: str, state: str) -> Optional[str]:
        if state == "recording":
            if fg == FG_OTHER:
                if self._other_since is None:
                    self._other_since = now_ms
                if now_ms - self._other_since >= self.debounce_ms:
                    self._other_since = None
                    return "pause"
            else:
                self._other_since = None
            return None
        self._other_since = None
        if state == "auto" and fg == FG_TARGET:
            return "resume"
        return None


# ========================================================================== masker
class AppMasker:
    """Per-frame mask for one target app on one capture rectangle.

    ``enumerate_fn(bounds) -> [WinInfo]`` and ``foreground_fn() -> dict`` default
    to the Win32 implementations; tests inject fakes.  With injected fakes (or
    off Windows) the desktop-peek probe, the process-tree tracker and the DPI
    query are off unless ``peek_fn`` / ``process_fns=(snapshot_fn, info_fn)``
    (``info_fn(pid) -> (creation_time, image_path)``) / ``dpi_fn`` are given.

    * ``hold_ms``: a pixel that goes from black to revealed stays black this
      long (newly covered pixels are black at once); a covering window that is
      minimized or restored blacks the whole frame this long.
    * ``dpi_fn(bounds) -> dpi or None``: the capture monitor's DPI (96 when
      unknown; the Win32 default also says unknown unless the computing
      thread is per-monitor DPI aware), re-read every :data:`DPI_REFRESH_MS`;
      scales the IME candidate-size caps.
    * ``peek_classes``: extra thumbnail / task-switcher window classes
      (``"Class"`` or ``("exe", "Class")``) on top of :data:`PEEK_WINDOW_CLASSES`.
    * ``overlay_exes``: extra benign full-screen click-through overlay exes on
      top of :data:`KNOWN_OVERLAY_EXES`.
    * ``clock_fn() -> ms`` (default ``perf_counter``) drives the hold.
    """

    def __init__(self, target_exe: str, allow_exes: Iterable[str] = (), own_pid: Optional[int] = None,
                 capture_rect: XYWH = (0, 0, 0, 0), *,
                 enumerate_fn: Optional[Callable[[Rect], List[WinInfo]]] = None,
                 foreground_fn: Optional[Callable[[], Dict]] = None,
                 hold_ms: float = DEFAULT_HOLD_MS, include_defaults: bool = True,
                 clock_fn: Optional[Callable[[], float]] = None,
                 peek_fn: Optional[Callable[[], Optional[str]]] = None,
                 peek_classes: Iterable = (), overlay_exes: Iterable[str] = (),
                 process_fns: Optional[Tuple[Callable, Callable]] = None,
                 dpi_fn: Optional[Callable[[Rect], Optional[float]]] = None):
        self.target = normalize_exe(target_exe)
        if not self.target:
            raise ValueError(L("请选择要录制的应用（exe 名称）", "Choose the application (exe name) to record"))
        allow = list(DEFAULT_APP_ALLOW) if include_defaults else []
        allow += [a for a in allow_exes or () if a]
        self.allow = sorted({normalize_exe(a) for a in allow if normalize_exe(a)} - {self.target})
        self._allow_set = frozenset(self.allow)
        self.own_pid = os.getpid() if own_pid is None else int(own_pid)
        self.hold_ms = max(0.0, float(hold_ms))
        self._clock = clock_fn or _perf_ms
        self.peek_classes = PEEK_WINDOW_CLASSES + _peek_pairs(peek_classes or ())
        self._peek_class_names = frozenset(c for _, c in self.peek_classes)
        self.overlay_exes = frozenset(KNOWN_OVERLAY_EXES) | {normalize_exe(e) for e in overlay_exes or () if e}
        real = enumerate_fn is None and sys.platform == "win32"
        self._enumerate = enumerate_fn or (lambda bounds: enumerate_windows(self.own_pid, bounds,
                                                                            self._peek_class_names))
        self._foreground = foreground_fn or foreground_info
        self._peek_fn = peek_fn if peek_fn is not None else (DesktopPeekProbe() if real else None)
        if process_fns is None and real:
            process_fns = (win_process_snapshot, win_process_info)
        self.procs: Optional[ProcessTreeTracker] = None
        if process_fns:
            self.procs = ProcessTreeTracker(self.target, process_fns[0], process_fns[1],
                                            own_pid=self.own_pid, clock_fn=self._clock)
        self._dpi_fn = dpi_fn if dpi_fn is not None else (win_monitor_dpi if real else None)
        self._dpi_scale = 1.0
        self._dpi_at: Optional[float] = None
        self._lock = threading.Lock()
        self._bounds: Optional[Rect] = None
        self._held: List[Tuple[List[Rect], float]] = []    # (black rects, black until ms)
        self._pending: Optional[List[Rect]] = None          # begin_segment() hold, armed at next compute
        # {hwnd: (role, reason, rect)} from the last enumeration (minimize / restore animations)
        self._prev_roles: Optional[Dict[int, Tuple[str, str, Rect]]] = None
        self.set_capture_rect(capture_rect)
        # stats
        self.computes = 0
        self.recomputes = 0
        self.failures = 0
        self.last_error = ""
        self.max_black_rects = 0
        self.overlays_seen = 0
        self.unknown_overlays: Set[str] = set()        # cumulative (capped)
        self.last_unknown_overlays: Set[str] = set()   # covered in the last compute
        self.peek_frames = 0
        self.last_peek = ""
        self.peek_errors = 0
        self.segment_holds = 0
        self.animation_holds = 0
        self.last_animation = ""
        self.dpi_errors = 0
        self.enum_ms_total = 0.0
        self.last_reasons: Dict[str, int] = {}
        self.last: Optional[MaskResult] = None

    # ----------------------------------------------------------------- geometry
    @property
    def capture_rect(self) -> XYWH:
        b = self._bounds or (0, 0, 0, 0)
        return (b[0], b[1], b[2] - b[0], b[3] - b[1])

    def _full_locked(self) -> List[Rect]:
        b = self._bounds or (0, 0, 0, 0)
        w, h = b[2] - b[0], b[3] - b[1]
        return [(0, 0, w, h)] if w > 0 and h > 0 else []

    def set_capture_rect(self, capture_rect: XYWH) -> None:
        """Move the mask to another rectangle.  A no-op when it is unchanged (the
        hold survives a segment restart on the same monitor)."""
        with self._lock:
            new = xywh_to_rect(capture_rect)
            if new == self._bounds:
                return
            self._bounds = new
            self._sig = None
            self._black: List[Rect] = []
            self._vis_area = 0
            self._have_mask = False       # self._black is a computed mask
            self._known = False           # self._effective was applied to a frame
            self._effective: List[Rect] = []
            self._eff_base: Optional[List[Rect]] = None
            self._eff_dirty = True
            self._prev_roles = None
            self._dpi_at = None           # another monitor: re-read its DPI
            # Held masks are in the old coordinates: keep their timing but black
            # out the whole new frame instead.
            full = self._full_locked()
            if self._held and full:
                self._held = [(full, max(until for _, until in self._held))]
            else:
                self._held = []
            if self._pending is not None:
                self._pending = full

    def begin_segment(self, first: Optional[bool] = None) -> None:
        """Call at every segment start (app mode; first clip, resume, restart,
        backend fallback).  ``hold_ms`` of extra black, counted from the first
        :meth:`compute` after this call (so a slow backend start-up cannot use
        up the hold):

        * the first segment (``first=None``: the first call on this masker) --
          the last mask applied to a frame, or a whole black frame if none is
          known;
        * every later one (resume, crash restart, fallback) -- a whole black
          frame: windows may have been moved, maximized, minimized or closed
          while nothing was computed, and the minimize / close animation that
          triggered an auto-resume runs outside every rect we know.
        """
        with self._lock:
            if first is None:
                first = self.segment_holds == 0
            if first and self._known:
                self._pending = list(self._effective)
            else:
                self._pending = self._full_locked()
            self.segment_holds += 1

    def _hold(self, rects: Sequence[Rect], now: float) -> None:
        """Keep ``rects`` black until ``now + hold_ms``."""
        if not rects or self.hold_ms <= 0:
            return
        self._held.append((list(rects), now + self.hold_ms))
        if len(self._held) > MAX_HELD_MASKS:
            merged = normalize([r for rects_, _ in self._held for r in rects_])
            self._held = [(merged, max(until for _, until in self._held))]
        self._eff_dirty = True

    def _effective_locked(self, now: float) -> List[Rect]:
        """Current mask OR every held mask that has not expired (cached while
        nothing changes, so the steady state returns the same list object)."""
        held = self._held
        if held:
            alive = [e for e in held if e[1] > now]
            if len(alive) != len(held):
                self._held = held = alive
                self._eff_dirty = True
        if not self._eff_dirty and self._eff_base is self._black:
            return self._effective
        extra = [r for rects, _ in held for r in rects]
        eff = normalize(extra + list(self._black)) if extra else self._black
        self._effective, self._eff_base, self._eff_dirty = eff, self._black, False
        return eff

    # ------------------------------------------------------------------ per frame
    def compute(self) -> MaskResult:
        """Black rectangles for *now* (call right after grabbing a frame)."""
        with self._lock:
            return self._compute_locked()

    def _peek_locked(self, windows: Sequence[WinInfo]) -> str:
        w = find_peek_window(windows, self.peek_classes)
        if w is not None:
            return w.class_name or "peek_window"
        if self._peek_fn is not None:
            try:
                return str(self._peek_fn() or "")
            except Exception as exc:          # a broken probe must not black out every frame
                self.peek_errors += 1
                self.last_error = f"peek probe: {type(exc).__name__}: {exc}"
        return ""

    def _dpi_scale_locked(self, now: float) -> float:
        """The capture monitor's DPI scale, re-read every :data:`DPI_REFRESH_MS`
        (unknown / error -> 1.0: the smaller caps, fail closed)."""
        if self._dpi_fn is None:
            return 1.0
        at = self._dpi_at
        if at is None or now < at or now - at >= DPI_REFRESH_MS:
            self._dpi_at = now
            try:
                dpi = self._dpi_fn(self._bounds or (0, 0, 0, 0))
            except Exception as exc:
                self.dpi_errors += 1
                self.last_error = f"dpi: {type(exc).__name__}: {exc}"
                dpi = None
            self._dpi_scale = dpi_to_scale(dpi) if dpi else 1.0
        return self._dpi_scale

    def _window_animation_locked(self, roles: Dict[int, Tuple[str, str, Rect]], bounds: Rect) -> str:
        """``"minimize"`` / ``"restore"`` when a covering window was just
        minimized (last seen covering ``bounds``) or restored -- DWM animates it
        between its rect and its taskbar button, outside every rect we know --
        else ``""``.

        :func:`enumerate_windows` drops on-screen windows that miss ``bounds``
        but always reports minimized ones, so a window on another monitor shows
        up only as a change between "minimized" and absent:

        * newly reported as minimized (no previous entry) -> ``"minimize"``
          (e.g. minimized from another monitor toward a button on this taskbar);
        * last seen minimized, now absent -> ``"restore"`` (e.g. restored onto
          another monitor from a button on this taskbar)."""
        prev, self._prev_roles = self._prev_roles, roles
        if not prev:
            return ""
        for hwnd, (role, reason, _rect) in roles.items():
            p = prev.get(hwnd)
            if p is None:
                if reason == "minimized":
                    return "minimize"
                continue
            prole, preason, prect = p
            if reason == "minimized" and prole == ROLE_COVER and intersect(prect, bounds) is not None:
                return "minimize"
            if preason == "minimized" and role == ROLE_COVER:
                return "restore"
        for hwnd, (_prole, preason, _prect) in prev.items():
            if preason == "minimized" and hwnd not in roles:
                return "restore"
        return ""

    def _compute_locked(self) -> MaskResult:
        self.computes += 1
        now = float(self._clock())
        bounds = self._bounds or (0, 0, 0, 0)
        full = self._full_locked()
        try:
            fg = self.foreground_state(now)
        except Exception:
            fg = {"state": FG_NONE, "exe": ""}
        state = fg.get("state", FG_NONE)
        try:
            t = time.perf_counter()
            windows = self._enumerate(bounds)
            stats: Dict[str, int] = {}
            seen: Dict[str, Set[str]] = {"overlay_unknown": set()}
            roles: Dict[int, Tuple[str, str, Rect]] = {}
            pids = self.procs.pids(now) if self.procs is not None else frozenset()
            layers = build_layers(windows, self.target, self._allow_set, self.own_pid, bounds, stats,
                                  fg_state=state, target_pids=pids, overlay_exes=self.overlay_exes,
                                  reason_exes=seen, dpi_scale=self._dpi_scale_locked(now),
                                  window_roles=roles)
            peek = self._peek_locked(windows)
            animation = self._window_animation_locked(roles, bounds)
            self.enum_ms_total += (time.perf_counter() - t) * 1000.0
        except Exception as exc:          # fail closed: never leak on an API error
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.last_unknown_overlays = set()   # nothing enumerated (the frame is black anyway)
            self.last_reasons = {}               # ... so no stale per-window reasons either
            self._sig = None
            self._hold(full, now)         # ... and stay black for hold_ms after it recovers
            self._effective, self._eff_base, self._eff_dirty = full, None, True
            self._known = True
            res = MaskResult(full, False, state == FG_TARGET, fg.get("exe", ""), state, 0, True, True)
            self.last = res
            return res
        self.last_reasons = stats
        self.overlays_seen += stats.get("overlay", 0)
        self.last_unknown_overlays = seen["overlay_unknown"]
        if seen["overlay_unknown"] and len(self.unknown_overlays) < 32:
            self.unknown_overlays |= seen["overlay_unknown"]
        sig = ("peek", peek) if peek else tuple(layers)
        recomputed = sig != self._sig
        if recomputed:
            if peek:
                black, vis = full, 0
            else:
                black, vis = mask_from_layers(layers, bounds)
            if self._have_mask and black != self._black:
                self._hold(self._black, now)   # what just stopped being black stays black a while
            self._sig, self._black, self._vis_area, self._have_mask = sig, black, vis, True
            self.recomputes += 1
        if peek:
            self.peek_frames += 1
            self.last_peek = peek
        if animation:
            self._hold(full, now)          # the whole frame: the animation path is unknown
            self.animation_holds += 1
            self.last_animation = animation
        if self._pending is not None:
            self._hold(self._pending, now)
            self._pending = None
        effective = self._effective_locked(now)
        self._known = True
        self.max_black_rects = max(self.max_black_rects, len(effective))
        res = MaskResult(effective, self._vis_area > 0, state == FG_TARGET, fg.get("exe", ""),
                         state, self._vis_area, recomputed, peek=peek)
        self.last = res
        return res

    apply = staticmethod(apply_mask)

    # ---------------------------------------------------------------- foreground
    def foreground_state(self, now: Optional[float] = None) -> Dict:
        """``{"state", "exe", "name", "pid", "hwnd"}`` for the foreground window."""
        info = self._foreground() or {}
        exe = info.get("exe", "") or ""
        pid = int(info.get("pid", 0) or 0)
        owner_exes = info.get("owner_exes", ()) or ()
        state = classify_foreground(pid, exe, self.target, self._allow_set, self.own_pid, owner_exes)
        if state == FG_OTHER and pid and self.procs is not None:
            # a helper process the target launched?  (refreshes early for a new PID)
            state = classify_foreground(pid, exe, self.target, self._allow_set, self.own_pid, owner_exes,
                                        target_pids=self.procs.pids(now, want=pid))
        if not info.get("hwnd") and not exe:
            state = FG_NONE
        out = dict(info)
        out["state"] = state
        out.setdefault("name", _friendly(exe))
        return out

    def is_target_pid(self, pid: int) -> bool:
        """Is ``pid`` a process descended from the target (as last refreshed)?
        For the recorder's key filter; the target exe itself is matched by name."""
        return bool(pid) and self.procs is not None and int(pid) in self.procs.current

    def target_rects(self) -> List[Rect]:
        """Rects of the target's visible windows (any monitor) -- picks the monitor."""
        try:
            windows = self._enumerate(None)  # type: ignore[arg-type]
        except Exception:
            return []
        out = []
        for w in windows:
            role, reason = classify_window(w, self.target, self._allow_set, self.own_pid, None)
            if reason == "target":
                out.append(w.rect)
        return out

    def stats(self) -> Dict:
        last = self.last
        procs = self.procs
        return {
            "computes": self.computes,
            "recomputes": self.recomputes,
            "failures": self.failures,
            "last_error": self.last_error,
            "max_black_rects": self.max_black_rects,
            "transparent_overlays_seen": self.overlays_seen,
            "covered_unknown_overlays": sorted(self.unknown_overlays),
            "last_unknown_overlays": sorted(self.last_unknown_overlays),
            "avg_enum_ms": round(self.enum_ms_total / self.computes, 3) if self.computes else None,
            "last_window_reasons": dict(self.last_reasons),
            "target_visible": bool(last.target_visible) if last else None,
            "hold_ms": self.hold_ms,
            "segment_holds": self.segment_holds,
            "animation_holds": self.animation_holds,
            "last_animation": self.last_animation,
            "dpi_scale": self._dpi_scale,
            "dpi_errors": self.dpi_errors,
            "peek_frames": self.peek_frames,
            "last_peek": self.last_peek,
            "peek_errors": self.peek_errors,
            "target_descendants": sorted(procs.current) if procs is not None else None,
            "process_tree_errors": procs.errors if procs is not None else None,
        }


def _friendly(exe: str) -> str:
    try:
        from .win_window import friendly_name
        return friendly_name(exe)
    except Exception:  # pragma: no cover
        return os.path.splitext(os.path.basename(exe or ""))[0]


# ========================================================================= Windows
_API = None
_API_LOCK = threading.Lock()


def _api() -> "_Win32":
    global _API
    if _API is None:
        if sys.platform != "win32":
            raise OSError("app-only masking needs Windows")
        with _API_LOCK:
            if _API is None:
                _API = _Win32()
    return _API


class _Win32:
    """Lazily-bound Win32 calls used by the masker / app picker (own WinDLL
    instances, so argtypes never clash with other modules)."""

    GW_OWNER = 4
    GWL_EXSTYLE = -20
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    DWMWA_CLOAKED = 14
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def __init__(self):
        import ctypes
        from ctypes import wintypes as W

        self.ctypes, self.W = ctypes, W
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            d = ctypes.WinDLL("dwmapi")
            d.DwmGetWindowAttribute.argtypes = [W.HWND, W.DWORD, ctypes.c_void_p, W.DWORD]
            d.DwmGetWindowAttribute.restype = ctypes.c_long
        except (OSError, AttributeError):
            d = None
        self.WNDENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        self.RECT = RECT

        u.EnumWindows.argtypes = [self.WNDENUMPROC, W.LPARAM]
        u.EnumWindows.restype = W.BOOL
        u.IsWindowVisible.argtypes = [W.HWND]
        u.IsWindowVisible.restype = W.BOOL
        u.IsIconic.argtypes = [W.HWND]
        u.IsIconic.restype = W.BOOL
        u.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
        u.GetWindowThreadProcessId.restype = W.DWORD
        if ctypes.sizeof(ctypes.c_void_p) == 8 and hasattr(u, "GetWindowLongPtrW"):
            self._get_long = u.GetWindowLongPtrW
            self._get_long.restype = ctypes.c_ssize_t
        else:
            self._get_long = u.GetWindowLongW
            self._get_long.restype = ctypes.c_long
        self._get_long.argtypes = [W.HWND, ctypes.c_int]
        u.GetWindow.argtypes = [W.HWND, W.UINT]
        u.GetWindow.restype = W.HWND
        u.GetWindowRect.argtypes = [W.HWND, ctypes.POINTER(RECT)]
        u.GetWindowRect.restype = W.BOOL
        u.GetWindowTextLengthW.argtypes = [W.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetForegroundWindow.argtypes = []
        u.GetForegroundWindow.restype = W.HWND
        # desktop-peek probe
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.GetCursorPos.argtypes = [ctypes.POINTER(W.POINT)]
        u.GetCursorPos.restype = W.BOOL
        u.FindWindowExW.argtypes = [W.HWND, W.HWND, W.LPCWSTR, W.LPCWSTR]
        u.FindWindowExW.restype = W.HWND
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        # monitor DPI (IME candidate-size caps)
        u.MonitorFromRect.argtypes = [ctypes.POINTER(RECT), W.DWORD]
        u.MonitorFromRect.restype = W.HANDLE
        get_ctx = getattr(u, "GetThreadDpiAwarenessContext", None)              # Windows 10 1607+
        get_awareness = getattr(u, "GetAwarenessFromDpiAwarenessContext", None)
        if get_ctx is not None and get_awareness is not None:
            get_ctx.argtypes = []
            get_ctx.restype = W.HANDLE                  # DPI_AWARENESS_CONTEXT (pointer-sized)
            get_awareness.argtypes = [W.HANDLE]
            get_awareness.restype = ctypes.c_int
        try:
            s = ctypes.WinDLL("shcore")                 # Windows 8.1+
            s.GetDpiForMonitor.argtypes = [W.HANDLE, ctypes.c_int, ctypes.POINTER(W.UINT), ctypes.POINTER(W.UINT)]
            s.GetDpiForMonitor.restype = ctypes.c_long
        except (OSError, AttributeError):
            s = None
        self.shcore = s
        self._get_affinity = getattr(u, "GetWindowDisplayAffinity", None)
        if self._get_affinity is not None:
            self._get_affinity.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
            self._get_affinity.restype = W.BOOL
        k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        k.OpenProcess.restype = W.HANDLE
        k.CloseHandle.argtypes = [W.HANDLE]
        k.CloseHandle.restype = W.BOOL
        k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)]
        k.QueryFullProcessImageNameW.restype = W.BOOL

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", W.DWORD),
                        ("cntThreads", W.DWORD), ("th32ParentProcessID", W.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", W.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]
        self.PROCESSENTRY32W = PROCESSENTRY32W
        k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
        k.CreateToolhelp32Snapshot.restype = W.HANDLE
        k.Process32FirstW.argtypes = [W.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32FirstW.restype = W.BOOL
        k.Process32NextW.argtypes = [W.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32NextW.restype = W.BOOL
        k.GetProcessTimes.argtypes = [W.HANDLE] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
        k.GetProcessTimes.restype = W.BOOL
        self.user32, self.kernel32, self.dwm = u, k, d
        self._exe_cache: Dict[int, Tuple[str, float]] = {}
        self._desc_cache: Dict[str, str] = {}

    # ------------------------------------------------------------------ windows
    def enum_hwnds(self) -> List[int]:
        out: List[int] = []

        def _cb(hwnd, lparam):
            out.append(hwnd or 0)
            return True
        proc = self.WNDENUMPROC(_cb)
        self.user32.EnumWindows(proc, 0)
        return out

    def pid_of(self, hwnd: int) -> int:
        pid = self.W.DWORD(0)
        self.user32.GetWindowThreadProcessId(hwnd, self.ctypes.byref(pid))
        return int(pid.value)

    def frame_rect(self, hwnd: int) -> Rect:
        r = self.RECT()
        if self.dwm is not None:
            try:
                if self.dwm.DwmGetWindowAttribute(hwnd, self.DWMWA_EXTENDED_FRAME_BOUNDS,
                                                  self.ctypes.byref(r), self.ctypes.sizeof(r)) == 0 \
                        and r.right > r.left and r.bottom > r.top:
                    return (r.left, r.top, r.right, r.bottom)
            except OSError:
                pass
        if self.user32.GetWindowRect(hwnd, self.ctypes.byref(r)):
            return (r.left, r.top, r.right, r.bottom)
        return (0, 0, 0, 0)

    def cloaked(self, hwnd: int) -> bool:
        if self.dwm is None:
            return False
        v = self.W.DWORD(0)
        try:
            if self.dwm.DwmGetWindowAttribute(hwnd, self.DWMWA_CLOAKED, self.ctypes.byref(v), 4) == 0:
                return v.value != 0
        except OSError:
            pass
        return False

    def ex_style(self, hwnd: int) -> int:
        return int(self._get_long(hwnd, self.GWL_EXSTYLE)) & 0xFFFFFFFF

    def owners(self, hwnd: int, limit: int = 8) -> List[int]:
        out: List[int] = []
        h = self.user32.GetWindow(hwnd, self.GW_OWNER) or 0
        while h and len(out) < limit and h not in out:
            out.append(h)
            h = self.user32.GetWindow(h, self.GW_OWNER) or 0
        return out

    def affinity(self, hwnd: int) -> int:
        if self._get_affinity is None:
            return 0
        v = self.W.DWORD(0)
        return int(v.value) if self._get_affinity(hwnd, self.ctypes.byref(v)) else 0

    def title(self, hwnd: int) -> str:
        n = self.user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = self.ctypes.create_unicode_buffer(n + 2)
        self.user32.GetWindowTextW(hwnd, buf, n + 2)
        return buf.value

    def class_name(self, hwnd: int) -> str:
        buf = self.ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    # ------------------------------------------------------- desktop-peek probe
    def key_state(self, vk: int) -> int:
        """``GetAsyncKeyState`` as an unsigned 16-bit value (0x8000 = down)."""
        return int(self.user32.GetAsyncKeyState(int(vk))) & 0xFFFF

    def cursor_pos(self) -> Optional[Tuple[int, int]]:
        pt = self.W.POINT()
        if self.user32.GetCursorPos(self.ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
        return None

    def is_visible(self, hwnd: int) -> bool:
        return bool(hwnd) and bool(self.user32.IsWindowVisible(hwnd))

    def window_rect(self, hwnd: int) -> Rect:
        r = self.RECT()
        if hwnd and self.user32.GetWindowRect(hwnd, self.ctypes.byref(r)):
            return (r.left, r.top, r.right, r.bottom)
        return (0, 0, 0, 0)

    def find_top_windows(self, classes: Sequence[str], limit: int = 16) -> List[int]:
        out: List[int] = []
        for cls in classes:
            h = 0
            while len(out) < limit:
                h = self.user32.FindWindowExW(None, h or None, cls, None) or 0
                if not h or h in out:
                    break
                out.append(h)
        return out

    def find_descendant(self, parent: int, path: Sequence[str]) -> int:
        h = parent
        for cls in path:
            h = self.user32.FindWindowExW(h, None, cls, None) or 0
            if not h:
                return 0
        return int(h)

    def virtual_screen(self) -> Rect:
        g = self.user32.GetSystemMetrics
        x, y, w, h = g(76), g(77), g(78), g(79)      # SM_[XY]VIRTUALSCREEN, SM_C[XY]VIRTUALSCREEN
        return (x, y, x + w, y + h)

    def thread_per_monitor_aware(self) -> bool:
        """Is the calling thread per-monitor DPI aware (v1 or v2)?  ``False``
        when it cannot tell (``GetThreadDpiAwarenessContext`` is Windows 10
        1607+)."""
        u = self.user32
        get_ctx = getattr(u, "GetThreadDpiAwarenessContext", None)
        get_awareness = getattr(u, "GetAwarenessFromDpiAwarenessContext", None)
        if get_ctx is None or get_awareness is None:
            return False
        return int(get_awareness(get_ctx())) == 2     # DPI_AWARENESS_PER_MONITOR_AWARE

    def monitor_dpi(self, rect: Rect) -> Optional[int]:
        """Effective DPI of the monitor nearest ``rect`` (96 = 100 %), or ``None``
        (the caller then uses 96: the smaller IME caps, fail closed).

        ``None`` unless the calling thread is per-monitor DPI aware: a
        system-aware (or unaware) thread can be told the *system* DPI -- the
        primary monitor's, the same for every monitor -- e.g. 192 on a 100 %
        capture monitor next to a 200 % primary, which would let a 100 %
        clipboard / emoji panel pass as candidate-sized."""
        if self.shcore is None or not rect:
            return None
        if not self.thread_per_monitor_aware():
            return None
        r = self.RECT(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
        hmon = self.user32.MonitorFromRect(self.ctypes.byref(r), 2)     # MONITOR_DEFAULTTONEAREST
        if not hmon:
            return None
        x, y = self.W.UINT(0), self.W.UINT(0)
        if self.shcore.GetDpiForMonitor(hmon, 0, self.ctypes.byref(x), self.ctypes.byref(y)) != 0:
            return None                                                  # 0 = MDT_EFFECTIVE_DPI
        return int(x.value) or None

    # ---------------------------------------------------------------- processes
    def process_snapshot(self) -> List[Tuple[int, int, str]]:
        """``[(pid, parent_pid, exe_name)]`` of every process (Toolhelp32)."""
        k, ctypes = self.kernel32, self.ctypes
        h = k.CreateToolhelp32Snapshot(0x00000002, 0)          # TH32CS_SNAPPROCESS
        if not h or h == ctypes.c_void_p(-1).value:
            raise OSError(f"CreateToolhelp32Snapshot failed ({ctypes.get_last_error()})")
        try:
            entry = self.PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            out: List[Tuple[int, int, str]] = []
            ok = k.Process32FirstW(h, ctypes.byref(entry))
            while ok:
                out.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
                ok = k.Process32NextW(h, ctypes.byref(entry))
            return out
        finally:
            k.CloseHandle(h)

    def process_info(self, pid: int) -> Optional[Tuple[int, str]]:
        """``(creation time (FILETIME, 100 ns units), full image path)`` of ``pid``
        from one process handle (so both describe the same process even if the
        PID is reused), or ``None``; the path is ``""`` when unreadable."""
        if not pid:
            return None
        k, ctypes = self.kernel32, self.ctypes
        h = k.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            c, e, kt, ut = (ctypes.c_ulonglong() for _ in range(4))
            if not k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                return None
            path = ""
            for size in (1024, 32768):
                buf = ctypes.create_unicode_buffer(size)
                n = self.W.DWORD(size)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    path = buf.value
                    break
            return int(c.value), path
        finally:
            k.CloseHandle(h)

    def process_create_time(self, pid: int) -> Optional[int]:
        """Creation time (FILETIME, 100 ns units) of ``pid``, or ``None``."""
        info = self.process_info(pid)
        return info[0] if info else None

    # ---------------------------------------------------------------- processes (names)
    def exe_path(self, pid: int, ttl_s: float = 1.0) -> str:
        """Full image path of ``pid`` (cached briefly; works for elevated apps)."""
        if not pid:
            return ""
        now = time.monotonic()
        hit = self._exe_cache.get(pid)
        if hit is not None and now - hit[1] < ttl_s:
            return hit[0]
        path = ""
        h = self.kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            try:
                for size in (1024, 32768):
                    buf = self.ctypes.create_unicode_buffer(size)
                    n = self.W.DWORD(size)
                    if self.kernel32.QueryFullProcessImageNameW(h, 0, buf, self.ctypes.byref(n)):
                        path = buf.value
                        break
            finally:
                self.kernel32.CloseHandle(h)
        if len(self._exe_cache) > 1024:
            self._exe_cache.clear()
        self._exe_cache[pid] = (path, now)
        return path

    def exe_name(self, pid: int) -> str:
        p = self.exe_path(pid)
        return p.replace("\\", "/").rsplit("/", 1)[-1] if p else ""

    def file_description(self, path: str) -> str:
        """FileDescription (or ProductName) from the exe's version resource."""
        if not path:
            return ""
        if path in self._desc_cache:
            return self._desc_cache[path]
        desc = ""
        try:
            ctypes, W = self.ctypes, self.W
            ver = ctypes.WinDLL("version")
            ver.GetFileVersionInfoSizeW.argtypes = [W.LPCWSTR, ctypes.POINTER(W.DWORD)]
            ver.GetFileVersionInfoSizeW.restype = W.DWORD
            ver.GetFileVersionInfoW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p]
            ver.GetFileVersionInfoW.restype = W.BOOL
            ver.VerQueryValueW.argtypes = [ctypes.c_void_p, W.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                           ctypes.POINTER(W.UINT)]
            ver.VerQueryValueW.restype = W.BOOL
            size = ver.GetFileVersionInfoSizeW(path, None)
            if size:
                buf = ctypes.create_string_buffer(size)
                if ver.GetFileVersionInfoW(path, 0, size, buf):
                    ptr, ln = ctypes.c_void_p(), W.UINT()
                    pairs = []
                    if ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr),
                                          ctypes.byref(ln)) and ptr.value and ln.value >= 4:
                        words = (ctypes.c_ushort * (ln.value // 2)).from_address(ptr.value)
                        pairs = [(words[i], words[i + 1]) for i in range(0, len(words) - 1, 2)]
                    pairs += [(0x0409, 0x04B0), (0x0409, 0x04E4), (0x0804, 0x04B0)]
                    for key in ("FileDescription", "ProductName"):
                        for lang, cp in pairs:
                            sub = f"\\StringFileInfo\\{lang:04x}{cp:04x}\\{key}"
                            if ver.VerQueryValueW(buf, sub, ctypes.byref(ptr), ctypes.byref(ln)) \
                                    and ptr.value and ln.value > 1:
                                desc = ctypes.wstring_at(ptr.value).strip()
                                if desc:
                                    break
                        if desc:
                            break
        except Exception:
            desc = ""
        self._desc_cache[path] = desc
        return desc


def enumerate_windows(own_pid: int = 0, bounds: Optional[Rect] = None,
                      keep_classes: Iterable[str] = ()) -> List[WinInfo]:
    """Visible top-level windows, top -> bottom, with their class names.
    Windows that cannot touch ``bounds`` are dropped early (cheaper per-frame
    enumeration) -- except on-screen windows whose class is in ``keep_classes``
    (thumbnail / peek hosts on another monitor's taskbar)."""
    api = _api()
    u = api.user32
    keep = frozenset((c or "").lower() for c in keep_classes or ())
    vscreen: Optional[Rect] = None
    out: List[WinInfo] = []
    for hwnd in api.enum_hwnds():
        if not hwnd or not u.IsWindowVisible(hwnd):
            continue
        minimized = bool(u.IsIconic(hwnd))
        cloaked = False if minimized else api.cloaked(hwnd)
        if minimized or cloaked:
            out.append(WinInfo(hwnd, 0, "", (0, 0, 0, 0), True, cloaked, minimized))
            continue
        rect = api.frame_rect(hwnd)
        cls = api.class_name(hwnd)
        if bounds is not None and intersect(rect, bounds) is None:
            if not keep or cls.lower() not in keep:
                continue
            if vscreen is None:
                vscreen = api.virtual_screen()
            if intersect(rect, vscreen) is None:
                continue                  # parked off-screen: not drawn anywhere
        pid = api.pid_of(hwnd)
        exe = api.exe_name(pid)
        ex = api.ex_style(hwnd)
        owner_exes: Tuple[str, ...] = ()
        owners = api.owners(hwnd)
        if owners:
            seen = []
            for oh in owners:
                opid = api.pid_of(oh)
                if opid and opid != pid:
                    seen.append(api.exe_name(opid))
            owner_exes = tuple(seen)
        aff = api.affinity(hwnd) if own_pid and pid == own_pid else 0
        out.append(WinInfo(hwnd, pid, exe, rect, True, False, False, ex, owner_exes, aff, cls))
    return out


def win_process_snapshot() -> List[Tuple[int, int, str]]:
    """``[(pid, parent_pid, exe_name)]`` of all processes (Windows only)."""
    return _api().process_snapshot()


def win_process_create_time(pid: int) -> Optional[int]:
    """Process creation time (FILETIME) or ``None`` (Windows only)."""
    return _api().process_create_time(pid)


def win_process_info(pid: int) -> Optional[Tuple[int, str]]:
    """``(creation time (FILETIME), image path)`` or ``None`` (Windows only)."""
    return _api().process_info(pid)


def win_monitor_dpi(rect: Rect) -> Optional[int]:
    """Effective DPI of the monitor nearest ``rect``, or ``None`` (= 96) when it
    is unknown or the calling thread is not per-monitor DPI aware (Windows only)."""
    return _api().monitor_dpi(rect)


class DesktopPeekProbe:
    """Is "Peek at desktop" showing?  ``Win`` held after ``,`` (Win+,), or the
    cursor on the Show-desktop button at the far end of a taskbar (hovering it
    peeks when that option is on).  Returns a reason string or ``None``.

    Win32 goes through ``api`` (default :func:`_api`): ``key_state``,
    ``cursor_pos``, ``find_top_windows``, ``find_descendant``, ``is_visible``,
    ``window_rect`` -- so the logic is unit-tested with a fake.
    """

    VK_LWIN, VK_RWIN, VK_OEM_COMMA = 0x5B, 0x5C, 0xBC
    TRAY_CLASSES = ("Shell_TrayWnd", "Shell_SecondaryTrayWnd")
    BUTTON_PATH = ("TrayNotifyWnd", "TrayShowDesktopButtonWClass")
    END_PX = 12               # taskbar end used when the button window is not found (Windows 11)
    REFRESH_MS = 2000.0       # re-find the taskbar windows this often

    def __init__(self, api=None, clock_fn: Optional[Callable[[], float]] = None):
        self._api_obj = api
        self._clock = clock_fn or _perf_ms
        self._win_comma = False
        self._trays: List[Tuple[int, int]] = []        # (taskbar hwnd, button hwnd or 0)
        self._trays_at: Optional[float] = None

    def __call__(self) -> Optional[str]:
        api = self._api_obj if self._api_obj is not None else _api()
        if self._win_comma_held(api):
            return "desktop_peek_win_comma"
        if self._cursor_on_show_desktop(api):
            return "desktop_peek_button"
        return None

    def _win_comma_held(self, api) -> bool:
        comma = api.key_state(self.VK_OEM_COMMA)     # read every poll: consumes the "pressed since" bit
        win = api.key_state(self.VK_LWIN) | api.key_state(self.VK_RWIN)
        if not win & 0x8000:
            self._win_comma = False
            return False
        if comma & 0x8001:
            self._win_comma = True                    # stays on until Win is released
        return self._win_comma

    def show_desktop_rects(self, api) -> List[Rect]:
        now = self._clock()
        if self._trays_at is None or now < self._trays_at or now - self._trays_at >= self.REFRESH_MS:
            self._trays = [(t, api.find_descendant(t, self.BUTTON_PATH))
                           for t in api.find_top_windows(self.TRAY_CLASSES)]
            self._trays_at = now
        out: List[Rect] = []
        for tray, button in self._trays:
            if not api.is_visible(tray):
                continue
            tr = api.window_rect(tray)
            if not _valid(tr):
                continue
            br = api.window_rect(button) if button and api.is_visible(button) else None
            if br is not None and _valid(br) and intersect(br, tr) is not None:
                out.append(br)
            elif tr[2] - tr[0] >= tr[3] - tr[1]:      # horizontal taskbar: right end
                out.append((tr[2] - self.END_PX, tr[1], tr[2], tr[3]))
            else:                                     # vertical taskbar: bottom end
                out.append((tr[0], tr[3] - self.END_PX, tr[2], tr[3]))
        return out

    def _cursor_on_show_desktop(self, api) -> bool:
        pt = api.cursor_pos()
        if pt is None:
            return False
        x, y = pt
        return any(r[0] <= x < r[2] and r[1] <= y < r[3] for r in self.show_desktop_rects(api))


def foreground_info() -> Dict:
    """``{"hwnd", "pid", "exe", "owner_exes", "name"}`` of the foreground window."""
    if sys.platform != "win32":
        return {"hwnd": 0, "pid": 0, "exe": "", "owner_exes": (), "name": ""}
    api = _api()
    hwnd = api.user32.GetForegroundWindow() or 0
    if not hwnd:
        return {"hwnd": 0, "pid": 0, "exe": "", "owner_exes": (), "name": ""}
    pid = api.pid_of(hwnd)
    exe = api.exe_name(pid)
    owner_exes = tuple(api.exe_name(api.pid_of(h)) for h in api.owners(hwnd, limit=4))
    return {"hwnd": int(hwnd), "pid": pid, "exe": exe, "owner_exes": owner_exes, "name": _friendly(exe)}


def app_display_name(exe_path: str) -> str:
    """Human name for an exe: known friendly name, else its FileDescription, else the stem."""
    base = exe_path.replace("\\", "/").rsplit("/", 1)[-1]
    try:
        from .win_window import FRIENDLY_NAMES
        known = FRIENDLY_NAMES.get(base.lower())
        if known:
            return known
    except Exception:  # pragma: no cover
        pass
    if sys.platform == "win32":
        try:
            desc = _api().file_description(exe_path)
            if desc:
                return desc
        except Exception:
            pass
    return os.path.splitext(base)[0]


def list_apps(include_minimized: bool = False) -> List[Dict]:
    """Applications the user can pick for app-only recording.

    One entry per exe that has a visible, uncloaked, non-minimized (unless
    ``include_minimized``), titled top-level window, excluding our own process:
    ``{"exe", "name", "pids", "titles", "hwnds", "path"}`` sorted by name.
    Never raises; returns ``[]`` off Windows.
    """
    if sys.platform != "win32":
        return []
    try:
        api = _api()
        u = api.user32
        own = os.getpid()
        apps: Dict[str, Dict] = {}
        for hwnd in api.enum_hwnds():
            try:
                if not hwnd or not u.IsWindowVisible(hwnd):
                    continue
                if u.IsIconic(hwnd) and not include_minimized:
                    continue
                if api.cloaked(hwnd):
                    continue
                ex = api.ex_style(hwnd)
                if ex & WS_EX_TOOLWINDOW and not ex & WS_EX_APPWINDOW:
                    continue
                title = api.title(hwnd).strip()
                if not title:
                    continue
                if not u.IsIconic(hwnd) and not _valid(api.frame_rect(hwnd)):
                    continue
                pid = api.pid_of(hwnd)
                if not pid or pid == own:
                    continue
                path = api.exe_path(pid)
                if not path:
                    continue
                exe = path.replace("\\", "/").rsplit("/", 1)[-1]
                entry = apps.get(exe.lower())
                if entry is None:
                    entry = apps[exe.lower()] = {"exe": exe, "name": app_display_name(path), "pids": [],
                                                 "titles": [], "hwnds": [], "path": path}
                if pid not in entry["pids"]:
                    entry["pids"].append(pid)
                entry["titles"].append(title)
                entry["hwnds"].append(int(hwnd))
            except Exception:
                continue
        return sorted(apps.values(), key=lambda a: (a["name"].lower(), a["exe"].lower()))
    except Exception:
        return []
