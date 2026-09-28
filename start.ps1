Remove-Item Env:PLAYERS_JSON_URL -ErrorAction SilentlyContinue
Remove-Item Env:JSON_URL -ErrorAction SilentlyContinue
python app.py
