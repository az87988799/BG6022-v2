@echo off
setlocal
pushd "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment is missing.
  echo Run this manually from the repository root: uv sync --extra p7 --dev
  pause
  popd
  exit /b 2
)
".venv\Scripts\python.exe" "scripts\start_chat.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
