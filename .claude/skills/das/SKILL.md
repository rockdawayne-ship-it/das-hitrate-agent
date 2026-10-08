---
name: das
description: DAS 히트율 시뮬레이터를 Claude Code 안에서 바로 쓴다. "DAS 기본 6개 비교해줘", "이 출고 파일 등록해줘", "24셀 20PCS면 기준보다 나아?", "히트율 KPI 보여줘", "엑셀로 내보내줘" 같은 요청, 또는 /das 로 호출. Streamlit 화면 없이 das.cmd(das_agent.cli)를 실행해 계산 엔진 결과(토트·OL/TOTE·PCS/TOTE·증감·엑셀 경로)를 그대로 보고한다.
---

# DAS 히트율 시뮬레이터 (CLI 경로)

숫자는 전부 `das_agent/engine.py`가 만든다. 이 스킬은 요청을 CLI 명령으로 바꾸고 결과를 그대로 전달한다. 결과 숫자를 바꾸거나 보충하지 않는다.

## 실행 환경

프로젝트 루트 = 이 파일의 세 단계 위 폴더(`app.py`, `das.cmd`가 있는 곳). Claude Code 안에서는 **Bash 도구**로 파이썬을 직접 호출한다
(PowerShell 도구는 `%LOCALAPPDATA%`·사용자 프로필이 샌드박스 경로로 가상화되어 앱의 실제 DB가 비어 보인다).

```bash
cd "<프로젝트 루트>" && PY=$(for c in .venv/Scripts/python.exe "$LOCALAPPDATA/DAS_HitRate_Agent/venv/Scripts/python.exe" "$USERPROFILE/.venvs/das-hitrate/Scripts/python.exe" python; do "$c" -c "import duckdb, streamlit, pandas" >/dev/null 2>&1 && { echo "$c"; break; }; done) && PYTHONIOENCODING=utf-8 "$PY" -m das_agent.cli <명령>
```

사용자가 직접 쓸 때(터미널·탐색기)는 `das.cmd <명령>` 하나면 된다. 같은 탐색 순서로 파이썬을 찾는다.

**잠금 주의**: DuckDB는 한 프로세스만 연다. 결과가 `저장소를 열 수 없습니다`면 Streamlit 앱(`run_app.bat`, `Start-DAS.cmd`, 프리뷰 서버)이 떠 있는 것이다. 사용자에게 앱을 닫을지 묻거나, 이 세션이 띄운 프리뷰 서버라면 `preview_stop` 후 재시도한다.

## 요청 → 명령

| 사용자 요청 | 명령 |
|---|---|
| 데이터셋 뭐 있어 | `datasets` |
| 이 파일 등록해줘 (경로) | `register "<경로>" [--name 이름]` → 매핑 결과를 보여주고, 주문 열이 `배송처차수`가 맞는지 한 번 확인 |
| KPI / 현재 히트율 | `kpi [--dataset X] [--cells N --tote N --unit 배송처차수|배송처 --hours H] [--avg-excl] [--daily]` |
| 기본 6개 비교 | `compare [--export]` |
| 특정 조합 비교 (예: 24셀 20PCS, 32셀 20PCS) | `compare --grid "16:15,24:20,32:20" [--export]` |
| 그 밖의 자연어 (절반·두 배·배송처로 묶어서·1월만 등) | `ask "<요청문 그대로>" --llm claude` (사용자가 로컬만 원하면 `--llm ollama`) |
| 엑셀로 내보내줘 | 직전 요청에 `--export` 또는 `ask "... 엑셀로 내보내줘"` |

기준 설정(셀·토트·주문 단위·작업시간)을 사용자가 말하면 `--cells/--tote/--unit/--hours`로 넘긴다. 말하지 않으면 기본 16셀·15PCS·배송처차수·10h를 쓰고 답변에 그 기준을 적는다.

## 답변 형식

1. 기준 설정과 데이터셋·기간·출고일 수 한 줄.
2. CLI 출력의 비교표 또는 KPI 수치 그대로 (반올림 변경 금지).
3. 엑셀을 만들었으면 경로 한 줄.
4. 특이일이 있으면 "합계·기간 비율에 포함, AVG 제외 옵션은 `--avg-excl`" 한 줄.
5. "설비 비용·인원은 포함되지 않는다"는 엔진 문구가 있으면 그대로 둔다.

`--llm claude`는 요청문·설정·기간·검토기준 값만 Claude 구독으로 보낸다(출고 원본 행 미전송). 사용자가 외부 전송을 원치 않으면 `--llm ollama` 또는 `none`.

## 예시

```bash
PYTHONIOENCODING=utf-8 "$PY" -m das_agent.cli compare --export
```

```bash
PYTHONIOENCODING=utf-8 "$PY" -m das_agent.cli ask "셀을 절반으로 줄이고 토트 25개면 기준 대비 어때?" --llm claude
```
