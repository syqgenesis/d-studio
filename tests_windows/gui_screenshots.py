"""Render the recorder bar in both languages via the GUI's CI hook and keep the PNGs.

    python tests_windows/gui_screenshots.py --out artifacts/screenshots

Required (contract): ``python -m recorder gui --screenshot OUT.png --lang en|zh [--expand]``
exits 0 and writes a real PNG. Extra demo states (``--state recording-demo`` ...)
are captured when the GUI supports them and only warn on failure.
Writes ``<out>/report.json``; exit 1 if a required screenshot failed.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXTRA_STATES = ("recording-demo", "autopaused-demo", "paused-demo", "done-demo")


def png_size(path: str):
    """(width, height) from the PNG IHDR chunk, or None if this is not a PNG."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def shoot(out_png: str, extra, env, timeout: float = 90.0) -> dict:
    if os.path.exists(out_png):
        os.remove(out_png)
    cmd = [sys.executable, "-m", "recorder", "gui", "--screenshot", out_png, *extra]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, timeout=timeout,
                              creationflags=0x08000000 if sys.platform == "win32" else 0)
        code, output = proc.returncode, (proc.stdout + proc.stderr).decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        code, output = None, f"timeout after {timeout}s\n" + ((exc.stdout or b"") + (exc.stderr or b"")).decode("utf-8", "replace")
    size = png_size(out_png)
    ok = code == 0 and size is not None and size[0] >= 100 and size[1] >= 20
    return {"cmd": " ".join(cmd[1:]), "exit": code, "png": out_png if size else None, "size": size, "ok": ok,
            "seconds": round(time.time() - t0, 1), "output": output.strip()[-1500:]}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "artifacts", "screenshots"))
    args = ap.parse_args(argv)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    env.setdefault("DSTUDIO_SETTINGS", os.path.join(out, "_settings.json"))
    env.setdefault("DSTUDIO_DATA_DIR", os.path.join(out, "_data"))

    help_text = subprocess.run([sys.executable, "-m", "recorder", "gui", "--help"], cwd=ROOT, env=env,
                               capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    supports_state = "--state" in help_text
    shots = []
    for lang in ("en", "zh"):
        shots.append(("required", f"gui-{lang}.png", ["--lang", lang]))
        shots.append(("required", f"gui-{lang}-expanded.png", ["--lang", lang, "--expand"]))
        if supports_state:
            for state in EXTRA_STATES:
                shots.append(("optional", f"gui-{lang}-{state}.png", ["--lang", lang, "--state", state]))
    results = []
    for kind, name, extra in shots:
        res = shoot(os.path.join(out, name), extra, env)
        res["kind"] = kind
        results.append(res)
        print(f"[screens] {'ok  ' if res['ok'] else ('FAIL' if kind == 'required' else 'warn')} {name} "
              f"exit={res['exit']} size={res['size']} ({res['seconds']}s)", flush=True)
        if not res["ok"]:
            print("    " + res["output"].replace("\n", "\n    "), flush=True)
    ok = all(r["ok"] for r in results if r["kind"] == "required")
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as fh:
        json.dump({"ok": ok, "supports_state": supports_state, "shots": results}, fh, ensure_ascii=False, indent=2)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
