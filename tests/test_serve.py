"""Annotation viewer server (tools/serve.py) and viewer static checks.

    python -m unittest tests.test_serve -v

Covers: start_server() on an ephemeral port with a temp data dir, dataset
listing, Range requests, the save API, path-traversal / cross-site refusals,
port-in-use errors, the CLI entry point, and that every viewer string is
bilingual (plus `node --check` on the JS when node is installed).
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.serve import ServerStartError, list_datasets, start_server, stop_server  # noqa: E402

VIEWER = os.path.join(ROOT, "viewer")
VIDEO_BYTES = bytes(range(256)) * 4          # 1024 bytes, every offset distinct mod 256
SECRET = "top-secret-do-not-serve"


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def make_dataset(folder, *, video_name="clip.mp4", video_file_key=True, cursor=True):
    meta = {"workflow_id": os.path.basename(folder), "title": "Fixture", "total_frames": 30,
            "fps": 30, "total_duration_ms": 1000, "event_count": 2, "events_schema": 2,
            "clips": [{"clip_index": 0, "frame_start": 0, "frame_count": 30}],
            "capture_mode": "app", "cursor_in_video": cursor}
    if video_file_key:
        meta["video_file"] = video_name
    write_json(os.path.join(folder, "metadata.json"), meta)
    with open(os.path.join(folder, video_name), "wb") as fh:
        fh.write(VIDEO_BYTES)
    write_json(os.path.join(folder, "narration.json"), {"summary": "", "annotations": []})
    os.makedirs(os.path.join(folder, "view"), exist_ok=True)
    with open(os.path.join(folder, "view", "events.jsonl"), "w", encoding="utf-8") as fh:
        fh.write('{"t":0.0,"type":"active_app","name":"Fixture"}\n')
        fh.write('{"t":500.0,"type":"privacy_mask_start","appName":"WeChat","trigger":"non_target_app"}\n')


class ServeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="dstudio-serve-")
        cls.data = os.path.join(cls.tmp, "data")
        os.makedirs(cls.data)
        with open(os.path.join(cls.tmp, "secret.txt"), "w", encoding="utf-8") as fh:
            fh.write(SECRET)
        make_dataset(os.path.join(cls.data, "rec-test"))
        make_dataset(os.path.join(cls.data, "legacy"), video_name="video.mp4",
                     video_file_key=False, cursor=False)
        make_dataset(os.path.join(cls.data, "录制-1"))
        os.makedirs(os.path.join(cls.data, "no-metadata"))
        make_dataset(os.path.join(cls.data, ".hidden"))
        cls.server, cls.thread = start_server(port=0, data_dir=cls.data, quiet=True)
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        stop_server(cls.server)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ----------------------------------------------------------------- helpers
    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            data = body.encode("utf-8") if isinstance(body, str) else body
            conn.request(method, path, body=data, headers=headers or {})
            res = conn.getresponse()
            return res.status, {k.lower(): v for k, v in res.getheaders()}, res.read()
        finally:
            conn.close()

    def save(self, file, payload, dataset="rec-test", headers=None):
        body = payload if isinstance(payload, (str, bytes)) else json.dumps(payload)
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        return self.request("POST", f"/api/save?dataset={quote(dataset)}&file={file}", body, hdrs)

    # ------------------------------------------------------------------- tests
    def test_server_runs_on_daemon_thread(self):
        self.assertTrue(self.thread.daemon)
        self.assertTrue(self.thread.is_alive())
        self.assertNotEqual(self.port, 0)
        self.assertEqual(self.server.url, f"http://127.0.0.1:{self.port}/viewer/")

    def test_datasets_listing(self):
        status, headers, body = self.request("GET", "/api/datasets")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["content-type"])
        data = json.loads(body)
        self.assertEqual(os.path.normcase(data["data_dir"]), os.path.normcase(os.path.abspath(self.data)))
        by_id = {d["id"]: d for d in data["datasets"]}
        self.assertEqual(set(by_id), {"rec-test", "legacy", "录制-1"})   # no metadata / hidden excluded
        rec = by_id["rec-test"]
        self.assertEqual(rec["video_file"], "clip.mp4")
        self.assertTrue(rec["has_video"])
        self.assertTrue(rec["cursor_in_video"])
        self.assertEqual(rec["capture_mode"], "app")
        self.assertEqual(rec["total_frames"], 30)
        legacy = by_id["legacy"]
        self.assertEqual(legacy["video_file"], "video.mp4")   # fallback without metadata.video_file
        self.assertFalse(legacy["cursor_in_video"])
        self.assertEqual(len(list_datasets(self.data)), 3)

    def test_single_dataset_and_ping(self):
        status, _, body = self.request("GET", "/api/dataset?id=rec-test")
        self.assertEqual(status, 200)
        info = json.loads(body)
        self.assertEqual(info["id"], "rec-test")
        self.assertGreater(info["narration_mtime"], 0)
        self.assertEqual(self.request("GET", "/api/dataset?id=..")[0], 404)
        self.assertEqual(self.request("GET", "/api/dataset?id=missing")[0], 404)
        status, _, body = self.request("GET", "/api/ping")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_range_requests(self):
        path = "/data/rec-test/clip.mp4"
        status, headers, body = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(body, VIDEO_BYTES)
        self.assertEqual(headers["accept-ranges"], "bytes")
        self.assertEqual(headers["content-type"], "video/mp4")

        status, headers, body = self.request("GET", path, headers={"Range": "bytes=10-19"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["content-range"], f"bytes 10-19/{len(VIDEO_BYTES)}")
        self.assertEqual(headers["content-length"], "10")
        self.assertEqual(body, VIDEO_BYTES[10:20])

        status, headers, body = self.request("GET", path, headers={"Range": "bytes=1000-"})
        self.assertEqual(status, 206)
        self.assertEqual(body, VIDEO_BYTES[1000:])

        status, headers, body = self.request("GET", path, headers={"Range": "bytes=-5"})
        self.assertEqual(status, 206)
        self.assertEqual(body, VIDEO_BYTES[-5:])

        status, headers, _ = self.request("GET", path, headers={"Range": "bytes=5000-6000"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["content-range"], f"bytes */{len(VIDEO_BYTES)}")

        status, headers, body = self.request("HEAD", path, headers={"Range": "bytes=0-99"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["content-length"], "100")
        self.assertEqual(body, b"")

    def test_unicode_dataset_id(self):
        status, _, body = self.request("GET", "/data/" + quote("录制-1") + "/clip.mp4",
                                       headers={"Range": "bytes=0-3"})
        self.assertEqual(status, 206)
        self.assertEqual(body, VIDEO_BYTES[:4])
        status, _, _ = self.save("draft.json", {"summary": "", "annotations": []}, dataset="录制-1")
        self.assertEqual(status, 200)

    def test_viewer_static(self):
        status, headers, body = self.request("GET", "/viewer/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertIn(b"i18n.js", body)
        status, headers, _ = self.request("GET", "/viewer/app.js")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/javascript"))
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers["location"], "/viewer/")
        status, headers, _ = self.request("GET", "/?lang=en")
        self.assertEqual(headers["location"], "/viewer/?lang=en")

    def test_save_narration(self):
        payload = {"summary": "Model a bracket", "annotations": [
            {"start_s": 0.0, "end_s": 0.5, "action": "Opened 草图", "intent": "Start", "app": "SW", "details": ""},
            {"start_s": 0.5, "end_s": 1.0, "action": "Extruded", "intent": "Solid", "app": "SW", "details": ""},
        ]}
        status, _, body = self.save("narration.json", payload)
        self.assertEqual(status, 200, body)
        reply = json.loads(body)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["annotations"], 2)
        with open(os.path.join(self.data, "rec-test", "narration.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), payload)
        status, _, body = self.request("GET", "/data/rec-test/narration.json")
        self.assertEqual(json.loads(body), payload)
        leftovers = [n for n in os.listdir(os.path.join(self.data, "rec-test")) if n.endswith(".tmp")]
        self.assertEqual(leftovers, [])

        status, _, _ = self.save("draft.json", {"annotations": [{"start_s": 0, "end_s": 1, "action": ""}]})
        self.assertEqual(status, 200)

    def test_save_rejections(self):
        meta_path = os.path.join(self.data, "rec-test", "metadata.json")
        with open(meta_path, "rb") as fh:
            meta_before = fh.read()
        good = {"summary": "", "annotations": []}
        cases = [
            (self.save("metadata.json", good), 400),
            (self.save("../metadata.json", good), 400),
            (self.save("narration.json", good, dataset=".."), 400),
            (self.save("narration.json", good, dataset="a/b"), 400),
            (self.save("narration.json", good, dataset="does-not-exist"), 404),
            (self.save("narration.json", "{not json"), 400),
            (self.save("narration.json", {"summary": ""}), 400),
            (self.save("narration.json", '{"annotations": [{"start_s": NaN, "end_s": 1}]}'), 400),
            (self.save("narration.json", {"annotations": [{"start_s": "0", "end_s": 1}]}), 400),
            (self.save("narration.json", good, headers={"Content-Type": "text/plain"}), 415),
            (self.save("narration.json", good, headers={"Origin": "http://evil.example"}), 403),
            (self.save("narration.json", good, headers={"Origin": "null"}), 403),
            (self.request("POST", "/api/other", "{}", {"Content-Type": "application/json"}), 404),
        ]
        for i, ((status, _, body), expected) in enumerate(cases):
            self.assertEqual(status, expected, f"case {i}: {body!r}")
        with open(meta_path, "rb") as fh:
            self.assertEqual(fh.read(), meta_before)

    def test_same_origin_beacon_is_accepted(self):
        # navigator.sendBeacon() posts text/plain; allowed only with our own Origin
        origin = f"http://127.0.0.1:{self.port}"
        status, _, body = self.save("draft.json", {"annotations": []},
                                    headers={"Content-Type": "text/plain;charset=UTF-8", "Origin": origin})
        self.assertEqual(status, 200, body)

    def test_path_traversal_rejected(self):
        attempts = [
            "/data/../secret.txt",
            "/data/%2e%2e/secret.txt",
            "/data/..%2fsecret.txt",
            "/data/rec-test/../../secret.txt",
            "/data/rec-test/..%2f..%2fsecret.txt",
            "/data/rec-test/%2e%2e%2f%2e%2e%2fsecret.txt",
            "/data/rec-test/..%5c..%5csecret.txt",
            "/data/rec-test/%2e%2e%5c%2e%2e%5csecret.txt",
            "/data/rec-test/C:%5csecret.txt",
            "/data/rec-test/clip.mp4%00.txt",
            "/data/rec-test/",
            "/data/rec-test",
            "/data/.hidden/metadata.json",
            "/viewer/../tools/serve.py",
            "/viewer/%2e%2e/tools/serve.py",
            "/viewer/..%2f..%2fsecret.txt",
            "/tools/serve.py",
            "/README.md",
        ]
        for path in attempts:
            status, _, body = self.request("GET", path)
            self.assertIn(status, (400, 404), path)
            self.assertNotIn(SECRET.encode(), body, path)
            self.assertNotIn(b"def start_server", body, path)

    def test_foreign_host_header_rejected(self):
        status, _, _ = self.request("GET", "/api/datasets", headers={"Host": "attacker.example:80"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/ping", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)

    def test_reuse_in_same_process(self):
        again, thread = start_server(port=0, data_dir=self.data, quiet=True)
        self.assertIs(again, self.server)
        self.assertIs(thread, self.thread)


class PortTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dstudio-port-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_port_in_use(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy = blocker.getsockname()[1]
            with self.assertRaises(OSError) as ctx:
                start_server(port=busy, data_dir=self.tmp, port_tries=1, quiet=True)
            self.assertIsInstance(ctx.exception, ServerStartError)
            self.assertIn(str(busy), str(ctx.exception))
            self.assertNotIn("[Errno", str(ctx.exception))   # readable for the GUI

            server, thread = start_server(port=busy, data_dir=self.tmp, port_tries=20, quiet=True)
            try:
                self.assertNotEqual(server.server_address[1], busy)
                self.assertGreater(server.server_address[1], busy)
                conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                conn.request("GET", "/api/ping")
                self.assertEqual(conn.getresponse().status, 200)
                conn.close()
            finally:
                stop_server(server)
            self.assertFalse(thread.is_alive())
        finally:
            blocker.close()

    def test_cli_entry_point(self):
        """`python tools/serve.py` (script mode: repo root not on sys.path)."""
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "tools", "serve.py"), "--port", "0",
             "--data-dir", self.tmp, "--quiet"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=self.tmp, env=env)
        lines = []
        timer = threading.Timer(20, proc.kill)
        timer.start()
        try:
            url = None
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                lines.append(line)
                m = re.search(r"http://127\.0\.0\.1:(\d+)/viewer/", line)
                if m:
                    url = int(m.group(1))
                    break
            self.assertIsNotNone(url, "\n".join(lines))
            conn = http.client.HTTPConnection("127.0.0.1", url, timeout=10)
            conn.request("GET", "/api/datasets")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            self.assertEqual(json.loads(res.read())["datasets"], [])
            conn.close()
        finally:
            timer.cancel()
            proc.kill()
            proc.wait(10)
            proc.stdout.close()


# ------------------------------------------------------------- viewer checks
DICT_ENTRY = re.compile(r"""'([\w.]+)':\s*\{\s*zh:\s*'((?:[^'\\]|\\.)*)',\s*en:\s*'((?:[^'\\]|\\.)*)'\s*\}""")
CJK = re.compile(r"[　-〿一-鿿＀-￯]")


def read(name):
    with open(os.path.join(VIEWER, name), encoding="utf-8") as fh:
        return fh.read()


class _I18nAudit(HTMLParser):
    """Collect data-i18n keys and CJK text that is not covered by one."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.keys = set()
        self.uncovered = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for attr in ("data-i18n", "data-i18n-title", "data-i18n-placeholder", "data-i18n-aria"):
            if a.get(attr):
                self.keys.add(a[attr])
        for attr, cover in (("title", "data-i18n-title"), ("placeholder", "data-i18n-placeholder")):
            value = a.get(attr)
            if value and CJK.search(value) and not a.get(cover):
                self.uncovered.append(f"<{tag} {attr}={value!r}>")
        if tag not in ("input", "meta", "link", "br", "img"):
            self.stack.append((tag, a))

    def handle_endtag(self, tag):
        while self.stack:
            t, _ = self.stack.pop()
            if t == tag:
                break

    def handle_data(self, data):
        if not CJK.search(data) or not self.stack:
            return
        tag, a = self.stack[-1]
        if tag == "title" or "data-i18n" in a or "data-lang" in a:
            return
        self.uncovered.append(data.strip())


class ViewerStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dict = {m.group(1): (m.group(2), m.group(3)) for m in DICT_ENTRY.finditer(read("i18n.js"))}

    def test_dictionary_complete(self):
        self.assertGreater(len(self.dict), 50)
        for key, (zh, en) in self.dict.items():
            self.assertTrue(zh.strip(), key)
            self.assertTrue(en.strip(), key)
            self.assertFalse(CJK.search(en.replace("中文", "")), f"{key}: Chinese in English text")
            self.assertEqual(set(re.findall(r"\{(\w+)\}", zh)), set(re.findall(r"\{(\w+)\}", en)), key)

    def test_index_html_fully_translated(self):
        audit = _I18nAudit()
        audit.feed(read("index.html"))
        self.assertEqual(audit.uncovered, [])
        missing = sorted(k for k in audit.keys if k not in self.dict)
        self.assertEqual(missing, [])
        self.assertIn('id="btn-lang"', read("index.html"))

    def test_scripts_use_dictionary(self):
        prefixes = {k.split(".")[0] for k in self.dict}
        for name in ("app.js", "overlay.js"):
            src = read(name)
            self.assertFalse(CJK.search(src), f"{name} has hard-coded Chinese; use I18N.t()")
            used = {k for k in re.findall(r"'([a-z]+\.[\w.]+)'", src) if k.split(".")[0] in prefixes}
            missing = sorted(k for k in used if k not in self.dict)
            self.assertEqual(missing, [], name)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_javascript_parses(self):
        for name in ("i18n.js", "overlay.js", "app.js"):
            res = subprocess.run(["node", "--check", os.path.join(VIEWER, name)],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"{name}: {res.stderr}")


if __name__ == "__main__":
    unittest.main()
