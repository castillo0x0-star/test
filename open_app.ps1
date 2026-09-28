$ErrorActionPreference = 'Stop'
Set-Location "C:\Users\casti\test"

Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
}

Remove-Item Env:PLAYERS_JSON_URL -ErrorAction SilentlyContinue
Remove-Item Env:JSON_URL -ErrorAction SilentlyContinue
$pythonPath = (& python -c "import sys; print(sys.executable)").Trim()
$server = Start-Process -FilePath $pythonPath -ArgumentList "app.py" -WorkingDirectory $PSScriptRoot -PassThru

try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            Invoke-WebRequest -Uri "http://localhost:8000/" -UseBasicParsing -TimeoutSec 1 | Out-Null
            $ready = $true
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }

    if (-not $ready) {
        throw "El servidor no respondió en http://localhost:8000"
    }

    Start-Process "http://localhost:8000"
    Write-Host "Servidor activo en http://localhost:8000 (PID $($server.Id))"
} catch {
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    throw
}
