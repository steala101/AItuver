@echo off
rem Neuro Voice AI launcher
cd /d "%~dp0"
chcp 65001 >nul

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" run.py %*

if errorlevel 1 pause
