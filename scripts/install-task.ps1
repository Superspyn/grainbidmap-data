# Register the Windows scheduled task that refreshes cash bids from this PC.
#
# Run once:
#     powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1
#
# Remove it again with:
#     Unregister-ScheduledTask -TaskName 'Update cash bids' -Confirm:$false
#
# No administrator rights needed - the task is registered for the current user
# and runs only while that user is logged on. Git pushes through the same
# credential manager entry you already use, which is per-user and interactive.

$ErrorActionPreference = 'Stop'

$taskName = 'Update cash bids'
$repo     = Split-Path -Parent $PSScriptRoot
$script   = Join-Path $repo 'scripts\update-bids.ps1'

if (-not (Test-Path $script)) { throw "cannot find $script" }

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $repo

# Weekdays, every 30 minutes from 8:15am to 5:45pm local time.
#
# Offset from the cloud run's :00/:30 so the two rarely collide, which also
# means bids refresh about every 15 minutes while this PC is on. Local time
# follows daylight saving on its own - the GitHub cron cannot, which is why
# its window has to be an hour wider than the trading day at both ends.
$trigger = New-ScheduledTaskTrigger `
    -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At '8:15AM'
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At '8:15AM' `
    -RepetitionInterval (New-TimeSpan -Minutes 30) `
    -RepetitionDuration (New-TimeSpan -Hours 9 -Minutes 30)).Repetition

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName    $taskName `
    -Description 'Scrapes corn and soybean cash bids and pushes docs/bids.json. Covers NEW Co-op and Landus, which block GitHub Actions runner IPs.' `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $settings `
    -Force | Out-Null

$t = Get-ScheduledTask -TaskName $taskName
"registered: $($t.TaskName)  [$($t.State)]"
"next run:   $((Get-ScheduledTaskInfo -TaskName $taskName).NextRunTime)"
