"""App-only masking: geometry, window classification, auto-pause (runs anywhere).

Window lists are fakes (``WinInfo``) in EnumWindows order (top -> bottom), so
everything here runs on macOS/Linux; the Win32 enumeration itself is covered
by the Windows CI recordings.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recorder import app_mask  # noqa: E402
from recorder.app_mask import (  # noqa: E402
    FG_ALLOW, FG_NONE, FG_OTHER, FG_OWN, FG_TARGET, ROLE_COVER, ROLE_REVEAL, ROLE_SKIP,
    WDA_EXCLUDEFROMCAPTURE, WS_EX_LAYERED, WS_EX_TRANSPARENT, AppMasker, AutoPauseController,
    DesktopPeekProbe, ProcessTreeTracker, WinInfo, apply_mask, classify_foreground, classify_window,
    complement, compute_mask, descendant_pids, dpi_to_scale, enumerate_windows, find_peek_window,
    ime_candidate_sized, install_root, normalize, normalize_exe, path_under, pick_capture_monitor, subtract_rect,
    total_area, visible_region)

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy ships with bettercam on Windows
    np = None

W, H = 1920, 1080
SCREEN = (0, 0, W, H)
OWN = 4242
TASKBAR = WinInfo(0x10, 100, "explorer.exe", (0, 1032, W, H))
DESKTOP = WinInfo(0x11, 100, "explorer.exe", (0, 0, W, H))      # Progman, bottom of z-order


def area(rects):
    return total_area(rects)


def covers(rects, x, y):
    """Is capture-local pixel (x, y) painted black?"""
    return any(x0 <= x < x1 and y0 <= y < y1 for x0, y0, x1, y1 in rects)


def mask(windows, rect=(0, 0, W, H), allow=(), target="SLDWORKS.exe"):
    return compute_mask(windows, target, allow, OWN, rect)


POWER_TYPES = ("battery_status", "power_source_change")

class GeometryTest(unittest.TestCase):
    def test_subtract_rect_cases(self):
        r = (0, 0, 10, 10)
        self.assertEqual(subtract_rect(r, (20, 20, 30, 30)), [r])            # disjoint
        self.assertEqual(subtract_rect(r, (-5, -5, 15, 15)), [])             # fully covered
        hole = subtract_rect(r, (2, 3, 5, 6))                                # hole in the middle
        self.assertEqual(area(hole), 100 - 9)
        self.assertEqual(len(hole), 4)
        self.assertEqual(area(subtract_rect(r, (5, -1, 20, 20))), 50)      # right half
        # pieces are disjoint
        self.assertEqual(area(normalize(hole)), area(hole))

    def test_normalize_merges_and_dedupes(self):
        rs = [(0, 0, 5, 10), (5, 0, 10, 10), (0, 0, 10, 10)]
        self.assertEqual(normalize(rs), [(0, 0, 10, 10)])
        self.assertEqual(normalize([(0, 0, 10, 5), (0, 5, 10, 10)]), [(0, 0, 10, 10)])
        self.assertEqual(normalize([(0, 0, 0, 5), (3, 3, 1, 1)]), [])

    def test_complement(self):
        self.assertEqual(complement([], (0, 0, 10, 10)), [(0, 0, 10, 10)])
        self.assertEqual(complement([(0, 0, 10, 10)], (0, 0, 10, 10)), [])
        black = complement([(2, 2, 8, 8)], (0, 0, 10, 10))
        self.assertEqual(area(black), 100 - 36)
        self.assertLessEqual(len(black), 4)

    def test_visible_region_walks_bottom_to_top(self):
        layers = [((0, 0, 50, 50), ROLE_COVER),       # top: covers part of the target
                  ((0, 0, 100, 100), ROLE_REVEAL)]    # bottom: target
        vis = visible_region(layers, (0, 0, 100, 100))
        self.assertEqual(area(vis), 100 * 100 - 50 * 50)
        # reversed z-order: target on top of the other window -> fully visible
        vis = visible_region(list(reversed(layers)), (0, 0, 100, 100))
        self.assertEqual(area(vis), 100 * 100)

    def test_many_overlapping_windows_stay_bounded(self):
        layers = []
        for i in range(60):
            layers.append(((i * 13 % 1700, i * 7 % 900, i * 13 % 1700 + 300, i * 7 % 900 + 200),
                           ROLE_COVER if i % 2 else ROLE_REVEAL))
        vis = visible_region(layers, SCREEN)
        black = complement(vis, SCREEN)
        self.assertEqual(area(vis) + area(black), W * H)
        self.assertLess(len(black), 200)


class ScenarioTest(unittest.TestCase):
    def test_maximized_target_masks_only_taskbar(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        res = mask([TASKBAR, target, DESKTOP])
        self.assertTrue(res.target_visible)
        self.assertEqual(res.black_rects, [(0, 1032, W, H)])

    def test_toast_over_target_is_masked(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        toast = WinInfo(2, 300, "ShellExperienceHost.exe", (1500, 800, 1900, 1000),
                        class_name="Windows.UI.Core.CoreWindow")
        wechat = WinInfo(3, 301, "WeChat.exe", (1600, 600, 1900, 760))   # chat pop-up, other process
        res = mask([toast, wechat, TASKBAR, target, DESKTOP])
        for x, y in ((1600, 900), (1700, 700), (10, 1050)):
            self.assertTrue(covers(res.black_rects, x, y), (x, y))
        self.assertFalse(covers(res.black_rects, 100, 100))
        self.assertEqual(area(res.black_rects),
                         400 * 200 + 300 * 160 + W * (H - 1032))

    def test_target_behind_other_window(self):
        other = WinInfo(5, 400, "chrome.exe", (100, 100, 900, 700))
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, 1200, 900))
        res = mask([other, target, TASKBAR, DESKTOP])
        self.assertTrue(covers(res.black_rects, 500, 400))      # chrome over the target
        self.assertFalse(covers(res.black_rects, 1000, 50))     # target still visible
        self.assertTrue(covers(res.black_rects, 1500, 50))      # desktop
        self.assertEqual(area(res.black_rects), W * H - (1200 * 900 - 800 * 600))

    def test_same_process_menus_and_dialogs_stay_visible(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, 1000, 800))
        menu = WinInfo(7, 200, "SLDWORKS.exe", (900, 100, 1300, 500))     # popup menu / dialog
        res = mask([menu, target, DESKTOP])
        self.assertFalse(covers(res.black_rects, 1200, 300))

    def test_owned_dialog_from_other_process_is_revealed(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, 1000, 800))
        printer = WinInfo(8, 900, "splwow64.exe", (1100, 100, 1500, 500), owner_exes=("SLDWORKS.exe",))
        stranger = WinInfo(9, 901, "splwow64.exe", (1100, 600, 1500, 900))
        res = mask([printer, stranger, target, DESKTOP])
        self.assertFalse(covers(res.black_rects, 1200, 200))
        self.assertTrue(covers(res.black_rects, 1200, 700))

    def test_own_overlay_excluded_from_capture_is_ignored(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        bar = WinInfo(20, OWN, "python.exe", (700, 0, 1220, 40), affinity=WDA_EXCLUDEFROMCAPTURE)
        res = mask([bar, TASKBAR, target, DESKTOP])
        self.assertFalse(covers(res.black_rects, 800, 20))      # no black box under the bar
        # same bar without exclusion is in the pixels -> masked
        bar.affinity = 0
        res = mask([bar, TASKBAR, target, DESKTOP])
        self.assertTrue(covers(res.black_rects, 800, 20))

    def test_fullscreen_click_through_overlay_does_not_occlude(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        gpu = WinInfo(30, 500, "NVIDIA Share.exe", SCREEN, ex_style=WS_EX_LAYERED | WS_EX_TRANSPARENT)
        res = mask([gpu, TASKBAR, target, DESKTOP])
        self.assertEqual(res.black_rects, [(0, 1032, W, H)])
        role, reason = classify_window(gpu, "sldworks.exe", (), OWN, SCREEN)
        self.assertEqual((role, reason), (ROLE_SKIP, "overlay"))
        # a *small* click-through pop-up from another app is still masked
        osd = WinInfo(31, 501, "volume.exe", (100, 100, 300, 200), ex_style=WS_EX_LAYERED | WS_EX_TRANSPARENT)
        res = mask([osd, TASKBAR, target, DESKTOP])
        self.assertTrue(covers(res.black_rects, 150, 150))

    def test_target_click_through_window_never_reveals_what_is_under_it(self):
        target_overlay = WinInfo(40, 200, "SLDWORKS.exe", SCREEN, ex_style=WS_EX_LAYERED | WS_EX_TRANSPARENT)
        chat = WinInfo(41, 300, "QQ.exe", (0, 0, 400, 400))
        res = mask([target_overlay, chat, DESKTOP])
        self.assertTrue(covers(res.black_rects, 100, 100))
        self.assertFalse(res.target_visible)

    def test_cloaked_minimized_and_hidden_windows_are_ignored(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        ghosts = [WinInfo(50, 600, "Calculator.exe", SCREEN, cloaked=True),
                  WinInfo(51, 601, "notepad.exe", SCREEN, minimized=True),
                  WinInfo(52, 602, "x.exe", SCREEN, visible=False)]
        res = mask(ghosts + [TASKBAR, target, DESKTOP])
        self.assertEqual(res.black_rects, [(0, 1032, W, H)])

    def test_allow_listed_ime_window_stays_visible(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        ime = WinInfo(60, 700, "TextInputHost.exe", (500, 980, 900, 1060))
        res = compute_mask([ime, TASKBAR, target, DESKTOP], "SLDWORKS.exe", ["textinputhost.exe"], OWN, SCREEN,
                           foreground_state=FG_TARGET)
        self.assertFalse(covers(res.black_rects, 600, 1040))     # IME candidates over the taskbar
        self.assertTrue(covers(res.black_rects, 100, 1050))

    def test_target_not_on_screen_blacks_out_everything(self):
        res = mask([TASKBAR, DESKTOP, WinInfo(1, 200, "SLDWORKS.exe", (3000, 0, 3800, 600))])
        self.assertFalse(res.target_visible)
        self.assertEqual(res.black_rects, [SCREEN])

    def test_second_monitor_uses_capture_local_coordinates(self):
        rect = (1920, -200, 2560, 1440)            # monitor right of the primary, offset up
        target = WinInfo(1, 200, "SLDWORKS.exe", (2000, 0, 3000, 600))
        res = mask([target], rect=rect)
        self.assertFalse(covers(res.black_rects, 2000 - 1920 + 10, 0 + 200 + 10))
        self.assertTrue(covers(res.black_rects, 5, 5))
        self.assertEqual(area(res.black_rects), 2560 * 1440 - 1000 * 600)
        for x0, y0, x1, y1 in res.black_rects:
            self.assertTrue(0 <= x0 < x1 <= 2560 and 0 <= y0 < y1 <= 1440)

    def test_exe_match_is_case_insensitive(self):
        self.assertEqual(normalize_exe("SLDWORKS"), "sldworks.exe")
        self.assertEqual(normalize_exe(r'"C:\Program Files\SW\SLDWORKS.exe"'), "sldworks.exe")
        res = mask([WinInfo(1, 200, "sldworks.EXE", SCREEN)])
        self.assertEqual(res.black_rects, [])


class ForegroundTest(unittest.TestCase):
    def test_classify_foreground(self):
        t, allow = "sldworks.exe", {"textinputhost.exe"}
        self.assertEqual(classify_foreground(1, "SLDWORKS.exe", t, allow, OWN), FG_TARGET)
        self.assertEqual(classify_foreground(OWN, "python.exe", t, allow, OWN), FG_OWN)
        self.assertEqual(classify_foreground(3, "TextInputHost.exe", t, allow, OWN), FG_ALLOW)
        self.assertEqual(classify_foreground(4, "WeChat.exe", t, allow, OWN), FG_OTHER)
        self.assertEqual(classify_foreground(0, "", t, allow, OWN), FG_NONE)
        self.assertEqual(classify_foreground(5, "splwow64.exe", t, allow, OWN, ("SLDWORKS.exe",)), FG_TARGET)
        self.assertEqual(classify_foreground(6, "", t, allow, OWN), FG_OTHER)   # unreadable process


class MaskerTest(unittest.TestCase):
    def make(self, windows, fg=None, **kw):
        self.windows = windows
        self.fg = fg or {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe"}
        self.now = 0.0
        kw.setdefault("clock_fn", lambda: self.now)
        return AppMasker("SLDWORKS.exe", own_pid=OWN, capture_rect=(0, 0, W, H),
                         enumerate_fn=lambda bounds: list(self.windows),
                         foreground_fn=lambda: self.fg, **kw)

    def test_mask_is_cached_while_layout_is_unchanged(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        m = self.make([TASKBAR, target, DESKTOP])
        first = m.compute()
        for _ in range(5):
            again = m.compute()
        self.assertEqual(m.recomputes, 1)
        self.assertIs(again.black_rects, first.black_rects)
        self.assertTrue(again.foreground_is_target)
        self.assertEqual(again.foreground_exe, "SLDWORKS.exe")

    def test_revealed_pixels_stay_black_for_hold_ms(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        popup = WinInfo(2, 301, "WeChat.exe", (100, 100, 500, 400))
        m = self.make([popup, TASKBAR, target, DESKTOP])
        self.assertEqual(m.hold_ms, 350.0)
        self.assertTrue(covers(m.compute().black_rects, 200, 200))
        self.now = 1000.0
        self.windows = [TASKBAR, target, DESKTOP]                 # pop-up closed (its fade-out still drawn)
        held = m.compute()
        self.assertTrue(held.recomputed)
        self.assertTrue(covers(held.black_rects, 200, 200))
        for t in (1016.7, 1100.0, 1349.0):                        # 60 fps frames inside the hold
            self.now = t
            self.assertTrue(covers(m.compute().black_rects, 200, 200), t)
        self.now = 1351.0
        after = m.compute()
        self.assertFalse(covers(after.black_rects, 200, 200))
        self.assertEqual(after.black_rects, [(0, 1032, W, H)])
        self.assertEqual(m.recomputes, 2)
        self.now = 1400.0
        self.assertIs(m.compute().black_rects, after.black_rects)   # cached again once the hold expired

    def test_maximize_keeps_the_old_surroundings_black_during_the_animation(self):
        normal = WinInfo(1, 200, "SLDWORKS.exe", (200, 100, 1200, 800))
        m = self.make([TASKBAR, normal, DESKTOP])
        before = m.compute().black_rects
        self.assertTrue(covers(before, 1500, 500))               # desktop / WeChat behind: black
        self.now = 500.0
        self.windows = [TASKBAR, WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032)), DESKTOP]   # maximized
        for t in (500.0, 533.3, 700.0, 849.0):                   # DWM still growing the window visual
            self.now = t
            res = m.compute()
            self.assertTrue(covers(res.black_rects, 1500, 500), t)
            self.assertTrue(covers(res.black_rects, 50, 50), t)
            self.assertFalse(covers(res.black_rects, 600, 400), t)   # the target itself stays visible
        self.now = 851.0
        self.assertEqual(m.compute().black_rects, [(0, 1032, W, H)])

    def test_newly_covered_pixels_are_black_at_once(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        m = self.make([TASKBAR, target, DESKTOP])
        self.assertFalse(covers(m.compute().black_rects, 200, 200))
        self.now = 33.0
        self.windows = [WinInfo(2, 301, "WeChat.exe", (100, 100, 500, 400)), TASKBAR, target, DESKTOP]
        self.assertTrue(covers(m.compute().black_rects, 200, 200))

    def test_unchanged_capture_rect_keeps_the_hold(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        m = self.make([WinInfo(2, 301, "WeChat.exe", (100, 100, 500, 400)), TASKBAR, target, DESKTOP])
        m.compute()
        self.windows = [TASKBAR, target, DESKTOP]
        self.now = 10.0
        m.compute()
        m.set_capture_rect((0, 0, W, H))                          # segment restart on the same monitor
        self.now = 50.0
        res = m.compute()
        self.assertTrue(covers(res.black_rects, 200, 200))
        self.assertFalse(res.recomputed)                          # layout cache survived too

    def test_first_segment_rearms_the_last_mask_from_the_first_frame(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        m = self.make([WinInfo(2, 301, "WeChat.exe", (100, 100, 500, 400)), TASKBAR, target, DESKTOP])
        m.compute()                                               # a mask is known before the first clip
        self.windows = [TASKBAR, target, DESKTOP]
        self.now = 5000.0
        m.begin_segment()                                         # first clip
        self.now = 5400.0                                         # first frame after a slow backend start
        for t in (5400.0, 5600.0, 5749.0):
            self.now = t
            res = m.compute()
            self.assertTrue(covers(res.black_rects, 200, 200), t)
            self.assertFalse(covers(res.black_rects, 1000, 600), t)   # pre-pause mask, not a black frame
        self.now = 5751.0
        self.assertEqual(m.compute().black_rects, [(0, 1032, W, H)])
        self.assertEqual(m.stats()["segment_holds"], 1)

    def test_later_segments_start_with_a_black_frame(self):
        """Resume after WeChat was moved during the pause and then minimized: its
        minimize animation and its new place are outside the pre-pause mask."""
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        m = self.make([TASKBAR, target, DESKTOP])
        m.begin_segment()                                         # first clip
        m.compute()
        self.now = 1000.0
        self.windows = [WinInfo(2, 301, "WeChat.exe", (100, 100, 700, 600)), TASKBAR, target, DESKTOP]
        m.compute()                                               # WeChat in front -> auto-pause
        # paused: WeChat moved to (1000, 300, 1500, 700), then minimized -> auto-resume
        self.windows = [WinInfo(2, 0, "", (0, 0, 0, 0), minimized=True), TASKBAR, target, DESKTOP]
        self.now = 9000.0
        m.begin_segment()                                         # resume (no compute while paused)
        self.now = 9400.0
        for t in (9400.0, 9600.0, 9749.0):
            self.now = t
            res = m.compute()
            self.assertEqual(res.black_rects, [(0, 0, W, H)], t)
            for x, y in ((650, 700), (1200, 500), (300, 300)):   # minimize path, moved rect, target
                self.assertTrue(covers(res.black_rects, x, y), (t, x, y))
        self.now = 9751.0
        self.assertEqual(m.compute().black_rects, [(0, 1032, W, H)])
        self.assertEqual(m.stats()["segment_holds"], 2)

    def test_begin_segment_first_flag_can_be_given(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        m = self.make([TASKBAR, target, DESKTOP])
        m.compute()
        m.begin_segment(first=False)                              # e.g. a restart: black frame
        self.now = 10.0
        self.assertEqual(m.compute().black_rects, [(0, 0, W, H)])
        self.now = 1000.0
        m.compute()
        m.begin_segment(first=True)                               # explicit first: last mask only
        self.now = 1010.0
        self.assertEqual(m.compute().black_rects, [(0, 1032, W, H)])

    def test_begin_segment_without_a_known_mask_is_fully_black(self):
        m = self.make([TASKBAR, WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032)), DESKTOP])
        m.begin_segment()                                         # first clip: nothing known yet
        self.now = 100.0
        self.assertEqual(m.compute().black_rects, [(0, 0, W, H)])
        self.now = 449.0
        self.assertEqual(m.compute().black_rects, [(0, 0, W, H)])
        self.now = 451.0
        self.assertEqual(m.compute().black_rects, [(0, 1032, W, H)])

    def test_minimizing_a_covering_window_blacks_the_whole_frame(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        m = self.make([WinInfo(2, 301, "WeChat.exe", (100, 100, 700, 600)), TASKBAR, target, DESKTOP])
        m.compute()
        self.now = 1000.0
        self.windows = [WinInfo(2, 0, "", (0, 0, 0, 0), minimized=True), TASKBAR, target, DESKTOP]
        for t in (1000.0, 1100.0, 1349.0):                        # shrinking toward its taskbar button
            self.now = t
            res = m.compute()
            self.assertEqual(res.black_rects, [(0, 0, W, H)], t)
            self.assertTrue(covers(res.black_rects, 650, 700), t)  # outside its old rect
        self.now = 1351.0
        self.assertEqual(m.compute().black_rects, [(0, 1032, W, H)])
        st = m.stats()
        self.assertEqual((st["animation_holds"], st["last_animation"]), (1, "minimize"))

    def test_restoring_a_minimized_window_blacks_the_whole_frame(self):
        """Taskbar click / Alt+Tab to minimized WeChat: the zoom-up crosses the
        target outside WeChat's final rect, inside the auto-pause debounce."""
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        m = self.make([WinInfo(2, 0, "", (0, 0, 0, 0), minimized=True), TASKBAR, target, DESKTOP])
        self.assertFalse(covers(m.compute().black_rects, 650, 700))
        self.now = 2000.0
        self.windows = [WinInfo(2, 301, "WeChat.exe", (100, 100, 500, 400)), TASKBAR, target, DESKTOP]
        for t in (2000.0, 2200.0, 2349.0):
            self.now = t
            res = m.compute()
            self.assertTrue(covers(res.black_rects, 300, 300), t)    # final rect: black at once
            self.assertTrue(covers(res.black_rects, 650, 700), t)    # zoom path: black too
        self.now = 2351.0
        res = m.compute()
        self.assertTrue(covers(res.black_rects, 300, 300))
        self.assertFalse(covers(res.black_rects, 650, 700))
        self.assertEqual(m.stats()["last_animation"], "restore")

    def test_minimize_of_the_target_or_of_windows_elsewhere_is_not_a_full_hold(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        helper = WinInfo(3, 400, "Helper.exe", (1200, 100, 1800, 600))           # allow-listed: revealed
        far = WinInfo(4, 401, "chrome.exe", (2500, 0, 3500, 900))                # another monitor
        m = self.make([helper, far, TASKBAR, target, DESKTOP], allow_exes=["Helper.exe"])
        m.compute()
        self.now = 100.0
        self.windows = [WinInfo(3, 0, "", minimized=True), WinInfo(4, 0, "", minimized=True),
                        TASKBAR, target, DESKTOP]
        self.assertEqual(m.compute().black_rects, [(0, 1032, W, H)])       # neither covered the target
        self.now = 200.0
        self.windows = [helper, WinInfo(4, 0, "", minimized=True), TASKBAR, target, DESKTOP]
        m.compute()                                                          # allow-listed restore
        self.assertEqual(m.stats()["animation_holds"], 0)
        self.now = 300.0
        self.windows = [helper, far, TASKBAR, target, DESKTOP]
        # a covering window restored anywhere: its zoom may start at a taskbar
        # button on this monitor (the real enumeration only reports it here)
        self.assertEqual(m.compute().black_rects, [(0, 0, W, H)])
        self.assertEqual(m.stats()["animation_holds"], 1)
        # the target itself: minimize -> black at once; restore -> the usual reveal hold
        m2 = self.make([TASKBAR, target, DESKTOP])
        m2.compute()
        self.now = 1000.0
        self.windows = [TASKBAR, WinInfo(1, 0, "", minimized=True), DESKTOP]
        self.assertEqual(m2.compute().black_rects, [(0, 0, W, H)])
        self.now = 2000.0
        self.windows = [TASKBAR, target, DESKTOP]
        self.assertEqual(m2.compute().black_rects, [(0, 0, W, H)])
        self.now = 2351.0
        self.assertEqual(m2.compute().black_rects, [(0, 1032, W, H)])
        self.assertEqual(m2.stats()["animation_holds"], 0)

    def make_filtered(self, windows, **kw):
        """:meth:`make` with an enumeration that filters like
        :func:`enumerate_windows`: on-screen windows that miss the bounds are
        dropped, minimized (and cloaked) ones are always reported."""
        self.windows = windows
        self.fg = {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe"}
        self.now = 0.0
        kw.setdefault("clock_fn", lambda: self.now)

        def enumerate_fn(bounds):
            return [w for w in self.windows
                    if w.minimized or w.cloaked or app_mask.intersect(w.rect, bounds) is not None]
        return AppMasker("SLDWORKS.exe", own_pid=OWN, capture_rect=(0, 0, W, H), enumerate_fn=enumerate_fn,
                         foreground_fn=lambda: self.fg, **kw)

    def test_minimize_and_restore_on_another_monitor_black_the_whole_frame(self):
        """Taskbar buttons on all taskbars: a window restored from this
        monitor's taskbar onto another monitor, or minimized on another monitor
        toward a button here, zooms across the target -- yet the enumeration
        reports it here only while it is minimized."""
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        mini = WinInfo(2, 0, "", minimized=True)
        far = WinInfo(2, 301, "WeChat.exe", (2100, 100, 2900, 700))           # on monitor 1
        steady, full = [(0, 1032, W, H)], [(0, 0, W, H)]
        m = self.make_filtered([mini, TASKBAR, target, DESKTOP])
        self.assertEqual(m.compute().black_rects, steady)                      # minimized before the start
        self.now = 1000.0
        self.windows = [far, TASKBAR, target, DESKTOP]                         # restored onto monitor 1
        self.assertEqual([w.hwnd for w in m._enumerate(m._bounds)], [0x10, 1, 0x11])
        for t in (1000.0, 1200.0, 1349.0):
            self.now = t
            self.assertEqual(m.compute().black_rects, full, t)
        self.now = 1351.0
        self.assertEqual(m.compute().black_rects, steady)
        st = m.stats()
        self.assertEqual((st["animation_holds"], st["last_animation"]), (1, "restore"))
        self.now = 2000.0
        self.assertEqual(m.compute().black_rects, steady)                      # stays on monitor 1
        self.windows = [mini, TASKBAR, target, DESKTOP]                        # minimized from monitor 1
        for t in (2000.0, 2200.0, 2349.0):
            self.now = t
            self.assertEqual(m.compute().black_rects, full, t)
        self.now = 2351.0
        self.assertEqual(m.compute().black_rects, steady)
        self.now = 3000.0
        self.assertEqual(m.compute().black_rects, steady)                      # stays minimized
        st = m.stats()
        self.assertEqual((st["animation_holds"], st["last_animation"]), (2, "minimize"))
        # a new capture rect starts a new comparison: what was minimized there is not a restore here
        m.set_capture_rect((W, 0, W, H))
        self.windows = [far, TASKBAR, target, DESKTOP]
        self.now = 4000.0
        m.compute()
        self.assertEqual(m.stats()["animation_holds"], 2)

    def test_capture_rect_change_turns_held_masks_fully_black(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, 3000, 1032))
        m = self.make([TASKBAR, target, DESKTOP])
        m.compute()
        m.begin_segment()
        m.set_capture_rect((0, 0, 1280, 720))                     # the backend corrected the rect
        self.now = 10.0
        self.assertEqual(m.compute().black_rects, [(0, 0, 1280, 720)])
        self.now = 400.0
        self.assertEqual(m.compute().black_rects, [])

    def test_hold_can_be_disabled(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        m = self.make([WinInfo(2, 301, "WeChat.exe", (100, 100, 500, 400)), TASKBAR, target, DESKTOP], hold_ms=0)
        m.compute()
        self.windows = [TASKBAR, target, DESKTOP]
        self.assertFalse(covers(m.compute().black_rects, 200, 200))

    def test_failure_stays_black_for_the_hold_after_recovery(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        m = self.make([TASKBAR, target, DESKTOP])
        good = m._enumerate

        def boom(bounds):
            raise OSError("access denied")
        m._enumerate = boom
        self.assertEqual(m.compute().black_rects, [(0, 0, W, H)])
        m._enumerate = good
        self.now = 200.0
        self.assertEqual(m.compute().black_rects, [(0, 0, W, H)])
        self.now = 351.0
        self.assertEqual(m.compute().black_rects, [(0, 1032, W, H)])

    def test_enumeration_failure_fails_closed(self):
        m = self.make([])

        def boom(bounds):
            raise OSError("access denied")
        m._enumerate = boom
        res = m.compute()
        self.assertTrue(res.failed)
        self.assertEqual(res.black_rects, [(0, 0, W, H)])
        self.assertEqual(m.failures, 1)

    def test_capture_rect_change_resets_cache(self):
        m = self.make([WinInfo(1, 200, "SLDWORKS.exe", (0, 0, 100, 100))])
        m.compute()
        m.set_capture_rect((1920, 0, 1280, 1024))
        res = m.compute()
        self.assertEqual(res.black_rects, [(0, 0, 1280, 1024)])
        self.assertEqual(m.capture_rect, (1920, 0, 1280, 1024))

    def test_default_allow_list_and_foreground_state(self):
        m = self.make([], fg={"hwnd": 9, "pid": 77, "exe": "ctfmon.exe"})
        self.assertIn("textinputhost.exe", m.allow)
        self.assertEqual(m.foreground_state()["state"], FG_ALLOW)
        self.fg = {"hwnd": 9, "pid": 78, "exe": "DingTalk.exe"}
        self.assertEqual(m.foreground_state()["state"], FG_OTHER)
        self.fg = {"hwnd": 0, "pid": 0, "exe": ""}
        self.assertEqual(m.foreground_state()["state"], FG_NONE)

    def test_target_rects_for_monitor_choice(self):
        m = self.make([WinInfo(1, 200, "SLDWORKS.exe", (2000, 0, 3000, 900)),
                       WinInfo(2, 200, "SLDWORKS.exe", (0, 0, 10, 10), minimized=True),
                       WinInfo(3, 300, "x.exe", (0, 0, 500, 500))])
        self.assertEqual(m.target_rects(), [(2000, 0, 3000, 900)])

    def test_empty_target_is_rejected(self):
        with self.assertRaises(ValueError):
            AppMasker("  ", own_pid=OWN)


THUMB_CLS = "TaskListThumbnailWnd"


class PeekTest(MaskerTest):
    """Finding 3: taskbar thumbnail / task-switcher peek blacks the whole frame."""

    def test_thumbnail_blacks_the_whole_frame_and_holds_after_it_hides(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        thumb = WinInfo(70, 100, "explorer.exe", (800, 850, 1100, 1032), class_name=THUMB_CLS)
        m = self.make([thumb, TASKBAR, target, DESKTOP])
        res = m.compute()
        self.assertEqual(res.black_rects, [(0, 0, W, H)])
        self.assertEqual(res.peek, THUMB_CLS)
        self.assertFalse(res.target_visible)
        self.windows = [TASKBAR, target, DESKTOP]                 # mouse left the thumbnail
        self.now = 2000.0
        self.assertEqual(m.compute().black_rects, [(0, 0, W, H)])
        self.now = 2349.0
        self.assertEqual(m.compute().black_rects, [(0, 0, W, H)])
        self.now = 2351.0
        res = m.compute()
        self.assertEqual(res.black_rects, [(0, 1032, W, H)])
        self.assertEqual(res.peek, "")
        self.assertEqual(m.stats()["peek_frames"], 1)
        self.assertEqual(m.stats()["last_peek"], THUMB_CLS)

    def test_thumbnail_on_another_monitor_still_counts(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        thumb = WinInfo(70, 100, "explorer.exe", (2500, 1200, 2800, 1392), class_name=THUMB_CLS)
        self.assertEqual(self.make([thumb, TASKBAR, target, DESKTOP]).compute().black_rects, [(0, 0, W, H)])

    def test_hidden_cloaked_or_foreign_peek_classes_are_ignored(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        ghosts = [WinInfo(70, 100, "explorer.exe", (800, 850, 1100, 1032), class_name=THUMB_CLS, visible=False),
                  WinInfo(71, 100, "explorer.exe", (800, 850, 1100, 1032), class_name=THUMB_CLS, cloaked=True),
                  WinInfo(72, 100, "explorer.exe", (0, 0, 0, 0), class_name=THUMB_CLS),
                  # the target's own XAML pop-up is not a shell peek host
                  WinInfo(73, 200, "SLDWORKS.exe", (10, 10, 300, 200), class_name="Xaml_WindowedPopupClass")]
        res = self.make(ghosts + [TASKBAR, target, DESKTOP]).compute()
        self.assertEqual(res.black_rects, [(0, 1032, W, H)])

    def test_windows11_and_alt_tab_hosts_and_extension(self):
        for cls in ("XamlExplorerHostIslandWindow", "Xaml_WindowedPopupClass", "MultitaskingViewFrame"):
            w = WinInfo(80, 100, "explorer.exe", (600, 300, 1300, 700), class_name=cls)
            self.assertIs(find_peek_window([w]), w, cls)
        custom = WinInfo(81, 999, "", (600, 300, 1300, 700), class_name="MyPeekHost")   # unreadable exe
        self.assertIsNone(find_peek_window([custom]))
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        m = self.make([custom, target], peek_classes=["MyPeekHost", ("other.exe", "OtherHost")])
        self.assertEqual(m.compute().black_rects, [(0, 0, W, H)])
        self.assertIn(("other.exe", "otherhost"), m.peek_classes)
        res = compute_mask([WinInfo(70, 100, "explorer.exe", (800, 850, 1100, 1032), class_name=THUMB_CLS), target],
                           "SLDWORKS.exe", (), OWN, SCREEN)
        self.assertEqual((res.black_rects, res.peek), ([(0, 0, W, H)], THUMB_CLS))

    def test_desktop_peek_probe_blacks_the_frame_and_errors_fail_open(self):
        target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
        self.peek = "desktop_peek_button"
        m = self.make([TASKBAR, target, DESKTOP], peek_fn=lambda: self.peek)
        res = m.compute()
        self.assertEqual((res.black_rects, res.peek), ([(0, 0, W, H)], "desktop_peek_button"))
        self.peek = None
        self.now = 400.0                                          # peek over: still held ...
        self.assertEqual(m.compute().black_rects, [(0, 0, W, H)])
        self.now = 751.0                                          # ... for hold_ms
        self.assertEqual(m.compute().black_rects, [(0, 1032, W, H)])

        def broken():
            raise OSError("no desktop")
        m2 = self.make([TASKBAR, target, DESKTOP], peek_fn=broken)
        self.assertEqual(m2.compute().black_rects, [(0, 1032, W, H)])
        self.assertEqual(m2.stats()["peek_errors"], 1)


class FakeShellApi:
    """What :class:`DesktopPeekProbe` needs from ``_Win32``."""

    def __init__(self):
        self.keys = {}
        self.cursor = (100, 100)
        self.rects = {0x10: (0, 1032, W, H), 0x12: (W - 10, 1032, W, H)}
        self.visible = {0x10: True, 0x12: True}
        self.button = 0x12
        self.finds = 0

    def key_state(self, vk):
        return self.keys.get(vk, 0)

    def cursor_pos(self):
        return self.cursor

    def find_top_windows(self, classes):
        self.finds += 1
        return [0x10]

    def find_descendant(self, parent, path):
        self.assertPath = tuple(path)
        return self.button

    def is_visible(self, hwnd):
        return self.visible.get(hwnd, False)

    def window_rect(self, hwnd):
        return self.rects.get(hwnd, (0, 0, 0, 0))


class DesktopPeekProbeTest(unittest.TestCase):
    def make(self):
        self.api = FakeShellApi()
        self.now = 0.0
        return DesktopPeekProbe(self.api, clock_fn=lambda: self.now)

    def test_win_comma_latches_until_win_is_released(self):
        p = self.make()
        api = self.api
        self.assertIsNone(p())
        api.keys = {p.VK_LWIN: 0x8000}                       # Win alone (Start menu, Win+D...)
        self.assertIsNone(p())
        api.keys = {p.VK_LWIN: 0x8000, p.VK_OEM_COMMA: 0x8000}
        self.assertEqual(p(), "desktop_peek_win_comma")
        api.keys = {p.VK_LWIN: 0x8000}                       # comma released, Win still held: still peeking
        self.assertEqual(p(), "desktop_peek_win_comma")
        api.keys = {}
        self.assertIsNone(p())
        api.keys = {p.VK_RWIN: 0x8000, p.VK_OEM_COMMA: 0x0001}   # quick tap between two polls
        self.assertEqual(p(), "desktop_peek_win_comma")

    def test_cursor_on_the_show_desktop_button(self):
        p = self.make()
        api = self.api
        api.cursor = (W - 5, 1050)
        self.assertEqual(p(), "desktop_peek_button")
        self.assertEqual(api.assertPath, ("TrayNotifyWnd", "TrayShowDesktopButtonWClass"))
        api.cursor = (W - 30, 1050)                          # elsewhere on the taskbar
        self.assertIsNone(p())
        api.cursor = None
        self.assertIsNone(p())

    def test_taskbar_end_is_used_when_the_button_is_missing(self):
        p = self.make()
        api = self.api
        api.button = 0                                       # Windows 11: no classic button window
        api.cursor = (W - 3, 1040)
        self.assertEqual(p(), "desktop_peek_button")
        api.cursor = (W - 20, 1040)
        self.assertIsNone(p())
        # vertical taskbar on the left: the bottom end
        api.rects[0x10] = (0, 0, 62, 1080)
        self.now = 5000.0                                    # cached windows are refreshed
        api.cursor = (30, 1075)
        self.assertEqual(p(), "desktop_peek_button")
        self.assertEqual(api.finds, 2)
        api.visible[0x10] = False                            # auto-hidden / gone
        self.assertIsNone(p())


class ImeHelperTest(unittest.TestCase):
    """Finding 10: IME helpers are revealed only for the target, only candidate-sized."""
    TARGET = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
    CANDIDATES = WinInfo(60, 700, "TextInputHost.exe", (500, 980, 900, 1060))
    CLIPBOARD = WinInfo(61, 700, "TextInputHost.exe", (1400, 400, 1760, 860))     # Win+V panel

    def run_mask(self, windows, fg, allow=("textinputhost.exe",)):
        return compute_mask(windows + [TASKBAR, self.TARGET, DESKTOP], "SLDWORKS.exe", allow, OWN, SCREEN,
                            foreground_state=fg)

    def test_candidates_only_while_the_target_is_in_front(self):
        self.assertFalse(covers(self.run_mask([self.CANDIDATES], FG_TARGET).black_rects, 600, 1040))
        for fg in (FG_OTHER, FG_ALLOW, FG_OWN, FG_NONE, None):     # e.g. pinyin typed into WeChat
            self.assertTrue(covers(self.run_mask([self.CANDIDATES], fg).black_rects, 600, 1040), fg)
        self.assertEqual(classify_window(self.CANDIDATES, "sldworks.exe", {"textinputhost.exe"}, OWN, SCREEN,
                                         fg_state=FG_OTHER), (ROLE_COVER, "ime_background"))

    def test_clipboard_history_and_emoji_panels_stay_black(self):
        res = self.run_mask([self.CLIPBOARD], FG_TARGET)
        self.assertTrue(covers(res.black_rects, 1500, 600))
        self.assertEqual(classify_window(self.CLIPBOARD, "sldworks.exe", {"textinputhost.exe"}, OWN, SCREEN,
                                         fg_state=FG_TARGET), (ROLE_COVER, "ime_panel"))

    def test_panels_stay_black_on_4k_and_ultrawide_displays(self):
        """The panel's share of a 4K / 5120x1440 capture is ~2 %: an area share
        would reveal it; the absolute, DPI-scaled caps do not."""
        allow = ["textinputhost.exe"]
        for cw, ch in ((3840, 2160), (5120, 1440), (W, H)):
            target = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, cw, ch - 48))
            for scale in (1.0, 1.25, 1.5, 2.0):
                pw, ph = int(360 * scale), int(460 * scale)          # Win+V / emoji panel at this scale
                panel = WinInfo(61, 700, "TextInputHost.exe", (1400, 400, 1400 + pw, 400 + ph))
                bar = WinInfo(60, 700, "TextInputHost.exe", (500, ch - 44, 500 + int(400 * scale),
                                                              ch - 44 + int(80 * scale)))   # candidate bar
                res = compute_mask([panel, bar, target], "SLDWORKS.exe", allow, OWN, (0, 0, cw, ch),
                                   foreground_state=FG_TARGET, dpi_scale=scale)
                self.assertTrue(covers(res.black_rects, 1400 + pw // 2, 400 + ph // 2), (cw, ch, scale))
                self.assertFalse(covers(res.black_rects, 510, ch - 30), (cw, ch, scale))   # over the taskbar
                self.assertEqual(classify_window(panel, "sldworks.exe", set(allow), OWN, (0, 0, cw, ch),
                                                 fg_state=FG_TARGET, dpi_scale=scale), (ROLE_COVER, "ime_panel"))

    def test_candidate_size_rule(self):
        self.assertTrue(ime_candidate_sized((0, 0, 1900, 240)))                 # wide but short bar
        self.assertTrue(ime_candidate_sized((0, 0, 200, 300)))                  # tall list, 60,000 px
        self.assertFalse(ime_candidate_sized((0, 0, 260, 300)))                 # 78,000 px
        self.assertFalse(ime_candidate_sized((0, 0, 360, 460)))                 # Win+V panel at 100 %
        self.assertFalse(ime_candidate_sized((0, 0, 0, 0)))
        # scaled by DPI: a 200 % candidate list (400x600) passes, the 200 % panel does not
        self.assertTrue(ime_candidate_sized((0, 0, 400, 600), 2.0))
        self.assertFalse(ime_candidate_sized((0, 0, 400, 600), 1.0))
        self.assertTrue(ime_candidate_sized((0, 0, 1900, 480), 2.0))
        self.assertFalse(ime_candidate_sized((0, 0, 720, 920), 2.0))
        # unknown scale -> 96 DPI (smaller caps: fails closed)
        for bad in (None, 0, -1.0):
            self.assertFalse(ime_candidate_sized((0, 0, 400, 600), bad), bad)
        self.assertFalse(ime_candidate_sized((0, 0, 5000, 2000), 50.0))         # capped at 500 %

    def test_dpi_to_scale(self):
        self.assertEqual(dpi_to_scale(96), 1.0)
        self.assertEqual(dpi_to_scale(144), 1.5)
        self.assertEqual(dpi_to_scale(10_000), 5.0)
        for bad in (None, 0, -96, "x", float("nan")):
            self.assertEqual(dpi_to_scale(bad), 1.0, bad)

    def test_masker_uses_the_monitor_dpi(self):
        tall_list = WinInfo(60, 700, "TextInputHost.exe", (500, 300, 900, 900))   # 400x600: 200 % list
        dpi = {"v": 192}
        asked = []
        now = [0.0]

        def dpi_fn(bounds):
            asked.append(bounds)
            if dpi["v"] == "boom":
                raise OSError("no shcore")
            return dpi["v"]
        fg = {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe"}
        m = AppMasker("SLDWORKS.exe", own_pid=OWN, capture_rect=(0, 0, W, H), clock_fn=lambda: now[0],
                      enumerate_fn=lambda b: [tall_list, TASKBAR, self.TARGET, DESKTOP],
                      foreground_fn=lambda: fg, dpi_fn=dpi_fn, hold_ms=0)
        self.assertFalse(covers(m.compute().black_rects, 700, 600))
        self.assertEqual(asked, [(0, 0, W, H)])
        self.assertEqual(m.stats()["dpi_scale"], 2.0)
        dpi["v"] = None                                       # unknown -> 96
        now[0] = 1000.0
        self.assertFalse(covers(m.compute().black_rects, 700, 600))    # cached for DPI_REFRESH_MS
        now[0] = 2500.0
        self.assertTrue(covers(m.compute().black_rects, 700, 600))
        dpi["v"] = "boom"
        now[0] = 5000.0
        self.assertTrue(covers(m.compute().black_rects, 700, 600))
        self.assertEqual(m.stats()["dpi_errors"], 1)
        self.assertEqual(len(asked), 3)
        # without dpi_fn (fakes / off Windows): 96 DPI
        m2 = AppMasker("SLDWORKS.exe", own_pid=OWN, capture_rect=(0, 0, W, H), clock_fn=lambda: 0.0,
                       enumerate_fn=lambda b: [tall_list, TASKBAR, self.TARGET, DESKTOP],
                       foreground_fn=lambda: fg)
        self.assertTrue(covers(m2.compute().black_rects, 700, 600))
        self.assertEqual(m2.stats()["dpi_scale"], 1.0)

    def test_user_allow_listed_exe_is_still_revealed(self):
        helper = WinInfo(62, 701, "Helper.exe", (1200, 100, 1900, 900))
        for fg in (FG_TARGET, FG_OTHER):
            res = compute_mask([helper, TASKBAR, self.TARGET, DESKTOP], "SLDWORKS.exe",
                               ["textinputhost.exe", "helper.exe"], OWN, SCREEN, foreground_state=fg)
            self.assertFalse(covers(res.black_rects, 1500, 500), fg)

    def test_masker_covers_candidates_as_soon_as_another_app_is_in_front(self):
        fg = {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe"}
        m = AppMasker("SLDWORKS.exe", own_pid=OWN, capture_rect=(0, 0, W, H), clock_fn=lambda: 0.0,
                      enumerate_fn=lambda b: [self.CANDIDATES, TASKBAR, self.TARGET, DESKTOP],
                      foreground_fn=lambda: fg)
        self.assertFalse(covers(m.compute().black_rects, 600, 1040))
        fg = {"hwnd": 9, "pid": 301, "exe": "WeChat.exe"}
        self.assertTrue(covers(m.compute().black_rects, 600, 1040))


ANSYS = r"C:\Program Files\ANSYS Inc\v232"
SYS32 = r"C:\Windows\System32"


class InstallRootTest(unittest.TestCase):
    def test_vendor_dir_below_shared_program_folders(self):
        cases = {
            ANSYS + r"\Framework\bin\Win64\AnsysFWW.exe": r"c:\program files\ansys inc",
            r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe": r"c:\program files (x86)\tencent",
            r"C:\Program Files\Common Files\Vendor\bin\x.exe": r"c:\program files\common files\vendor",
            r"C:\Program Files\WindowsApps\Pkg_1.0_x64\app.exe": r"c:\program files\windowsapps\pkg_1.0_x64",
            r"C:\Users\bob\AppData\Local\Programs\Foo\Foo.exe": r"c:\users\bob\appdata\local\programs\foo",
            r"C:\ProgramData\Vendor\tool.exe": r"c:\programdata\vendor",
            "C:/Program Files/Dassault Systemes/SOLIDWORKS/SLDWORKS.exe": r"c:\program files\dassault systemes",
            r"\\?\C:\Program Files\X\y\z.exe": r"c:\program files\x",
        }
        for path, root in cases.items():
            self.assertEqual(install_root(path), root, path)

    def test_grandparent_fallback_never_a_broad_folder(self):
        cases = {
            r"D:\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe": r"d:\solidworks corp",
            r"D:\Tools\app.exe": r"d:\tools",                          # grandparent is the drive root
            r"C:\Users\bob\Desktop\tool\app.exe": r"c:\users\bob\desktop\tool",
            r"C:\Users\bob\Downloads\app.exe": "",                     # profile root / Downloads: unknown
            r"C:\Users\bob\app.exe": "",
            SYS32 + r"\notepad.exe": "",
            r"C:\Program Files\app.exe": "",                           # no vendor directory
            r"C:\app.exe": "",
            r"\\server\share\app.exe": "",
            r"\\server\share\cad\bin\cad.exe": r"server\share\cad",
            "": "",
            "SLDWORKS.exe": "",
        }
        for path, root in cases.items():
            self.assertEqual(install_root(path), root, path)

    def test_path_under(self):
        root = install_root(ANSYS + r"\Framework\bin\Win64\AnsysFWW.exe")
        self.assertTrue(path_under(ANSYS + r"\aisol\bin\winx64\AnsysWBU.exe", root))
        self.assertTrue(path_under(ANSYS.upper().replace("\\", "/") + "/x.exe", root))
        self.assertFalse(path_under(r"C:\Program Files\ANSYS Inc2\x.exe", root))   # prefix of a sibling
        self.assertFalse(path_under(r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE", root))
        self.assertFalse(path_under("", root))
        self.assertFalse(path_under(ANSYS + r"\x.exe", ""))


class ProcessTreeTest(unittest.TestCase):
    """Finding 11: windows of processes the target launched count as the target --
    only those installed under the target's install root."""
    PROCS = [(4, 0, "System"), (100, 4, "RunWB2.exe"), (110, 100, "AnsysFWW.exe"), (120, 110, "AnsysWBU.exe"),
             (121, 120, "conhost.exe"), (130, 110, "chrome.exe"), (131, 130, "chrome.exe"),
             (140, 110, "Old.exe"), (150, 999, "WeChat.exe"), (160, 110, "Unknown.exe"),
             (170, 110, "EXCEL.EXE"), (180, 110, "Acme.exe"), (181, 180, "conhost.exe"),
             (182, 180, "AnsysSolver.exe"), (190, 110, "AcroRd32.exe")]
    INFO = {100: (10, ANSYS + r"\Framework\bin\Win64\RunWB2.exe"),
            110: (20, ANSYS + r"\Framework\bin\Win64\AnsysFWW.exe"),
            120: (30, ANSYS + r"\aisol\bin\winx64\AnsysWBU.exe"),
            121: (31, SYS32 + r"\conhost.exe"),
            130: (40, r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            131: (41, r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            140: (5, ANSYS + r"\Old.exe"),                               # predates its "parent": PID reuse
            150: (1, r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe"),
            170: (50, r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"),
            180: (60, r"C:\Program Files\Acme\Acme.exe"),               # not deny-listed, installed elsewhere
            181: (61, SYS32 + r"\conhost.exe"),
            182: (62, ANSYS + r"\solver\AnsysSolver.exe"),
            190: (70, r"C:\Program Files (x86)\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe")}

    def info(self, pid):
        self.asked.append(pid)
        return self.INFO.get(pid)

    def test_descendants_with_pid_reuse_install_root_and_deny_list(self):
        self.asked = []
        got = descendant_pids(self.PROCS, "ansysfww", self.info)
        self.assertEqual(got, {120, 121})
        self.assertNotIn(150, self.asked)                    # only the target's subtree is queried
        self.assertNotIn(131, self.asked)                    # nothing below a denied exe
        self.assertNotIn(170, self.asked)                    # denied: not even queried
        self.assertNotIn(182, self.asked)                    # nothing below an app installed elsewhere
        self.assertEqual(descendant_pids(self.PROCS, "AnsysFWW.exe", self.info, own_pid=120), set())
        self.assertEqual(descendant_pids(self.PROCS, "notrunning.exe", self.info), set())
        self.assertEqual(descendant_pids(self.PROCS, "AnsysFWW.exe", lambda pid: None), set())   # fail closed

    def test_launched_apps_installed_elsewhere_never_inherit_even_without_the_deny_list(self):
        self.asked = []
        got = descendant_pids(self.PROCS, "AnsysFWW.exe", self.info, deny_exes=())
        self.assertEqual(got, {120, 121})                    # Excel, Acrobat, Chrome, Acme: outside the root
        self.assertNotIn(181, got)                           # conhost of a non-qualifying parent

    def test_unknown_path_or_root_fails_closed(self):
        times_only = {pid: t for pid, (t, _) in self.INFO.items()}
        self.assertEqual(descendant_pids(self.PROCS, "AnsysFWW.exe", times_only.get), set())   # paths unknown
        no_root = dict(self.INFO)
        no_root[110] = (20, "")
        self.assertEqual(descendant_pids(self.PROCS, "AnsysFWW.exe", no_root.get), set())
        no_child = dict(self.INFO)
        no_child[120] = (30, "")
        self.assertEqual(descendant_pids(self.PROCS, "AnsysFWW.exe", no_child.get), set())

        def boom(pid):
            raise OSError("access denied")
        self.assertEqual(descendant_pids(self.PROCS, "AnsysFWW.exe", boom), set())
        # a target in a broad folder (System32, Downloads...) has no install root
        procs = [(10, 4, "notepad.exe"), (11, 10, "helper.exe")]
        infos = {10: (1, SYS32 + r"\notepad.exe"), 11: (2, SYS32 + r"\helper.exe")}
        self.assertEqual(descendant_pids(procs, "notepad.exe", infos.get, deny_exes=()), set())

    def test_tracker_refreshes_and_fails_closed(self):
        calls = []
        procs = list(self.PROCS)
        infos = dict(self.INFO)

        def snap():
            calls.append(1)
            if procs is None:
                raise OSError("snapshot failed")
            return list(procs)
        now = [0.0]
        t = ProcessTreeTracker("AnsysFWW.exe", snap, infos.get, clock_fn=lambda: now[0])
        self.assertEqual(t.pids(), {120, 121})
        now[0] = 100.0
        t.pids()
        t.pids(want=120)                                     # known pid: no early refresh
        self.assertEqual(len(calls), 1)
        procs.append((175, 110, "Mechanical.exe"))
        infos[175] = (55, ANSYS + r"\aisol\bin\winx64\Mechanical.exe")
        self.assertNotIn(175, t.pids(want=175))              # too soon (< retry_ms)
        now[0] = 300.0
        self.assertIn(175, t.pids(want=175))                 # unknown foreground pid: early refresh
        self.assertEqual(len(calls), 2)
        now[0] = 1400.0
        self.assertIn(175, t.pids())                         # periodic refresh
        self.assertEqual(len(calls), 3)
        procs = None
        now[0] = 2500.0
        self.assertEqual(t.pids(), frozenset())
        self.assertEqual(t.errors, 1)

    def test_tracker_requeries_a_reused_pid_and_unread_paths(self):
        rows = [(100, 4, "AnsysFWW.exe"), (120, 100, "AnsysWBU.exe")]
        infos = {100: (10, ANSYS + r"\AnsysFWW.exe"), 120: (30, "")}     # path not readable yet
        asked = []

        def info(pid):
            asked.append(pid)
            return infos.get(pid)
        now = [0.0]
        t = ProcessTreeTracker("AnsysFWW.exe", lambda: list(rows), info, clock_fn=lambda: now[0])
        self.assertEqual(t.pids(), frozenset())
        infos[120] = (30, ANSYS + r"\AnsysWBU.exe")
        now[0] = 1000.0
        self.assertEqual(t.pids(), {120})                    # an unknown path is asked again
        n = len(asked)
        now[0] = 2000.0
        self.assertEqual(t.pids(), {120})
        self.assertEqual(len(asked), n)                      # known (time, path) cached
        rows[1] = (120, 100, "Other.exe")                    # pid 120 died and was reused ...
        infos[120] = (5, ANSYS + r"\Other.exe")              # ... re-read: older than the target
        now[0] = 3000.0
        self.assertEqual(t.pids(), frozenset())

    def test_masker_reveals_helper_windows_and_counts_them_as_foreground_target(self):
        rows = [(200, 4, "RunWB2.exe"), (210, 200, "AnsysFWW.exe"), (220, 210, "AnsysWBU.exe"),
                (230, 210, "EXCEL.EXE"), (240, 210, "Acme.exe")]
        infos = {200: (1, ANSYS + r"\RunWB2.exe"), 210: (2, ANSYS + r"\Framework\AnsysFWW.exe"),
                 220: (3, ANSYS + r"\aisol\AnsysWBU.exe"),
                 230: (4, r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"),
                 240: (5, r"C:\Program Files\Acme\Acme.exe")}
        fg = {"hwnd": 5, "pid": 220, "exe": "AnsysWBU.exe"}
        target = WinInfo(1, 210, "AnsysFWW.exe", (0, 0, 900, 1032))
        mech = WinInfo(5, 220, "AnsysWBU.exe", (300, 100, 1900, 1000))     # unowned top-level window
        m = AppMasker("AnsysFWW.exe", own_pid=OWN, capture_rect=(0, 0, W, H), clock_fn=lambda: 0.0,
                      enumerate_fn=lambda b: list(self.wins), foreground_fn=lambda: fg,
                      process_fns=(lambda: rows, infos.get))
        self.wins = [mech, TASKBAR, target, DESKTOP]
        self.assertEqual(m.foreground_state()["state"], FG_TARGET)
        res = m.compute()
        self.assertFalse(covers(res.black_rects, 1500, 500))
        self.assertEqual(m.last_reasons.get("target_descendant"), 1)
        self.assertTrue(m.is_target_pid(220))
        self.assertFalse(m.is_target_pid(200))
        self.assertEqual(m.stats()["target_descendants"], [220])
        self.assertEqual(classify_foreground(220, "AnsysWBU.exe", "ansysfww.exe", (), OWN, (), {220}), FG_TARGET)
        # "open after export": Excel / an app installed elsewhere launched by the target stay foreign
        for pid, exe in ((230, "EXCEL.EXE"), (240, "Acme.exe")):
            fg = {"hwnd": 6, "pid": pid, "exe": exe}
            self.wins = [WinInfo(6, pid, exe, (300, 100, 1900, 1000)), TASKBAR, target, DESKTOP]
            self.assertEqual(m.foreground_state()["state"], FG_OTHER, exe)
            self.assertTrue(covers(m.compute().black_rects, 1500, 500), exe)
            self.assertFalse(m.is_target_pid(pid))
        # without process tracking (fakes / off Windows) the helper is a foreign app
        fg = {"hwnd": 5, "pid": 220, "exe": "AnsysWBU.exe"}
        m2 = AppMasker("AnsysFWW.exe", own_pid=OWN, capture_rect=(0, 0, W, H), clock_fn=lambda: 0.0,
                       enumerate_fn=lambda b: [mech, TASKBAR, target, DESKTOP], foreground_fn=lambda: fg)
        self.assertIsNone(m2.procs)
        self.assertEqual(m2.foreground_state()["state"], FG_OTHER)
        self.assertTrue(covers(m2.compute().black_rects, 1500, 500))


class ClickThroughOverlayTest(unittest.TestCase):
    """Finding 25: only known-benign full-screen click-through overlays are skipped."""
    TARGET = WinInfo(1, 200, "SLDWORKS.exe", (0, 0, W, 1032))
    CT = WS_EX_LAYERED | WS_EX_TRANSPARENT

    def test_unknown_fullscreen_click_through_window_is_covered(self):
        for exe in ("GameBar.exe", "danmaku.exe", "wemeetapp.exe", ""):
            layer = WinInfo(30, 500, exe, SCREEN, ex_style=self.CT)
            self.assertEqual(classify_window(layer, "sldworks.exe", (), OWN, SCREEN), (ROLE_COVER, "overlay_unknown"))
            self.assertEqual(mask([layer, TASKBAR, self.TARGET, DESKTOP]).black_rects, [SCREEN], exe)

    def test_known_and_user_listed_overlays_are_skipped(self):
        for exe in ("NVIDIA Overlay.exe", "nvcontainer.exe", "RTSS.exe"):
            layer = WinInfo(30, 500, exe, SCREEN, ex_style=self.CT)
            self.assertEqual(mask([layer, TASKBAR, self.TARGET, DESKTOP]).black_rects, [(0, 1032, W, H)], exe)
        hud = WinInfo(31, 501, "MyHud.exe", SCREEN, ex_style=self.CT)
        m = AppMasker("SLDWORKS.exe", own_pid=OWN, capture_rect=(0, 0, W, H), clock_fn=lambda: 0.0,
                      enumerate_fn=lambda b: [hud, TASKBAR, self.TARGET, DESKTOP],
                      foreground_fn=lambda: {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe"})
        self.assertEqual(m.compute().black_rects, [SCREEN])
        self.assertEqual(m.stats()["covered_unknown_overlays"], ["MyHud.exe"])
        m2 = AppMasker("SLDWORKS.exe", own_pid=OWN, capture_rect=(0, 0, W, H), clock_fn=lambda: 0.0,
                       enumerate_fn=lambda b: [hud, TASKBAR, self.TARGET, DESKTOP],
                       foreground_fn=lambda: {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe"},
                       overlay_exes=["MyHud"])
        self.assertEqual(m2.compute().black_rects, [(0, 1032, W, H)])

    def test_stats_list_the_unknown_overlays_of_the_last_compute(self):
        hud = WinInfo(31, 501, "MyHud.exe", SCREEN, ex_style=self.CT)
        chat = WinInfo(32, 502, "danmaku.exe", SCREEN, ex_style=self.CT)
        fps = WinInfo(33, 503, "RTSS.exe", SCREEN, ex_style=self.CT)               # known: skipped
        tip = WinInfo(34, 504, "tip.exe", (0, 0, 200, 200), ex_style=self.CT)      # not full-screen
        state = {"windows": [hud, chat, fps, tip, TASKBAR, self.TARGET, DESKTOP], "fail": False}

        def enumerate_fn(bounds):
            if state["fail"]:
                raise OSError("EnumWindows failed")
            return list(state["windows"])
        m = AppMasker("SLDWORKS.exe", own_pid=OWN, capture_rect=(0, 0, W, H), clock_fn=lambda: 0.0,
                      enumerate_fn=enumerate_fn,
                      foreground_fn=lambda: {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe"})
        self.assertEqual(m.stats()["last_unknown_overlays"], [])                  # nothing computed yet
        m.compute()
        st = m.stats()
        self.assertEqual(st["last_unknown_overlays"], ["MyHud.exe", "danmaku.exe"])
        self.assertEqual(st["covered_unknown_overlays"], ["MyHud.exe", "danmaku.exe"])
        self.assertEqual(st["last_window_reasons"]["overlay_unknown"], 2)
        json.dumps(st)                                                             # goes into capture.json
        state["windows"] = [chat, TASKBAR, self.TARGET, DESKTOP]                  # the HUD closed
        m.compute()
        st = m.stats()
        self.assertEqual(st["last_unknown_overlays"], ["danmaku.exe"])
        self.assertEqual(st["covered_unknown_overlays"], ["MyHud.exe", "danmaku.exe"])   # cumulative
        state["windows"] = [fps, tip, TASKBAR, self.TARGET, DESKTOP]
        m.compute()
        st = m.stats()
        self.assertEqual(st["last_unknown_overlays"], [])
        self.assertNotIn("overlay_unknown", st["last_window_reasons"])
        self.assertEqual(st["covered_unknown_overlays"], ["MyHud.exe", "danmaku.exe"])
        state["windows"] = [hud, TASKBAR, self.TARGET, DESKTOP]
        m.compute()
        self.assertEqual(m.stats()["last_unknown_overlays"], ["MyHud.exe"])
        state["fail"] = True                                                       # nothing enumerated
        self.assertEqual(m.compute().black_rects, [SCREEN])
        st = m.stats()
        self.assertEqual((st["failures"], st["last_unknown_overlays"]), (1, []))
        self.assertEqual(st["last_window_reasons"], {})     # both fields describe the failed compute

    def test_click_through_windows_of_the_target_side_are_still_skipped(self):
        helper = WinInfo(32, 701, "Helper.exe", SCREEN, ex_style=self.CT)
        owned = WinInfo(33, 900, "splwow64.exe", SCREEN, ex_style=self.CT, owner_exes=("SLDWORKS.exe",))
        child = WinInfo(34, 950, "AnsysWBU.exe", SCREEN, ex_style=self.CT)
        for w, reason in ((helper, "allow_click_through"), (owned, "owned_click_through"),
                          (child, "target_click_through")):
            self.assertEqual(classify_window(w, "sldworks.exe", {"textinputhost.exe", "helper.exe"}, OWN,
                                             SCREEN, target_pids={950}), (ROLE_SKIP, reason))

    def test_click_through_ime_windows_follow_the_ime_rule(self):
        allow = {"textinputhost.exe"}
        full = WinInfo(32, 700, "TextInputHost.exe", SCREEN, ex_style=self.CT)
        bar = WinInfo(35, 700, "TextInputHost.exe", (500, 980, 900, 1060), ex_style=self.CT)
        cases = ((full, None, (ROLE_COVER, "ime_background")),
                 (full, FG_OTHER, (ROLE_COVER, "ime_background")),
                 (full, FG_TARGET, (ROLE_COVER, "ime_panel")),
                 (bar, FG_OTHER, (ROLE_COVER, "ime_background")),
                 (bar, FG_TARGET, (ROLE_SKIP, "allow_click_through")))
        for w, fg, want in cases:
            self.assertEqual(classify_window(w, "sldworks.exe", allow, OWN, SCREEN, fg_state=fg), want, (w, fg))
        res = compute_mask([full, TASKBAR, self.TARGET, DESKTOP], "SLDWORKS.exe", allow, OWN, SCREEN,
                           foreground_state=FG_OTHER)
        self.assertEqual(res.black_rects, [SCREEN])


class FakeEnumApi:
    """What :func:`enumerate_windows` needs from ``_Win32`` (no ctypes)."""

    def __init__(self, wins):
        self.wins = {w["hwnd"]: w for w in wins}
        self.order = [w["hwnd"] for w in wins]
        self.user32 = self

    def enum_hwnds(self):
        return list(self.order)

    def IsWindowVisible(self, h):
        return self.wins[h].get("visible", True)

    def IsIconic(self, h):
        return self.wins[h].get("iconic", False)

    def cloaked(self, h):
        return self.wins[h].get("cloaked", False)

    def frame_rect(self, h):
        return self.wins[h]["rect"]

    def class_name(self, h):
        return self.wins[h]["cls"]

    def pid_of(self, h):
        return self.wins[h]["pid"]

    def exe_name(self, pid):
        return next(w["exe"] for w in self.wins.values() if w["pid"] == pid)

    def ex_style(self, h):
        return 0

    def owners(self, h, limit=8):
        return []

    def affinity(self, h):
        return 0

    def virtual_screen(self):
        return (0, 0, 2 * W, H)


class EnumerateWindowsTest(unittest.TestCase):
    def test_class_names_are_filled_and_offscreen_thumbnails_kept(self):
        fake = FakeEnumApi([
            {"hwnd": 1, "pid": 10, "exe": "explorer.exe", "rect": (2500, 900, 2800, 1032), "cls": THUMB_CLS},
            {"hwnd": 2, "pid": 11, "exe": "explorer.exe", "rect": (-32000, -32000, -31800, -31900), "cls": THUMB_CLS},
            {"hwnd": 3, "pid": 12, "exe": "chrome.exe", "rect": (2000, 0, 3000, 800), "cls": "Chrome_WidgetWin_1"},
            {"hwnd": 4, "pid": 13, "exe": "SLDWORKS.exe", "rect": (0, 0, W, 1032), "cls": "Afx:SW"},
            {"hwnd": 5, "pid": 14, "exe": "x.exe", "rect": (0, 0, 10, 10), "cls": "X", "iconic": True},
        ])
        with patch.object(app_mask, "_api", lambda: fake):
            got = enumerate_windows(OWN, SCREEN, keep_classes={THUMB_CLS.lower()})
            plain = enumerate_windows(OWN, SCREEN)
        self.assertEqual([w.hwnd for w in got], [1, 4, 5])
        self.assertEqual(got[0].class_name, THUMB_CLS)
        self.assertEqual(got[1].class_name, "Afx:SW")
        self.assertTrue(got[2].minimized)
        self.assertEqual([w.hwnd for w in plain], [4, 5])
        self.assertIs(find_peek_window(got), got[0])


class FakeKernel32:
    """``OpenProcess`` / ``GetProcessTimes`` / ``QueryFullProcessImageNameW`` / ``CloseHandle``."""

    def __init__(self, procs):
        self.procs = procs                       # pid -> (ctime or None, path or None)
        self.opened, self.closed = [], []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, pid))
        return 0x5000 + pid if pid in self.procs else 0

    def GetProcessTimes(self, h, c, e, k, u):
        t = self.procs[h - 0x5000][0]
        if t is None:
            return 0
        c._obj.value = t
        return 1

    def QueryFullProcessImageNameW(self, h, flags, buf, n):
        path = self.procs[h - 0x5000][1]
        if path is None or len(path) + 1 > n._obj.value:
            return 0                             # unreadable / ERROR_INSUFFICIENT_BUFFER
        buf.value = path
        n._obj.value = len(path)
        return 1

    def CloseHandle(self, h):
        self.closed.append(h)
        return 1


class FakeDpiApis:
    CONTEXT = 0x7FFE0012                         # a DPI_AWARENESS_CONTEXT handle

    def __init__(self, dpi=144, awareness=2):
        self.dpi, self.rects, self.awareness, self.contexts = dpi, [], awareness, []

    def GetThreadDpiAwarenessContext(self):
        return self.CONTEXT

    def GetAwarenessFromDpiAwarenessContext(self, ctx):
        self.contexts.append(ctx)
        return self.awareness                    # -1 invalid, 0 unaware, 1 system, 2 per-monitor

    def MonitorFromRect(self, rect, flags):
        r = rect._obj
        self.rects.append(((r.left, r.top, r.right, r.bottom), flags))
        return 0x77

    def GetDpiForMonitor(self, hmon, kind, x, y):
        if self.dpi is None:
            return -2147024809                   # E_INVALIDARG
        x._obj.value = y._obj.value = self.dpi
        return 0


class Win32BindingsTest(unittest.TestCase):
    """The Win32 wrappers' logic with fake DLLs (no Windows needed)."""

    def make(self, procs=None, dpi=144):
        import ctypes
        from ctypes import wintypes
        api = object.__new__(app_mask._Win32)    # skip __init__ (it binds real DLLs)

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        api.ctypes, api.W, api.RECT = ctypes, wintypes, RECT
        api.kernel32 = FakeKernel32(procs or {})
        api.user32 = api.shcore = FakeDpiApis(dpi)
        return api

    def test_process_info_reads_time_and_path_from_one_handle(self):
        long_path = "C:\\" + "d" * 1500 + "\\x.exe"
        api = self.make({10: (123, ANSYS + r"\AnsysWBU.exe"), 11: (124, None), 12: (None, "C:\\a.exe"),
                         13: (125, long_path)})
        self.assertEqual(api.process_info(10), (123, ANSYS + r"\AnsysWBU.exe"))
        self.assertEqual(api.kernel32.opened, [(0x1000, 10)])             # PROCESS_QUERY_LIMITED_INFORMATION
        self.assertEqual(api.kernel32.closed, [0x5000 + 10])
        self.assertEqual(api.process_info(11), (124, ""))                 # path unreadable
        self.assertIsNone(api.process_info(12))                           # no times
        self.assertEqual(api.process_info(13), (125, long_path))          # second, larger buffer
        self.assertIsNone(api.process_info(99))                           # OpenProcess failed
        self.assertIsNone(api.process_info(0))
        self.assertEqual(len(api.kernel32.closed), 4)                     # every opened handle closed
        self.assertEqual(api.process_create_time(10), 123)
        with patch.object(app_mask, "_api", lambda: api):
            self.assertEqual(app_mask.win_process_info(10), (123, ANSYS + r"\AnsysWBU.exe"))

    def test_monitor_dpi(self):
        api = self.make(dpi=144)
        self.assertEqual(api.monitor_dpi((1920, 0, 4480, 1440)), 144)
        self.assertEqual(api.user32.rects, [((1920, 0, 4480, 1440), 2)])  # MONITOR_DEFAULTTONEAREST
        api.shcore.dpi = None
        self.assertIsNone(api.monitor_dpi((0, 0, 10, 10)))
        api.shcore = None                                                  # no shcore (Windows < 8.1)
        self.assertIsNone(api.monitor_dpi((0, 0, 10, 10)))
        with patch.object(app_mask, "_api", lambda: self.make(dpi=120)):
            self.assertEqual(app_mask.win_monitor_dpi((0, 0, 10, 10)), 120)

    def test_monitor_dpi_needs_a_per_monitor_aware_thread(self):
        """A system-aware thread can be told the primary monitor's DPI for every
        monitor (192 next to a 200 % primary): unknown instead -> 96, fail closed."""
        api = self.make(dpi=192)
        self.assertTrue(api.thread_per_monitor_aware())
        self.assertEqual(api.monitor_dpi((0, 0, 10, 10)), 192)
        self.assertEqual(api.user32.contexts[-1], FakeDpiApis.CONTEXT)
        for awareness in (1, 0, -1):                       # system aware, unaware, invalid context
            api = self.make(dpi=192)
            api.user32.awareness = awareness
            self.assertFalse(api.thread_per_monitor_aware(), awareness)
            self.assertIsNone(api.monitor_dpi((0, 0, 10, 10)), awareness)
            self.assertEqual(api.user32.rects, [], awareness)             # never asked the monitor
        for missing in ("GetThreadDpiAwarenessContext", "GetAwarenessFromDpiAwarenessContext"):
            api = self.make(dpi=192)                       # Windows 8.1 / 10 before 1607
            setattr(api.user32, missing, None)
            self.assertFalse(api.thread_per_monitor_aware(), missing)
            self.assertIsNone(api.monitor_dpi((0, 0, 10, 10)), missing)
        # the masker turns that into the 96-DPI caps
        api = self.make(dpi=192)
        api.user32.awareness = 1
        with patch.object(app_mask, "_api", lambda: api):
            m = AppMasker("SLDWORKS.exe", own_pid=OWN, capture_rect=SCREEN, clock_fn=lambda: 0.0,
                          enumerate_fn=lambda b: [], foreground_fn=lambda: {}, dpi_fn=app_mask.win_monitor_dpi)
            m.compute()
        self.assertEqual((m.stats()["dpi_scale"], m.stats()["dpi_errors"]), (1.0, 0))

    def test_masker_defaults_stay_off_without_windows(self):
        m = AppMasker("SLDWORKS.exe", own_pid=OWN, capture_rect=SCREEN, enumerate_fn=lambda b: [],
                      foreground_fn=lambda: {})
        self.assertIsNone(m._dpi_fn)
        self.assertIsNone(m.procs)
        self.assertIsNone(m._peek_fn)


class ApplyMaskTest(unittest.TestCase):
    def test_apply_uses_slice_assignment(self):
        calls = []

        class Frame:
            def __setitem__(self, key, value):
                calls.append((key, value))
        apply_mask(Frame(), [(0, 10, 20, 30), (-5, -5, 3, 3), (5, 5, 5, 9)])
        self.assertEqual(calls[0], ((slice(10, 30), slice(0, 20)), 0))
        self.assertEqual(calls[1], ((slice(0, 3), slice(0, 3)), 0))   # clipped, no negative wrap
        self.assertEqual(len(calls), 2)                               # empty rect skipped

    @unittest.skipIf(np is None, "numpy not installed")
    def test_apply_on_numpy_frame(self):
        frame = np.full((6, 8, 4), 200, np.uint8)
        apply_mask(frame, [(0, 0, 3, 2), (6, 4, 8, 6)])
        self.assertTrue((frame[0:2, 0:3] == 0).all())
        self.assertTrue((frame[4:6, 6:8] == 0).all())
        self.assertEqual(int((frame == 0).sum()), (6 + 4) * 4)


@unittest.skipIf(np is None, "numpy not installed")
class CursorSpriteTest(unittest.TestCase):
    def test_sprite_blend_handles_alpha_opaque_and_invert(self):
        from recorder.cursor_draw import blend_sprite, make_sprite
        w, h = 4, 1
        # px0 transparent, px1 opaque red, px2 50% white, px3 inverting (XOR)
        on_black = bytes([0, 0, 0, 0, 0, 0, 255, 255, 128, 128, 128, 255, 255, 255, 255, 255])
        on_white = bytes([255, 255, 255, 255, 0, 0, 255, 255, 255, 255, 255, 255, 0, 0, 0, 255])
        sprite = make_sprite(on_black, on_white, w, h)
        base, k, xo, yo = sprite
        self.assertEqual((xo, yo), (1, 0))              # cropped past the transparent pixel
        frame = np.zeros((3, 6, 4), np.uint8)
        frame[:, :, :3] = 100
        self.assertTrue(blend_sprite(frame, base, k, 2 + xo, 1 + yo))
        row = frame[1, :, :3]
        self.assertEqual(row[2].tolist(), [100, 100, 100])   # transparent px untouched
        self.assertEqual(row[3].tolist(), [0, 0, 255])       # opaque red (BGR)
        self.assertEqual(row[4].tolist(), [178, 178, 178])   # 128 + 100*127/255
        self.assertEqual(row[5].tolist(), [155, 155, 155])   # 255 - 100 (inverted)
        self.assertEqual(frame[0, :, :3].tolist(), [[100] * 3] * 6)

    def test_fully_transparent_cursor_and_offscreen_blend(self):
        from recorder.cursor_draw import blend_sprite, make_sprite
        self.assertIsNone(make_sprite(bytes(16), bytes([255, 255, 255, 0] * 4), 2, 2))
        base = np.zeros((2, 2, 3), np.int32)
        k = np.zeros((2, 2, 3), np.int32)
        frame = np.zeros((4, 4, 4), np.uint8)
        self.assertFalse(blend_sprite(frame, base, k, 10, 10))
        self.assertTrue(blend_sprite(frame, base, k, -1, -1))   # clipped at the corner


class MonitorChoiceTest(unittest.TestCase):
    SCREENS = [
        {"index": 0, "originX": 0, "originY": 0, "width": 1920, "height": 1080, "primary": True,
         "deviceName": r"\\.\DISPLAY1"},
        {"index": 1, "originX": 1920, "originY": 0, "width": 2560, "height": 1440, "primary": False,
         "deviceName": r"\\.\DISPLAY2"},
    ]

    def test_largest_target_window_decides(self):
        mon = pick_capture_monitor(self.SCREENS, [(0, 0, 300, 300), (2000, 100, 4000, 1300)])
        self.assertEqual(mon["index"], 1)

    def test_primary_when_no_window_and_explicit_index(self):
        self.assertEqual(pick_capture_monitor(self.SCREENS, [])["index"], 0)
        self.assertEqual(pick_capture_monitor(self.SCREENS, [(0, 0, 10, 10)], app_monitor=1)["index"], 1)
        with self.assertRaises(ValueError):
            pick_capture_monitor(self.SCREENS, [], app_monitor=5)
        self.assertIsNone(pick_capture_monitor([], []))


class AutoPauseControllerTest(unittest.TestCase):
    def test_debounce_then_pause(self):
        c = AutoPauseController(300)
        self.assertIsNone(c.update(0, FG_OTHER, "recording"))
        self.assertIsNone(c.update(200, FG_OTHER, "recording"))
        self.assertEqual(c.update(300, FG_OTHER, "recording"), "pause")

    def test_brief_switch_and_neutral_windows_do_not_pause(self):
        c = AutoPauseController(300)
        c.update(0, FG_OTHER, "recording")
        self.assertIsNone(c.update(250, FG_TARGET, "recording"))
        self.assertIsNone(c.update(400, FG_OTHER, "recording"))     # debounce restarted
        self.assertIsNone(c.update(500, FG_OWN, "recording"))       # our bar resets it
        self.assertIsNone(c.update(600, FG_OTHER, "recording"))
        self.assertIsNone(c.update(800, FG_ALLOW, "recording"))
        self.assertIsNone(c.update(1200, FG_NONE, "recording"))
        for t in range(1300, 3000, 100):
            self.assertIsNone(c.update(t, FG_OWN, "recording"))

    def test_resume_only_when_target_is_back_and_never_from_manual(self):
        c = AutoPauseController(300)
        self.assertIsNone(c.update(0, FG_OTHER, "auto"))
        self.assertIsNone(c.update(100, FG_OWN, "auto"))
        self.assertIsNone(c.update(150, FG_ALLOW, "auto"))
        self.assertEqual(c.update(200, FG_TARGET, "auto"), "resume")
        for t in range(0, 2000, 100):
            self.assertIsNone(c.update(t, FG_TARGET, "manual"))
            self.assertIsNone(c.update(t, FG_OTHER, "manual"))


# --------------------------------------------------------------- recorder flow
class FakeDesktop:
    def __init__(self):
        self.screens = [{"index": 0, "originX": 0, "originY": 0, "width": W, "height": H,
                         "visibleWidth": W, "visibleHeight": 1032, "scaleFactor": 1.0, "clipIndex": 0,
                         "deviceName": r"\\.\DISPLAY1", "primary": True}]
        self.fg = self.target_fg()

    @staticmethod
    def target_fg():
        return {"name": "SOLIDWORKS", "bundleId": "SLDWORKS.exe", "windowTitle": "Part1.SLDPRT",
                "pid": 200, "isFullScreen": False, "hwnd": 1, "windowClass": "Afx", "exePath": ""}

    def screen_config_event(self, reason):
        return {"type": "screen_config", "screenCount": 1, "screens": self.screens, "reason": reason}

    def foreground(self, force=False):
        return dict(self.fg)

    def event_context(self):
        return {"appName": self.fg["name"], "bundleId": self.fg["bundleId"], "screen": 0}

    def window_rect(self):
        return {"x": 0, "y": 0, "width": W, "height": 1032}


class FakeSegment:
    backend = "gdi"
    encoder = "libx264"
    crashed = False
    masked_now = True
    cursor_in_video = True

    def __init__(self, index):
        from recorder.clock import wall_ms
        self.index = index
        self.first_frame_wall = wall_ms()
        self.frame_count = 30
        self.masked_frames = 12
        self.path = ""

    def stop(self):
        return self

    def request_stop(self):
        pass

    def to_dict(self):
        return {"index": self.index, "frames": [[0, self.first_frame_wall]]}

    def capture_info(self):
        return {"api": "fake"}


class RecorderAutoPauseTest(unittest.TestCase):
    def setUp(self):
        from recorder.recorder import Recorder, parse_args
        self.tmp = tempfile.TemporaryDirectory(prefix="destudio-app-mode-")
        args = parse_args(["start", "--source", "app:SLDWORKS.exe", "--out", self.tmp.name,
                           "--codec", "libx264", "--ffmpeg", "ffmpeg", "--app-allow", "Helper.exe"])
        with patch("recorder.recorder.Desktop", FakeDesktop):
            self.rec = rec = Recorder(args)
        self.desktop = rec.desktop
        self.fg = {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe", "name": "SOLIDWORKS"}
        rec.masker._foreground = lambda: dict(self.fg)
        rec.masker._enumerate = lambda bounds: []
        self.started = []

        def start_segment():
            seg = FakeSegment(len(rec.segments))
            rec.segments.append(seg)
            rec.segment = seg
            self.started.append(seg)

        def stop_segment():
            rec.segment = None
        rec.start_segment = start_segment
        rec.stop_segment = stop_segment
        # what run() does before the watcher starts (no hooks / threads here)
        with rec.lock:
            rec.start_segment()
            rec._set_capturing(True)
        rec._after_resume(rec.segment, initial=True)

    def tearDown(self):
        try:
            self.rec.log.close()
        finally:
            self.tmp.cleanup()

    def events(self):
        self.rec.log._fh.flush()
        with open(os.path.join(self.rec.raw, "events.jsonl"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def switch_to_other(self, t0=10_000.0):
        self.fg = {"hwnd": 77, "pid": 301, "exe": "WeChat.exe", "name": "WeChat"}
        self.desktop.fg = {"name": "WeChat", "bundleId": "WeChat.exe", "windowTitle": "Secret chat",
                           "pid": 301, "isFullScreen": False, "hwnd": 77, "windowClass": "WeChatMainWnd",
                           "exePath": ""}
        for dt in (0, 100, 200, 300):
            self.rec._watch_tick(t0 + dt)

    def switch_to_target(self, t=20_000.0):
        self.fg = {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe", "name": "SOLIDWORKS"}
        self.desktop.fg = FakeDesktop.target_fg()
        self.rec._watch_tick(t)

    def test_initial_snapshot_opens_the_first_clip(self):
        # battery/power events only exist on machines that report them (real Windows)
        types = [e["type"] for e in self.events() if e["type"] not in POWER_TYPES]
        self.assertEqual(types[:5], ["screen_config", "active_app", "appearance_change",
                                     "memory_pressure", "mouse_move"])
        first = self.events()[0]
        self.assertEqual(first["reason"], "open")
        # stamped just before frame 0 (first_frame_wall - 0.001 ms, then rounded)
        frame0 = self.started[0].first_frame_wall
        self.assertGreaterEqual(first["timestamp"], frame0 - 0.002)
        self.assertLessEqual(first["timestamp"], frame0)

    def test_auto_pause_and_resume_cycle(self):
        rec = self.rec
        self.switch_to_other()
        self.assertTrue(rec.paused and rec.auto_paused)
        self.assertFalse(rec._capturing)
        evs = self.events()
        start = [e for e in evs if e["type"] == "privacy_mask_start"]
        self.assertEqual(len(start), 1)
        self.assertEqual(start[0]["maskDetail"], "wechat.exe")
        self.assertEqual(start[0]["exeName"], "WeChat.exe")
        self.assertEqual(start[0]["windowTitle"], "")
        self.assertEqual(start[0]["trigger"], "non_target_app")
        self.assertEqual(start[0]["maskedDisplay"], 0)
        # nothing about WeChat (title or active_app) ever reaches the log
        self.assertFalse(any("Secret chat" in json.dumps(e) for e in evs))
        self.assertFalse(any(e["type"] == "active_app" and e.get("bundleId") == "WeChat.exe" for e in evs))

        before = len(self.events())
        for t in range(11_000, 12_000, 100):      # paused: watcher writes nothing
            rec._watch_tick(t)
        self.assertEqual(len(self.events()), before)

        self.switch_to_target()
        self.assertFalse(rec.paused)
        self.assertEqual(len(self.started), 2)
        tail_events = [e for e in self.events()[before:] if e["type"] not in POWER_TYPES]
        tail = [e["type"] for e in tail_events]
        self.assertEqual(tail[:5], ["screen_config", "active_app", "appearance_change",
                                    "memory_pressure", "mouse_move"])
        self.assertEqual(tail[5], "privacy_mask_end")
        end = tail_events[5]
        skip = ("type", "timestamp", "seq", "osTime")
        self.assertEqual({k: end[k] for k in start[0] if k not in skip},
                         {k: start[0][k] for k in start[0] if k not in skip})
        self.assertEqual(rec.auto_pauses, 1)
        self.assertGreater(rec.auto_paused_ms, 0)
        snap = rec.snapshot()
        self.assertEqual(snap["capture_mode"], "app")
        self.assertEqual(snap["target_exe"], "SLDWORKS.exe")
        self.assertFalse(snap["auto_paused"])
        self.assertTrue(snap["masked_now"])

    def test_manual_pause_is_never_auto_resumed(self):
        rec = self.rec
        rec.toggle_pause()
        self.assertTrue(rec.paused and not rec.auto_paused)
        self.switch_to_other()
        self.switch_to_target()
        self.assertTrue(rec.paused)
        self.assertEqual(len(self.started), 1)
        rec.toggle_pause()
        self.assertFalse(rec.paused)
        self.assertEqual(len(self.started), 2)

    def test_f9_during_auto_pause_holds_the_pause(self):
        rec = self.rec
        self.switch_to_other()
        rec.toggle_pause()                         # F9 while auto-paused -> manual hold
        self.assertTrue(rec.paused and not rec.auto_paused)
        self.switch_to_target()
        self.assertTrue(rec.paused)                # not auto-resumed
        rec.toggle_pause()
        self.assertFalse(rec.paused)
        self.assertEqual([e["type"] for e in self.events()].count("privacy_mask_end"), 1)

    def test_f9_resume_while_other_app_in_front_waits_for_target(self):
        rec = self.rec
        rec.toggle_pause()
        self.fg = {"hwnd": 77, "pid": 301, "exe": "WeChat.exe", "name": "WeChat"}
        rec.toggle_pause()                          # resume requested, but WeChat is in front
        self.assertTrue(rec.paused and rec.auto_paused)
        self.assertEqual(len(self.started), 1)      # no clip of WeChat
        self.switch_to_target()
        self.assertFalse(rec.paused)
        self.assertEqual(len(self.started), 2)

    def test_keys_typed_into_other_apps_are_dropped(self):
        rec = self.rec
        rec._on_input({"type": "key", "keyCode": 65, "characters": "a", "bundleId": "WeChat.exe",
                       "timestamp": 1.0})
        rec._on_input({"type": "key", "keyCode": 0x11, "characters": "", "bundleId": "WeChat.exe",
                       "timestamp": 2.0})           # Ctrl release is kept
        rec._on_input({"type": "key", "keyCode": 66, "characters": "b", "bundleId": "SLDWORKS.exe",
                       "timestamp": 3.0})
        rec._on_input({"type": "key", "keyCode": 67, "characters": "c", "bundleId": "helper.exe",
                       "timestamp": 4.0})           # allow-listed via --app-allow
        rec._on_input({"type": "click", "x": 1, "y": 2, "bundleId": "WeChat.exe", "timestamp": 5.0})
        keys = [e for e in self.events() if e["type"] in ("key", "click")]
        self.assertEqual([e.get("keyCode") for e in keys], [0x11, 66, 67, None])
        self.assertEqual(rec.suppressed_keys, 1)

    def test_capture_json_has_v02_fields(self):
        rec = self.rec
        self.switch_to_other()
        rec._write_capture("recording")
        with open(os.path.join(rec.raw, "capture.json"), encoding="utf-8") as fh:
            cap = json.load(fh)
        self.assertEqual(cap["capture_mode"], "app")
        self.assertEqual(cap["capture_rect"], {"x": 0, "y": 0, "width": W, "height": H})
        self.assertTrue(cap["cursor_in_video"])
        self.assertEqual(cap["app_target"]["exe"], "SLDWORKS.exe")
        self.assertTrue(cap["app_target"]["auto_pause"])
        self.assertIn("textinputhost.exe", cap["app_target"]["allow"])
        self.assertIn("helper.exe", cap["app_target"]["allow"])
        for key in ("masked_frames", "auto_pauses", "auto_paused_ms"):
            self.assertIn(key, cap["mask_stats"])
        self.assertEqual(cap["mask_stats"]["auto_pauses"], 1)
        self.assertEqual(cap["mask_stats"]["masked_frames"], 12)

    def test_stop_while_auto_paused_closes_the_mask_inside_the_last_clip(self):
        rec = self.rec
        self.switch_to_other()
        paused_at = rec._paused_at
        rec._write_segments = lambda: None
        rec.stop()
        with open(os.path.join(rec.raw, "events.jsonl"), encoding="utf-8") as fh:
            evs = [json.loads(line) for line in fh if line.strip()]
        tail = evs[-2:]
        self.assertEqual([e["type"] for e in tail], ["privacy_mask_end", "screen_config"])
        self.assertTrue(all(abs(e["timestamp"] - paused_at) < 1e-6 for e in tail))
        self.assertEqual(tail[1]["reason"], "close")


if __name__ == "__main__":
    unittest.main(verbosity=2)
