"""Install DStudio-Setup-<ver>.exe silently and check the installed app.

    python tests_windows/installed_check.py --dist dist --out artifacts/installed [--keep-installed]

1. /VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER install (per-user, no admin)
2. files: INSTALLED marker, python\\pythonw.exe, ffmpeg\\ffmpeg.exe, dstudio.ico; Start-menu shortcuts
3. installed self-test with python.exe (exit code must be 0) + JSON report; its
   DXGI check must pass when artifacts/env (env_report.py) shows that DXGI
   duplication works on this machine
   3b. a real 3 s desktop recording with the INSTALLED interpreter
   (python\\python.exe -E -s -m recorder, cwd = program folder, clean env: the
   shipped hash-pinned packages, not the CI's), packaged and validated with
   --decode by the same interpreter: exit codes, technical_ok, real captures > 0,
   libx264, and DXGI (dxcam) used where DXGI works here
4. the pythonw.exe path writes its report to the recordings folder (Documents\\DStudio)
5. --version through the launcher equals the installer version
6. reinstall over the existing install (upgrade in place) and self-test again (--quick)
7. uninstall: program folder gone, a planted recording in Documents\\DStudio kept
Exit 1 when any hard check fails. Writes <out>/report.json.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from e2e_lib import (CREATE_NO_WINDOW, Procs, Report, env_dxgi_works, kill_tree, load_env_report,  # noqa: E402
                     load_json, load_segments, spawn_helper, summarize_segments, win)

FLAGS = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/CURRENTUSER", "/NORESTART", "/SP-"]


def documents_dir() -> str:
    import ctypes
    from ctypes import wintypes

    buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
        return buf.value
    return os.path.join(os.path.expanduser("~"), "Documents")


def clean_env() -> dict:
    env = dict(os.environ)
    for k in ("DSTUDIO_DATA_DIR", "DSTUDIO_FFMPEG", "DSTUDIO_SETTINGS", "PYTHONPATH", "PYTHONHOME"):
        env.pop(k, None)   # exercise the real installed defaults
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run(cmd, timeout=600, **kw) -> subprocess.CompletedProcess:
    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
    out = (proc.stdout or b"").decode("utf-8", "replace") + (proc.stderr or b"").decode("utf-8", "replace")
    if out.strip():
        print("    " + out.strip()[-3000:].replace("\n", "\n    "), flush=True)
    proc.text = out  # type: ignore[attr-defined]
    return proc


def install(setup: str, log: str) -> int:
    return run([setup, *FLAGS, f"/LOG={log}"], timeout=900).returncode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dist", default="dist")
    ap.add_argument("--out", default=os.path.join("artifacts", "installed"))
    ap.add_argument("--keep-installed", action="store_true", help="skip the uninstall step")
    ap.add_argument("--allow-existing", action="store_true",
                    help="outside CI: test even though D Studio is already installed (it gets reinstalled/uninstalled)")
    args = ap.parse_args(argv)
    rep = Report("installed", args.out)
    if sys.platform != "win32":
        rep.check("runs on Windows", True, "skipped", soft=True)
        rep.write()
        return 0
    try:
        body(rep, args)
    except Exception:
        import traceback

        rep.error = traceback.format_exc()
        print(rep.error, file=sys.stderr)
    rep.write()
    return 0 if rep.ok else 1


def record_with_bundle(rep: Report, app: str, py: str, env: dict, dxgi_works: bool) -> None:
    """3 s desktop recording -> packager.build -> packager.validate --decode, all with the
    installed python\\python.exe -E -s from the program folder (what the shortcuts run).
    The dataset goes to a temp folder and is deleted (it shows the runner desktop)."""
    tmp = tempfile.mkdtemp(prefix="dstudio-installed-rec-")
    out = os.path.join(tmp, "rec-installed")
    procs = Procs()
    try:
        w32 = win()
        w32.prepare()
        pw, ph = w32.primary_size()
        pad = spawn_helper(procs, rep.out_dir, "pad", color="#1565C0", x=pw // 4, y=ph // 4, w=pw // 2, h=ph // 2,
                           title="D Studio installed-check pad", label="installed bundle")
        w32.force_foreground(pad.hwnd, click_at=pad.center())
        x0, y0, x1, y1 = pad.rect
        cmd = [py, "-E", "-s", "-m", "recorder", "start", "--out", out, "--source", "desktop", "--fps", "30",
               "--max-seconds", "3"]
        print("$ " + " ".join(cmd), flush=True)
        log = os.path.join(rep.out_dir, "installed-recorder.log")
        with open(log, "w", encoding="utf-8") as fh:
            p = procs.add(subprocess.Popen(cmd, cwd=app, env=env, stdin=subprocess.DEVNULL, stdout=fh,
                                           stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW))
        deadline, i = time.time() + 120, 0
        while p.poll() is None and time.time() < deadline:   # keep the cursor moving over the pad
            i += 1
            try:
                w32.move_abs(x0 + 40 + (i * 23) % max(1, x1 - x0 - 80), y0 + 60 + (i * 13) % max(1, y1 - y0 - 100))
            except OSError:
                pass
            time.sleep(0.05)
        rc = p.poll()
        if rc is None:
            kill_tree(p.pid)
        with open(log, encoding="utf-8", errors="replace") as fh:
            print("    " + fh.read().strip()[-2000:].replace("\n", "\n    "), flush=True)
        if not rep.check("installed interpreter: 3 s recording exit code 0", rc == 0, f"exit={rc}"):
            return
        pk = run([py, "-E", "-s", "-m", "packager.build", "--raw", os.path.join(out, "raw"), "--out", out,
                  "--id", "rec-installed"], env=env, cwd=app, timeout=600, creationflags=CREATE_NO_WINDOW)
        if not rep.check("installed interpreter: packager exit code 0", pk.returncode == 0, pk.returncode):
            return
        va = run([py, "-E", "-s", "-m", "packager.validate", "--dataset", out, "--decode"], env=env, cwd=app,
                 timeout=600, creationflags=CREATE_NO_WINDOW)
        # 0 = ready for delivery, 2 = technically fine but delivery items missing (no narration...)
        rep.check("installed interpreter: validate --decode exit code 0/2", va.returncode in (0, 2), va.returncode)
        vpath = os.path.join(out, "validation.json")
        val = load_json(vpath) if os.path.isfile(vpath) else {}
        rep.check("installed recording: validation technical_ok", bool(val.get("technical_ok")),
                  val.get("technical_errors"))
        cpath = os.path.join(out, "raw", "capture.json")
        cap = load_json(cpath) if os.path.isfile(cpath) else {}
        stats = summarize_segments(load_segments(out))
        rep.note("installed_recording", {"backend": cap.get("backend"), "fallbacks": cap.get("backend_fallbacks"),
                                         "codec": cap.get("codec"), "capture_rect": cap.get("capture_rect"),
                                         "cursor_in_video": cap.get("cursor_in_video"),
                                         **{k: stats[k] for k in ("segments", "frames", "captures", "real_ratio",
                                                                  "cursor_frames", "backends")}})
        rep.check("installed recording: real captures > 0", bool(stats["captures"]), stats["captures"])
        rep.check("installed recording: encoded with libx264", cap.get("codec") == "libx264", cap.get("codec"))
        rep.check("installed recording: DXGI (dxcam) used where DXGI duplication works",
                  cap.get("backend") == "dxcam" and not cap.get("backend_fallbacks"),
                  {"backend": cap.get("backend"), "fallbacks": cap.get("backend_fallbacks")}, soft=not dxgi_works)
    finally:
        procs.cleanup()
        try:
            win().release_all_input()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


def body(rep: Report, args) -> None:
    setups = sorted(glob.glob(os.path.join(os.path.abspath(args.dist), "DStudio-Setup-*.exe")))
    if not rep.check("installer exists", bool(setups), args.dist):
        return
    setup = setups[-1]
    version = re.search(r"DStudio-Setup-(.+)\.exe$", os.path.basename(setup)).group(1)
    rep.note("setup", {"file": setup, "bytes": os.path.getsize(setup), "version": version})
    app = os.path.join(os.environ["LOCALAPPDATA"], "Programs", "DStudio")
    docs = os.path.join(documents_dir(), "DStudio")
    env = clean_env()
    env_report = load_env_report()
    dxgi_works = env_dxgi_works(env_report)
    rep.note("env_dxgi_duplication_works", dxgi_works if env_report else "no artifacts/env/env.json")
    on_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    if os.path.exists(os.path.join(app, "unins000.exe")) and not on_ci and not args.allow_existing:
        rep.check("no existing D Studio install on this PC", False,
                  "this check reinstalls and then uninstalls D Studio; pass --allow-existing to do that anyway")
        return

    # 1. install
    t0 = time.time()
    code = install(setup, os.path.join(rep.out_dir, "install.log"))
    rep.note("install_seconds", round(time.time() - t0, 1))
    if not rep.check("silent install exit code 0", code == 0, code):
        return

    # 2. files + shortcuts
    for rel in ("INSTALLED", "dstudio.pyw", "dstudio.ico", "VERSION.txt", r"python\python.exe", r"python\pythonw.exe",
                r"ffmpeg\ffmpeg.exe", r"ffmpeg\ffprobe.exe", r"recorder\gui.py", r"viewer\index.html", "unins000.exe"):
        rep.check(f"installed {rel}", os.path.isfile(os.path.join(app, rel)))
    rep.check("no recordings folder inside the program folder", not os.path.isdir(os.path.join(app, "data")), soft=True)
    menu = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs", "D Studio")
    links = sorted(os.listdir(menu)) if os.path.isdir(menu) else []
    rep.note("start_menu", links)
    rep.check("Start-menu shortcut 'D Studio'", "D Studio.lnk" in links)
    rep.check("Start-menu annotator + self-test shortcuts", len([n for n in links if n.endswith(".lnk")]) >= 4, links)
    py = os.path.join(app, "python", "python.exe")
    pyw = os.path.join(app, "python", "pythonw.exe")
    launcher = os.path.join(app, "dstudio.pyw")

    # 3. self-test (console, exactly like the Start-menu shortcut)
    report_json = os.path.join(rep.out_dir, "selftest-installed.json")
    st = run([py, "-E", "-s", launcher, "--selftest", "--no-pause", "--json", report_json], env=env, cwd=app, timeout=900)
    with open(os.path.join(rep.out_dir, "selftest-installed.txt"), "w", encoding="utf-8") as fh:
        fh.write(st.text)  # type: ignore[attr-defined]
    rep.check("installed self-test exit code 0", st.returncode == 0, st.returncode)
    if os.path.isfile(report_json):
        with open(report_json, encoding="utf-8") as fh:
            data = json.load(fh)
        rep.note("selftest", {c["id"]: [c["status"], c["detail"][:160]] for c in data.get("checks", [])})
        rep.check("self-test layout is 'installed'", data.get("layout") == "installed", data.get("layout"))
        rep.check("self-test runs on the bundled Python", bool(data.get("bundled_python")))
        ff = next((c for c in data.get("checks", []) if c["id"] == "ffmpeg"), {})
        rep.check("self-test uses the bundled FFmpeg", os.path.normcase(app) in os.path.normcase(ff.get("detail", "")),
                  ff.get("detail"))
        dd = next((c for c in data.get("checks", []) if c["id"] == "data_dir"), {})
        rep.check("recordings go to Documents\\DStudio", os.path.normcase(docs) in os.path.normcase(dd.get("detail", "")),
                  dd.get("detail"))
        dx = next((c for c in data.get("checks", []) if c["id"] == "dxgi"), {})
        rep.check("installed self-test: DXGI capture works with the bundled packages", dx.get("status") == "ok",
                  dx.get("detail"), soft=not dxgi_works)

    # 3b. record + package + validate with the installed interpreter and packages
    record_with_bundle(rep, app, py, env, dxgi_works)

    # 4. pythonw (no console): report must land in the recordings folder
    log_path = os.path.join(docs, "dstudio-selftest.log")
    if os.path.exists(log_path):
        os.remove(log_path)
    pw = subprocess.run([pyw, "-E", "-s", launcher, "--selftest", "--no-dialog", "--quick"], env=env, cwd=app,
                        timeout=600)
    rep.check("pythonw self-test exit code 0", pw.returncode == 0, pw.returncode)
    rep.check("pythonw self-test wrote dstudio-selftest.log to the recordings folder", os.path.isfile(log_path), log_path)
    if os.path.isfile(log_path):
        shutil.copy(log_path, os.path.join(rep.out_dir, "dstudio-selftest-pythonw.log"))

    # 5. version
    v = run([py, "-E", "-s", launcher, "--version"], env=env, cwd=app, timeout=120)
    rep.check("launcher --version matches the installer", v.text.strip().splitlines()[-1:] == [version],  # type: ignore[attr-defined]
              v.text.strip())  # type: ignore[attr-defined]

    # 6. upgrade in place (same AppId): reinstall and run a quick self-test
    planted = os.path.join(docs, "rec-installer-check")
    os.makedirs(planted, exist_ok=True)
    with open(os.path.join(planted, "metadata.json"), "w", encoding="utf-8") as fh:
        json.dump({"note": "planted by installed_check.py"}, fh)
    code = install(setup, os.path.join(rep.out_dir, "reinstall.log"))
    rep.check("reinstall over the existing install exit code 0", code == 0, code)
    st2 = run([py, "-E", "-s", launcher, "--selftest", "--no-pause", "--quick"], env=env, cwd=app, timeout=600)
    rep.check("self-test after reinstall exit code 0", st2.returncode == 0, st2.returncode)
    uninstallers = glob.glob(os.path.join(app, "unins*.exe"))
    rep.check("one uninstaller after reinstall (same AppId)", len(uninstallers) == 1, uninstallers)

    if args.keep_installed:
        return
    # 7. uninstall; the uninstaller re-launches itself from %TEMP%, so poll for the result
    unins = os.path.join(app, "unins000.exe")
    run([unins, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={os.path.join(rep.out_dir, 'uninstall.log')}"],
        timeout=600)
    deadline = time.time() + 120
    while time.time() < deadline and os.path.exists(unins):
        time.sleep(1)
    time.sleep(2)
    rep.check("uninstaller removed the program", not os.path.exists(launcher) and not os.path.exists(py))
    leftovers = []
    if os.path.isdir(app):
        for d, _, fs in os.walk(app):
            leftovers += [os.path.relpath(os.path.join(d, f), app) for f in fs]
    rep.check("program folder empty after uninstall", not leftovers, leftovers[:20], soft=True)
    rep.check("recordings kept after uninstall", os.path.isfile(os.path.join(planted, "metadata.json")))
    rep.check("Start-menu folder removed", not os.path.isdir(menu) or not os.listdir(menu), soft=True)
    shutil.rmtree(planted, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
