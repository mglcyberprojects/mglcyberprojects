# Registers a Windows Scheduled Task that launches the IWM 0DTE agent
# (via run_agent.bat's restart loop) every time you log in.
#
# Run this ONCE from a normal PowerShell prompt in this deploy\ folder:
#   .\setup_autostart.ps1          # paper mode (default, safe)
#   .\setup_autostart.ps1 -Live    # LIVE mode -- real orders, real money
#
# This does NOT require an elevated/admin prompt -- it registers the task
# for your own user account only (/RL LIMITED), triggered at your own logon.

param(
    [switch]$Live
)

$ErrorActionPreference = "Stop"

if ($Live) {
    $taskName = "IWM0DTEAgentLive"
    $batPath = Join-Path $PSScriptRoot "run_agent_live.bat"
    $windowTitle = "IWM 0DTE Agent LIVE"
    $otherTaskName = "IWM0DTEAgent"
} else {
    $taskName = "IWM0DTEAgent"
    $batPath = Join-Path $PSScriptRoot "run_agent.bat"
    $windowTitle = "IWM 0DTE Agent"
    $otherTaskName = "IWM0DTEAgentLive"
}

if (-not (Test-Path $batPath)) {
    Write-Error "Could not find $batPath -- run this script from inside the deploy\ folder."
    exit 1
}

if ($Live) {
    Write-Host ""
    Write-Host "*** Registering LIVE mode: real orders on Robinhood, real money. ***" -ForegroundColor Red
    Write-Host ""
}

schtasks /Create /TN $taskName /TR "`"$batPath`"" /SC ONLOGON /RL LIMITED /F

Write-Host ""
Write-Host "Scheduled task '$taskName' created."
Write-Host "It will start automatically next time you log in to Windows."
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Start it right now:  schtasks /Run /TN $taskName"
Write-Host "  Check its status:    schtasks /Query /TN $taskName /V /FO LIST"
Write-Host "  Remove it entirely:  schtasks /Delete /TN $taskName /F"
Write-Host ""
Write-Host "To actually stop the running agent, close its console window (titled"
Write-Host "'$windowTitle'), or:"
Write-Host "  taskkill /FI `"WINDOWTITLE eq $windowTitle`" /T /F"
Write-Host "(the /FI filter avoids killing unrelated python.exe processes you may have running)"

Write-Host ""
Write-Host "IMPORTANT: never have both IWM0DTEAgent (paper) and IWM0DTEAgentLive enabled" -ForegroundColor Yellow
Write-Host "at the same time -- they'd both poll the same Telegram bot and could both act" -ForegroundColor Yellow
Write-Host "on the same real account. If '$otherTaskName' exists, disable it first:" -ForegroundColor Yellow
Write-Host "  schtasks /Change /TN $otherTaskName /DISABLE" -ForegroundColor Yellow
