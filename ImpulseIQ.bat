@echo off
title ImpulseIQ
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on this system.
    echo.
    echo Install it from https://python.org/downloads
    echo IMPORTANT: check "Add python.exe to PATH" during setup.
    echo Then double-click this file again.
    echo.
    pause
    exit /b 1
)

python launch.py
pause
