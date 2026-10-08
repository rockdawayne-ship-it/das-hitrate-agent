"""화면 없이 쓰는 명령줄 인터페이스. Claude Code 스킬(`/das`)과 배치 실행이 이 모듈을 호출한다.

사용 (venv 파이썬으로):
  python -m das_agent.cli datasets
  python -m das_agent.cli register "C:\\path\\data.xlsx" [--sheet data] [--name 이름]
  python -m das_agent.cli kpi [--dataset ID|이름] [--cells 16 --tote 15 --unit 배송처차수 --hours 10]
  python -m das_agent.cli ask "기본 6개 비교하고 엑셀로 내보내줘" [--llm none|ollama|claude] [--json]
  python -m das_agent.cli compare [--grid "16:15,24:20,32:20"] [--export]

주의: DuckDB는 한 번에 한 프로세스만 쓴다. Streamlit 앱이 떠 있으면 같은 DB를 열 수 없으므로 앱을 닫거나
`--data-dir`로 다른 폴더를 지정한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

from . import ENGINE_VERSION
from .agent import Agent, AgentContext
from .engine import Scenario, Filters, ORDER_UNITS
from .llm import get_provider
from .storage import Storage
from .tools import Toolbox, ToolError

DEFAULT_GRID = [(16, 15), (16, 20), (24, 15), (24, 20), (32, 15), (32, 20)]


def _open(args) -> tuple[Storage, Toolbox]:
    try:
        st = Storage(args.data_dir) if args.data_dir else Storage()
    except Exception as e:  # duckdb.IOException 등
        msg = str(e).splitlines()[0]
        sys.exit(f"[DAS] 저장소를 열 수 없습니다: {msg}\n"
                 f"      Streamlit 앱이 실행 중이면 닫고 다시 시도하거나 --data-dir 로 다른 폴더를 지정하세요.")
    return st, Toolbox(st)


def _pick_dataset(st: Storage, key: Optional[str]) -> dict:
    df = st.list_datasets()
    if df.empty:
        sys.exit("[DAS] 등록된 데이터셋이 없습니다. 먼저 `register <파일>` 을 실행하세요.")
    if key:
        hit = df[(df["dataset_id"] == key) | (df["name"] == key)]
        if hit.empty:
            sys.exit(f"[DAS] 데이터셋을 찾을 수 없습니다: {key}. 가능한 값: {df['dataset_id'].tolist()} / {df['name'].tolist()}")
        row = hit.iloc[0]
    else:
        row = df.sort_values("created_at", ascending=False).iloc[0]
    return st.get_dataset(row["dataset_id"])


def _baseline(args) -> Scenario:
    return Scenario("기준", int(args.cells), int(args.tote), args.unit, float(args.hours))


def _context(st: Storage, meta: dict, args) -> AgentContext:
    return AgentContext(dataset_id=meta["dataset_id"], baseline=_baseline(args),
                        filters=Filters(args.start, args.end, args.category),
                        date_min=str(meta["date_min"]), date_max=str(meta["date_max"]),
                        categories=st.categories(meta["dataset_id"]))


def _emit(obj: dict, as_json: bool, text: str = "") -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, default=str, indent=1))
    else:
        print(text or json.dumps(obj, ensure_ascii=False, default=str, indent=1))


# ---------------------------------------------------------------- commands
def cmd_datasets(args):
    st, _ = _open(args)
    df = st.list_datasets()
    cols = [c for c in ("dataset_id", "name", "row_count", "date_min", "date_max", "file_name", "created_at") if c in df.columns]
    _emit({"datasets": df[cols].to_dict("records")}, args.json,
          "등록된 데이터셋이 없습니다." if df.empty else df[cols].to_string(index=False))
    st.close()


def cmd_register(args):
    st, tb = _open(args)
    try:
        insp = tb.inspect_file(args.file, args.sheet)
        mapping = dict(insp["mapping"])
        for k, v in (args.map or []):
            mapping[k] = v
        if insp.get("missing_required") and any(not mapping.get(k) for k in insp["missing_required"]):
            sys.exit(f"[DAS] 필수 열을 찾지 못했습니다: {insp['missing_required']}. --map 키=열이름 으로 지정하세요. 열 목록: {insp['columns']}")
        reg = tb.register_dataset(args.file, insp["sheet"], mapping, name=args.name)
    except ToolError as e:
        sys.exit(f"[DAS] {e}")
    out = {"sheet": insp["sheet"], "mapping": mapping, "notes": insp.get("notes", []), **reg}
    txt = (f"데이터셋 {reg.get('dataset_id')} {'(기존 재사용)' if reg.get('reused') else '등록'} · {reg.get('row_count', 0):,}행 · "
           f"{reg.get('date_min')}~{reg.get('date_max')} · 시트 {insp['sheet']}\n매핑: {mapping}")
    if reg.get("error"):
        txt = f"[DAS] 저장 중단: {reg['error']}\n" + json.dumps(reg["validation"], ensure_ascii=False, indent=1)[:3000]
    _emit(out, args.json, txt)
    st.close()


def cmd_kpi(args):
    st, _ = _open(args)
    meta = _pick_dataset(st, args.dataset)
    base = _baseline(args)
    try:
        rid, res = st.run_scenario(meta["dataset_id"], base, Filters(args.start, args.end, args.category))
    except ValueError as e:
        sys.exit(f"[DAS] {e}")
    a = res.avg_ex if args.avg_excl else res.avg
    p = res.period
    out = {"dataset": meta["name"], "run_id": rid, "scenario": base.to_dict(), "filters": res.filters.to_dict(),
           "n_days": res.n_days, "period": p, "avg": res.avg, "avg_excl_outliers": res.avg_ex, "flags": res.flags,
           "engine_version": res.engine_version}
    txt = (f"데이터셋 {meta['name']} · {base.cells}셀 · {base.tote_capacity}PCS · {base.order_unit} · {base.hours_per_day:g}h · "
           f"범위 {res.filters.describe()} · 출고일 {res.n_days}일 · 엔진 v{res.engine_version}\n"
           f"기간: OL/TOTE {p['ol_per_tote']:.2f} · PCS/TOTE {p['pcs_per_tote']:.2f} · 총 토트 {p['tote']:,} · tote/h {p['tote_per_h']} · "
           f"order {p['order']:,} · sku {p['sku']:,} · OL {p['order_line']:,} · PCS {p['pcs']:,}\n"
           f"{'일평균(특이일 제외)' if args.avg_excl else '일평균'}: OL/TOTE {a['ol_per_tote']:.2f} · PCS/TOTE {a['pcs_per_tote']:.2f} · 토트 {a['tote']:.1f}\n"
           f"특이일 {len(res.flags.get('outlier_days', []))}일 {res.flags.get('outlier_days', [])}")
    _emit(out, args.json, txt)
    if args.daily and not args.json:
        d = res.daily.copy()
        d["date"] = d["date"].dt.strftime("%Y-%m-%d")
        print(d.to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    st.close()


def _run_agent(st: Storage, tb: Toolbox, meta: dict, args, text: str):
    ctx = _context(st, meta, args)
    agent = Agent(tb, get_provider(args.llm))
    reply = agent.handle(text, ctx)
    out = {"text": reply.text, "plan": (reply.plan.__dict__ if reply.plan else None),
           "tool_calls": [{"tool": c.tool, "ok": c.ok, "summary": c.summary} for c in reply.tool_calls],
           "run_ids": reply.run_ids, "export_path": reply.export_path, "needs_input": reply.needs_input,
           "llm": f"{agent.provider.name} {agent.provider.model}" if agent.provider.available() else "none",
           "compare_table": reply.compare_table.to_dict("records") if reply.compare_table is not None else None}
    lines = [reply.text]
    if reply.compare_table is not None:
        cols = [c for c in ("시나리오", "DAS 셀", "토트 용량", "주문 단위", "총 토트", "토트 증감", "토트 증감률(%)", "OL/TOTE(기간)", "PCS/TOTE(기간)", "tote/h(기간)", "특이일 수") if c in reply.compare_table.columns]
        lines.append(reply.compare_table[cols].to_string(index=False, float_format=lambda x: f"{x:.2f}"))
    if reply.export_path:
        lines.append(f"엑셀: {reply.export_path}")
    lines.append(f"실행 기록 — 계획: {reply.plan.source if reply.plan else '-'} · 도구 {len(reply.tool_calls)}회 · 언어모델 {out['llm']}")
    _emit(out, args.json, "\n".join(lines))


def cmd_ask(args):
    st, tb = _open(args)
    meta = _pick_dataset(st, args.dataset)
    _run_agent(st, tb, meta, args, args.text)
    st.close()


def cmd_compare(args):
    st, tb = _open(args)
    meta = _pick_dataset(st, args.dataset)
    grid = DEFAULT_GRID
    if args.grid:
        grid = [tuple(int(x) for x in g.split(":")) for g in args.grid.split(",")]
    text = " ".join(f"{c}셀 {t}PCS" for c, t in grid) + " 비교" + ("하고 엑셀로 내보내줘" if args.export else "해줘")
    # 규칙 파서가 확실히 잡는 정형 문장으로 변환해 실행 (언어모델 불필요)
    args.llm = "none"
    _run_agent(st, tb, meta, args, text)
    st.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="das", description=f"DAS 히트율 시뮬레이터 CLI (엔진 v{ENGINE_VERSION})")
    ap.add_argument("--data-dir", default=os.environ.get("DAS_DATA_DIR"), help="DuckDB 폴더 (기본 %%LOCALAPPDATA%%\\das_hitrate)")
    ap.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("datasets", help="저장된 데이터셋 목록").set_defaults(fn=cmd_datasets)

    r = sub.add_parser("register", help="출고 파일 검사·매핑·등록")
    r.add_argument("file"); r.add_argument("--sheet"); r.add_argument("--name")
    r.add_argument("--map", nargs=2, action="append", metavar=("KEY", "COLUMN"),
                   help="열 매핑 덮어쓰기 (KEY: date, order_key, sku, order_line, pcs, category)")
    r.set_defaults(fn=cmd_register)

    def scenario_args(p):
        p.add_argument("--dataset", help="데이터셋 ID 또는 이름 (기본: 최근 등록)")
        p.add_argument("--cells", type=int, default=16); p.add_argument("--tote", type=int, default=15)
        p.add_argument("--unit", choices=ORDER_UNITS, default="배송처차수"); p.add_argument("--hours", type=float, default=10.0)
        p.add_argument("--start"); p.add_argument("--end"); p.add_argument("--category")
        p.add_argument("--avg-excl", action="store_true", help="특이일을 AVG에서 제외")

    k = sub.add_parser("kpi", help="기준 시나리오 KPI"); scenario_args(k)
    k.add_argument("--daily", action="store_true", help="날짜별 표 출력"); k.set_defaults(fn=cmd_kpi)

    a = sub.add_parser("ask", help="자연어 요청을 에이전트로 처리"); scenario_args(a)
    a.add_argument("text"); a.add_argument("--llm", default=os.environ.get("DAS_LLM", "none"), choices=["none", "ollama", "claude"])
    a.set_defaults(fn=cmd_ask)

    c = sub.add_parser("compare", help="시나리오 그리드 비교 (기본 6개)"); scenario_args(c)
    c.add_argument("--grid", help='예: "16:15,24:20,32:20" (셀:토트)'); c.add_argument("--export", action="store_true")
    c.set_defaults(fn=cmd_compare)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
