@echo off
title Install Dependencies
chcp 65001 >nul
cd /d "%~dp0"
echo Installing dependencies for ShanghaiTech GenAI Proxy...
python --version >nul 2>&1
if %errorlevel% equ 0 (
    python -m pip install -r requirements.txt
) else (
    py -3 -m pip install -r requirements.txt
)
if errorlevel 1 (
    echo.
    echo Installation failed.
    pause
) else (
    echo.
    echo All dependencies installed successfully!
    pause
)
