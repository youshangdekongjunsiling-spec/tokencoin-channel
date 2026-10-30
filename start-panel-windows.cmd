@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  py -3 -B codex-channel\scripts\dashboard.py
) else (
  python -B codex-channel\scripts\dashboard.py
)
pause
