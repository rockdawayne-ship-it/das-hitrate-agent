param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$dasRoot = $PSScriptRoot
$dasRuntime = Join-Path $env:LOCALAPPDATA 'DAS_HitRate_Agent'
$dasPython = Join-Path $dasRuntime 'venv\Scripts\python.exe'
$dasUrl = 'http://127.0.0.1:8516'
New-Item -ItemType Directory -Path $dasRuntime -Force | Out-Null
if (-not (Test-Path -LiteralPath $dasPython)) {
    python -m venv (Join-Path $dasRuntime 'venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
}
& $dasPython -c 'import streamlit, pandas, duckdb, openpyxl, python_calamine, plotly, pyarrow, requests' 2>$null
if ($LASTEXITCODE -ne 0) {
    & $dasPython -m pip install -r (Join-Path $dasRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
$dasPidPath = Join-Path $dasRuntime 'server.pid'
$dasOllama = Get-Command ollama -ErrorAction SilentlyContinue
if ($dasOllama) {
    try { $null = Invoke-WebRequest 'http://127.0.0.1:11434/api/version' -UseBasicParsing -TimeoutSec 2 }
    catch {
        Start-Process -FilePath $dasOllama.Source -ArgumentList 'serve' -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dasRuntime 'ollama.log') -RedirectStandardError (Join-Path $dasRuntime 'ollama-error.log') | Out-Null
        Start-Sleep -Seconds 2
    }
}
$dasRunning = $false
if (Test-Path -LiteralPath $dasPidPath) {
    $dasSavedPid = [int](Get-Content -LiteralPath $dasPidPath -Raw)
    $dasProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$dasSavedPid" -ErrorAction SilentlyContinue
    if ($dasProcess -and $dasProcess.CommandLine.Contains((Join-Path $dasRoot 'app.py'))) {
        try { $dasRunning = (Invoke-WebRequest "$dasUrl/_stcore/health" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { }
    }
}
if (-not $dasRunning) {
    $dasListener = Get-NetTCPConnection -LocalPort 8516 -State Listen -ErrorAction SilentlyContinue
    if ($dasListener) { throw 'Port 8516 is already in use. Close that service or choose another port.' }
    $env:PYTHONIOENCODING = 'utf-8'
    $env:DAS_LLM = 'ollama'
    $dasApp = Join-Path $dasRoot 'app.py'
    $dasArguments = @('-m', 'streamlit', 'run', ('"' + $dasApp + '"'), '--server.address', '127.0.0.1', '--server.port', '8516', '--server.headless', 'true', '--browser.gatherUsageStats', 'false')
    $dasProcess = Start-Process -FilePath $dasPython -ArgumentList $dasArguments -WorkingDirectory $dasRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dasRuntime 'server.log') -RedirectStandardError (Join-Path $dasRuntime 'server-error.log') -PassThru
    $dasProcess.Id | Set-Content -LiteralPath $dasPidPath -Encoding ascii
    for ($dasAttempt = 0; $dasAttempt -lt 30; $dasAttempt++) {
        Start-Sleep -Milliseconds 500
        try {
            if ((Invoke-WebRequest "$dasUrl/_stcore/health" -UseBasicParsing -TimeoutSec 1).StatusCode -eq 200) { $dasRunning = $true; break }
        } catch { }
    }
    if (-not $dasRunning) { throw "Startup failed. See $dasRuntime\server-error.log" }
}
Write-Output "DAS is running: $dasUrl"
if (-not $NoBrowser) { Start-Process $dasUrl }
