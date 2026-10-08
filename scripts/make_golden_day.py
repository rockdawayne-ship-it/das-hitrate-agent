"""sample_data/ 에 1일치 합성 정답 데이터와 엑셀 수작업 정답 통합문서를 만든다.

사용: C:/Users/rockd/.venvs/das-hitrate/Scripts/python.exe scripts/make_golden_day.py
생성물은 모두 합성 데이터이며 실제 출고 실적이 아니다.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from das_agent.ingestion import make_golden_day_file, read_raw  # noqa: E402
from test_golden_day import CELLS, CAP, HOURS, SOFFICE, build_manual_workbook, recalc_with_libreoffice, read_summary  # noqa: E402

out = ROOT / "sample_data"
out.mkdir(exist_ok=True)
data = make_golden_day_file(out / "golden_day_2025-01-15_synthetic.xlsx")
rows = read_raw(data, "data").to_dict("records")
print("data:", data, len(rows), "rows")
for unit in ("배송처차수", "배송처"):
    book = build_manual_workbook(rows, CELLS, CAP, unit, HOURS, out / f"golden_day_manual_{unit}.xlsx")
    if SOFFICE:
        recalced = recalc_with_libreoffice(book)
        recalced.replace(book)
        (out / "recalc").rmdir()
        print(unit, read_summary(book))
    else:
        print(unit, "수식만 저장 (LibreOffice 없음, 엑셀에서 열면 계산됨)")
