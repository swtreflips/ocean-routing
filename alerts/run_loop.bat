@echo off
REM Unattended launcher for the alert engine (point Task Scheduler at this file).
REM Runs --loop until MAX_RUNTIME_HOURS (config.py), then exits; the daily task relaunches.
REM Requires an interactive desktop (visible Chrome) -> set the task to
REM "Run only when user is logged on", and set the power plan to never sleep on AC.

setlocal
set PYTHONUTF8=1

REM repo root = the folder above this script (alerts\)
cd /d "%~dp0.."

set "PY=C:\Users\Mike\OneDrive - Prime Time Packaging\Schedules\schedulesenv\Scripts\python.exe"

echo ==== launch %date% %time% ==== >> "alerts\data\run.log"
"%PY%" -m alerts.run --loop >> "alerts\data\run.log" 2>&1
echo ==== exit %date% %time% ==== >> "alerts\data\run.log"

endlocal
