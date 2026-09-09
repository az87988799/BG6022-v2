@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment is missing.
  echo Run this manually from the repository root: uv sync --extra p7 --dev
  pause
  popd
  exit /b 2
)
".venv\Scripts\python.exe" -X utf8 -u "scripts\start_chat.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
