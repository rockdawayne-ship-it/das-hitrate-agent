$ErrorActionPreference = 'Stop'
$dasPidPath = Join-Path $env:LOCALAPPDATA 'DAS_HitRate_Agent\server.pid'
if (Test-Path -LiteralPath $dasPidPath) {
    $dasSavedPid = [int](Get-Content -LiteralPath $dasPidPath -Raw)
    $dasProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$dasSavedPid" -ErrorAction SilentlyContinue
    if ($dasProcess -and $dasProcess.CommandLine.Contains((Join-Path $PSScriptRoot 'app.py'))) {
        Stop-Process -Id $dasSavedPid
        Write-Output 'DAS stopped. Saved datasets and reports are preserved.'
    }
}
