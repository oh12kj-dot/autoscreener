<#
.SYNOPSIS
    Windows タスクスケジューラ(タスク名 TENX-DailyPipeline)から呼ばれる実行ラッパー。

.DESCRIPTION
    タスクスケジューラの Action は標準ではstdout/stderrをファイルに落とせないため、
    このPowerShellスクリプトでラップしてリダイレクトする(scripts/run_daily_pipeline.bat の
    既存パターンと同じ理由。あちらは cmd.exe 版、これはタスクスケジューラ登録を
    scripts/register_scheduled_task.ps1 で完結させるためのPowerShell版)。

    register_scheduled_task.ps1 はこのファイルを直接指す Action を登録する
    (このファイル自体をユーザーが手動編集する必要は無い)。
#>

$ErrorActionPreference = "Continue"

# タスクスケジューラは作業ディレクトリを保証しないため、このスクリプトの場所から
# リポジトリルートを自力で決定する(.env と config/ を相対パスで読むため必須)。
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$LogDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}
$LogFile = Join-Path $LogDir "daily_pipeline_$(Get-Date -Format yyyyMMdd).log"

"==== $(Get-Date -Format o) start ====" | Out-File -FilePath $LogFile -Append -Encoding utf8

# uv の実体を解決する。タスクスケジューラのセッションは対話ログオンシェルと
# PATH が異なることがあるため、PATH 探索が失敗した場合のフォールバックを持つ
# (scripts/run_daily_pipeline.bat が uv.exe の絶対パスを直書きしているのと
# 同じ問題への対処。ただしこちらはPATH探索を優先し、ハードコードは最後の手段)。
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

# Postgres起動待ち(Docker Desktopがスケジューラ起動直後はまだ起動中の可能性があるため)。
docker compose up -d --wait *>> $LogFile

& $uvPath run python -m autoscreener.cli run-daily-pipeline *>> $LogFile
$exitCode = $LASTEXITCODE

"==== $(Get-Date -Format o) end (exit $exitCode) ====" | Out-File -FilePath $LogFile -Append -Encoding utf8
exit $exitCode
