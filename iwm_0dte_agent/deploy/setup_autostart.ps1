# Registers a Windows Scheduled Task that launches the IWM 0DTE agent
# (via run_agent.bat's restart loop), triggered BOTH at your next Windows
# logon AND automatically every weekday morning at a fixed clock time --
# so it starts whether or not you happen to log in around market open.
# Leaving the machine on and logged in (a locked screen is fine, signing
# out is not) is enough; you don't need to be there at trigger time.
#
# Run this ONCE from a normal PowerShell prompt in this deploy\ folder:
#   .\setup_autostart.ps1                       # paper mode, default 9:10 AM trigger
#   .\setup_autostart.ps1 -Live                 # LIVE mode -- real orders, real money
#   .\setup_autostart.ps1 -DailyTime "08:45AM"  # customize the daily trigger time
#
# This does NOT require an elevated/admin prompt -- it registers the task
# for your own user account only (limited run level), triggered at your
# own logon and on the configured weekday schedule.

param(
    [switch]$Live,
    [string]$DailyTime = "09:10AM"
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

$dailyTriggerTime = [DateTime]::Parse($DailyTime)

if ($Live) {
    Write-Host ""
    Write-Host "*** Registering LIVE mode: real orders on Robinhood, real money. ***" -ForegroundColor Red
    Write-Host ""
}

$action = New-ScheduledTaskAction -Execute $batPath
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$dailyTrigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $dailyTriggerTime
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger @($logonTrigger, $dailyTrigger) -Settings $settings -RunLevel Limited -Force | Out-Null

Write-Host ""
Write-Host "Scheduled task '$taskName' created."
Write-Host "It starts automatically both at your next Windows logon AND every"
Write-Host "weekday at $($dailyTriggerTime.ToString('h:mm tt')) -- whichever comes first. You don't need"
Write-Host "to be at the machine or log in right at that time, just leave it powered"
Write-Host "on and logged in (locked screen is fine)."
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

if ($Live) {
    Write-Host ""
    Write-Host "Reminder: --live still requires you to reply LIVE to a Telegram prompt" -ForegroundColor Yellow
    Write-Host "before any trading starts (see notifier.confirm_live_start). If you're not" -ForegroundColor Yellow
    Write-Host "around to answer it right at the trigger time, the restart loop just keeps" -ForegroundColor Yellow
    Write-Host "re-asking every few minutes until you do (or until market close) -- no strict" -ForegroundColor Yellow
    Write-Host "deadline, just reply whenever you next check your phone." -ForegroundColor Yellow
}
