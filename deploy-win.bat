@echo off
REM NVRR Windows Dev/Test Deploy Script
REM Run from the NVRR project directory — handles a completely fresh system

setlocal enabledelayedexpansion

set INSTALL_DIR=%~dp0
set DATA_DIR=%INSTALL_DIR%data
set MEDIAMTX_VERSION=1.9.3
set MEDIAMTX_DIR=%INSTALL_DIR%mediamtx
set FFMPEG_DIR=%INSTALL_DIR%ffmpeg
set PATH=%USERPROFILE%\.local\bin;%PATH%

echo === NVRR Windows Deploy ===
echo.

REM 1. Install uv if missing
echo [1/4] Checking uv...
uv --version >nul 2>&1
if errorlevel 1 (
    echo uv not found, installing...
    powershell -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    REM Verify it worked
    uv --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: uv installation failed. Install manually from https://docs.astral.sh/uv/
        exit /b 1
    )
    echo uv installed successfully.
) else (
    echo uv found.
)

REM 2. Sync Python + dependencies (uv handles Python download if needed)
echo [2/4] Syncing Python and dependencies...
cd /d "%INSTALL_DIR%"
REM Remove stale .venv (e.g. copied from another machine)
if exist "%INSTALL_DIR%.venv" (
    uv run python --version >nul 2>&1
    if errorlevel 1 (
        echo Removing invalid .venv, will recreate...
        rmdir /s /q "%INSTALL_DIR%.venv"
    )
)
uv sync
if errorlevel 1 (
    echo ERROR: uv sync failed.
    exit /b 1
)

REM 3. Download MediaMTX
if not exist "%MEDIAMTX_DIR%" mkdir "%MEDIAMTX_DIR%"
if not exist "%MEDIAMTX_DIR%\mediamtx.exe" (
    echo [3/4] Downloading MediaMTX %MEDIAMTX_VERSION%...
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/bluenviron/mediamtx/releases/download/v%MEDIAMTX_VERSION%/mediamtx_v%MEDIAMTX_VERSION%_windows_amd64.zip' -OutFile '%MEDIAMTX_DIR%\mediamtx.zip'"
    if errorlevel 1 (
        echo ERROR: MediaMTX download failed.
        exit /b 1
    )
    powershell -Command "Expand-Archive -Path '%MEDIAMTX_DIR%\mediamtx.zip' -DestinationPath '%MEDIAMTX_DIR%' -Force"
    del "%MEDIAMTX_DIR%\mediamtx.zip"
    echo MediaMTX downloaded.
) else (
    echo [3/4] MediaMTX already downloaded.
)

REM Copy base config
copy /y "%INSTALL_DIR%config\mediamtx.yml" "%MEDIAMTX_DIR%\mediamtx.yml" >nul

REM 4. Download FFmpeg (static build)
if not exist "%FFMPEG_DIR%" mkdir "%FFMPEG_DIR%"
if not exist "%FFMPEG_DIR%\ffmpeg.exe" (
    echo [4/5] Downloading FFmpeg...
    powershell -Command "Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile '%FFMPEG_DIR%\ffmpeg.zip'"
    if errorlevel 1 (
        echo ERROR: FFmpeg download failed.
        exit /b 1
    )
    REM Extract and move ffmpeg.exe from nested folder to ffmpeg/
    powershell -Command "Expand-Archive -Path '%FFMPEG_DIR%\ffmpeg.zip' -DestinationPath '%FFMPEG_DIR%' -Force"
    powershell -Command "$bin = Get-ChildItem -Path '%FFMPEG_DIR%' -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1; if ($bin) { Copy-Item $bin.FullName '%FFMPEG_DIR%\ffmpeg.exe' -Force }"
    del "%FFMPEG_DIR%\ffmpeg.zip"
    if not exist "%FFMPEG_DIR%\ffmpeg.exe" (
        echo ERROR: ffmpeg.exe not found after extraction.
        exit /b 1
    )
    echo FFmpeg downloaded.
) else (
    echo [4/5] FFmpeg already downloaded.
)

REM 5. Create data dir
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%"

echo [5/5] Done!
echo.
echo === Setup complete. Start NVRR with: start-win.bat ===
echo.
