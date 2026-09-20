@echo off
REM ============================================================
REM  transcript-lens -- start the GUI straight from source.
REM
REM  Double-click this file while developing. It always runs the
REM  CURRENT code in core/ ui/ ai/, so there is nothing to rebuild
REM  and no waiting.
REM
REM  Only reason to use dist\*.exe instead: verifying that the
REM  packaged build still works. See build_exe.ps1.
REM
REM  Why this file is pure ASCII on purpose, in a Chinese project:
REM  cmd.exe reads .cmd files in the system OEM codepage (GBK on
REM  this machine). UTF-8 Chinese inside a .cmd shows up as mojibake
REM  unless you fight with chcp, and it silently breaks again the
REM  moment an editor re-saves the file as UTF-8. English text is
REM  simply correct here. The GUI itself is entirely Chinese.
REM ============================================================

REM Switch the console to UTF-8 so Python tracebacks with Chinese
REM file names / messages are readable instead of garbled.
chcp 65001 > nul

REM Run from the repo root no matter where this file was invoked from.
cd /d "%~dp0"

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

python main.py %*
set CODE=%ERRORLEVEL%

REM Keep the window open on failure -- otherwise a crash just makes a
REM black window flash and disappear, and you never see the traceback.
if not "%CODE%"=="0" (
    echo.
    echo [transcript-lens] python main.py exited with code %CODE%
    echo [transcript-lens] the window stays open so you can read the error above.
    echo.
    pause
)
