@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 goto :error
)

call ".venv\Scripts\activate.bat"

if not exist ".venv\.ipplan_ready" (
  echo Installing dependencies...
  python -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto :error
  echo ready> ".venv\.ipplan_ready"
)

echo.
echo IP Plan Manager: http://127.0.0.1:5080
echo Close this window to stop the local web application.
echo.
python app.py
goto :eof

:error
echo.
echo Failed to start IP Plan Manager.
echo Check that Python 3 is installed and pip can install packages from requirements.txt.
pause
exit /b 1
