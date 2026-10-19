@echo off
rem Double-click launcher: checks and downloads what is needed, then starts the service.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
echo.
echo Сервис остановлен. Нажмите любую клавишу, чтобы закрыть окно.
pause >nul
