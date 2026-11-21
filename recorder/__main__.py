"""``python -m recorder start --out ...`` (CLI) or ``python -m recorder gui`` (bar).

``gui`` is dispatched here, before ``recorder.recorder`` is imported, so the
bar (and its CI ``--screenshot`` hook) starts even where the capture stack
cannot be imported; the recorder is loaded when a recording starts.
"""
import sys

from . import i18n


def _main() -> int:
    # first: bilingual output must never raise on a cp1252 pipe (installed app runs python -E)
    i18n.safe_stdio()
    argv = sys.argv[1:]
    if argv and argv[0] == "gui":
        from .gui import main as gui_main

        return gui_main(argv[1:])
    from .recorder import main

    return main(argv)


raise SystemExit(_main())
