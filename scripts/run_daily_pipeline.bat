@echo off
REM Wrapper invoked by Windows Task Scheduler for the daily pipeline.
REM PYTHONUTF8=1 is required or typer/rich output gets mangled in the log
REM file (codepage mismatch with the system's default codepage).

set PYTHONUTF8=1
cd /d "C:\AI\App_Dev\AutoScreener"

set LOGFILE=C:\AI\App_Dev\AutoScreener\logs\daily_pipeline_%date:~0,4%%date:~5,2%%date:~8,2%.log

echo ==== %date% %time% start ==== >> "%LOGFILE%"

REM Ensure Postgres is up (Docker Desktop may still be starting when the
REM scheduler fires) before running the pipeline itself.
docker compose up -d --wait >> "%LOGFILE%" 2>&1

"C:\Users\oh12k\AppData\Local\Programs\Python\Python310\Scripts\uv.exe" run python -m autoscreener.cli run-daily-pipeline >> "%LOGFILE%" 2>&1
echo ==== %date% %time% end (exit code %errorlevel%) ==== >> "%LOGFILE%"
