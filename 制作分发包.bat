@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
echo.
echo ============================================
echo   GSVI Adapter - Build Distribution Package
echo ============================================
echo.
if exist "runtime\python.exe" (
    runtime\python.exe 制作分发包.py
) else (
    python 制作分发包.py
)
echo.
pause
