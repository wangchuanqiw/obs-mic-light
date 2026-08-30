@echo off
chcp 65001 >nul 2>&1
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist "_log.txt" (
    echo _log.txt not found.
    echo Run run.pyw first, then try again.
    pause
    exit /b
)

rem Show last 40 lines (UTF-8 encoding, otherwise Chinese log lines show as garbled text)
powershell -Command "Get-Content '_log.txt' -Tail 40 -Encoding UTF8"
echo.
echo --- Full log below (press any key to close) ---
pause >nul
notepad "_log.txt"
