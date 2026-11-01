@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" launch.py
) else if exist "..\video-manual-lab\.venv\Scripts\python.exe" (
  "..\video-manual-lab\.venv\Scripts\python.exe" launch.py
) else (
  echo Please run setup.ps1 first. See README.md.
  pause
)
