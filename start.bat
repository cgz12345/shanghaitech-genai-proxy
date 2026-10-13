@echo off
cd /d "%~dp0"

set "PY_CMD="

python --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=python"
    goto :RUN
)

py -3 --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=py -3"
    goto :RUN
)

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :RUN
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto :RUN
)
if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" (
    set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    goto :RUN
)

echo ================================================================
echo [ERROR] Python environment was not found!
echo Please install Python 3.10+ and make sure Add to PATH is checked.
echo ================================================================
echo.
pause
exit /b 1

:RUN
%PY_CMD% server.py %*

echo.
echo ================================================================
echo Service has stopped. Press any key to close this window.
echo ================================================================
pause
