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

echo === Starting NVRR (Windows — %STREAM_MODE% mode) ===
echo.
echo Admin password: %ADMIN_PASSWORD%
echo Database: %NVRR_DB_PATH%
echo Stream mode: %STREAM_MODE%
echo.

REM Reset MediaMTX config to base (clears stale paths from previous runs)
copy /y "%INSTALL_DIR%config\mediamtx.yml" "%MEDIAMTX_CONFIG_PATH%" >nul

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

REM Start FastAPI
echo Starting backend on http://localhost:8000
echo.
echo   Viewer: http://localhost:8000
echo   Admin:  http://localhost:8000/admin.html
echo.
cd /d "%INSTALL_DIR%"
uv run python -m uvicorn main:app --host 0.0.0.0 --port 8000 --app-dir "%INSTALL_DIR%backend"
