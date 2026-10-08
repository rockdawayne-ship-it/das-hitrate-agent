"""에이전트가 호출할 수 있는 도구 5개 (설계안 3장). 모두 JSON 직렬화 가능한 dict를 돌려준다.

inspect_file → register_dataset → simulate_scenarios → compare_results → export_report

모델은 이 함수들만 호출할 수 있다. 자유 SQL·셸·임의 코드 실행 경로는 없다.
"""
from __future__ import annotations

import shutil
import math
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import ENGINE_VERSION
from .engine import Scenario, Filters, RunResult, compare
from .ingestion import inspect_file as _inspect, read_raw, normalize, REQUIRED, LABELS
from .reporting import export_excel, explain_comparison
from .storage import Storage

MAX_SCENARIOS = 12
MAX_CELLS = 999
MAX_TOTE = 9999


class ToolError(ValueError):
    """사용자에게 그대로 보여줄 수 있는 도구 오류."""


class Toolbox:
    def __init__(self, storage: Storage):
        self.st = storage
        self._results: dict[str, RunResult] = {}

    # ------------------------------------------------------------ 1
    def inspect_file(self, path: str, sheet: Optional[str] = None) -> dict:
        p = Path(path)
        if not p.exists():
            raise ToolError(f"파일이 없습니다: {path}")
        insp = _inspect(p, sheet)
        d = insp.to_dict()
        d["existing_dataset_id"] = self.st.find_dataset(insp.file_hash, insp.sheet, insp.mapping)
        return d

    # ------------------------------------------------------------ 2
    def register_dataset(self, path: str, sheet: str, mapping: dict, name: Optional[str] = None,
                         keep_copy: bool = True) -> dict:
        p = Path(path)
        if not p.exists():
            raise ToolError(f"파일이 없습니다: {path}")
        missing = [LABELS[k] for k in REQUIRED if not mapping.get(k)]
        if missing:
            raise ToolError(f"필수 열 매핑이 비어 있습니다: {missing}")
        insp = _inspect(p, sheet)
        existing = self.st.find_dataset(insp.file_hash, insp.sheet, mapping)
        if existing:
            meta = self.st.get_dataset(existing)
            return {"dataset_id": existing, "reused": True, "row_count": meta["row_count"],
                    "date_min": str(meta["date_min"]), "date_max": str(meta["date_max"]),
                    "validation": meta["validation"]}
        t0 = datetime.now()
        try:
            raw = read_raw(p, insp.sheet)
            df, val = normalize(raw, mapping)
        except (ValueError, TypeError, OverflowError) as e:
            raise ToolError(str(e)) from e
        if not val.ok:
            return {"dataset_id": None, "reused": False, "row_count": val.row_count,
                    "validation": val.to_dict(),
                    "error": f"치명 오류 {len(val.errors)}건(표시 최대 50). 수정 후 다시 올리세요."}
        if keep_copy:
            dest = self.st.data_dir / "uploads" / f"{insp.file_hash}_{p.name}"
            if not dest.exists():
                shutil.copy2(p, dest)
        ds_id = self.st.register_dataset(
            df, name=name or p.stem, file_name=p.name, file_hash=insp.file_hash,
            sheet=insp.sheet, mapping=mapping, validation=val.to_dict(),
        )
        return {"dataset_id": ds_id, "reused": False, "row_count": val.kept_rows,
                "date_min": val.date_min, "date_max": val.date_max, "validation": val.to_dict(),
                "elapsed_sec": (datetime.now() - t0).total_seconds()}

    # ------------------------------------------------------------ 3
    @staticmethod
    def _validate_scenarios(scenarios: list[dict], defaults: Optional[Scenario] = None) -> list[Scenario]:
        if not scenarios:
            raise ToolError("시나리오가 비어 있습니다.")
        if len(scenarios) > MAX_SCENARIOS:
            raise ToolError(f"시나리오는 최대 {MAX_SCENARIOS}개까지 비교할 수 있습니다 (요청 {len(scenarios)}개).")
        d = defaults or Scenario()
        out: list[Scenario] = []
        for i, s in enumerate(scenarios):
            try:
                for key in ("cells", "tote_capacity"):
                    value = s.get(key)
                    if value is not None and (isinstance(value, bool) or not math.isfinite(float(value)) or float(value) != int(float(value))):
                        raise ValueError("정수가 아닙니다")
                cells = int(d.cells if s.get("cells") is None else s.get("cells"))
                tote = int(d.tote_capacity if s.get("tote_capacity") is None else s.get("tote_capacity"))
            except (TypeError, ValueError):
                raise ToolError(f"시나리오 {i + 1}: 셀 수·토트 용량은 정수여야 합니다: {s}")
            if not (1 <= cells <= MAX_CELLS):
                raise ToolError(f"시나리오 {i + 1}: DAS 셀 수 {cells}는 1~{MAX_CELLS} 범위를 벗어났습니다.")
            if not (1 <= tote <= MAX_TOTE):
                raise ToolError(f"시나리오 {i + 1}: 토트 용량 {tote}는 1~{MAX_TOTE} 범위를 벗어났습니다.")
            unit = s.get("order_unit") or d.order_unit
            try:
                value = d.hours_per_day if s.get("hours_per_day") is None else s.get("hours_per_day")
                if isinstance(value, bool):
                    raise ValueError()
                hours = float(value)
            except (ValueError, TypeError):
                raise ToolError("작업시간은 0보다 크고 24 이하의 수여야 합니다.")
            sc = Scenario(
                name=str(s.get("name") or f"{cells}셀·{tote}PCS" + ("" if unit == "배송처차수" else f"·{unit}")),
                cells=cells, tote_capacity=tote, order_unit=unit, hours_per_day=hours,
            )
            try:
                sc.validate()
            except ValueError as e:
                raise ToolError(str(e)) from e
            out.append(sc)
        # 동일 설정 중복 제거(이름 유지)
        seen: set[str] = set()
        uniq = []
        for sc in out:
            if sc.key() in seen:
                continue
            seen.add(sc.key())
            uniq.append(sc)
        return uniq

    def simulate_scenarios(self, dataset_id: str, scenarios: list[dict], filters: Optional[dict] = None,
                           defaults: Optional[Scenario] = None, progress=None) -> dict:
        if not dataset_id or self.st.get_dataset(dataset_id) is None:
            raise ToolError(f"등록되지 않은 데이터셋입니다: {dataset_id!r}. 먼저 파일을 올려 저장하세요.")
        scs = self._validate_scenarios(scenarios, defaults)
        f = Filters(**{k: (filters or {}).get(k) for k in ("start", "end", "category")})
        try:
            f.validate()
        except ValueError as e:
            raise ToolError(str(e)) from e
        if f.category and f.category not in self.st.categories(dataset_id):
            raise ToolError(f"검토기준 '{f.category}'가 데이터에 없습니다. 가능한 값: {self.st.categories(dataset_id)}")
        runs = []
        for i, sc in enumerate(scs):
            try:
                rid, res = self.st.run_scenario(dataset_id, sc, f)
            except ValueError as e:
                raise ToolError(str(e))
            self._results[rid] = res
            runs.append({"run_id": rid, "scenario": sc.to_dict(), "n_days": res.n_days,
                         "period": res.period, "avg": {k: res.avg[k] for k in ("ol_per_tote", "pcs_per_tote", "tote")},
                         "avg_excl_outliers": {k: res.avg_ex.get(k) for k in
                                               ("ol_per_tote", "pcs_per_tote", "tote", "days_included", "days_excluded")},
                         "flags": {k: len(v) for k, v in res.flags.items()}})
            if progress:
                progress(i + 1, len(scs), sc)
        return {"dataset_id": dataset_id, "filters": f.to_dict(), "engine_version": ENGINE_VERSION, "runs": runs}

    # ------------------------------------------------------------ 4
    def _get(self, run_id: str) -> RunResult:
        if run_id not in self._results:
            try:
                self._results[run_id] = self.st.load_run(run_id)
            except (KeyError, ValueError) as e:
                raise ToolError(f"실행 결과를 찾을 수 없습니다: {run_id} ({e})")
        return self._results[run_id]

    def compare_results(self, run_ids: list[str], baseline_run_id: Optional[str] = None) -> dict:
        if not run_ids:
            raise ToolError("비교할 실행 ID가 없습니다.")
        base = baseline_run_id or run_ids[0]
        if base not in run_ids:
            run_ids = [base] + list(run_ids)
        results = [self._get(r) for r in run_ids]
        metas = [self.st.run_meta(r) for r in run_ids]
        ds = {m["dataset_id"] for m in metas}
        if len(ds) > 1:
            raise ToolError(f"서로 다른 데이터셋의 결과는 비교하지 않습니다: {sorted(ds)}")
        bi = run_ids.index(base)
        try:
            table = compare(results, bi)
        except ValueError as e:
            raise ToolError(str(e))
        return {
            "baseline_run_id": base,
            "run_ids": run_ids,
            "table": table.to_dict(orient="records"),
            "explanation": explain_comparison(results, bi),
            "min_tote": int(table["총 토트"].min()),
            "winners": table.loc[table["총 토트"] == table["총 토트"].min(), "시나리오"].tolist(),
        }

    # ------------------------------------------------------------ 5
    def export_report(self, run_ids: list[str], baseline_run_id: Optional[str] = None,
                      path: Optional[str] = None) -> dict:
        if not run_ids:
            raise ToolError("내보낼 실행 ID가 없습니다.")
        base = baseline_run_id or run_ids[0]
        if base not in run_ids:
            run_ids = [base] + list(run_ids)
        self.compare_results(run_ids, base)
        results = [self._get(r) for r in run_ids]
        meta = self.st.run_meta(run_ids[0])
        ds = self.st.get_dataset(meta["dataset_id"])
        out = Path(path) if path else self.st.data_dir / "exports" / f"DAS_hitrate_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.xlsx"
        try:
            ds = {**ds, "run_ids": run_ids}
            export_excel(results, out, dataset=ds, baseline_index=run_ids.index(base))
        except (ValueError, OSError) as e:
            raise ToolError(str(e)) from e
        return {"path": str(out), "sheets": 3 + len(results), "run_ids": run_ids}

    # 보조: 드릴다운 (P2)
    def drilldown(self, dataset_id: str, scenario: dict, date: str, filters: Optional[dict] = None) -> dict:
        sc = self._validate_scenarios([scenario])[0]
        f = Filters(**{k: (filters or {}).get(k) for k in ("start", "end", "category")})
        try:
            f.validate()
            Filters(start=date, end=date).validate()
            df = self.st.drilldown(dataset_id, sc, date, f)
        except (ValueError, KeyError) as e:
            raise ToolError(str(e)) from e
        return {"date": date, "scenario": sc.to_dict(), "rows": df.to_dict(orient="records"),
                "tote": int(df["tote"].sum()) if len(df) else 0}

    def result(self, run_id: str) -> RunResult:
        return self._get(run_id)
