# Registers a Windows Scheduled Task that launches the IWM 0DTE agent
# (via run_agent.bat's restart loop) every time you log in.
#
# Run this ONCE from a normal PowerShell prompt in this deploy\ folder:
#   .\setup_autostart.ps1
#
# This does NOT require an elevated/admin prompt -- it registers the task
# for your own user account only (/RL LIMITED), triggered at your own logon.

$ErrorActionPreference = "Stop"

$taskName = "IWM0DTEAgent"
$batPath = Join-Path $PSScriptRoot "run_agent.bat"

if (-not (Test-Path $batPath)) {
    Write-Error "Could not find $batPath -- run this script from inside the deploy\ folder."
    exit 1
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
Write-Host "'IWM 0DTE Agent'), or:"
Write-Host "  taskkill /FI `"WINDOWTITLE eq IWM 0DTE Agent`" /T /F"
Write-Host "(the /FI filter avoids killing unrelated python.exe processes you may have running)"
