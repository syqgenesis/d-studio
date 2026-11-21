@echo off
rem Recorder bar with the FFmpeg gdigrab backend and libx264 (no DXGI / bettercam / numpy needed),
rem in a console window so FFmpeg messages stay visible. Markov CAD export is the default
rem (legacy layout: the checkbox under the gear -> DELIVERY, or --legacy-delivery).
rem FFmpeg lookup order (recorder/paths.py): DSTUDIO_FFMPEG, bundled ffmpeg\, D:\ffmpeg\bin, PATH.
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
rem --- interpreter: bundled python\ (installer / portable zip), else .venv, else PATH
set "HERE=%~dp0"
if exist "%HERE%dstudio.pyw" goto :files_ok
rem started from inside the zip (Explorer extracts only this one file): ask to extract first
chcp 65001 >nul 2>nul
echo.
echo [!] 请先把压缩包全部解压到一个文件夹，再双击其中的 START-GDIGRAB.bat。
echo [!] Extract the whole zip to a folder first, then double-click START-GDIGRAB.bat there.
echo.
pause
exit /b 1
:files_ok
set "PY=python"
set "PYW=pythonw"
set "PYFLAGS="
if exist "%HERE%.venv\Scripts\python.exe" set "PY=%HERE%.venv\Scripts\python.exe"
if exist "%HERE%.venv\Scripts\pythonw.exe" set "PYW=%HERE%.venv\Scripts\pythonw.exe"
if exist "%HERE%python\python.exe" set "PY=%HERE%python\python.exe"
if exist "%HERE%python\pythonw.exe" set "PYW=%HERE%python\pythonw.exe"
rem -E -s: the bundled Python ignores PYTHONPATH / PYTHONHOME / user site-packages
if exist "%HERE%python\python.exe" set "PYFLAGS=-E -s"
if not "%PYW%"=="pythonw" goto :pyw_ok
where pythonw >nul 2>nul
if errorlevel 1 set "PYW="
:pyw_ok
"%PY%" %PYFLAGS% -c "import os, shutil, sys; from recorder.paths import ffmpeg_path; f = ffmpeg_path(); print('FFmpeg:', f); sys.exit(0 if (os.path.isfile(f) or shutil.which(f)) else 1)"
if errorlevel 1 goto :MissingFfmpeg
echo Starting D Studio with the FFmpeg gdigrab backend (libx264, CPU encoding).
echo Windows security settings are unchanged.
"%PY%" %PYFLAGS% "%HERE%dstudio.pyw" --backend gdigrab --codec libx264 %*
set "RECORDER_EXIT=%errorlevel%"
if "%RECORDER_EXIT%"=="0" goto :Done
echo.
echo [ERROR] D Studio exited with code %RECORDER_EXIT%. Keep the messages above.
pause
goto :Done

:MissingFfmpeg
echo [ERROR] FFmpeg was not found. Use the D Studio installer (it bundles FFmpeg),
echo         or install it:  winget install Gyan.FFmpeg   and open a new window.
pause
set "RECORDER_EXIT=1"

:Done
popd
exit /b %RECORDER_EXIT%
