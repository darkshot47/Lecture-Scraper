@echo off
title Lecture to MP4 - local relay
cd /d "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Node.js nahi mila.
  echo   Install karein: https://nodejs.org  ^(LTS version, "Next" dabate jayein^)
  echo   Install ke baad ye file dobara chalayein.
  echo.
  pause
  exit /b 1
)

echo.
echo   Local relay shuru ho raha hai...
echo   Browser khud khul jayega. Band karne ke liye ye window band karein.
echo.

start "" http://localhost:8787
node localrelay.mjs
pause
