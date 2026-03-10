@echo off
REM NVRR Windows Starter — launches MediaMTX + FastAPI backend
REM Run deploy-win.bat first on a fresh system!

setlocal
set INSTALL_DIR=%~dp0
set ADMIN_PASSWORD=admin
set NVRR_DB_PATH=%INSTALL_DIR%data\nvrr.db
set MEDIAMTX_CONFIG_PATH=%INSTALL_DIR%mediamtx\mediamtx.yml
set FFMPEG_PATH=%INSTALL_DIR%ffmpeg\ffmpeg.exe
set PATH=%INSTALL_DIR%ffmpeg;%USERPROFILE%\.local\bin;%PATH%

REM Stream mode: "sdk" uses HCNetSDK (port 8000), "rtsp" uses direct RTSP (port 554)
set STREAM_MODE=sdk
set HCNETSDK_DIR=%INSTALL_DIR%sdk

REM Check if deploy has been run
if not exist "%INSTALL_DIR%.venv" (
    echo ERROR: Dependencies not installed. Run deploy-win.bat first.
    pause
    exit /b 1
)

REM Clear Python cache to avoid stale bytecode
echo Clearing __pycache__...
if exist "%INSTALL_DIR%backend\__pycache__" rd /s /q "%INSTALL_DIR%backend\__pycache__"

REM Read port from config.json
set NVRR_PORT=8000
for /f "usebackq tokens=2 delims=:, " %%a in (`findstr /c:"\"port\"" "%INSTALL_DIR%config.json" 2^>nul`) do set NVRR_PORT=%%a

echo === Starting NVRR (Windows — %STREAM_MODE% mode) ===
echo.
echo Admin password: %ADMIN_PASSWORD%
echo Database: %NVRR_DB_PATH%
echo Stream mode: %STREAM_MODE%
echo Port: %NVRR_PORT%
echo.

REM Reset MediaMTX config to base (clears stale paths from previous runs)
copy /y "%INSTALL_DIR%config\mediamtx.yml" "%MEDIAMTX_CONFIG_PATH%" >nul

REM Kill any leftover MediaMTX from previous runs
taskkill /f /im mediamtx.exe >nul 2>&1

REM Start MediaMTX in background
if exist "%INSTALL_DIR%mediamtx\mediamtx.exe" (
    echo Starting MediaMTX...
    start "MediaMTX" /min "%INSTALL_DIR%mediamtx\mediamtx.exe" "%MEDIAMTX_CONFIG_PATH%"
    timeout /t 2 /nobreak >nul
) else (
    echo WARNING: MediaMTX not found. Streams will not work.
    echo Run deploy-win.bat to download it.
    echo.
)

REM Open browser after a short delay
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:%NVRR_PORT%"

REM Start FastAPI (loops to support restart from admin panel)
echo Starting backend on http://localhost:%NVRR_PORT%
echo.
echo   Viewer: http://localhost:%NVRR_PORT%
echo   Admin:  http://localhost:%NVRR_PORT%/admin.html
echo.
cd /d "%INSTALL_DIR%"
:start_loop
uv run python -m uvicorn main:app --host 0.0.0.0 --port %NVRR_PORT% --app-dir "%INSTALL_DIR%backend"
echo.
echo Server stopped. Restarting in 2 seconds... (Ctrl+C to quit)
timeout /t 2 /nobreak >nul
if exist "%INSTALL_DIR%backend\__pycache__" rd /s /q "%INSTALL_DIR%backend\__pycache__"
goto start_loop
