@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
echo.
echo ============================================
echo   GSVI Adapter for GPT-SoVITS
echo   Starting on http://127.0.0.1:8000
echo   Do NOT close this window.
echo ============================================
echo.
runtime\python.exe gsvi_adapter.py -a 127.0.0.1 -p 8000
pause
