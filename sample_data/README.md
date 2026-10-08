# sample_data — 합성 데이터만

이 폴더의 파일은 전부 코드로 만든 **합성 데이터**다. 실제 출고 실적이 아니다.

| 파일 | 생성 | 용도 |
|---|---|---|
| `golden_day_2025-01-15_synthetic.xlsx` | `scripts/make_golden_day.py` → `ingestion.make_golden_day_file` | PRD 성공 기준 2(1일치 정답 대조)의 합성 대체. 문자순 정렬·분할 행·용량 경계 사례 포함 |
| `golden_day_manual_배송처차수.xlsx` / `…_배송처.xlsx` | 같은 스크립트 | 엑셀 수작업(MATCH 웨이브 + SUMIFS 피벗 + ROUNDUP)을 수식으로 재현한 정답. `summary` 시트 |

검증: `tests/test_golden_day.py` — 엔진 / 손 절차(순수 파이썬) / 엑셀 수식(LibreOffice 재계산) 3중 대조, 주문 단위 2종.
