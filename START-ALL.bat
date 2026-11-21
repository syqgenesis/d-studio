@echo off
rem Annotator (browser) + recorder bar.
setlocal EnableExtensions DisableDelayedExpansion
rem --- interpreter: bundled python\ (installer / portable zip), else .venv, else PATH
set "HERE=%~dp0"
if exist "%HERE%dstudio.pyw" goto :files_ok
rem started from inside the zip (Explorer extracts only this one file): ask to extract first
chcp 65001 >nul 2>nul
echo.
echo [!] 请先把压缩包全部解压到一个文件夹，再双击其中的 START-ALL.bat。
echo [!] Extract the whole zip to a folder first, then double-click START-ALL.bat there.
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
if "%PYW%"=="" goto :console
start "" "%PYW%" %PYFLAGS% "%HERE%dstudio.pyw" --viewer
start "" "%PYW%" %PYFLAGS% "%HERE%dstudio.pyw" %*
exit /b 0

:console
start "D Studio annotator" "%PY%" %PYFLAGS% "%HERE%dstudio.pyw" --viewer
"%PY%" %PYFLAGS% "%HERE%dstudio.pyw" %*
if errorlevel 1 pause
exit /b %errorlevel%
