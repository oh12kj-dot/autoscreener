<#
.SYNOPSIS
    AutoScreenerDailyPipeline の異常終了時の再試行を設定する。

.DESCRIPTION
    既存タスクのトリガー、Action、電源条件、実行時間制限は保持し、
    RestartCount と RestartInterval だけを更新する。設定後はタスクを有効化する。
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateRange(1, 10)]
    [int]$RestartCount = 2,

    [ValidateRange(1, 1440)]
    [int]$RestartIntervalMinutes = 15,

    [string]$TaskName = "AutoScreenerDailyPipeline"
)

$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$settings = $task.Settings
$settings.RestartCount = $RestartCount
$settings.RestartInterval = "PT${RestartIntervalMinutes}M"

if ($PSCmdlet.ShouldProcess($TaskName, "異常終了時の再試行設定を更新して有効化")) {
    Set-ScheduledTask -TaskName $TaskName -Settings $settings | Out-Null
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
}

$configured = Get-ScheduledTask -TaskName $TaskName
[pscustomobject]@{
    TaskName = $TaskName
    State = $configured.State
    Enabled = $configured.Settings.Enabled
    RestartCount = $configured.Settings.RestartCount
    RestartInterval = $configured.Settings.RestartInterval
}
