@echo off
cd /d "%~dp0"
set PLAYERS_JSON_URL=
set JSON_URL=
python update_ranking.py
pause
