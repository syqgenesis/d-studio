"""Recorder v0.2.1 privacy + crash-safety contracts, driven with fakes (runs anywhere).

K1 unfinalized clips are probed / truncated / marked broken      (stop_segment)
K2 raw/segments.json: atomic, after every clip, periodic, in stop()'s finally
K4 password-manager blocklist = auto-pause with a time cut in every mode
K9 F9/F10 hotkeys: plain presses only, swallowed, never logged    (win_input)
K11 raw/.recording.lock held while recording, free after stop / failed start
plus: live foreground for key attribution, snapshot stamped before frame 0,
failed auto-resume retries, Stop during startup, masker re-arm per clip.

Every OS query the recorder makes (monitors, DPI, power, memory, appearance,
cursor, the app masker's window enumeration) is replaced by a fake, so the
results never depend on the machine (the Windows CI runner has one 1024x768
display, these fakes two monitors).
"""
from __future__ import annotations

import ctypes
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recorder import reclock  # noqa: E402
from recorder import recorder as recmod  # noqa: E402
from recorder import win_input, win_window  # noqa: E402
from recorder.app_mask import AppMasker  # noqa: E402
from recorder.clock import wall_ms  # noqa: E402
from recorder.i18n import get_language, set_language  # noqa: E402
from recorder.recorder import (POLICY_TRIGGER, Recorder, parse_args, probe_frame_count,  # noqa: E402
                               segment_finalization, write_text_atomic)

POWER_TYPES = ("battery_status", "power_source_change")
SNAPSHOT = ["screen_config", "active_app", "appearance_change", "memory_pressure", "mouse_move"]


def fg_of(name, exe, title="", hwnd=1, pid=200):
    return {"name": name, "bundleId": exe, "windowTitle": title, "pid": pid, "isFullScreen": False,
            "hwnd": hwnd, "windowClass": "W", "exePath": ""}


CAD = fg_of("SOLIDWORKS", "SLDWORKS.exe", "Part1.SLDPRT", 1, 200)
KEEPASS = fg_of("KeePass", "KeePass.exe", "Database.kdbx - KeePass", 55, 900)
WECHAT = fg_of("WeChat", "WeChat.exe", "Secret chat", 77, 301)


SCREENS = [{"index": 0, "originX": 0, "originY": 0, "width": 1920, "height": 1080,
            "deviceName": r"\\.\DISPLAY1", "primary": True},
           {"index": 1, "originX": 1920, "originY": 0, "width": 1280, "height": 1024,
            "deviceName": r"\\.\DISPLAY2", "primary": False}]


def fake_screens():
    return [dict(s) for s in SCREENS]


def fake_masker(*args, **kw):
    """A real AppMasker on fake window / foreground queries (no Win32 calls, no
    process-tree / desktop-peek / DPI probes: those are off with injected fakes)."""
    kw.setdefault("enumerate_fn", lambda bounds: [])
    kw.setdefault("foreground_fn", lambda: {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe", "name": "SOLIDWORKS"})
    return AppMasker(*args, **kw)


# recorder.recorder name -> fake: every machine-dependent query of the recorder
OS_FAKES = {
    "Desktop": None,                                    # FakeDesktop (set below)
    "list_monitors": fake_screens,
    "enable_dpi_awareness": lambda: "n/a",
    "ensure_thread_dpi_awareness": lambda: False,
    "virtual_screen_rect": lambda: None,
    "power_status": lambda: {},
    "memory_pressure": lambda: "unknown",
    "appearance": lambda: "unknown",
    "cursor_pos": lambda: (0, 0),
    "button_state": lambda: {"left": False, "right": False, "middle": False, "other_3": False, "other_4": False},
    "AppMasker": fake_masker,
}


def patch_os(test):
    """Start the OS_FAKES patches for ``test`` (undone by its cleanups)."""
    for name, value in OS_FAKES.items():
        p = patch(f"recorder.recorder.{name}", value if value is not None else FakeDesktop)
        p.start()
        test.addCleanup(p.stop)


class FakeDesktop:
    def __init__(self):
        self.screens = fake_screens()
        self.fg = dict(CAD)

    def screen_config_event(self, reason):
        return {"type": "screen_config", "screenCount": len(self.screens), "screens": self.screens,
                "reason": reason}

    def foreground(self, force=False):
        return dict(self.fg)

    def event_context(self):
        return {"appName": self.fg["name"], "bundleId": self.fg["bundleId"], "screen": 0}

    def window_rect(self):
        return {"x": 0, "y": 0, "width": 800, "height": 600}


class FakeSeg:
    """A clip whose frames run from start() to stop() at the real wall clock."""

    encoder = "libx264"
    masked_now = False
    cursor_in_video = True
    masked_frames = 0
    error_tail = ""

    def __init__(self, backend, index, path, fps, capture_rect=None, finalized=True, returncode=0,
                 fail_start=False, delay_frames="", on_start=None, **kw):
        self.backend = "dxcam" if backend == "auto" else backend
        self.fail_start = fail_start
        self.index, self.path, self.fps = index, path, fps
        self.capture_rect = capture_rect
        self.kw = kw
        self._fin, self._rc = finalized, returncode
        # "": frame 0 at start(); "until_stop": the first frames only arrive while
        # stopping (slow gdigrab start); "never": the clip never gets a frame
        self.delay_frames = delay_frames
        self.on_start = on_start
        self.frames = []
        self.start_wall_ms = 0.0
        self.stopped = False
        self.crashed = False

    def start(self):
        if self.fail_start:
            raise RuntimeError(f"{self.backend} first frame timeout")
        time.sleep(0.003)
        self.start_wall_ms = wall_ms()
        if not self.delay_frames:
            self.frames = [(0, self.start_wall_ms)]
        with open(self.path, "wb") as fh:
            fh.write(b"\0" * 64)
        if self.on_start:
            self.on_start(self)
        return self

    def tick(self):
        if not self.frames:
            if self.delay_frames != "until_stop":
                return
            self.frames = [(0, self.start_wall_ms)]
        dt = 1000.0 / self.fps
        t0 = self.frames[0][1]
        n = max(1, int((wall_ms() - t0) // dt) + 1)
        self.frames.extend((i, t0 + i * dt) for i in range(len(self.frames), n))

    def request_stop(self):
        pass

    def stop(self):
        if not self.stopped:
            time.sleep(0.003)
            self.tick()
            self.stopped = True
        return self

    @property
    def frame_count(self):
        return len(self.frames)

    @property
    def first_frame_wall(self):
        return self.frames[0][1] if self.frames else None

    @property
    def finalized(self):
        return bool(self.stopped and self._fin)

    @property
    def returncode(self):
        return self._rc if self.stopped else None

    def to_dict(self):
        return {"index": self.index, "file": os.path.basename(self.path), "fps": self.fps,
                "frame_count": len(self.frames), "frames": [[n, round(t, 3)] for n, t in self.frames],
                "backend": self.backend, "in_progress": not self.stopped,
                "finalized": self.finalized, "returncode": self.returncode}

    def capture_info(self):
        return {"api": "fake"}


class FakeHook:
    instances = []

    def __init__(self, *a, **kw):
        self.started = self.stopped = False
        self.enabled = kw.get("enabled", True)
        self.on_start = None
        self.fail_stop = False
        FakeHook.instances.append(self)

    def start(self):
        self.started = True
        if self.on_start:
            self.on_start()
        return self

    def request_stop(self):
        pass

    def stop(self):
        self.stopped = True
        if self.fail_stop:
            raise RuntimeError("input writer did not drain")

    def set_enabled(self, flag):
        self.enabled = flag

    def health(self):
        return {}


class RecorderEnv(unittest.TestCase):
    """A real Recorder on fake desktop / segments / hooks (no threads, no ffmpeg)."""

    source = "desktop"
    extra_args: tuple = ()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="destudio-privacy-")
        self.addCleanup(self.tmp.cleanup)
        out = patch("sys.stdout", new_callable=io.StringIO)
        out.start()
        self.addCleanup(out.stop)
        self.seg_opts = []          # per create_segment call: kwargs overrides or an exception
        self.created = []

        def create(backend, **kw):
            opt = self.seg_opts.pop(0) if self.seg_opts else {}
            if isinstance(opt, BaseException):
                raise opt
            seg = FakeSeg(backend, **{**kw, **opt})
            self.created.append(seg)
            return seg
        for target, value in (("recorder.recorder.create_segment", create),
                              ("recorder.recorder.pick_encoder", lambda *a, **k: "libx264")):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        patch_os(self)
        args = parse_args(["start", "--source", self.source, "--out", self.tmp.name,
                           "--codec", "libx264", "--ffmpeg", "ffmpeg", *self.extra_args])
        self.rec = Recorder(args)
        self.addCleanup(self._close_log)
        self.desktop = self.rec.desktop

    def _close_log(self):
        if not self.rec.log.closed:
            self.rec.log.close()

    def begin(self):
        """What run() does once the hook is up (no watcher thread here)."""
        rec = self.rec
        with rec.lock:
            rec.start_segment()
            rec._set_capturing(True)
        rec._after_resume(rec.segment, initial=True)

    def events(self):
        if not self.rec.log.closed:
            self.rec.log._fh.flush()
        with open(os.path.join(self.rec.raw, "events.jsonl"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def sidecar(self):
        with open(os.path.join(self.rec.raw, "segments.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def front(self, fg):
        self.desktop.fg = dict(fg)

    def frames_inside(self, start, end):
        return [(s.index, n, w) for s in self.rec.segments for n, w in s.frames
                if start < round(w, 3) < end]


# ------------------------------------------------------------------------- K4
class DesktopBlocklistTest(RecorderEnv):
    def setUp(self):
        super().setUp()
        self.begin()

    def test_password_manager_in_front_cuts_time_and_masks(self):
        rec = self.rec
        time.sleep(0.01)
        self.front(KEEPASS)
        rec._watch_tick(10_000.0)                          # no debounce for a password manager
        self.assertTrue(rec.paused and rec.auto_paused)
        self.assertEqual(rec.snapshot()["auto_pause_reason"], "policy")
        self.assertIsNone(rec.segment)
        self.assertTrue(self.created[0].stopped)
        start = [e for e in self.events() if e["type"] == "privacy_mask_start"]
        self.assertEqual(len(start), 1)
        fields = {k: start[0][k] for k in ("trigger", "maskTrigger", "maskDetail", "appName", "exeName",
                                          "windowTitle", "maskedDisplay")}
        self.assertEqual(fields, {"trigger": POLICY_TRIGGER, "maskTrigger": POLICY_TRIGGER,
                                  "maskDetail": "keepass.exe", "appName": "KeePass",
                                  "exeName": "KeePass.exe", "windowTitle": "", "maskedDisplay": 0})
        before = len(self.events())
        for t in range(10_100, 11_000, 100):               # paused: nothing about KeePass is logged
            rec._watch_tick(float(t))
        self.assertEqual(len(self.events()), before)

        time.sleep(0.01)
        self.front(CAD)
        rec._watch_tick(11_000.0)
        self.assertFalse(rec.paused)
        self.assertEqual(len(rec.segments), 2)
        tail = [e for e in self.events()[before:] if e["type"] not in POWER_TYPES]
        self.assertEqual([e["type"] for e in tail][:6], SNAPSHOT + ["privacy_mask_end"])
        end = tail[5]
        self.assertEqual({k: end[k] for k in fields}, fields)
        # the masked time is really cut: no video frame lies inside [start, end]
        self.assertGreater(start[0]["timestamp"], self.created[0].frames[-1][1])
        self.assertLess(end["timestamp"], self.created[1].frames[0][1])
        self.assertEqual(self.frames_inside(start[0]["timestamp"], end["timestamp"]), [])
        self.assertFalse(any("Database.kdbx" in json.dumps(e) for e in self.events()))
        self.assertEqual(rec.policy_pauses, 1)

    def test_password_manager_title_never_logged_even_between_ticks(self):
        # KeePass comes to the front after this tick's auto-pause check (the hook
        # refreshed the foreground cache): no active_app / window_* with its title
        rec = self.rec
        self.front(KEEPASS)
        with rec.lock:
            rec._watch_foreground(10_000.0)
            rec._watch_rect(10_000.0)
        self.assertFalse(any("Database.kdbx" in json.dumps(e) for e in self.events()))
        self.front(CAD)
        with rec.lock:
            rec._watch_foreground(10_100.0)             # back to the CAD: a fresh active_app
        self.assertEqual(self.events()[-1]["type"], "active_app")

    def test_keys_typed_into_a_password_manager_are_never_logged(self):
        rec = self.rec
        for i, (exe, vk) in enumerate((("KeePass.exe", 65), ("keepassxc.exe", 66), ("1Password.exe", 0x10),
                                       ("Bitwarden.exe", 67), ("notepad.exe", 68))):
            rec._on_input({"type": "key", "keyCode": vk, "characters": "x", "bundleId": exe,
                           "timestamp": 1.0 + i})
        rec._on_input({"type": "click", "x": 1, "y": 2, "bundleId": "KeePass.exe", "timestamp": 9.0})
        rec._on_input({"type": "scroll", "x": 1, "y": 2, "bundleId": "KeePass.exe", "timestamp": 9.5})
        rec._on_input({"type": "click", "x": 1, "y": 2, "bundleId": "notepad.exe", "timestamp": 10.0})
        logged = [e for e in self.events() if e["type"] in ("key", "click", "scroll")]
        self.assertEqual([(e["type"], e.get("bundleId")) for e in logged],
                         [("key", "notepad.exe"), ("click", "notepad.exe")])
        self.assertEqual((rec.suppressed_keys, rec.suppressed_inputs), (4, 2))

    def test_auto_typed_keys_are_not_logged_while_a_password_manager_runs(self):
        # KeePass auto-type injects the password into the foreground app from the background
        rec = self.rec
        typed = [{"type": "key", "keyCode": 0xE7, "scanCode": ord(c), "characters": c, "synthetic": True,
                  "bundleId": "chrome.exe", "timestamp": 1.0 + i} for i, c in enumerate("hunter2")]
        real = {"type": "key", "keyCode": 0x53, "characters": "s", "synthetic": False,
                "bundleId": "chrome.exe", "timestamp": 20.0}
        rec._pm_running = True
        for r in typed + [real]:
            rec._on_input(dict(r))
        keys = [e for e in self.events() if e["type"] == "key"]
        self.assertEqual([e["characters"] for e in keys], ["s"])           # only the real keystroke
        self.assertNotIn("hunter2", json.dumps(self.events()))
        rec._pm_running = False                                            # automation without a PM
        rec._on_input(dict(typed[0], timestamp=30.0))
        self.assertEqual(len([e for e in self.events() if e["type"] == "key"]), 2)

    def test_password_manager_process_detection(self):
        rec = self.rec
        with patch("recorder.recorder.sys.platform", "win32"), \
                patch("recorder.app_mask.win_process_snapshot",
                      return_value=[(4, 0, "System"), (900, 4, "KeePass.exe")]):
            rec._refresh_pm_running()
        self.assertTrue(rec._pm_running)
        with patch("recorder.recorder.sys.platform", "win32"), \
                patch("recorder.app_mask.win_process_snapshot", side_effect=OSError("denied")):
            rec._refresh_pm_running()                                       # keeps the last answer
        self.assertTrue(rec._pm_running)
        with patch("recorder.recorder.sys.platform", "win32"), \
                patch("recorder.app_mask.win_process_snapshot", return_value=[(5, 4, "notepad.exe")]):
            rec._refresh_pm_running()
        self.assertFalse(rec._pm_running)

    def test_modified_f_keys_are_ordinary_keys(self):
        # the hook swallows plain F9/F10 itself; Shift+F10 etc. belong to the app and are logged
        self.rec._on_input({"type": "key", "keyCode": 0x79, "modifiers": 1, "bundleId": "SLDWORKS.exe",
                            "timestamp": 1.0})
        self.assertEqual([e["keyCode"] for e in self.events() if e["type"] == "key"], [0x79])

    def test_custom_blocklist_keeps_the_password_managers(self):
        rec = Recorder(parse_args(["start", "--out", os.path.join(self.tmp.name, "b"), "--codec", "libx264",
                                   "--ffmpeg", "ffmpeg", "--blocklist", "Vault.exe"]))   # (OS fakes from setUp)
        self.addCleanup(rec.log.close)
        for exe in ("vault.exe", "1password.exe", "keepass.exe", "keepassxc.exe", "bitwarden.exe"):
            self.assertIn(exe, rec._blocklist)

    def test_manual_pause_is_never_auto_resumed(self):
        rec = self.rec
        rec.toggle_pause()
        self.front(KEEPASS)
        rec._watch_tick(10_000.0)
        self.front(CAD)
        rec._watch_tick(10_100.0)
        self.assertTrue(rec.paused and not rec.auto_paused)
        self.assertEqual(len(rec.segments), 1)
        self.assertFalse(any(e["type"].startswith("privacy_mask") for e in self.events()))
        rec.toggle_pause()
        self.assertFalse(rec.paused)

    def test_f9_holds_a_policy_pause_and_resume_waits_for_the_password_manager(self):
        rec = self.rec
        time.sleep(0.01)
        self.front(KEEPASS)
        rec._watch_tick(10_000.0)
        rec.toggle_pause()                                  # F9: now a manual pause
        self.assertTrue(rec.paused and not rec.auto_paused)
        self.front(CAD)
        rec._watch_tick(10_100.0)
        self.assertTrue(rec.paused)                         # manual: not auto-resumed
        self.front(KEEPASS)
        rec.toggle_pause()                                  # F9 resume while KeePass is in front
        self.assertTrue(rec.paused and rec.auto_paused)
        self.assertEqual(len(rec.segments), 1)              # no clip of KeePass
        rec._watch_tick(10_200.0)
        self.assertTrue(rec.paused)
        time.sleep(0.01)
        self.front(CAD)
        rec._watch_tick(10_300.0)
        self.assertFalse(rec.paused)
        types = [e["type"] for e in self.events()]
        self.assertEqual((types.count("privacy_mask_start"), types.count("privacy_mask_end")), (1, 1))

    def test_stop_while_policy_paused_closes_the_mask_after_the_last_frame(self):
        rec = self.rec
        time.sleep(0.01)
        self.front(KEEPASS)
        rec._watch_tick(10_000.0)
        rec.stop()
        evs = self.events()
        start = next(e for e in evs if e["type"] == "privacy_mask_start")
        end = next(e for e in evs if e["type"] == "privacy_mask_end")
        self.assertGreaterEqual(end["timestamp"], start["timestamp"])
        self.assertEqual(self.frames_inside(start["timestamp"], end["timestamp"]), [])
        self.assertEqual([e["type"] for e in evs[-2:]], ["privacy_mask_end", "screen_config"])


class StartingClipPrivacyTest(RecorderEnv):
    """K4 while a clip waits for its first frame (gdigrab: up to seconds; on an
    auto-resume that wait runs on the watcher thread itself): a password manager
    that comes to the front then pauses the clip at once, and its title is never
    logged."""

    def keepass_on_start(self, seg):
        self.front(KEEPASS)                                 # user re-focuses KeePass right away

    def no_keepass_title(self):
        self.assertFalse(any("Database.kdbx" in json.dumps(e) for e in self.events()))

    def policy_pause_then_back(self):
        self.begin()
        time.sleep(0.01)
        self.front(KEEPASS)
        self.rec._watch_tick(10_000.0)
        self.assertTrue(self.rec.paused and self.rec.auto_paused)
        self.front(CAD)

    def masks(self):
        evs = self.events()
        return ([e for e in evs if e["type"] == "privacy_mask_start"],
                [e for e in evs if e["type"] == "privacy_mask_end"])

    def test_auto_resume_wait_pauses_when_the_password_manager_returns(self):
        rec = self.rec
        self.policy_pause_then_back()
        before = len(self.events())
        self.seg_opts = [{"delay_frames": "never", "on_start": self.keepass_on_start}]
        rec._watch_tick(11_000.0)                           # resume; KeePass is back before frame 0
        self.assertTrue(rec.paused and rec.auto_paused)
        self.assertEqual(rec._auto_reason, "policy")
        self.assertIsNone(rec.segment)
        self.assertEqual(len(rec.segments), 1)              # the frameless clip is dropped
        self.assertEqual(self.events()[before:], [])        # no snapshot of KeePass, nothing logged
        self.assertTrue(self.created[1].stopped)
        time.sleep(0.01)
        self.front(CAD)
        rec._watch_tick(11_100.0)
        self.assertFalse(rec.paused)
        starts, ends = self.masks()
        self.assertEqual((len(starts), len(ends)), (1, 1))
        self.assertEqual(self.frames_inside(starts[0]["timestamp"], ends[0]["timestamp"]), [])
        self.no_keepass_title()

    def test_frames_that_arrive_while_pausing_stay_outside_both_masks(self):
        rec = self.rec
        self.policy_pause_then_back()
        self.seg_opts = [{"delay_frames": "until_stop", "on_start": self.keepass_on_start}]
        rec._watch_tick(11_000.0)
        self.assertTrue(rec.paused and rec.auto_paused)
        self.assertEqual(len(rec.segments), 2)              # it holds frames: kept, between two masks
        time.sleep(0.01)
        self.front(CAD)
        rec._watch_tick(11_100.0)
        self.assertFalse(rec.paused)
        starts, ends = self.masks()
        self.assertEqual((len(starts), len(ends)), (2, 2))
        for start, end in zip(starts, ends):
            self.assertLess(start["timestamp"], end["timestamp"])
            self.assertEqual(self.frames_inside(start["timestamp"], end["timestamp"]), [])
        self.no_keepass_title()

    def test_first_clip_wait_pauses_too(self):
        rec = self.rec
        self.seg_opts = [{"delay_frames": "never", "on_start": self.keepass_on_start}]
        self.begin()                                        # run(): before the watcher exists
        self.assertTrue(rec.paused and rec.auto_paused)
        self.assertEqual(rec.segments, [])
        self.assertEqual([e["type"] for e in self.events()], ["privacy_mask_start"])
        time.sleep(0.01)
        self.front(CAD)
        rec._watch_tick(10_000.0)
        self.assertFalse(rec.paused)
        snap = [e for e in self.events() if e["type"] not in POWER_TYPES]
        self.assertEqual([e["type"] for e in snap], ["privacy_mask_start"] + SNAPSHOT + ["privacy_mask_end"])
        self.assertEqual(snap[5]["source"], "initial_cursor")
        self.no_keepass_title()

    def test_the_start_check_never_resumes(self):
        self.policy_pause_then_back()                       # policy pause, KeePass gone
        self.rec._watch_auto_pause(wall_ms(), allow_resume=False)
        self.assertTrue(self.rec.paused)
        self.assertEqual(len(self.rec.segments), 1)

    def test_snapshot_never_logs_a_password_managers_title(self):
        self.begin()
        self.front(KEEPASS)                                 # in front before the watcher noticed
        with self.rec.lock:
            self.rec._emit_state_snapshot(wall_ms(), "resume_snapshot")
        app = [e for e in self.events() if e["type"] == "active_app"][-1]
        self.assertEqual((app["bundleId"], app["windowTitle"]), ("KeePass.exe", ""))
        self.no_keepass_title()


class PinnedDisplayTest(RecorderEnv):
    """K3: the first clip pins the display; a renumbered DISPLAYn device with the
    same rect is the same picture, any other geometry is refused."""

    def setUp(self):
        super().setUp()
        self.begin()
        self.rec.toggle_pause()

    def resume_with(self, screens):
        with patch("recorder.recorder.list_monitors", lambda: [dict(s) for s in screens]):
            self.rec.toggle_pause()

    def test_renumbered_display_with_the_same_rect_is_accepted(self):
        renamed = fake_screens()
        renamed[0]["deviceName"], renamed[1]["deviceName"] = r"\\.\DISPLAY7", r"\\.\DISPLAY8"
        self.resume_with(renamed)
        rec = self.rec
        self.assertFalse(rec.paused)
        self.assertEqual(self.created[1].kw["monitor_device"], r"\\.\DISPLAY7")
        self.assertEqual(self.created[1].capture_rect, (0, 0, 1920, 1080))
        self.assertEqual((rec._pinned_device, rec.capture_monitor["deviceName"]), (r"\\.\DISPLAY7",) * 2)
        self.assertEqual(rec.last_error, "")

    def test_display_with_another_geometry_is_still_refused(self):
        for name in (r"\\.\DISPLAY1", r"\\.\DISPLAY7"):      # resized, or resized + renumbered
            with self.subTest(name=name):
                smaller = fake_screens()
                smaller[0].update(width=1280, height=720, deviceName=name)
                self.resume_with(smaller)
                self.assertTrue(self.rec.paused)
                self.assertEqual(len(self.created), 1)      # nothing was even started
                self.assertIn("(0, 0, 1920, 1080)", self.rec.last_error)
                if name.endswith("1"):
                    self.assertIn("(0, 0, 1280, 720)", self.rec.last_error)
                self.assertEqual(self.rec._pinned_device, r"\\.\DISPLAY1")


class AppModeBlocklistTest(RecorderEnv):
    source = "app:SLDWORKS.exe"

    def setUp(self):
        super().setUp()
        self.fgm = {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe", "name": "SOLIDWORKS"}
        self.rec.masker._foreground = lambda: dict(self.fgm)
        self.rec.masker._enumerate = lambda bounds: []
        self.begin()

    def switch(self, fgm, fg):
        self.fgm = fgm
        self.front(fg)

    def test_password_manager_pauses_at_once_and_resume_waits_for_the_target(self):
        rec = self.rec
        time.sleep(0.01)
        self.switch({"hwnd": 55, "pid": 900, "exe": "KeePass.exe", "name": "KeePass"}, KEEPASS)
        rec._watch_tick(10_000.0)                           # one tick: no 300 ms debounce
        self.assertTrue(rec.paused and rec.auto_paused)
        start = [e for e in self.events() if e["type"] == "privacy_mask_start"]
        self.assertEqual([e["trigger"] for e in start], [POLICY_TRIGGER])
        self.switch({"hwnd": 77, "pid": 301, "exe": "WeChat.exe", "name": "WeChat"}, WECHAT)
        for t in (10_100.0, 10_200.0, 10_300.0, 10_400.0):
            rec._watch_tick(t)
        self.assertTrue(rec.paused)                         # app mode: WeChat is not the target
        time.sleep(0.01)
        self.switch({"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe", "name": "SOLIDWORKS"}, CAD)
        rec._watch_tick(10_500.0)
        self.assertFalse(rec.paused)
        ends = [e for e in self.events() if e["type"] == "privacy_mask_end"]
        self.assertEqual([e["trigger"] for e in ends], [POLICY_TRIGGER])
        self.assertEqual(self.frames_inside(start[0]["timestamp"], ends[0]["timestamp"]), [])

    def test_mask_is_rearmed_at_every_clip_and_rect_set_only_on_change(self):
        rec = self.rec
        rec.masker.begin_segment = Mock()
        set_rect = Mock(wraps=rec.masker.set_capture_rect)
        rec.masker.set_capture_rect = set_rect
        screens = [dict(s) for s in self.desktop.screens]
        with patch("recorder.recorder.list_monitors", lambda: [dict(s) for s in screens]):
            rec.toggle_pause()
            rec.toggle_pause()
            rec.toggle_pause()
            rec.toggle_pause()
        self.assertEqual(rec.masker.begin_segment.call_count, 2)
        set_rect.assert_not_called()                        # same monitor: the hold history survives

    def test_password_manager_during_a_clip_start_pauses_it(self):
        rec = self.rec
        rec.toggle_pause()

        def keepass(seg):
            self.switch({"hwnd": 55, "pid": 900, "exe": "KeePass.exe", "name": "KeePass"}, KEEPASS)
        self.seg_opts = [{"delay_frames": "never", "on_start": keepass}]
        rec.toggle_pause()                                  # F9 resume; KeePass in front before frame 0
        self.assertTrue(rec.paused and rec.auto_paused)
        self.assertEqual(rec._auto_reason, "policy")
        self.assertFalse(any("Database.kdbx" in json.dumps(e) for e in self.events()))

    def test_renumbered_capture_monitor_keeps_its_rect_under_the_new_name(self):
        rec = self.rec
        renamed = fake_screens()
        renamed[0]["deviceName"] = r"\\.\DISPLAY7"
        with patch("recorder.recorder.list_monitors", lambda: [dict(s) for s in renamed]):
            rec.toggle_pause()
            rec.toggle_pause()
        self.assertFalse(rec.paused)
        self.assertEqual(self.created[-1].kw["monitor_device"], r"\\.\DISPLAY7")
        self.assertEqual(self.created[-1].capture_rect, (0, 0, 1920, 1080))
        self.assertEqual(rec._pinned_device, r"\\.\DISPLAY7")

    def test_first_clip_and_backend_fallback_rearm_the_mask_too(self):
        rec = self.rec
        self.assertEqual(rec.masker.segment_holds, 1)       # begin(): the first clip
        rec.masker.begin_segment = Mock()
        screens = [dict(s) for s in self.desktop.screens]
        with patch("recorder.recorder.list_monitors", lambda: [dict(s) for s in screens]):
            rec.toggle_pause()
            self.seg_opts = [{"fail_start": True}, {}]      # DXGI fails on resume -> gdi fallback
            rec.toggle_pause()
        self.assertFalse(rec.paused)
        self.assertEqual(rec.segment.backend, "gdi")
        self.assertEqual([c.kwargs for c in rec.masker.begin_segment.call_args_list],
                         [{"first": False}, {"first": False}])


# ----------------------------------------------------------------- finding 20
class FailedAutoResumeTest(RecorderEnv):
    def setUp(self):
        super().setUp()
        self.begin()

    def test_failed_auto_resume_stays_auto_paused_and_retries_with_backoff(self):
        rec = self.rec
        time.sleep(0.01)
        self.front(KEEPASS)
        rec._watch_tick(10_000.0)
        self.front(CAD)
        self.seg_opts = [RuntimeError("DXGI not ready"), RuntimeError("DXGI not ready")]
        rec._watch_tick(10_100.0)                           # attempt 1 fails
        self.assertTrue(rec.paused and rec.auto_paused)
        self.assertEqual(rec._auto_reason, "policy")
        self.assertIn("DXGI", rec.last_error)
        rec._watch_tick(10_600.0)                           # inside the 1 s backoff: no attempt
        self.assertEqual(len(self.seg_opts), 1)
        rec._watch_tick(11_100.0)                           # attempt 2 fails -> 2 s backoff
        self.assertEqual(len(self.seg_opts), 0)
        self.assertTrue(rec.paused and rec.auto_paused)
        rec._watch_tick(12_000.0)
        self.assertTrue(rec.paused)
        time.sleep(0.01)
        rec._watch_tick(13_100.0)                           # attempt 3 succeeds
        self.assertFalse(rec.paused)
        self.assertEqual(rec.last_error, "")                # the bar no longer shows the old failure
        self.assertEqual(len(rec.segments), 2)
        types = [e["type"] for e in self.events()]
        self.assertEqual(types.count("privacy_mask_end"), 1)

    def test_failed_manual_resume_stays_manual(self):
        rec = self.rec
        rec.toggle_pause()
        self.seg_opts = [RuntimeError("no capture")]
        rec.toggle_pause()
        self.assertTrue(rec.paused and not rec.auto_paused)
        rec._watch_tick(20_000.0)
        self.assertTrue(rec.paused)


class AppModeFailedResumeTest(RecorderEnv):
    source = "app:SLDWORKS.exe"

    def test_app_mode_auto_pause_survives_a_failed_resume(self):
        rec = self.rec
        fgm = {"v": {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe", "name": "SOLIDWORKS"}}
        rec.masker._foreground = lambda: dict(fgm["v"])
        rec.masker._enumerate = lambda bounds: []
        self.begin()
        fgm["v"] = {"hwnd": 77, "pid": 301, "exe": "WeChat.exe", "name": "WeChat"}
        for t in (10_000.0, 10_100.0, 10_200.0, 10_300.0):
            rec._watch_tick(t)
        self.assertTrue(rec.auto_paused)
        fgm["v"] = {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe", "name": "SOLIDWORKS"}
        self.seg_opts = [RuntimeError("first frame timeout")]
        rec._watch_tick(20_000.0)
        self.assertTrue(rec.paused and rec.auto_paused)     # not silently turned into a manual pause
        time.sleep(0.01)
        rec._watch_tick(21_000.0)
        self.assertFalse(rec.paused)


# ----------------------------------------------------------------- finding 27
class SnapshotTimingTest(RecorderEnv):
    def test_snapshot_is_stamped_just_before_frame_zero(self):
        self.begin()
        rec = self.rec
        first = [e for e in self.events() if e["type"] not in POWER_TYPES][:5]
        self.assertEqual([e["type"] for e in first], SNAPSHOT)
        f0 = round(self.created[0].frames[0][1], 3)
        self.assertTrue(all(e["timestamp"] < f0 for e in first))
        self.assertIn("buttons", first[4])
        rec.toggle_pause()
        before = len(self.events())
        rec.toggle_pause()
        snap = [e for e in self.events()[before:] if e["type"] not in POWER_TYPES][:5]
        self.assertEqual([e["type"] for e in snap], SNAPSHOT)
        f0 = round(self.created[1].frames[0][1], 3)
        self.assertTrue(all(f0 - 1.0 < e["timestamp"] < f0 for e in snap))
        self.assertEqual(snap[4]["source"], "resume_snapshot")


# ------------------------------------------------------------------------- K2
class SidecarTest(RecorderEnv):
    def setUp(self):
        super().setUp()
        self.begin()

    def test_written_after_every_clip_and_periodically_with_the_running_clip(self):
        rec = self.rec
        rec._write_segments()
        self.assertEqual([(s["index"], s["in_progress"]) for s in self.sidecar()], [(0, True)])
        rec.toggle_pause()                                  # clip 0 stops -> written at once
        side = self.sidecar()
        self.assertEqual([(s["index"], s["in_progress"], s["finalized"]) for s in side], [(0, False, True)])
        self.assertEqual(len(side[0]["frames"]), self.created[0].frame_count)
        rec.toggle_pause()
        rec.quiet = True
        rec._sidecar_due = 0.0
        rec.log.emit("probe_marker")
        rec._status()                                       # the periodic write
        self.assertEqual([(s["index"], s["in_progress"]) for s in self.sidecar()], [(0, False), (1, True)])
        self.assertGreater(rec._sidecar_due, time.monotonic() + 5)
        with open(os.path.join(rec.raw, "events.jsonl"), encoding="utf-8") as fh:
            self.assertIn('"probe_marker"', fh.read())     # the event log was flushed too
        self.assertFalse([f for f in os.listdir(rec.raw) if f.endswith(".tmp")])

    def test_periodic_write_encodes_only_the_new_frames(self):
        rec = self.rec
        seg = self.created[0]
        t0 = seg.frames[0][1]
        seg.frames[:] = [(i, t0 + i * 1000.0 / 60) for i in range(5000)]
        rec._write_segments()
        seg.frames.extend((i, t0 + i * 1000.0 / 60) for i in range(5000, 5010))
        real, sizes = json.dumps, []

        def spy(obj, *a, **k):
            if isinstance(obj, (list, tuple)):
                sizes.append(len(obj))
            elif isinstance(obj, dict) and "frames" in obj:
                sizes.append(len(obj["frames"]))
            return real(obj, *a, **k)
        with patch.object(recmod.json, "dumps", spy):
            rec._write_segments()
        self.assertLessEqual(max(sizes), 10)                # only the 10 new frames were encoded
        entry = self.sidecar()[0]
        self.assertEqual(entry["frames"], seg.to_dict()["frames"])
        self.assertEqual((entry["frame_count"], entry["in_progress"]), (5010, True))
        del seg.frames[100:]                                # a list that was cut is encoded anew
        rec._write_segments()
        self.assertEqual(self.sidecar()[0]["frames"], seg.to_dict()["frames"])
        rec.toggle_pause()                                  # settled: same content as a full dump
        entry = self.sidecar()[0]
        self.assertEqual(entry["frames"], seg.to_dict()["frames"])
        self.assertEqual(entry, json.loads(json.dumps(dict(seg.to_dict(), in_progress=False))))

    def test_stop_writes_the_sidecar_even_when_the_input_hook_fails(self):
        rec = self.rec
        rec._hook = FakeHook()
        rec._hook.fail_stop = True
        with self.assertRaises(RuntimeError):
            rec.stop()
        side = self.sidecar()
        self.assertEqual([(s["index"], s["in_progress"]) for s in side], [(0, False)])
        with open(os.path.join(rec.raw, "capture.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["status"], "failed")

    def test_atomic_write_retries_while_a_reader_holds_the_file(self):
        path = os.path.join(self.tmp.name, "x.json")
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) < 3:
                raise PermissionError("in use")
            return real(src, dst)
        with patch("recorder.recorder.os.replace", flaky):
            write_text_atomic(path, "[1]", delay_s=0.001)
        self.assertEqual(len(calls), 3)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "[1]")
        self.assertEqual(os.listdir(self.tmp.name).count("x.json"), 1)
        self.assertFalse([f for f in os.listdir(self.tmp.name) if f.endswith(".tmp")])


# ------------------------------------------------------------------------- K1
def _probe_result(pts, returncode=0, stderr=b""):
    out = "".join(f"{t:.6f}\n" for t in pts)
    return SimpleNamespace(stdout=out.encode(), stderr=stderr, returncode=returncode)


INVALID_INPUT = b"[mov,mp4 @ 0x1] moov atom not found\nseg_000.mp4: Invalid data found when processing input\n"


class UnfinalizedClipTest(RecorderEnv):
    def stop_unfinalized(self, run):
        self.seg_opts = [{"finalized": False, "returncode": 1}]
        self.begin()
        seg = self.created[0]
        t0 = seg.frames[0][1]
        seg.frames[:] = [(i, t0 + i * 1000.0 / 30) for i in range(30)]   # 30 frames handed to ffmpeg
        with patch("recorder.recorder.subprocess.run", run):
            self.rec.stop_segment()
            self.rec._join_verify()                         # the probe runs on a worker thread
        return seg

    def test_frames_are_cut_to_what_the_file_holds(self):
        # B-frame order, killed encoder: frame 17 never reached the file
        pts = [0, 3, 1, 2, 6, 4, 5, 9, 7, 8, 12, 10, 11, 15, 13, 14, 16, 19, 18]
        seg = self.stop_unfinalized(lambda *a, **k: _probe_result([p / 30.0 for p in pts]))
        self.assertEqual([n for n, _ in seg.frames], list(range(17)))
        entry = self.sidecar()[0]
        self.assertFalse(entry["finalized"])
        self.assertEqual((entry["probed_frames"], entry["truncated_from"]), (17, 30))
        self.assertNotIn("broken", entry)
        self.assertEqual(entry["frame_count"], 17)
        self.assertEqual(len(entry["frames"]), 17)

    def test_unreadable_clip_is_kept_as_broken_gap(self):
        for rc in (0, 1):                                   # ffprobe 5.1 exits 0 even then
            with self.subTest(returncode=rc):
                self.created.clear()
                seg = self.stop_unfinalized(lambda *a, **k: _probe_result([], returncode=rc, stderr=INVALID_INPUT))
                self.assertEqual(seg.frame_count, 0)
                self.assertIn(seg, self.rec.segments)       # kept: its time becomes a gap
                entry = self.sidecar()[-1]
                self.assertEqual((entry["broken"], entry["frames"], entry["frame_count"]), (True, [], 0))
                self.assertTrue(os.path.exists(seg.path))

    def test_failing_ffprobe_is_unavailable_not_broken(self):
        # missing DLL / antivirus / crash: non-zero exit, nothing on stdout -> the
        # clip may be fine; it is left to the packager, never emptied
        for err in (b"", b"The code execution cannot proceed because avcodec-59.dll was not found.\n"):
            with self.subTest(stderr=err):
                self.created.clear()
                seg = self.stop_unfinalized(lambda *a, **k: _probe_result([], returncode=3221225781, stderr=err))
                self.assertEqual(seg.frame_count, 30)
                entry = self.sidecar()[-1]
                self.assertEqual((entry["probe"], len(entry["frames"]), entry["finalized"]), ("unavailable", 30, False))
                self.assertNotIn("broken", entry)

    def test_probe_runs_off_the_recorder_lock(self):
        # pause with an unfinalized clip: F9 / the GUI snapshot / the watcher never
        # wait for ffprobe reading a long clip; stop() waits for it before its last write
        self.seg_opts = [{"finalized": False, "returncode": 1}]
        self.begin()
        rec = self.rec
        probing, release = threading.Event(), threading.Event()

        def slow_probe(*a, **k):
            probing.set()
            release.wait(5)
            return _probe_result([n / 30.0 for n in range(5)])
        with patch("recorder.recorder.subprocess.run", slow_probe):
            rec.toggle_pause()                              # returns while ffprobe still runs
            self.assertTrue(probing.wait(2))
            got = []
            t = threading.Thread(target=lambda: got.append(rec.snapshot()["paused"]))
            t.start()
            t.join(1)
            self.assertEqual(got, [True])                   # the lock is free meanwhile
            entry = self.sidecar()[0]
            self.assertTrue(entry["in_progress"] and not entry["finalized"])   # not settled yet: safe
            stopper = threading.Thread(target=rec.stop)
            stopper.start()
            time.sleep(0.05)
            self.assertFalse(rec._stop_done.is_set())       # stop() waits for the probe ...
            release.set()
            stopper.join(5)
        self.assertTrue(rec._stop_done.is_set())
        entry = self.sidecar()[0]                           # ... and writes its result
        self.assertEqual((entry["in_progress"], entry["finalized"], entry["probed_frames"]), (False, False, 5))
        self.assertEqual(entry["frame_count"], len(entry["frames"]))

    def test_missing_ffprobe_leaves_the_clip_to_the_packager(self):
        def missing(*a, **k):
            raise FileNotFoundError("ffprobe")
        seg = self.stop_unfinalized(missing)
        self.assertEqual(seg.frame_count, 30)
        entry = self.sidecar()[0]
        self.assertEqual((entry["probe"], len(entry["frames"]), entry["finalized"]), ("unavailable", 30, False))

    def test_finalized_clip_is_not_probed(self):
        self.begin()
        with patch("recorder.recorder.subprocess.run", side_effect=AssertionError("probed")):
            self.rec.stop_segment()
        self.assertTrue(self.sidecar()[0]["finalized"])


class ProbeHelpersTest(unittest.TestCase):
    def test_probe_counts_the_gap_free_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seg.mp4")
            with open(path, "wb") as fh:
                fh.write(b"x")
            ok = _probe_result([0.1 + n / 60.0 for n in (0, 2, 1, 3, 5)])
            with patch("recorder.recorder.subprocess.run", return_value=ok):
                self.assertEqual(probe_frame_count(path, 60, "ffprobe"), 4)
            with patch("recorder.recorder.subprocess.run",
                       return_value=SimpleNamespace(stdout=b"N/A\n0.000000,\n", returncode=0)):
                self.assertEqual(probe_frame_count(path, 30, "ffprobe"), 1)
            with patch("recorder.recorder.subprocess.run", side_effect=OSError("no ffprobe")):
                self.assertIsNone(probe_frame_count(path, 30, "ffprobe"))
            self.assertEqual(probe_frame_count(os.path.join(tmp, "missing.mp4"), 30, "ffprobe"), 0)

    def test_probe_failure_is_unavailable_unless_the_input_is_reported_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seg.mp4")
            with open(path, "wb") as fh:
                fh.write(b"x" * 10)
            cases = [(_probe_result([], 1), None),                           # tool failed: unknown
                     (_probe_result([], 1, b"Access is denied.\n"), None),
                     (SimpleNamespace(stdout=b"", returncode=-9), None),     # killed, no stderr attr
                     (_probe_result([], 1, INVALID_INPUT), 0),               # positively unreadable
                     (_probe_result([], 0, b"seg.mp4: No such file or directory\n"), 0),
                     (_probe_result([], 0), 0),                              # ran fine: no video packet
                     (_probe_result([0.0, 1 / 30.0], 1), 2)]                 # packets: counted as before
            for res, want in cases:
                with self.subTest(res=res), patch("recorder.recorder.subprocess.run", return_value=res):
                    self.assertEqual(probe_frame_count(path, 30, "ffprobe"), want)

    def test_packet_cut_short_at_the_end_of_the_file_is_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seg.mp4")
            with open(path, "wb") as fh:
                fh.write(b"x" * 1000)
            out = ("pts_time=0.000000|size=100|pos=100\n"
                   "pts_time=0.033333|size=100|pos=200\n"
                   "pts_time=0.066667|size=N/A|pos=N/A\n"          # no position: kept
                   "pts_time=0.100000|size=750|pos=300\n"          # ends at byte 1050, past EOF (1000)
                   ).encode()
            with patch("recorder.recorder.subprocess.run",
                       return_value=SimpleNamespace(stdout=out, stderr=b"", returncode=0)):
                self.assertEqual(probe_frame_count(path, 30, "ffprobe"), 3)   # the last one reaches EOF
            out = b"pts_time=0.000000|size=100|pos=100\npts_time=0.033333|size=100|pos=200\n"
            with patch("recorder.recorder.subprocess.run",
                       return_value=SimpleNamespace(stdout=out, stderr=b"", returncode=0)):
                self.assertEqual(probe_frame_count(path, 30, "ffprobe"), 2)

    def test_segment_finalization_reads_the_k1_fields_defensively(self):
        self.assertEqual(segment_finalization(SimpleNamespace(finalized=False, returncode=1)), (False, 1))
        legacy = SimpleNamespace(to_dict=lambda: {"frames": []}, _proc=SimpleNamespace(returncode=3))
        self.assertEqual(segment_finalization(legacy), (False, 3))
        self.assertEqual(segment_finalization(SimpleNamespace(to_dict=lambda: {})), (True, None))


def _tool_runs(path):
    try:
        return subprocess.run([path, "-hide_banner", "-version"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


FFMPEG, FFPROBE = recmod.ffmpeg_path(), recmod.ffprobe_path()
MEDIA_OK = _tool_runs(FFMPEG) and _tool_runs(FFPROBE)


@unittest.skipUnless(MEDIA_OK, f"FFmpeg/FFprobe not runnable here ({FFMPEG}, {FFPROBE})")
class RealProbeTest(unittest.TestCase):
    """K1 with the real tools: a killed (truncated) fragmented clip never counts a
    frame it cannot play back."""

    def test_truncated_fragmented_clip(self):
        with tempfile.TemporaryDirectory(prefix="destudio-probe-") as tmp:
            src = os.path.join(tmp, "seg.mp4")
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                            "-frames:v", "150", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                            "-g", "60", "-movflags", "+frag_keyframe+empty_moov+default_base_moof", src],
                           check=True, capture_output=True, timeout=120)
            self.assertEqual(probe_frame_count(src, 30, FFPROBE), 150)
            data = open(src, "rb").read()
            for frac in (0.3, 0.62, 0.999):
                cut = os.path.join(tmp, "cut.mp4")
                with open(cut, "wb") as fh:
                    fh.write(data[:int(len(data) * frac)])
                n = probe_frame_count(cut, 30, FFPROBE)
                decoded = subprocess.run([FFPROBE, "-v", "error", "-count_frames", "-select_streams", "v:0",
                                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", cut],
                                         capture_output=True, text=True, timeout=120).stdout.strip()
                self.assertGreater(n, 0)
                self.assertLessEqual(n, int(decoded), frac)        # never overstated
                self.assertGreaterEqual(n, int(decoded) - 3, frac)  # ... and not much below


# ----------------------------------------------------------------- finding 29
class StartupStopTest(RecorderEnv):
    def setUp(self):
        super().setUp()
        FakeHook.instances = []
        p = patch("recorder.recorder.InputHook", FakeHook)
        p.start()
        self.addCleanup(p.stop)

    def capture_status(self):
        with open(os.path.join(self.rec.raw, "capture.json"), encoding="utf-8") as fh:
            return json.load(fh)["status"]

    def test_stop_while_the_hook_starts_removes_it(self):
        rec = self.rec
        orig = FakeHook.start

        def start_then_user_stops(hook):
            orig(hook)
            threading.Thread(target=rec.stop).start()
            self.assertTrue(rec._stop_done.wait(5))
        with patch.object(FakeHook, "start", start_then_user_stops):
            rec.run()
        hook = FakeHook.instances[0]
        self.assertTrue(hook.started and hook.stopped)
        self.assertFalse(hook.enabled)
        self.assertIsNone(rec._hook)
        self.assertEqual(rec.segments, [])
        self.assertEqual(self.capture_status(), "completed")

    def test_stop_before_run_installs_nothing(self):
        self.rec.stop()
        self.rec.run()
        self.assertEqual(FakeHook.instances, [])
        self.assertEqual(self.capture_status(), "completed")


# ------------------------------------------------------------------------ K11
class RecordingLockTest(RecorderEnv):
    """raw/.recording.lock: held from before the first clip until stop() wrote
    everything, so the packager can tell a crashed session from a live one."""

    def setUp(self):
        super().setUp()
        FakeHook.instances = []
        p = patch("recorder.recorder.InputHook", FakeHook)
        p.start()
        self.addCleanup(p.stop)
        self.rec.quiet = True
        self.writes = []
        real = self.rec._write_capture

        def spy(status, **kw):
            real(status, **kw)
            self.writes.append((status, reclock.is_live(self.rec.raw)))
        self.rec._write_capture = spy

    def capture(self):
        with open(os.path.join(self.rec.raw, "capture.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def start_thread(self):
        t = threading.Thread(target=self.rec.run, name="recorder-run", daemon=True)
        t.start()
        deadline = time.time() + 5
        while self.rec._watcher is None and time.time() < deadline:
            time.sleep(0.005)
        self.assertIsNotNone(self.rec._watcher)
        return t

    def test_held_while_recording_and_free_once_stop_returns(self):
        raw = self.rec.raw
        self.assertFalse(reclock.is_live(raw))
        t = self.start_thread()
        self.assertTrue(reclock.is_live(raw))
        self.assertEqual(len(self.rec.segments), 1)
        self.assertTrue(self.capture()["recording_lock"])
        self.rec.stop()
        self.assertFalse(reclock.is_live(raw))              # free before stop() returns
        t.join(5)
        self.assertEqual(self.capture()["status"], "completed")
        self.assertEqual(self.writes[0], ("recording", True))
        self.assertIn(("completed", True), self.writes)     # held until everything was written

    def test_recording_without_the_lock_still_records_and_warns(self):
        with patch("recorder.recorder.reclock.acquire", return_value=None):
            t = self.start_thread()
            self.assertEqual(len(self.rec.segments), 1)
            self.assertFalse(self.capture()["recording_lock"])
            self.rec.stop()
            t.join(5)
        self.assertIn("raw/.recording.lock", sys.stdout.getvalue())
        self.assertEqual(self.capture()["status"], "completed")

    def test_failed_start_writes_failed_then_frees_the_lock(self):
        def broken_hook(hook):
            raise RuntimeError("Keyboard/mouse hook failed to start: timeout")
        with patch.object(FakeHook, "start", broken_hook), self.assertRaises(RuntimeError):
            self.rec.run()
        self.assertEqual(self.writes[-1], ("failed", True))
        self.assertFalse(reclock.is_live(self.rec.raw))
        self.assertEqual(self.capture()["status"], "failed")

    def test_stop_before_run_takes_no_lock(self):
        self.rec.stop()
        self.rec.run()
        self.assertFalse(reclock.is_live(self.rec.raw))
        self.assertEqual(self.capture()["status"], "completed")


# ------------------------------------------------------------------- K9 / input
class FakeUser32:
    def __init__(self):
        self.next_hook_calls = 0
        self.fg = 1

    def GetDoubleClickTime(self):
        return 500

    def GetCursorPos(self, pt):
        return 1

    def CallNextHookEx(self, *a):
        self.next_hook_calls += 1
        return 0

    def GetForegroundWindow(self):
        return self.fg


class HotkeyTest(unittest.TestCase):
    def setUp(self):
        self.user32 = FakeUser32()
        p = patch.object(win_input, "user32", self.user32)
        p.start()
        self.addCleanup(p.stop)
        self.mods = 0
        p = patch.object(win_input, "modifiers_mask", lambda: self.mods)
        p.start()
        self.addCleanup(p.stop)
        self.fired = []
        self.got = []
        self.hook = win_input.InputHook(self.got.append, lambda: {"appName": "", "bundleId": "x.exe", "screen": 0},
                                        hotkeys={0x78: lambda: self.fired.append("F9"),
                                                 0x79: lambda: self.fired.append("F10")})
        self.hook._vk_to_char = lambda vk, scan: ("", False)
        self.hook._hkl_hex = lambda: "00000409"

    def key(self, vk, up=False):
        kb = win_input.KBDLLHOOKSTRUCT(vk, 0x43, win_input.LLKHF_UP if up else 0, 0, 0)
        return self.hook._kb_cb(0, 0x101 if up else 0x100, ctypes.addressof(kb))

    def queued(self):
        out = []
        while not self.hook._queue.empty():
            out.append(self.hook._queue.get_nowait())
        return out

    def wait_fired(self, n):
        deadline = time.time() + 2
        while len(self.fired) < n and time.time() < deadline:
            time.sleep(0.005)

    def test_plain_hotkey_fires_and_is_swallowed_with_its_repeats_and_key_up(self):
        self.assertEqual(self.key(0x78), 1)                 # consumed: not passed on
        self.assertEqual(self.key(0x78), 1)                 # auto-repeat
        self.assertEqual(self.key(0x78, up=True), 1)
        self.wait_fired(1)
        self.assertEqual(self.fired, ["F9"])
        self.assertEqual(self.user32.next_hook_calls, 0)
        self.assertEqual(self.queued(), [])                 # never logged
        self.assertEqual(self.hook.health()["hotkeys_fired"], 1)

    def test_modified_hotkey_is_an_ordinary_key(self):
        for mod in (win_input.MOD_SHIFT, win_input.MOD_CTRL, win_input.MOD_ALT, win_input.MOD_WIN):
            self.mods = mod
            self.assertEqual(self.key(0x79), 0)             # Shift+F10 = context menu, passed on
            self.assertEqual(self.key(0x79, up=True), 0)
        time.sleep(0.05)
        self.assertEqual(self.fired, [])
        keys = [r for r in self.queued() if r["type"] == "key"]
        self.assertEqual(len(keys), 8)
        self.assertTrue(all(r["keyCode"] == 0x79 for r in keys))
        self.mods = win_input.MOD_CAPS                      # Caps Lock on is not a modifier chord
        self.assertEqual(self.key(0x79), 1)
        self.wait_fired(1)
        self.assertEqual(self.fired, ["F10"])

    def test_lost_key_up_does_not_kill_the_next_press(self):
        self.key(0x78)
        self.hook._swallowed[0x78] -= win_input.HOTKEY_REPEAT_WINDOW_MS + 1   # key-up never seen
        self.assertEqual(self.key(0x78), 1)
        self.wait_fired(2)
        self.assertEqual(self.fired, ["F9", "F9"])

    def test_other_keys_pass_through_and_are_logged(self):
        self.assertEqual(self.key(0x41), 0)
        self.assertEqual(self.user32.next_hook_calls, 1)
        self.assertEqual([r["keyCode"] for r in self.queued() if r["type"] == "key"], [0x41])


class LiveForegroundContextTest(unittest.TestCase):
    def setUp(self):
        p = patch.object(win_window, "_monitors", lambda: [])     # Desktop() never reads real monitors
        p.start()
        self.addCleanup(p.stop)

    def test_key_right_after_a_focus_switch_gets_the_new_app(self):
        d = win_window.Desktop()
        calls = []

        def fake_foreground(force=False):
            calls.append(force)
            d._fg = fg_of("WeChat", "WeChat.exe", hwnd=77) if force else d._fg
            d._fg_hwnd = d._fg["hwnd"]
            return d._fg
        d._fg, d._fg_hwnd = fg_of("SOLIDWORKS", "SLDWORKS.exe", hwnd=1), 1
        d.foreground = fake_foreground
        user32 = FakeUser32()
        with patch.object(win_window, "IS_WINDOWS", True), patch.object(win_window, "user32", user32):
            self.assertEqual(d.event_context()["bundleId"], "SLDWORKS.exe")   # same window: cache
            user32.fg = 77                                                      # Alt+Tab / click
            self.assertEqual(d.event_context()["bundleId"], "WeChat.exe")      # live, not 100 ms stale
        self.assertEqual(calls, [False, True])

    def test_input_context_carries_the_live_foreground_pid(self):
        d = win_window.Desktop()
        d._fg, d._fg_hwnd = fg_of("SOLIDWORKS", "SLDWORKS.exe", hwnd=1, pid=200), 1

        def fake_foreground(force=False):
            if force:
                d._fg, d._fg_hwnd = fg_of("Mechanical", "AnsysWBU.exe", hwnd=88, pid=4242), 88
            return d._fg
        d.foreground = fake_foreground
        user32 = FakeUser32()
        with patch.object(win_window, "IS_WINDOWS", True), patch.object(win_window, "user32", user32):
            self.assertEqual(d.input_context()[win_window.INPUT_PID_KEY], 200)
            user32.fg = 88
            ctx = d.input_context()
        self.assertEqual((ctx["bundleId"], ctx[win_window.INPUT_PID_KEY]), ("AnsysWBU.exe", 4242))
        self.assertNotIn(win_window.INPUT_PID_KEY, d.event_context())     # snapshots never carry it


def _h(handle) -> int:
    return int(getattr(handle, "value", handle) or 0)


class FakeTextUser32(FakeUser32):
    """Window queries of win_window.Desktop; GetWindowTextW must never be used:
    it sends WM_GETTEXT to our own windows, which hangs while the Tk thread is busy."""

    def __init__(self):
        super().__init__()
        self.titles = {1: "Part1.SLDPRT - SOLIDWORKS", 55: "Database.kdbx - KeePass", 9: "D Studio"}
        self.pids = {1: 200, 55: 900, 9: os.getpid()}
        self.block_rect_for = None
        self.in_rect, self.release = threading.Event(), threading.Event()

    def InternalGetWindowText(self, hwnd, buf, n):
        buf.value = self.titles.get(_h(hwnd), "")
        return len(buf.value)

    def GetWindowTextW(self, hwnd, buf, n):
        raise AssertionError("GetWindowTextW sends WM_GETTEXT to windows of our own process")

    def GetClassNameW(self, hwnd, buf, n):
        buf.value = "C"
        return 1

    def GetWindowThreadProcessId(self, hwnd, ref):
        ref._obj.value = self.pids.get(_h(hwnd), 0)
        return 1

    def GetWindowRect(self, hwnd, ref):
        if threading.current_thread().name == "watcher" and _h(hwnd) == self.block_rect_for:
            self.in_rect.set()                  # a ctypes call: the GIL is free on Windows here
            self.release.wait(2)
        return 0


class DesktopForegroundTest(unittest.TestCase):
    EXES = {900: "KeePass.exe", 200: "SLDWORKS.exe"}        # (dirs: os.path.basename is the OS's own)

    def setUp(self):
        self.user32 = FakeTextUser32()
        for obj, name, value in ((win_window, "IS_WINDOWS", True), (win_window, "user32", self.user32),
                                 (win_window, "_monitors", lambda: []),
                                 (win_window.Desktop, "_exe_path", staticmethod(lambda pid: self.EXES.get(pid, "")))):
            p = patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_a_key_during_a_foreground_refresh_goes_to_the_new_app(self):
        u, d = self.user32, win_window.Desktop()
        self.assertEqual(d.foreground(force=True)["bundleId"], "SLDWORKS.exe")
        u.fg, u.block_rect_for = 55, 55                     # user switches to KeePass ...
        t = threading.Thread(target=lambda: d.foreground(force=True), name="watcher")
        t.start()                                           # ... the watcher refreshes and stalls
        self.assertTrue(u.in_rect.wait(2))
        ctx = d.input_context()                             # hook thread: a key typed into KeePass
        u.release.set()
        t.join(2)
        self.assertEqual((ctx["bundleId"], ctx[win_window.INPUT_PID_KEY]), ("KeePass.exe", 900))
        self.assertEqual(d.input_context()["bundleId"], "KeePass.exe")

    def test_an_older_refresh_finishing_last_never_overwrites_a_newer_one(self):
        u, d = self.user32, win_window.Desktop()
        u.fg, u.block_rect_for = 1, 1                       # SOLIDWORKS in front; the watcher's read stalls
        t = threading.Thread(target=lambda: d.foreground(force=True), name="watcher")
        t.start()
        self.assertTrue(u.in_rect.wait(2))
        u.fg = 55                                           # user switches to KeePass; the hook refreshes
        self.assertEqual(d.foreground(force=True)["bundleId"], "KeePass.exe")
        u.release.set()                                     # the stale read (SOLIDWORKS) finishes last
        t.join(2)
        hwnd, fg, _ = d._fg_cache
        self.assertEqual((hwnd, fg["bundleId"]), (55, "KeePass.exe"))   # pair kept whole, newest wins
        self.assertFalse(d.foreground_changed())
        self.assertEqual(d.event_context()["bundleId"], "KeePass.exe")

    def test_titles_are_read_without_wm_gettext(self):
        d = win_window.Desktop()
        self.assertEqual(win_window.Desktop._window_text(9), "D Studio")    # our own bar
        self.user32.fg = 1
        self.assertEqual(d.foreground(force=True)["windowTitle"], "Part1.SLDPRT - SOLIDWORKS")

    def test_without_internal_get_window_text_our_own_windows_are_never_asked(self):
        class Legacy(FakeTextUser32):
            InternalGetWindowText = None

            def GetWindowTextW(self, hwnd, buf, n):
                self.asked.append(_h(hwnd))
                buf.value = self.titles.get(_h(hwnd), "")
                return len(buf.value)
        legacy = Legacy()
        legacy.asked = []
        with patch.object(win_window, "user32", legacy):
            self.assertEqual(win_window.Desktop._window_text(9), "")
            self.assertEqual(win_window.Desktop._window_text(1), "Part1.SLDPRT - SOLIDWORKS")
        self.assertEqual(legacy.asked, [1])


# ---------------------------------------------------------- findings 10 / 11 (keys)
class AppModeKeyFilterTest(RecorderEnv):
    source = "app:SLDWORKS.exe"
    extra_args = ("--app-allow", "Companion.exe")
    PID = win_window.INPUT_PID_KEY

    def setUp(self):
        super().setUp()
        self.rec.masker.is_target_pid = lambda pid: pid == 4242     # Mechanical, launched by the target

    def key(self, exe, vk, pid=0, ch="x"):
        self.rec._on_input({"type": "key", "keyCode": vk, "characters": ch, "bundleId": exe,
                            "appName": exe, self.PID: pid, "timestamp": wall_ms()})

    def test_only_the_target_its_helpers_and_user_allowed_exes_get_keys(self):
        rec = self.rec
        self.key("SLDWORKS.exe", 65, pid=200, ch="a")              # the target
        self.key("TextInputHost.exe", 66, pid=500, ch="b")         # Win+V / emoji search: dropped
        self.key("TextInputHost.exe", 0xA0, pid=500, ch="")        # ... but modifiers are kept
        self.key("AnsysWBU.exe", 67, pid=4242, ch="c")             # helper process of the target
        self.key("AnsysWBU.exe", 68, pid=4300, ch="d")             # same exe, unrelated process
        self.key("Companion.exe", 69, pid=600, ch="e")             # named with --app-allow
        self.key("WeChat.exe", 70, pid=301, ch="f")                # another app
        self.key("KeePass.exe", 71, pid=900, ch="g")               # password manager
        rec._on_input({"type": "click", "x": 1, "y": 2, "bundleId": "WeChat.exe", self.PID: 301,
                       "timestamp": wall_ms()})
        logged = [e for e in self.events() if e["type"] in ("key", "click")]
        self.assertEqual([e.get("keyCode") for e in logged], [65, 0xA0, 67, 69, None])
        self.assertTrue(all(self.PID not in e for e in logged))     # the private PID is never logged
        self.assertEqual(rec.suppressed_keys, 4)

    def test_without_a_helper_lookup_only_the_exe_counts(self):
        self.rec.masker.is_target_pid = Mock(side_effect=OSError("snapshot failed"))
        self.key("AnsysWBU.exe", 67, pid=4242)
        self.key("SLDWORKS.exe", 65, pid=0)
        self.assertEqual([e["keyCode"] for e in self.events() if e["type"] == "key"], [65])

    def test_the_hook_reads_the_pid_carrying_context(self):
        made = []
        self.desktop.input_context = lambda: {"appName": "", "bundleId": "", "screen": 0, self.PID: 1}
        with patch("recorder.recorder.InputHook", lambda *a, **kw: made.append((a, kw)) or Mock()):
            self.rec._make_hook()
        self.assertIs(made[0][0][1], self.desktop.input_context)


# ----------------------------------------------------------------- finding 25
class OverlayAllowTest(RecorderEnv):
    source = "app:SLDWORKS.exe"
    extra_args = ("--app-overlay-allow", "CareUEyes.exe")

    def setUp(self):
        super().setUp()
        self.rec.masker._foreground = lambda: {"hwnd": 1, "pid": 200, "exe": "SLDWORKS.exe", "name": "SOLIDWORKS"}
        self.rec.masker._enumerate = lambda bounds: []
        self.stats = {"last_window_reasons": {}, "covered_unknown_overlays": []}
        self.rec.masker.stats = lambda: dict(self.stats)
        self.begin()

    def test_allowed_overlays_reach_the_masker_and_capture_json(self):
        rec = self.rec
        self.assertIn("careueyes.exe", rec.masker.overlay_exes)
        rec._write_capture("recording")
        with open(os.path.join(rec.raw, "capture.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["app_target"]["overlay_allow"], ["careueyes.exe"])

    def english(self):
        old = get_language()
        set_language("en", persist=False)
        self.addCleanup(set_language, old, False)

    def test_unknown_overlay_blacking_out_the_video_is_explained_while_it_lasts(self):
        rec = self.rec
        self.english()
        # the session-wide set still lists an overlay that is gone: only the current one is named
        self.stats = {"last_window_reasons": {"overlay_unknown": 1, "target": 1},
                      "covered_unknown_overlays": ["aaa-old.exe", "dimmer.exe"],
                      "last_unknown_overlays": ["dimmer.exe"]}
        rec._check_overlays()
        self.assertIn("--app-overlay-allow dimmer.exe", rec.last_error)
        self.assertNotIn("aaa-old.exe", rec.last_error)
        self.assertIn("covers (part of) the screen", rec.last_error)
        self.assertEqual(rec.snapshot()["last_error"], rec.last_error)
        self.stats["last_window_reasons"] = {"target": 1}         # the overlay closed
        rec._check_overlays()
        self.assertEqual(rec.last_error, "")

    def test_overlay_of_an_unidentified_process_is_never_offered_to_the_flag(self):
        rec = self.rec
        self.english()
        self.stats = {"last_window_reasons": {"overlay_unknown": 1}, "covered_unknown_overlays": ["?"],
                      "last_unknown_overlays": ["?"]}
        rec._check_overlays()
        self.assertNotIn("?", rec.last_error.replace("(part of)", ""))
        self.assertIn("could not be identified", rec.last_error)
        self.stats["last_unknown_overlays"] = ["?", "Dimmer.exe"]
        rec._check_overlays()
        self.assertIn("--app-overlay-allow Dimmer.exe", rec.last_error)
        self.assertNotIn("?", rec.last_error.replace("(part of)", ""))

    def test_message_is_bilingual_and_an_older_masker_still_works(self):
        rec = self.rec
        old = get_language()
        self.addCleanup(set_language, old, False)
        self.stats = {"last_window_reasons": {"overlay_unknown": 1},
                      "covered_unknown_overlays": ["dimmer.exe"]}  # no last_unknown_overlays key
        for lang, word in (("zh", "覆盖层"), ("en", "overlay")):
            set_language(lang, persist=False)
            rec._check_overlays()
            self.assertIn(word, rec.last_error)
            self.assertIn("--app-overlay-allow dimmer.exe", rec.last_error)

    def test_other_errors_are_never_replaced_or_cleared(self):
        rec = self.rec
        rec.last_error = "could not restart capture"
        rec._check_overlays()                                     # nothing covering: kept
        self.assertEqual(rec.last_error, "could not restart capture")
        rec.toggle_pause()
        self.stats = {"last_window_reasons": {"overlay_unknown": 1},
                      "covered_unknown_overlays": ["dimmer.exe"]}
        rec._check_overlays()                                     # paused: stale mask stats
        self.assertEqual(rec.last_error, "could not restart capture")

    def test_flag_is_parsed(self):
        args = parse_args(["start", "--source", "app:x.exe", "--app-overlay-allow", "a.exe", "B.EXE"])
        self.assertEqual(args.app_overlay_allow, ["a.exe", "B.EXE"])
        self.assertEqual(parse_args(["start"]).app_overlay_allow, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
