<#
.SYNOPSIS
    Windows タスクスケジューラ(タスク名 TENX-Doctor)から呼ばれる実行ラッパー。

.DESCRIPTION
    `doctor` 診断コマンドを実行し、結果をログファイルへ落とす。日次パイプラインの
    1時間後に走らせる想定(register_scheduled_task.ps1 が既定で登録する時刻)——
    パイプラインが完走(または途中停止)した後の状態を診断するため、パイプライン
    自体の実行と時間帯が重ならないようにする。

    `doctor` は所見にerrorが1件でもあれば終了コード1を返す(cli.py側の実装)。
    タスクスケジューラの「最終実行結果」列にその終了コードがそのまま出るため、
    ログを開かなくても「タスク一覧を見れば異常に気づける」状態になる。
#>

$ErrorActionPreference = "Continue"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$LogDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}
$LogFile = Join-Path $LogDir "doctor_$(Get-Date -Format yyyyMMdd).log"

$uvCmd = Get-Command uv.exe -ErrorAction SilentlyContinue
if (-not $uvCmd) { $uvCmd = Get-Command uv -ErrorAction SilentlyContinue }
$uvPath = $null
if ($uvCmd) { $uvPath = $uvCmd.Source }
if (-not $uvPath) {
    $fallback = Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\Scripts\uv.exe"
    if (Test-Path $fallback) { $uvPath = $fallback }
}
if (-not $uvPath) {
    "ERROR: uv.exe が見つかりません(PATHにもフォールバック先 $fallback にも無い)。" |
        Out-File -FilePath $LogFile -Append -Encoding utf8
    exit 1
}

"==== $(Get-Date -Format o) start ====" | Out-File -FilePath $LogFile -Append -Encoding utf8

& $uvPath run python -m autoscreener.cli doctor *>> $LogFile
$exitCode = $LASTEXITCODE

"==== $(Get-Date -Format o) end (exit $exitCode) ====" | Out-File -FilePath $LogFile -Append -Encoding utf8
exit $exitCode
