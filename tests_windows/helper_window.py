"""A plain Tk window in its own process, for the E2E scenarios.

    python helper_window.py --title "E2E pad" --color "#3366CC" --x 100 --y 100 --w 600 --h 400 \
        [--topmost] [--popup] [--text] [--label TEXT] --info-file out.json [--timeout 600]

* ``--popup``  : borderless (overrideredirect) like a toast / chat notification
* ``--topmost``: always on top (the "fake notification" over the target app)
* ``--text``   : lower half is a Text widget to type into; upper half stays a
                 uniform colour (used to look for the drawn cursor)

When the window is on screen it writes ``{"pid", "hwnd", "rect", "client"}``
(physical pixels; the process is per-monitor DPI aware) to ``--info-file``.
Exits by itself after ``--timeout`` seconds so a crashed test never leaves it behind.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _dpi_aware() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def _frame_info(root) -> dict:
    info = {"pid": os.getpid(), "hwnd": 0, "rect": None, "client": None}
    if sys.platform != "win32":
        return info
    import ctypes
    from ctypes import wintypes

    u = ctypes.windll.user32
    u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    u.GetAncestor.restype = wintypes.HWND
    hwnd = u.GetAncestor(wintypes.HWND(root.winfo_id()), 2) or root.winfo_id()   # GA_ROOT
    rect = wintypes.RECT()
    u.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    info.update(hwnd=int(hwnd), rect=[rect.left, rect.top, rect.right, rect.bottom],
                client=[root.winfo_rootx(), root.winfo_rooty(),
                        root.winfo_rootx() + root.winfo_width(), root.winfo_rooty() + root.winfo_height()])
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="D Studio E2E helper")
    ap.add_argument("--color", default="#3366CC")
    ap.add_argument("--x", type=int, default=100)
    ap.add_argument("--y", type=int, default=100)
    ap.add_argument("--w", type=int, default=400)
    ap.add_argument("--h", type=int, default=300)
    ap.add_argument("--topmost", action="store_true")
    ap.add_argument("--popup", action="store_true")
    ap.add_argument("--text", action="store_true")
    ap.add_argument("--label", default="")
    ap.add_argument("--info-file", required=True)
    ap.add_argument("--timeout", type=float, default=600)
    args = ap.parse_args(argv)

    _dpi_aware()
    import tkinter as tk

    root = tk.Tk()
    root.title(args.title)
    root.configure(bg=args.color, highlightthickness=0, bd=0)
    if args.popup:
        root.overrideredirect(True)
    if args.topmost:
        root.attributes("-topmost", True)
    root.geometry(f"{args.w}x{args.h}+{args.x}+{args.y}")
    if args.label:
        tk.Label(root, text=args.label, bg=args.color, fg="#ffffff", font=("Segoe UI", 14, "bold")).place(x=12, y=8)
    if args.text:
        text = tk.Text(root, bg="#ffffff", fg="#000000", font=("Consolas", 14), relief="flat", bd=0)
        text.place(x=0, rely=0.55, relwidth=1.0, relheight=0.45)
        text.focus_set()

    def report() -> None:
        root.update_idletasks()
        info = _frame_info(root)
        tmp = args.info_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(info, fh)
        os.replace(tmp, args.info_file)
        if args.topmost:   # keep it above windows that are activated later
            root.after(500, lambda: root.attributes("-topmost", True))

    root.after(400, report)
    root.after(int(args.timeout * 1000), root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
