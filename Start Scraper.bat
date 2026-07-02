@echo off
title 4chan Thread Scraper
cd /d "%~dp0"

echo ============================================
echo   4chan Thread Scraper
echo ============================================
echo.
echo   Keep THIS window open while using it on
echo   your phone. Close it to stop the server.
echo.

REM Make sure dependencies are installed (quiet, only does work the first time).
python -m pip install -q -r requirements.txt

REM Open the app on this PC automatically.
start "" http://localhost:5000

REM Start the server (this is what must stay running).
python app.py

echo.
echo Server stopped. Press any key to close.
pause >nul
