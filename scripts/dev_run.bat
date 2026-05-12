@echo off
REM Windows development runner — equivalent of dev_run.sh
REM Run from any directory; cd to the repo root first.
setlocal
cd /d "%~dp0\.."
uv run python -m modbus_simulator %*
endlocal
