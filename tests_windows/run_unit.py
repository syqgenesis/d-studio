"""Run the unit tests (tests/ + tests_windows/test_*.py) and write a JSON report.

    python tests_windows/run_unit.py [--report artifacts/unit.json]

Used by CI on ubuntu and windows. Off Windows, a test *module* that cannot be
imported only because it needs Windows (ctypes.WinDLL, winreg, comtypes...)
is reported as skipped instead of as an error, so the Linux job checks the
pure-Python parts; on Windows every import error is a failure.
Settings/data are isolated in a temp folder unless DSTUDIO_SETTINGS /
DSTUDIO_DATA_DIR are already set.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

WINDOWS_ONLY_HINTS = ("WinDLL", "windll", "WINFUNCTYPE", "oledll", "HRESULT", "wintypes",
                      "No module named 'winreg'", "No module named '_winapi'", "No module named 'msvcrt'",
                      "No module named 'winsound'", "No module named '_overlapped'", "No module named 'comtypes'",
                      "No module named 'bettercam'", "No module named 'dxcam'", "No module named 'win32",
                      "_type_ 'v' not supported", "getwindowsversion")


def is_windows_only_import_error(text: str) -> bool:
    return any(h in text for h in WINDOWS_ONLY_HINTS)


def _failed_import_cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _failed_import_cases(test)
        elif type(test).__name__ == "_FailedTest":
            yield test


def build_suite(start_dirs, on_windows: bool):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    skipped_modules = []
    for start in start_dirs:
        if not os.path.isdir(start):
            continue
        if start not in sys.path:
            sys.path.insert(0, start)
        discovered = loader.discover(start_dir=start, pattern="test_*.py", top_level_dir=start)
        if not on_windows:
            for case in list(_failed_import_cases(discovered)):
                exc = getattr(case, "_exception", None)
                text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else ""
                if is_windows_only_import_error(text):
                    name = case._testMethodName
                    skipped_modules.append({"module": name, "reason": text.strip().splitlines()[-1]})
                    setattr(case, case._testMethodName,
                            unittest.skip(f"needs Windows: {text.strip().splitlines()[-1]}")(lambda self: None).__get__(case))
        suite.addTests(discovered)
    return suite, skipped_modules


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--report", help="write a JSON summary here")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    tmp = tempfile.mkdtemp(prefix="dstudio-unit-")
    os.environ.setdefault("DSTUDIO_SETTINGS", os.path.join(tmp, "settings.json"))
    os.environ.setdefault("DSTUDIO_DATA_DIR", os.path.join(tmp, "data"))
    on_windows = sys.platform == "win32"
    suite, skipped = build_suite([os.path.join(ROOT, "tests"), HERE], on_windows)
    for s in skipped:
        print(f"[unit] skipped Windows-only module {s['module']}: {s['reason']}", flush=True)
    t0 = time.time()
    result = unittest.TextTestRunner(verbosity=1 if args.quiet else 2, stream=sys.stdout).run(suite)
    summary = {
        "platform": sys.platform, "python": sys.version.split()[0],
        "ran": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped), "windows_only_modules_skipped": skipped,
        "ok": result.wasSuccessful(), "seconds": round(time.time() - t0, 1),
        "problems": [{"test": str(t), "trace": tb[-3000:]} for t, tb in result.failures + result.errors],
    }
    if args.report:
        path = os.path.abspath(args.report)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
    print(f"[unit] ran {summary['ran']}, failures {summary['failures']}, errors {summary['errors']}, "
          f"skipped {summary['skipped']} -> {'OK' if summary['ok'] else 'FAILED'}", flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
