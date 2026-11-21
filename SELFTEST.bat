@echo off
chcp 65001 >nul 2>nul
rem D Studio environment self-test (+ unit tests in a source checkout). Bilingual output comes from dstudio.pyw.
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
rem --- interpreter: bundled python\ (installer / portable zip), else .venv, else PATH
set "HERE=%~dp0"
if exist "%HERE%dstudio.pyw" goto :files_ok
rem started from inside the zip (Explorer extracts only this one file): ask to extract first
echo.
echo [!] 请先把压缩包全部解压到一个文件夹，再双击其中的 SELFTEST.bat。
echo [!] Extract the whole zip to a folder first, then double-click SELFTEST.bat there.
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
echo ============================================
echo   D Studio self-test / 自检
echo ============================================
"%PY%" %PYFLAGS% "%HERE%dstudio.pyw" --selftest --no-pause %*
set "RC=%errorlevel%"
if not exist "%HERE%tests\test_pipeline.py" goto :done
echo.
echo [*] unit tests / 单元测试 ...
"%PY%" %PYFLAGS% -m unittest discover -s tests -q
if errorlevel 1 set "RC=1"

:done
echo.
if "%RC%"=="0" echo [ok] all checks passed / 全部通过
if not "%RC%"=="0" echo [fail] see the messages above / 请查看上面的提示
pause
popd
exit /b %RC%
