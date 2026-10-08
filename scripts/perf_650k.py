"""PRD 성공 기준 3 — 65만 행 성능 측정 (합성 데이터, 도구 경로 end-to-end).

사용: python scripts/perf_650k.py (프로젝트 venv) [--rows 650000] [--regen]

- 합성 파일·DB·내보내기는 Google Drive 밖 %LOCALAPPDATA%\\das_hitrate_perf 에 둔다 (설계안 6장).
- 측정 범위(설계안 9장): 업로드·저장 = 파일 읽기·검증·정규화·DB 저장 (매핑 확인 대기 제외).
  시나리오 계산 = 저장된 데이터에서 집계 결과 준비까지. AI 설명 시간은 포함하지 않는다.
- 결과는 docs/07_PERF_650K.md 에 기록한다.
"""
from __future__ import annotations

import argparse
import os
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from das_agent import ENGINE_VERSION  # noqa: E402
from das_agent.storage import Storage  # noqa: E402
from das_agent.tools import Toolbox  # noqa: E402

PERF_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "das_hitrate_perf"
GRID = [(16, 15), (16, 20), (24, 15), (24, 20), (32, 15), (32, 20)]


def generate(path: Path, rows_target: int, seed: int = 11) -> tuple[int, float]:
    """PRD 3장 열 이름 그대로, 약 rows_target 행의 1년치 합성 출고 파일(.xlsx)을 만든다."""
    rng = np.random.default_rng(seed)
    days = [d for d in np.arange("2025-01-02", "2026-01-01", dtype="datetime64[D]")
            if np.datetime64(d).astype("datetime64[D]").astype(object).weekday() < 5]
    days = days[:245]
    skus = np.array([f"FS3CPG{4000 + i}XBLK{i % 7:03d}" for i in range(1200)])
    sites = rng.integers(10000, 19999, size=900)
    per_day = rows_target / len(days)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("data")
    ws.append(["실확정일자", "검토기준", "배송처차수", "상품코드", "Order Line", "PCS"])
    n = 0
    t0 = time.perf_counter()
    for d in days:
        ymd = int(str(d).replace("-", ""))
        n_orders = int(rng.normal(per_day / 2.5, per_day / 25))
        order_sites = rng.choice(sites, size=n_orders)
        seqs = rng.integers(301, 304, size=n_orders)
        lines = rng.integers(1, 5, size=n_orders)
        keys = np.repeat([f"{s} {q}" for s, q in zip(order_sites, seqs)], lines)
        m = len(keys)
        sku = rng.choice(skus, size=m)
        ol = rng.choice([1, 1, 1, 1, 2], size=m)
        pcs = rng.integers(1, 20, size=m)
        cat = np.where(rng.random(m) < 0.9, "의류용품", "잡화")
        for i in range(m):
            ws.append([ymd, cat[i], keys[i], sku[i], int(ol[i]), int(pcs[i])])
        n += m
    wb.save(path)
    return n, time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=650_000)
    ap.add_argument("--regen", action="store_true")
    args = ap.parse_args()
    PERF_DIR.mkdir(parents=True, exist_ok=True)
    xlsx = PERF_DIR / f"synthetic_{args.rows // 1000}k.xlsx"
    timings: dict[str, float] = {}
    if args.regen or not xlsx.exists():
        n, t = generate(xlsx, args.rows)
        timings["generate_xlsx"] = t
        print(f"generated {n:,} rows in {t:.1f}s -> {xlsx}")
    size_mb = xlsx.stat().st_size / 1e6

    db_dir = PERF_DIR / f"db_{datetime.now():%Y%m%d_%H%M%S}"
    st = Storage(db_dir)
    tb = Toolbox(st)

    t = time.perf_counter()
    insp = tb.inspect_file(str(xlsx))
    timings["inspect_file"] = time.perf_counter() - t
    mapping = insp["mapping"]
    print("mapping:", mapping, "sheet:", insp["sheet"])

    t = time.perf_counter()
    reg = tb.register_dataset(str(xlsx), insp["sheet"], mapping, name="perf_synthetic", keep_copy=False)
    timings["register_dataset"] = time.perf_counter() - t
    assert reg["dataset_id"], reg
    print(f"registered {reg['row_count']:,} rows, {reg['date_min']}~{reg['date_max']} in {timings['register_dataset']:.1f}s")

    ds = reg["dataset_id"]
    per_scenario = []
    run_ids = []
    for cells, cap in GRID:
        t = time.perf_counter()
        out = tb.simulate_scenarios(ds, [{"name": f"{cells}셀·{cap}PCS", "cells": cells, "tote_capacity": cap}])
        dt_ = time.perf_counter() - t
        per_scenario.append(dt_)
        run_ids.append(out["runs"][0]["run_id"])
        print(f"  {cells}셀·{cap}PCS: {dt_:.2f}s  tote={out['runs'][0]['period']['tote']:,}")
    timings["simulate_each_max"] = max(per_scenario)
    timings["simulate_each_mean"] = sum(per_scenario) / len(per_scenario)
    timings["simulate_6_total"] = sum(per_scenario)

    t = time.perf_counter()
    cmp_ = tb.compare_results(run_ids, run_ids[0])
    timings["compare_results"] = time.perf_counter() - t
    t = time.perf_counter()
    exp = tb.export_report(run_ids, run_ids[0], path=str(PERF_DIR / "perf_export.xlsx"))
    timings["export_report"] = time.perf_counter() - t

    # 재사용 경로: 같은 조건 재계산은 DB에서 읽어야 한다
    t = time.perf_counter()
    tb.simulate_scenarios(ds, [{"name": "16셀·15PCS", "cells": 16, "tote_capacity": 15}])
    timings["simulate_reuse"] = time.perf_counter() - t
    st.close()

    ok_register = timings["register_dataset"] <= 90
    ok_scenario = timings["simulate_each_max"] <= 15
    cpu = platform.processor() or platform.machine()
    lines = [
        "# 성능·검증 기록 (합성 데이터)",
        "",
        f"측정일: {datetime.now():%Y-%m-%d %H:%M} · 엔진 v{ENGINE_VERSION} · Python {platform.python_version()} · {platform.system()} {platform.release()} · CPU {cpu}",
        "",
        "입력은 `scripts/perf_650k.py`가 만든 **합성** 파일이다. 실제 출고 실적이 아니며, 실데이터 검수(PRD 성공 기준 2·3의 실파일 대조)는 실파일 확보 후 다시 수행한다.",
        "",
        "## PRD 성공 기준 3 — 65만 행",
        "",
        "| 항목 | 측정값 | 기준 | 판정 |",
        "|---|---:|---:|---|",
        f"| 합성 파일 | {reg['row_count']:,}행, {size_mb:.1f}MB, {reg['date_min']}~{reg['date_max']} | 약 65만 행·18MB | - |",
        f"| 업로드·저장 (`register_dataset`: 읽기·검증·정규화·DuckDB 저장) | {timings['register_dataset']:.1f}초 | 90초 이내 | {'통과' if ok_register else '미달'} |",
        f"| 시나리오 1개 계산 (`simulate_scenarios`, 6개 중 최대) | {timings['simulate_each_max']:.2f}초 | 15초 이내 | {'통과' if ok_scenario else '미달'} |",
        f"| 시나리오 1개 계산 (평균) | {timings['simulate_each_mean']:.2f}초 | - | - |",
        f"| 기본 6개 비교 합계 | {timings['simulate_6_total']:.1f}초 | - | - |",
        f"| 비교표·설명 (`compare_results`) | {timings['compare_results']:.2f}초 | - | - |",
        f"| 엑셀 내보내기 6시나리오 (`export_report`) | {timings['export_report']:.1f}초 | - | - |",
        f"| 같은 조건 재계산 (DB 재사용) | {timings['simulate_reuse']:.2f}초 | 엑셀 재읽기 없음 | {'통과' if timings['simulate_reuse'] < timings['simulate_each_mean'] else '확인 필요'} |",
        f"| 파일 검사 (`inspect_file`: 해시·시트·표본·매핑 후보) | {timings['inspect_file']:.1f}초 | - | - |",
    ]
    if "generate_xlsx" in timings:
        lines.append(f"| (참고) 합성 xlsx 생성 | {timings['generate_xlsx']:.0f}초 | - | - |")
    lines += [
        "",
        "측정 범위(설계안 9장): 업로드·저장은 파일 읽기·검증·정규화·DB 저장을 포함하고 사용자 매핑 확인 대기 시간은 제외한다. "
        "시나리오 계산은 저장된 데이터에서 집계 결과가 준비될 때까지이며 AI 설명 시간은 별도다. 데이터·DB·내보내기 파일은 "
        f"동기화되지 않는 `%LOCALAPPDATA%\\das_hitrate_perf` 에 두었다.",
        "",
        "## PRD 성공 기준 1·2·4·5 — 테스트로 확인",
        "",
        "| 기준 | 확인 방법 | 상태 |",
        "|---|---|---|",
        "| 1. 4장 수작업 정답 2종 | `tests/test_engine.py::test_prd_golden_*` | 통과 |",
        "| 2. 1일치 정답 전 열 대조 | `tests/test_golden_day.py` — 합성 1일치를 엔진 / 손 절차(순수 파이썬) / 엑셀 수식(LibreOffice 재계산) 3중 대조, 주문 단위 2종 | 합성으로 통과. 실파일 대조는 미수행 |",
        "| 4. 6개 비교 3클릭 | 시나리오 비교 탭: '기본 6개 세트' → '계산' (2클릭) | 통과 (브라우저 확인) |",
        "| 5. 엑셀 수식 오류 0, 합계=화면 | `tests/test_reporting.py`, `tests/test_outliers.py` (LibreOffice 재계산) | 통과 |",
        "",
        "재실행: `python scripts/perf_650k.py` (프로젝트 venv. 파일이 있으면 생성 단계 생략, `--regen`으로 다시 생성).",
        "",
    ]
    out_md = ROOT / "docs" / "07_PERF_650K.md"
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[6:20]))
    print("written", out_md)


if __name__ == "__main__":
    main()
