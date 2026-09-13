@echo off
rem Remote Launcher in CONSOLE mode (no tray) - for troubleshooting / seeing errors.
cd /d "%~dp0"
chcp 65001 >nul
title Neuro Remote Launcher (console)

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" remote_launcher.py --console %*

if errorlevel 1 (
  echo.
  echo [!] Launcher exited. Check the message above and logs\remote_launcher.log
  echo     - .env: REMOTE_LAUNCHER_ENABLED=true and REMOTE_LAUNCHER_ADMIN_TOKEN
  echo     - Tailscale running?  run: tailscale ip -4
  echo     - Tray needs: pip install pystray pillow
  pause
)
