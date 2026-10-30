@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  py -3 codex-channel\scripts\install.py
) else (
  python codex-channel\scripts\install.py
)
pause
