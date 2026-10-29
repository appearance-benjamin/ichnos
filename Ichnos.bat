@echo off
rem Opens Ichnos in a window of its own; it stops when you close that window.
rem For a Start-menu and desktop shortcut with the icon, run once:  python -m ichnos shortcuts
title Ichnos
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11 or newer from python.org, then double-click this file again.
  pause
  exit /b 1
)
where pythonw >nul 2>nul
if errorlevel 1 (
  rem no pythonw: run with this console window open instead
  python -m ichnos desktop
  if errorlevel 1 pause
  exit /b
)
start "" pythonw -m ichnos desktop
