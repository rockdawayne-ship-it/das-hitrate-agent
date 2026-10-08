@echo off
chcp 65001 >nul
rem DAS 히트율 시뮬레이터 - 더블클릭 실행 (터미널 불필요)
rem 이 파일이 있는 폴더에서 Streamlit을 띄우고 브라우저를 연다. 창을 닫으면 종료된다.
setlocal
cd /d "%~dp0"
set "PY=C:\Users\rockd\.venvs\das-hitrate\Scripts\python.exe"
if not exist "%PY%" (
  echo [DAS] Python venv not found: %PY%
  echo [DAS] README.md의 "실행" 절대로 venv를 먼저 만드세요.
  pause
  exit /b 1
)
if "%DAS_LLM%"=="" set "DAS_LLM=ollama"
echo [DAS] starting on http://localhost:8502  (DAS_LLM=%DAS_LLM%)
start "" "http://localhost:8502"
"%PY%" -m streamlit run app.py --server.port 8502 --server.headless true --browser.gatherUsageStats false
endlocal
