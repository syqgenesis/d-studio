"""Sanity-check the bundled interpreter while building (run with the staged python.exe).

    python\\python.exe -E -s installer\\verify_bundle.py [--stage]

Exit 0 when the interpreter has everything D Studio needs; bettercam may fail
to *import* on a build machine without a DXGI output, which is only reported.
Console/log output only (build tool, not user-facing).
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    problems = []
    print(f"    python {sys.version.split()[0]} ({sys.maxsize > 2**32 and '64' or '32'}-bit) at {sys.prefix}")
    if sys.version_info < (3, 10) or sys.maxsize <= 2**32:
        problems.append("need 64-bit Python 3.10+")
    if not sys.flags.no_user_site:
        print("    note: user site-packages enabled (run the bundle with -E -s)")
    try:
        import tkinter

        print(f"    Tcl/Tk {tkinter.Tcl().eval('info patchlevel')}")
    except Exception as exc:
        problems.append(f"tkinter/Tcl unusable: {exc!r}")
    for mod in ("ssl", "sqlite3", "ctypes", "json", "http.server", "zipfile", "lzma", "bz2"):
        try:
            importlib.import_module(mod)
        except Exception as exc:
            problems.append(f"stdlib module {mod}: {exc!r}")
    if "--packages" in args:
        for mod in ("numpy", "comtypes", "PIL"):
            try:
                m = importlib.import_module(mod)
                print(f"    {mod} {getattr(m, '__version__', '?')}")
            except Exception as exc:
                problems.append(f"{mod}: {exc!r}")
        if importlib.util.find_spec("bettercam") is None:
            problems.append("bettercam is not installed")
        else:
            try:
                importlib.import_module("bettercam")
                print("    bettercam import ok")
            except Exception as exc:
                print(f"    bettercam installed; importing it on this machine raised {exc!r} (DXGI may be unavailable here)")
        site = os.path.join(sys.prefix, "Lib", "site-packages")
        foreign = [p for p in sys.path if p and os.path.isdir(p)
                   and not os.path.normcase(os.path.abspath(p)).startswith(os.path.normcase(sys.prefix))
                   and os.path.normcase(os.path.abspath(p)) != os.path.normcase(os.getcwd())]
        if foreign:
            print(f"    note: sys.path entries outside the bundle: {foreign}")
        if not os.path.isdir(site):
            problems.append(f"missing {site}")
    for p in problems:
        print(f"    PROBLEM: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
