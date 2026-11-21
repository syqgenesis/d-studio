"""recorder.settings: defaults, round-trip, sanitising, atomic + concurrent writes."""
import json
import os
import tempfile
import threading
import unittest

from recorder import settings
from recorder.settings import DEFAULTS, load_settings, sanitize_settings, save_settings, update_settings


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_env = os.environ.get("DSTUDIO_SETTINGS")
        self.path = os.path.join(self.tmp.name, "nested", "settings.json")
        os.environ["DSTUDIO_SETTINGS"] = self.path

    def tearDown(self):
        if self.saved_env is None:
            os.environ.pop("DSTUDIO_SETTINGS", None)
        else:
            os.environ["DSTUDIO_SETTINGS"] = self.saved_env
        self.tmp.cleanup()

    def write_raw(self, text, encoding="utf-8"):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding=encoding) as fh:
            fh.write(text)

    def test_defaults_when_missing(self):
        self.assertEqual(settings.settings_path(), self.path)
        self.assertEqual(load_settings(), DEFAULTS)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(DEFAULTS["fps"], 30)            # Markov default
        self.assertTrue(DEFAULTS["draw_cursor"])         # cursor in video by default
        self.assertTrue(DEFAULTS["markov_delivery"])     # Markov CAD export is the default delivery

    def test_round_trip(self):
        update_settings(language="en", fps=60, quality=20, capture_mode="app", app_exe="SLDWORKS.exe",
                        app_name="SOLIDWORKS", app_allow=["helper.exe"], draw_cursor=False,
                        backend="gdi", autopack=False, data_dir="D:\\rec", title="标题",
                        bar_pos=[100, 20], markov_delivery=False, app_overlay_allow=["dimmer.exe"],
                        recovery_dismissed=["rec-20261118-101500"])
        data = load_settings()
        self.assertEqual(data["language"], "en")
        self.assertEqual(data["fps"], 60)
        self.assertEqual(data["capture_mode"], "app")
        self.assertEqual(data["app_exe"], "SLDWORKS.exe")
        self.assertEqual(data["app_allow"], ["helper.exe"])
        self.assertFalse(data["draw_cursor"])
        self.assertEqual(data["backend"], "gdi")
        self.assertEqual(data["title"], "标题")
        self.assertEqual(data["bar_pos"], [100, 20])
        self.assertIs(data["markov_delivery"], False)
        self.assertEqual(data["app_overlay_allow"], ["dimmer.exe"])
        self.assertEqual(data["recovery_dismissed"], ["rec-20261118-101500"])
        with open(self.path, encoding="utf-8") as fh:
            self.assertIn("标题", fh.read())          # ensure_ascii=False, readable file

    def test_unknown_keys_dropped_and_partial_file_filled(self):
        self.write_raw(json.dumps({"fps": 60, "bogus": 1}))
        data = load_settings()
        self.assertEqual(data["fps"], 60)
        self.assertNotIn("bogus", data)
        self.assertEqual(data["backend"], "auto")
        update_settings(bogus=2)
        with open(self.path, encoding="utf-8") as fh:
            self.assertNotIn("bogus", json.load(fh))

    def test_corrupt_or_odd_files(self):
        self.write_raw("{not json")
        self.assertEqual(load_settings(), DEFAULTS)
        self.write_raw("[1, 2]")
        self.assertEqual(load_settings(), DEFAULTS)
        self.write_raw(json.dumps({"language": "zh"}), encoding="utf-8-sig")  # Notepad BOM
        self.assertEqual(load_settings()["language"], "zh")

    def test_sanitising(self):
        data = sanitize_settings({"fps": "60", "quality": 99, "backend": "GDI", "capture_mode": "window",
                                  "language": "fr", "app_allow": "a.exe; b.exe,a.exe", "draw_cursor": "false",
                                  "autopack": 0, "viewer_port": "70000", "bar_pos": [1, True],
                                  "app_exe": None, "title": 5, "markov_delivery": "off"})
        self.assertEqual(data["fps"], 60)
        self.assertEqual(data["quality"], DEFAULTS["quality"])
        self.assertEqual(data["backend"], "gdi")
        self.assertEqual(data["capture_mode"], "desktop")
        self.assertEqual(data["language"], "")
        self.assertEqual(data["app_allow"], ["a.exe", "b.exe"])
        self.assertIs(data["draw_cursor"], False)
        self.assertIs(data["autopack"], False)
        self.assertEqual(data["viewer_port"], 8000)
        self.assertEqual(data["bar_pos"], [])
        self.assertEqual(data["app_exe"], "")
        self.assertEqual(data["title"], DEFAULTS["title"])
        self.assertIs(data["markov_delivery"], False)
        self.assertIs(sanitize_settings({"markov_delivery": "maybe"})["markov_delivery"], True)
        # the overlay allow list (⚙ → App only) is cleaned like app_allow
        clean = sanitize_settings({"app_overlay_allow": " dimmer.exe; wm.exe,dimmer.exe ,",
                                   "recovery_dismissed": ["rec-1", "", "rec-1", 5, " rec-2 "]})
        self.assertEqual(clean["app_overlay_allow"], ["dimmer.exe", "wm.exe"])
        self.assertEqual(clean["recovery_dismissed"], ["rec-1", "rec-2"])
        for bad in (None, 5, {"a": 1}):
            self.assertEqual(sanitize_settings({"app_overlay_allow": bad, "recovery_dismissed": bad})
                             ["app_overlay_allow"], [])
        self.assertEqual(DEFAULTS["app_overlay_allow"], [])
        self.assertEqual(DEFAULTS["recovery_dismissed"], [])

    def test_older_settings_file_gets_markov_default(self):
        self.write_raw(json.dumps({"language": "zh", "fps": 30}))   # written by v0.2.0
        self.assertIs(load_settings()["markov_delivery"], True)

    def test_invalid_update_keeps_previous_value(self):
        update_settings(fps=60)
        update_settings(fps=500, backend="nope")
        data = load_settings()
        self.assertEqual(data["fps"], 60)
        self.assertEqual(data["backend"], "auto")

    def test_defaults_are_never_mutated_by_callers(self):
        data = load_settings()
        data["app_allow"].append("x.exe")
        data["bar_pos"].append(1)
        self.assertEqual(DEFAULTS["app_allow"], [])
        self.assertEqual(load_settings()["app_allow"], [])

    def test_atomic_write_leaves_no_temp_files(self):
        for i in range(5):
            save_settings(dict(load_settings(), fps=30 + i))
        folder = os.path.dirname(self.path)
        self.assertEqual(sorted(os.listdir(folder)), ["settings.json"])
        self.assertEqual(load_settings()["fps"], 34)

    def test_concurrent_updates_keep_every_key(self):
        keys = [("fps", 60), ("quality", 18), ("backend", "dxcam"), ("title", "t"),
                ("app_exe", "a.exe"), ("autopack", False), ("draw_cursor", False), ("language", "en")]
        threads = [threading.Thread(target=update_settings, kwargs={k: v}) for k, v in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        data = load_settings()
        for key, value in keys:
            self.assertEqual(data[key], value, key)


if __name__ == "__main__":
    unittest.main()
