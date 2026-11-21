@echo off
chcp 65001 >nul 2>nul
rem Developer install for a source folder: creates .venv and installs requirements.txt.
rem End users should run DStudio-Setup-VERSION.exe instead (bundles Python and FFmpeg).
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
echo ============================================
echo   D Studio - developer install / 开发环境安装
echo   (users: run DStudio-Setup.exe instead / 普通用户请使用安装包)
echo ============================================

where python >nul 2>nul
if errorlevel 1 goto :NoPython
python -c "import sys; assert sys.version_info >= (3, 10) and sys.maxsize > 2**32; print('[*] Python', sys.version.split()[0])"
if errorlevel 1 goto :NoPython

if exist ".venv\Scripts\python.exe" goto :HaveVenv
echo [*] Creating .venv / 创建虚拟环境 .venv ...
python -m venv .venv
if errorlevel 1 goto :VenvFailed
:HaveVenv

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --upgrade pip
echo [*] Installing requirements.txt (numpy, bettercam, comtypes, pillow) / 安装依赖 ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo [!] Some packages failed to install. Recording still works with the GDI / gdigrab backends.
  echo [!] 部分依赖安装失败；仍可使用 GDI / gdigrab 方式录制。
)

".venv\Scripts\python.exe" -c "import os, shutil, sys; from recorder.paths import ffmpeg_path; f = ffmpeg_path(); ok = os.path.isfile(f) or shutil.which(f); print('[*] FFmpeg:', f if ok else 'NOT FOUND'); sys.exit(0 if ok else 1)"
if errorlevel 1 (
  echo [!] FFmpeg not found. Install it:  winget install Gyan.FFmpeg   then open a new window.
  echo [!] 未找到 FFmpeg。请运行  winget install Gyan.FFmpeg  后重新打开窗口。
)

echo.
".venv\Scripts\python.exe" dstudio.pyw --selftest --no-pause
echo.
echo [*] Unit tests / 单元测试 ...
".venv\Scripts\python.exe" -m unittest discover -s tests -q
if errorlevel 1 (
  echo [!] Unit tests failed - see above. / 单元测试失败，请查看上面的输出。
) else (
  echo [ok] Unit tests passed. / 单元测试通过。
)

echo.
echo Next / 下一步:
echo   START-ALL.bat       annotator + recorder bar / 标注台 + 录制悬浮条
echo   START-OVERLAY.bat   recorder bar only / 只启动录制悬浮条
echo   START-VIEWER.bat    annotator only / 只启动标注台
pause
popd
exit /b 0

:NoPython
echo [x] 64-bit Python 3.10+ not found in PATH. / 未找到 64 位 Python 3.10+。
echo     Install it from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH".
echo     安装时请勾选 "Add python.exe to PATH"。或直接使用 DStudio-Setup.exe 安装包（自带 Python）。
pause
popd
exit /b 1

:VenvFailed
echo [x] Could not create the virtual environment. / 无法创建虚拟环境 .venv。
pause
popd
exit /b 1
