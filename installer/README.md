# Windows installer / 安装包构建

```
powershell -ExecutionPolicy Bypass -File installer\build_windows.ps1
```

Produces in `dist\`:

| file | what |
|---|---|
| `DStudio-Setup-<ver>.exe` | per-user installer (no admin), `%LOCALAPPDATA%\Programs\DStudio`, English + 简体中文 |
| `DStudio-Portable-<ver>.zip` | unzip and run `START-OVERLAY.bat`; recordings in `<folder>\data` |
| `SHA256SUMS.txt`, `build-info.json`, `selftest-stage.json` | checksums, pins/encoders used, self-test of the staged folder |

The version is the first line of `VERSION.txt`.

## What the build does

1. Downloads (cached in `build-cache\`, verified by sha256 from `pins.json`):
   * CPython 3.12 x64 **with tkinter** - python-build-standalone `install_only`
     (relocatable, no registry, no installer);
   * FFmpeg - gyan.dev *release essentials* build (static; libx264, h264_nvenc,
     h264_amf, h264_qsv, gdigrab) from its GitHub mirror.
2. `pip install --require-hashes -r requirements-build.txt` into that Python
   (numpy, bettercam, comtypes, pillow), strips tests/debug symbols,
   pre-compiles `.pyc` (checked-hash, so the install folder never has to be writable).
3. Copies the app (`recorder/ packager/ viewer/ tools/ schemas/ docs/`,
   `dstudio.pyw`, README / 使用说明 / VERSION, `START-*.bat`), never `data/` or `tests/`.
4. Generates `dstudio.ico` with Pillow (`make_icon.py`) unless `installer\dstudio.ico` exists.
5. Runs `dstudio.pyw --selftest` on the staged folder (`-StrictSelftest` makes a failure fatal).
6. Zips the folder **without** `INSTALLED` (portable layout), then writes `INSTALLED`
   and compiles `dstudio.iss` with Inno Setup 6 (found in Program Files / registry,
   else `choco install innosetup`).

## Installer behaviour

* `PrivilegesRequired=lowest`, fixed `AppId` -> new versions upgrade in place
  (old code folders are deleted first so no stale module survives).
* Fixed folder, no *Select Destination* page (`DisableDirPage=yes`). The
  `[InstallDelete]` / `[UninstallDelete]` folder entries only run inside a folder
  that is really D Studio's (`Check: IsOurInstall`: `INSTALLED` marker present, or
  none of those folders exist yet) - a foreign folder's `python\` / `docs\` is never deleted.
* Start menu: **D Studio** (`pythonw.exe -E -s dstudio.pyw`), **D Studio Annotator / 标注台**
  (`--viewer`), **D Studio Self-test / 自检** (`python.exe ... --selftest`),
  **D Studio Recordings / 录制文件夹** (`dstudio.pyw --open-data`: opens the current
  recordings folder, also after it was moved in ⚙ settings); optional desktop icon.
* Recordings: `Documents\DStudio` (the `INSTALLED` marker switches `recorder/paths.py`
  to it) or the folder set in settings (`data_dir`). Uninstall removes the program only -
  recordings and `%APPDATA%\DStudio\settings.json` stay; the closing message names the
  folder from `settings.json` `data_dir` (else `Documents\DStudio`).
* The launcher points `TCL_LIBRARY` / `TK_LIBRARY` at the bundled Tcl/Tk when it runs
  on the bundled Python (a machine-wide value from another tool would break Tk);
  the self-test notes a replaced value.
* Not code-signed: Windows SmartScreen shows "Windows protected your PC" ->
  *More info* -> *Run anyway* (更多信息 -> 仍要运行).

## Updating a pinned download

Change `url`, `file`, `sha256` (and `version`) together in `pins.json`. The sha256
of a GitHub release asset is shown by
`gh api repos/<owner>/<repo>/releases/tags/<tag> --jq '.assets[] | .name + " " + .digest'`.
Python packages: update `requirements-build.txt` with the cp312 win_amd64 wheel hashes
(`https://pypi.org/pypi/<name>/<version>/json`).

## Third-party components

The build downloads separate Windows dependencies using the versions and hashes in
[`pins.json`](pins.json) and [`requirements-build.txt`](../requirements-build.txt).
CPython and the Python wheels retain their own license files. FFmpeg and FFprobe
are separate executables from the [Gyan 9.0.2 Essentials build](https://github.com/GyanD/codexffmpeg/releases/tag/9.0.2).
The build includes their GPLv3 license and upstream build information. Inno Setup
and the [bundled translation](lang/README.txt) have their own license. Review the
applicable third-party terms and corresponding source before redistributing
compiled bundles.

## CI

The Windows workflow runs unit tests, captures bilingual GUI screenshots, and exercises
real-desktop recording. It builds the installer and verifies silent installation, the
installed self-test, a 3-second recording with the installed Python, and uninstall.
The workflow uploads test reports. Checks that depend on the runner
(DXGI duplication, a visible cursor) are hard when `tests_windows/env_report.py`
(`artifacts/env/env.json`) or `GetCursorInfo` shows the runner supports them.
