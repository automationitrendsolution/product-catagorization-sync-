# Registers (or replaces) the two Windows Task Scheduler tasks -- the Windows
# twin of install_cron.sh.
#
#   1. 'Product Categorisation Sync'            daily at 13:00 IST: full sync + validation report
#   2. 'Product Categorisation Sync (on change)' every 30 minutes: syncs only when one of the 4
#      source tables (or the target) changed since the last verified sync; otherwise exits in ~1 s
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_task_windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_task_windows.ps1 -Time 14:30 -CheckEveryMinutes 15
#   powershell -ExecutionPolicy Bypass -File scripts\install_task_windows.ps1 -CheckEveryMinutes 0   # daily task only
#   powershell -ExecutionPolicy Bypass -File scripts\install_task_windows.ps1 -Remove
#
# -Time is India Standard Time, converted to this PC's clock (no change on an
# IST PC). Tasks run as the current user while they are signed in to Windows,
# and only these two tasks are replaced. Both share the application's run lock,
# so they never write at the same time.

param(
    [string]$Time = '13:00',
    [int]$CheckEveryMinutes = 30,
    [string]$TaskName = 'Product Categorisation Sync',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$ChangeTaskName = "$TaskName (on change)"
if ($Remove) {
    foreach ($n in @($TaskName, $ChangeTaskName)) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Output "Removed task '$n'"
        }
    }
    exit 0
}
if ($Time -notmatch '^([01]?\d|2[0-3]):[0-5]\d$') { throw "-Time must be HH:mm (IST), got '$Time'" }
if ($CheckEveryMinutes -ne 0 -and ($CheckEveryMinutes -lt 5 -or $CheckEveryMinutes -gt 720)) {
    throw "-CheckEveryMinutes must be 0 (off) or 5-720"
}

$wrapper = (Resolve-Path (Join-Path $PSScriptRoot 'run_product_categorisation_sync.ps1')).Path
$projectDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

function New-Action([string]$RunArgs) {
    New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $projectDir `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`" $RunArgs"
}

# --- 1. daily full sync -------------------------------------------------------
$ist = [TimeZoneInfo]::FindSystemTimeZoneById('India Standard Time')
$todayIst = [TimeZoneInfo]::ConvertTime((Get-Date), $ist).Date
$istStart = [DateTime]::SpecifyKind($todayIst.Add([TimeSpan]::Parse($Time)), 'Unspecified')
$localStart = [TimeZoneInfo]::ConvertTime($istStart, $ist, [TimeZoneInfo]::Local)
# StartWhenAvailable: if the PC was off/asleep at 13:00, the run happens as soon as it is back.
$dailySettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName $TaskName -Action (New-Action '--sync') `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $localStart) -Settings $dailySettings -Principal $principal `
    -Description 'product_categorisation + _gr + _non_us + _uk -> powerbidb.product_categorisation_us_ca_uk_it, daily 13:00 IST full sync + validation report' `
    -Force | Out-Null
Write-Output "Registered task '$TaskName'"
Write-Output "  runs       : $wrapper --sync"
Write-Output "  schedule   : daily at $Time IST = $($localStart.ToString('HH:mm')) on this PC's clock ($([TimeZoneInfo]::Local.Id))"
Write-Output "  next run   : $((Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime)"

# --- 2. change check ----------------------------------------------------------
if ($CheckEveryMinutes -eq 0) {
    if (Get-ScheduledTask -TaskName $ChangeTaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $ChangeTaskName -Confirm:$false
        Write-Output "Removed task '$ChangeTaskName' (-CheckEveryMinutes 0)"
    }
    exit 0
}
$now = Get-Date
$first = $now.Date.AddMinutes([Math]::Ceiling(($now - $now.Date).TotalMinutes / $CheckEveryMinutes) * $CheckEveryMinutes)
if ($first -le $now.AddMinutes(1)) { $first = $first.AddMinutes($CheckEveryMinutes) }
$trigger = New-ScheduledTaskTrigger -Once -At $first -RepetitionInterval (New-TimeSpan -Minutes $CheckEveryMinutes)
$changeSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes ([Math]::Min(30, $CheckEveryMinutes)))
Register-ScheduledTask -TaskName $ChangeTaskName -Action (New-Action '--sync --if-changed') `
    -Trigger $trigger -Settings $changeSettings -Principal $principal `
    -Description "Every $CheckEveryMinutes min: sync powerbidb.product_categorisation_us_ca_uk_it only when a source table changed" `
    -Force | Out-Null
Write-Output "Registered task '$ChangeTaskName'"
Write-Output "  runs       : $wrapper --sync --if-changed"
Write-Output "  schedule   : every $CheckEveryMinutes minutes; writes only when a source or the target changed"
Write-Output "  next run   : $((Get-ScheduledTaskInfo -TaskName $ChangeTaskName).NextRunTime)"
