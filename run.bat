@echo off
SETLOCAL ENABLEDELAYEDEXPANSION
cd /d "%~dp0"

REM ============================================================
REM  Check Python
REM ============================================================
where python >nul 2>nul
IF ERRORLEVEL 1 (
    echo [ERROR] Python not found on PATH. Install Python 3.10+ first.
    exit /b 1
)

REM ============================================================
REM  Setup: .env
REM ============================================================
IF EXIST ".env" GOTO HAS_ENV
echo.
echo [SETUP] .env not found - creating it now.
set "PORT=8002"
:ASK_IP
set "IP="
set /p "IP=Enter Sonos IP address: "
if "!IP!"=="" goto ASK_IP
set /p "PORTPROMPT=Enter stream port [default 8002]: "
if not "!PORTPROMPT!"=="" set "PORT=!PORTPROMPT!"
(
    echo SONOS_IP=!IP!
    echo STREAM_PORT=!PORT!
) > ".env"
echo [SETUP] Created .env with SONOS_IP=!IP! STREAM_PORT=!PORT!
:HAS_ENV

REM ============================================================
REM  Setup: ffmpeg
REM ============================================================
set "FFMPEG_FOUND="
for /d %%D in ("ffmpeg\ffmpeg-*-essentials_build") do (
    if exist "%%D\bin\ffmpeg.exe" set "FFMPEG_FOUND=1"
)
IF NOT DEFINED FFMPEG_FOUND (
    echo.
    echo [SETUP] Downloading ffmpeg - latest release...
    curl.exe -L --fail -o ffmpeg.zip "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    IF ERRORLEVEL 1 (
        echo [ERROR] ffmpeg download failed.
        del ffmpeg.zip 2>nul
        exit /b 1
    )
    powershell -NoProfile -Command "Expand-Archive -Path 'ffmpeg.zip' -DestinationPath 'ffmpeg' -Force"
    del ffmpeg.zip
    echo [SETUP] ffmpeg installed.
) else (
    echo [SETUP] ffmpeg already present.
)

REM ============================================================
REM  Setup: mpv-bin
REM ============================================================
IF NOT EXIST "mpv-bin\libmpv-2.dll" (
    echo.
    echo [SETUP] Downloading latest mpv build...
    mkdir mpv-bin 2>nul
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $rss = [xml](Invoke-WebRequest -Uri 'https://sourceforge.net/projects/mpv-player-windows/rss?path=/libmpv' -UseBasicParsing).Content; $item = $rss.rss.channel.item | Where-Object { $_.title.InnerText -match '^/libmpv/mpv-dev-x86_64-\d.*\.7z$' } | Select-Object -First 1; if (-not $item) { Write-Error 'No mpv-dev x86_64 build found in RSS'; exit 1 }; $item.link | Set-Content -LiteralPath 'mpv_url.txt' -NoNewline"
    IF ERRORLEVEL 1 (
        echo [ERROR] Could not determine latest mpv build URL.
        del mpv_url.txt 2>nul
        exit /b 1
    )
    set /p MPV_URL=<mpv_url.txt
    del mpv_url.txt
    echo [SETUP] Downloading !MPV_URL!
    curl.exe -L --fail -o mpv.7z "!MPV_URL!"
    IF ERRORLEVEL 1 (
        echo [ERROR] mpv download failed.
        del mpv.7z 2>nul
        exit /b 1
    )
    tar.exe -xf mpv.7z -C mpv-bin
    IF ERRORLEVEL 1 (
        echo [ERROR] mpv extraction failed.
        del mpv.7z 2>nul
        exit /b 1
    )
    del mpv.7z
    echo [SETUP] mpv installed.
) else (
    echo [SETUP] mpv already present.
)

REM ============================================================
REM  venv + deps
REM ============================================================
IF NOT EXIST ".venv\Scripts\activate.bat" (
    echo.
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing requirements...
pip install -r requirements.txt
IF ERRORLEVEL 1 (
    echo [ERROR] Dependency install failed. Not starting the app.
    exit /b 1
)

echo.
echo Running main.py...
python main.py

ENDLOCAL
pause
