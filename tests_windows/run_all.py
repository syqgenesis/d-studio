"""Run every Windows E2E scenario, each in its own process with a timeout.

    python tests_windows/run_all.py --out artifacts/e2e [--only app_only pause_resume]

Writes ``<out>/summary.json`` and ``<out>/summary.md`` (also appended to
$GITHUB_STEP_SUMMARY on GitHub Actions) and exits 1 if any scenario failed.
Recordings are isolated: DSTUDIO_DATA_DIR / DSTUDIO_SETTINGS point into <out>.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SCENARIOS = [
    ("desktop30", "e2e_desktop30.py", 240),
    ("fps60", "e2e_fps60.py", 480),
    ("app_only", "e2e_app_only.py", 300),
    ("pause_resume", "e2e_pause_resume.py", 240),
    ("export", "e2e_export.py", 300),
    # v0.2.1
    ("mask_transition", "e2e_mask_transition.py", 240),
    ("crash_safety", "e2e_crash_safety.py", 240),
    ("display_encoder", "e2e_display_encoder.py", 240),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "artifacts", "e2e"))
    ap.add_argument("--only", nargs="*", help="run only these scenarios")
    args = ap.parse_args(argv)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    if sys.platform != "win32":
        print("[e2e] not Windows: nothing to run")
        with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
            json.dump({"skipped": True, "ok": True, "scenarios": []}, fh)
        return 0

    sys.path.insert(0, HERE)
    from e2e_lib import warn_takeover

    warn_takeover(8)
    env = dict(os.environ)
    env["DSTUDIO_E2E_CONFIRMED"] = "1"
    env.setdefault("DSTUDIO_DATA_DIR", os.path.join(out, "_data"))
    env.setdefault("DSTUDIO_SETTINGS", os.path.join(out, "_settings.json"))
    env.setdefault("DSTUDIO_LANG", "en")
    env.setdefault("DSTUDIO_ENV_REPORT", os.path.join(ROOT, "artifacts", "env", "env.json"))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    results = []
    for name, script, timeout in SCENARIOS:
        if args.only and name not in args.only:
            continue
        sdir = os.path.join(out, name)
        os.makedirs(sdir, exist_ok=True)
        print(f"\n========== {name} ==========", flush=True)
        t0 = time.time()
        log_path = os.path.join(sdir, "console.log")
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            proc = subprocess.Popen([sys.executable, os.path.join(HERE, script), "--out", sdir], cwd=ROOT, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            deadline = t0 + timeout
            timed_out = False
            assert proc.stdout is not None

            def pump() -> None:
                for raw in proc.stdout:   # type: ignore[union-attr]
                    line = raw.decode("utf-8", "replace")
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    log.write(line)

            th = threading.Thread(target=pump, daemon=True)
            th.start()
            while proc.poll() is None:
                if time.time() > deadline:
                    timed_out = True
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
                    break
                time.sleep(0.5)
            proc.wait(30)
            th.join(5)
        report_path = os.path.join(sdir, "report.json")
        report = {}
        if os.path.isfile(report_path):
            try:
                with open(report_path, encoding="utf-8") as fh:
                    report = json.load(fh)
            except ValueError:
                report = {}
        ok = proc.returncode == 0 and bool(report.get("ok")) and not timed_out
        failed = [c["name"] for c in report.get("checks", []) if not c.get("ok") and not c.get("soft")]
        warned = [c["name"] for c in report.get("checks", []) if not c.get("ok") and c.get("soft")]
        results.append({"name": name, "ok": ok, "exit": proc.returncode, "timed_out": timed_out,
                        "seconds": round(time.time() - t0, 1), "failed": failed, "warnings": warned,
                        "error": (report.get("error") or "").strip().splitlines()[-1:] if report.get("error") else []})
        print(f"[e2e] {name}: {'OK' if ok else 'FAILED'} in {results[-1]['seconds']}s", flush=True)
        # leave nothing running between scenarios (hooks, ffmpeg)
        time.sleep(1.0)

    summary = {"ok": all(r["ok"] for r in results), "scenarios": results}
    with open(os.path.join(out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    md = ["| scenario | result | seconds | failed checks | warnings |", "|---|---|---|---|---|"]
    for r in results:
        md.append(f"| {r['name']} | {'pass' if r['ok'] else ('TIMEOUT' if r['timed_out'] else 'FAIL')} | {r['seconds']} | "
                  f"{'; '.join(r['failed'] + r['error']) or '-'} | {'; '.join(r['warnings']) or '-'} |")
    text = "### D Studio Windows E2E\n\n" + "\n".join(md) + "\n"
    with open(os.path.join(out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass
    print("\n" + text)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
