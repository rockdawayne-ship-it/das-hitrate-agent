@echo off
rem DAS CLI wrapper: finds a working Python env (duckdb+streamlit+pandas) and forwards all args to das_agent.cli
rem usage: das.cmd datasets | register <file> | kpi | compare [--export] | ask "<text>" [--llm claude]
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PY="
call :try "%~dp0.venv\Scripts\python.exe"
call :try "%LOCALAPPDATA%\DAS_HitRate_Agent\venv\Scripts\python.exe"
call :try "%USERPROFILE%\.venvs\das-hitrate\Scripts\python.exe"
call :try python
if "%PY%"=="" (
  echo [DAS] No Python environment with duckdb/streamlit/pandas found. See README.md "run" section.
  exit /b 1
)
"%PY%" -m das_agent.cli %*
exit /b %ERRORLEVEL%

:try
if not "%PY%"=="" goto :eof
"%~1" -c "import duckdb, streamlit, pandas" >nul 2>nul && set "PY=%~1"
goto :eof
