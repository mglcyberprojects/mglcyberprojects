@echo off
setlocal
title IWM 0DTE Agent LIVE

rem Restart-loop wrapper for the IWM 0DTE agent on Windows -- LIVE mode
rem (real orders, real money). Restarts the agent automatically if it
rem exits (crash, unhandled error), so this is meant to be launched once
rem at logon by the Scheduled Task set up via:
rem   deploy\setup_autostart.ps1 -Live
rem not run by hand every day.
rem
rem Requires TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID configured in ..\.env --
rem with no console window in front of you at logon, the one-time "Type
rem LIVE to confirm" startup gate is answered by replying LIVE to a
rem Telegram message instead (see notifier.confirm_live_start in the code).
rem Every restart of this loop re-asks that confirmation -- an unanswered
rem restart just keeps the agent from placing any order, it never falls
rem through to trading unconfirmed.
rem
rem IMPORTANT: never run this at the same time as run_agent.bat (paper
rem mode) against the same TELEGRAM_BOT_TOKEN -- two processes polling the
rem same bot's getUpdates will steal each other's updates (including your
rem LIVE confirmation reply and trade approvals), and each tracks its own
rem in-memory daily trade count/risk state, so both could independently
rem decide it's fine to open a position on the same real account at once.
rem Only ever have one of run_agent.bat / run_agent_live.bat running.

cd /d "%~dp0.."

if not exist logs mkdir logs

set PYEXE=%~dp0..\.venv\Scripts\python.exe
if not exist "%PYEXE%" set PYEXE=python

:loop
echo [%date% %time%] Starting agent LIVE (%PYEXE%) >> logs\agent_live.log
"%PYEXE%" -m iwm_0dte_agent --live >> logs\agent_live.log 2>&1
echo [%date% %time%] Agent exited with code %ERRORLEVEL%, restarting in 30s >> logs\agent_live.log
rem ping-based sleep instead of `timeout` -- `timeout` needs a real console
rem input handle and fails if this ever runs under a hidden/no-console task.
ping -n 31 127.0.0.1 >nul
goto loop
