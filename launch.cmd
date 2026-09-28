@echo off
cd /d C:\Users\casti\test
for /f "tokens=1,* delims= " %%A in ('tasklist /FO CSV /NH /FI "IMAGENAME eq python.exe"') do (
    if not "%%~A"=="INFO:" taskkill /F /IM python.exe >nul 2>&1
)
for /f "tokens=1,* delims= " %%A in ('tasklist /FO CSV /NH /FI "IMAGENAME eq py.exe"') do (
    if not "%%~A"=="INFO:" taskkill /F /IM py.exe >nul 2>&1
)
set PLAYERS_JSON_URL=
set JSON_URL=
python app.py
