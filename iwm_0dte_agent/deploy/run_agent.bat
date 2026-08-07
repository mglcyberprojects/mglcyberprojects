@echo off
setlocal
title IWM 0DTE Agent

rem Restart-loop wrapper for the IWM 0DTE agent on Windows. Restarts the
rem agent automatically if it exits (crash, unhandled error), so this is
rem meant to be launched once at logon by the Scheduled Task set up in
rem deploy\setup_autostart.ps1, not run by hand every day.
rem
rem Runs in PAPER mode (no --live). Telegram alerts/confirmation should be
rem configured in ..\.env -- with no console window in front of you, a
rem terminal-only confirmation prompt would just hang forever.

cd /d "%~dp0.."

if not exist logs mkdir logs

set PYEXE=%~dp0..\.venv\Scripts\python.exe
if not exist "%PYEXE%" set PYEXE=python

:loop
echo [%date% %time%] Starting agent (%PYEXE%) >> logs\agent.log
"%PYEXE%" -m iwm_0dte_agent >> logs\agent.log 2>&1
echo [%date% %time%] Agent exited with code %ERRORLEVEL%, restarting in 30s >> logs\agent.log
rem ping-based sleep instead of `timeout` -- `timeout` needs a real console
rem input handle and fails if this ever runs under a hidden/no-console task.
ping -n 31 127.0.0.1 >nul
goto loop
