@echo off
title Get ShanghaiTech GenAI Token
chcp 65001 >nul
cd /d "%~dp0"
python --version >nul 2>&1
if %errorlevel% equ 0 (
    python get_token.py %*
) else (
    py -3 get_token.py %*
)
echo.
pause
