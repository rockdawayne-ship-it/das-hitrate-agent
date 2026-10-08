"""로컬 DuckDB 저장소 — 데이터셋·실행 기록 (설계안 6장).

DB 위치: 동기화되지 않는 PC 로컬 앱 데이터 폴더(%LOCALAPPDATA%/das_hitrate).
DAS_DATA_DIR 환경변수로 바꿀 수 있다.
"""
from __future__ import annotations

import json
import os
import time
import uuid
import threading
from functools import wraps
from datetime import datetime
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

from . import ENGINE_VERSION
from .engine import Scenario, Filters, RunResult, compute, apply_filters

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    dataset_id VARCHAR PRIMARY KEY,
    name VARCHAR,
    file_name VARCHAR,
    file_hash VARCHAR,
    sheet VARCHAR,
    mapping JSON,
    row_count BIGINT,
    date_min DATE,
    date_max DATE,
    validation JSON,
    created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS rows (
    dataset_id VARCHAR,
    src_row BIGINT,
    date DATE,
    order_key VARCHAR,
    sku VARCHAR,
    order_line BIGINT,
    pcs BIGINT,
    category VARCHAR
);
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR PRIMARY KEY,
    dataset_id VARCHAR,
    scenario JSON,
    scenario_key VARCHAR,
    filters JSON,
    engine_version VARCHAR,
    status VARCHAR,
    elapsed DOUBLE,
    period JSON,
    avg JSON,
    flags JSON,
    n_days INTEGER,
    created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS run_daily (
    run_id VARCHAR,
    date DATE,
    "order" BIGINT, sku BIGINT, order_line BIGINT, pcs BIGINT, waves BIGINT, tote BIGINT,
    ol_per_tote DOUBLE, pcs_per_tote DOUBLE, ol_per_order DOUBLE, pcs_per_order DOUBLE,
    pcs_per_ol DOUBLE, ol_per_sku DOUBLE, tote_per_h DOUBLE, pcs_per_h DOUBLE
);
CREATE TABLE IF NOT EXISTS run_monthly (
    run_id VARCHAR,
    month VARCHAR,
    "order" BIGINT, sku BIGINT, order_line BIGINT, pcs BIGINT, days BIGINT, tote BIGINT,
    ol_per_tote DOUBLE, pcs_per_tote DOUBLE, ol_per_order DOUBLE, pcs_per_order DOUBLE,
    pcs_per_ol DOUBLE, ol_per_sku DOUBLE, tote_per_h DOUBLE, pcs_per_h DOUBLE
);
"""
DAILY_COLS = ["date", "order", "sku", "order_line", "pcs", "waves", "tote", "ol_per_tote", "pcs_per_tote",
              "ol_per_order", "pcs_per_order", "pcs_per_ol", "ol_per_sku", "tote_per_h", "pcs_per_h"]
MONTHLY_COLS = ["month", "order", "sku", "order_line", "pcs", "days", "tote", "ol_per_tote", "pcs_per_tote",
                "ol_per_order", "pcs_per_order", "pcs_per_ol", "ol_per_sku", "tote_per_h", "pcs_per_h"]


def synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


def transaction(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        self.con.execute("BEGIN TRANSACTION")
        try:
            result = method(self, *args, **kwargs)
            self.con.execute("COMMIT")
            return result
        except Exception:
            self.con.execute("ROLLBACK")
            raise
    return call


def default_data_dir() -> Path:
    env = os.environ.get("DAS_DATA_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "das_hitrate"


class Storage:
    def __init__(self, data_dir: Optional[str | Path] = None, db_name: str = "das.duckdb"):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "uploads").mkdir(exist_ok=True)
        (self.data_dir / "exports").mkdir(exist_ok=True)
        self.db_path = self.data_dir / db_name
        self.con = duckdb.connect(str(self.db_path))
        self._lock = threading.RLock()
        self.con.execute(SCHEMA)
        self.con.execute("ALTER TABLE datasets ADD COLUMN IF NOT EXISTS normalization_version INTEGER DEFAULT 1")
        self.con.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS avg_ex JSON")
        self._frames: dict[str, pd.DataFrame] = {}  # dataset_id → 정규화 프레임 캐시

    # ------------------------------------------------------------ datasets
    def find_dataset(self, file_hash: str, sheet: str, mapping: dict) -> Optional[str]:
        """같은 파일·시트·매핑으로 이미 저장된 데이터셋 ID."""
        rows = self.con.execute(
            "SELECT dataset_id, mapping FROM datasets WHERE file_hash=? AND sheet=? AND normalization_version=2", [file_hash, sheet]
        ).fetchall()
        m = json.dumps(mapping, sort_keys=True, ensure_ascii=False)
        for ds_id, mp in rows:
            if json.dumps(json.loads(mp), sort_keys=True, ensure_ascii=False) == m:
                return ds_id
        return None

    @transaction
    def register_dataset(self, df: pd.DataFrame, *, name: str, file_name: str, file_hash: str,
                         sheet: str, mapping: dict, validation: dict) -> str:
        ds_id = "ds_" + uuid.uuid4().hex[:8]
        frame = df[["src_row", "date", "order_key", "sku", "order_line", "pcs", "category"]].copy()
        frame.insert(0, "dataset_id", ds_id)
        self.con.register("_frame", frame)
        self.con.execute("INSERT INTO rows SELECT * FROM _frame")
        self.con.unregister("_frame")
        self.con.execute(
            "INSERT INTO datasets VALUES (?,?,?,?,?,?,?,?,?,?,?,2)",
            [ds_id, name, file_name, file_hash, sheet, json.dumps(mapping, ensure_ascii=False),
             int(len(frame)), validation.get("date_min"), validation.get("date_max"),
             json.dumps(validation, ensure_ascii=False, default=str), datetime.now()],
        )
        self._frames[ds_id] = frame.drop(columns=["dataset_id"])
        return ds_id

    def list_datasets(self) -> pd.DataFrame:
        return self.con.execute(
            "SELECT dataset_id, name, file_name, sheet, row_count, date_min, date_max, created_at "
            "FROM datasets ORDER BY created_at DESC"
        ).df()

    def get_dataset(self, dataset_id: str) -> Optional[dict]:
        row = self.con.execute("SELECT * FROM datasets WHERE dataset_id=?", [dataset_id]).df()
        if row.empty:
            return None
        d = row.iloc[0].to_dict()
        d["mapping"] = json.loads(d["mapping"])
        d["validation"] = json.loads(d["validation"])
        return d

    def load_frame(self, dataset_id: str) -> pd.DataFrame:
        if dataset_id in self._frames:
            return self._frames[dataset_id]
        df = self.con.execute(
            "SELECT src_row, date, order_key, sku, order_line, pcs, category FROM rows WHERE dataset_id=?",
            [dataset_id],
        ).df()
        if df.empty:
            raise KeyError(f"등록되지 않은 데이터셋: {dataset_id}")
        df["date"] = pd.to_datetime(df["date"]).dt.date
        self._frames[dataset_id] = df
        return df

    def categories(self, dataset_id: str) -> list[str]:
        return [r[0] for r in self.con.execute(
            "SELECT DISTINCT category FROM rows WHERE dataset_id=? ORDER BY 1", [dataset_id]).fetchall()]

    def delete_dataset(self, dataset_id: str) -> None:
        run_ids = [r[0] for r in self.con.execute("SELECT run_id FROM runs WHERE dataset_id=?", [dataset_id]).fetchall()]
        for rid in run_ids:
            self.con.execute("DELETE FROM run_daily WHERE run_id=?", [rid])
            self.con.execute("DELETE FROM run_monthly WHERE run_id=?", [rid])
        self.con.execute("DELETE FROM runs WHERE dataset_id=?", [dataset_id])
        self.con.execute("DELETE FROM rows WHERE dataset_id=?", [dataset_id])
        self.con.execute("DELETE FROM datasets WHERE dataset_id=?", [dataset_id])
        self._frames.pop(dataset_id, None)

    # ------------------------------------------------------------ runs
    def find_run(self, dataset_id: str, scenario: Scenario, filters: Filters) -> Optional[str]:
        row = self.con.execute(
            "SELECT run_id FROM runs WHERE dataset_id=? AND scenario_key=? AND filters=? "
            "AND engine_version=? AND status='ok' AND json_extract_string(scenario, '$.name')=? ORDER BY created_at DESC LIMIT 1",
            [dataset_id, scenario.key(), json.dumps(filters.to_dict(), ensure_ascii=False), ENGINE_VERSION, scenario.name],
        ).fetchone()
        return row[0] if row else None

    @transaction
    def run_scenario(self, dataset_id: str, scenario: Scenario, filters: Optional[Filters] = None,
                     reuse: bool = True) -> tuple[str, RunResult]:
        filters = filters or Filters()
        if reuse:
            rid = self.find_run(dataset_id, scenario, filters)
            if rid:
                res = self.load_run(rid)
                res.scenario = scenario   # 같은 설정의 재사용: 요청한 이름을 유지
                return rid, res
        df = self.load_frame(dataset_id)
        t0 = time.perf_counter()
        run_id = "run_" + uuid.uuid4().hex[:8]
        try:
            res = compute(df, scenario, filters)
        except Exception as e:
            self.con.execute(
                "INSERT INTO runs (run_id, dataset_id, scenario, scenario_key, filters, engine_version, status, elapsed, "
                "period, avg, flags, n_days, created_at, avg_ex) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [run_id, dataset_id, json.dumps(scenario.to_dict(), ensure_ascii=False), scenario.key(),
                 json.dumps(filters.to_dict(), ensure_ascii=False), ENGINE_VERSION, f"error: {e}",
                 time.perf_counter() - t0, None, None, None, 0, datetime.now(), None],
            )
            raise
        elapsed = time.perf_counter() - t0
        self.con.execute(
            "INSERT INTO runs (run_id, dataset_id, scenario, scenario_key, filters, engine_version, status, elapsed, "
                "period, avg, flags, n_days, created_at, avg_ex) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [run_id, dataset_id, json.dumps(scenario.to_dict(), ensure_ascii=False), scenario.key(),
             json.dumps(filters.to_dict(), ensure_ascii=False), ENGINE_VERSION, "ok", elapsed,
             json.dumps(res.period, ensure_ascii=False), json.dumps(res.avg, ensure_ascii=False),
             json.dumps(res.flags, ensure_ascii=False), res.n_days, datetime.now(),
             json.dumps(res.avg_ex, ensure_ascii=False)],
        )
        d = res.daily[DAILY_COLS].copy()
        d.insert(0, "run_id", run_id)
        self.con.register("_d", d)
        self.con.execute("INSERT INTO run_daily SELECT * FROM _d")
        self.con.unregister("_d")
        m = res.monthly[MONTHLY_COLS].copy()
        m.insert(0, "run_id", run_id)
        self.con.register("_m", m)
        self.con.execute("INSERT INTO run_monthly SELECT * FROM _m")
        self.con.unregister("_m")
        return run_id, res

    def load_run(self, run_id: str) -> RunResult:
        row = self.con.execute("SELECT * FROM runs WHERE run_id=?", [run_id]).df()
        if row.empty:
            raise KeyError(f"없는 실행 ID: {run_id}")
        r = row.iloc[0]
        if r["status"] != "ok":
            raise ValueError(f"실패한 실행입니다: {r['status']}")
        daily = self.con.execute("SELECT * EXCLUDE run_id FROM run_daily WHERE run_id=? ORDER BY date", [run_id]).df()
        daily["date"] = pd.to_datetime(daily["date"])
        monthly = self.con.execute("SELECT * EXCLUDE run_id FROM run_monthly WHERE run_id=? ORDER BY month", [run_id]).df()
        return RunResult(
            scenario=Scenario.from_dict(json.loads(r["scenario"])),
            filters=Filters(**json.loads(r["filters"])),
            daily=daily, monthly=monthly,
            period=json.loads(r["period"]), avg=json.loads(r["avg"]), n_days=int(r["n_days"]),
            engine_version=r["engine_version"], flags=json.loads(r["flags"] or "{}"),
            avg_ex=json.loads(r["avg_ex"]) if r.get("avg_ex") else {},
        )

    def run_meta(self, run_id: str) -> dict:
        row = self.con.execute(
            "SELECT run_id, dataset_id, scenario, filters, engine_version, status, elapsed, created_at FROM runs WHERE run_id=?",
            [run_id]).df()
        if row.empty:
            raise KeyError(run_id)
        d = row.iloc[0].to_dict()
        d["scenario"] = json.loads(d["scenario"])
        d["filters"] = json.loads(d["filters"])
        return d

    def list_runs(self, dataset_id: Optional[str] = None, limit: int = 50) -> pd.DataFrame:
        q = "SELECT run_id, dataset_id, scenario, filters, status, elapsed, n_days, created_at FROM runs"
        args: list = []
        if dataset_id:
            q += " WHERE dataset_id=?"
            args.append(dataset_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return self.con.execute(q, args).df()

    def drilldown(self, dataset_id: str, scenario: Scenario, date: str, filters: Optional[Filters] = None) -> pd.DataFrame:
        """특정 날짜의 웨이브별·SKU별 토트 상세 (P2)."""
        df = self.load_frame(dataset_id)
        df = apply_filters(df, filters)
        day = pd.Timestamp(date).date()
        df = df[df["date"] == day]
        if df.empty:
            return pd.DataFrame(columns=["wave", "sku", "orders", "order_line", "pcs", "tote"])
        res = compute(df, scenario, keep_waves=True)
        from .engine import order_key_for_unit, assign_waves
        d = df[["date", "order_key", "sku", "order_line", "pcs"]].copy()
        d["unit_key"] = order_key_for_unit(d["order_key"], scenario.order_unit)
        d = assign_waves(d, scenario.cells)
        g = d.groupby(["wave", "sku"], as_index=False).agg(orders=("unit_key", "nunique"), order_line=("order_line", "sum"), pcs=("pcs", "sum"))
        w = res.waves[["wave", "sku", "tote"]]
        return g.merge(w, on=["wave", "sku"], how="left").sort_values(["wave", "sku"]).reset_index(drop=True)

    def close(self) -> None:
        self.con.close()


# Streamlit sessions may share the store. Serialize a full operation, including its transaction.
for _name, _method in list(vars(Storage).items()):
    if callable(_method) and not _name.startswith("_"):
        setattr(Storage, _name, synchronized(_method))
