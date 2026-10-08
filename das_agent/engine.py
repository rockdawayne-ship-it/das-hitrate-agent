"""계산 엔진 — PRD 4장 '계산 정의'를 코드로 고정한다.

규칙 요약
- 주문: 시나리오 주문 단위 값 1개 = DAS 셀 1칸.
  배송처차수(기본) = 주문 키 전체, 배송처 = 첫 공백 앞부분.
- 웨이브: 날짜별 고유 주문 키를 문자 오름차순 정렬, 셀 수만큼 묶어 1,2,3…
  날짜가 바뀌면 1부터 다시.
- 토트: (날짜, 웨이브, SKU)별 PCS 합계를 구한 뒤 ceil(합계 / 토트 용량).
- 기간 고유 수(order, sku)는 기간 전체 고유 수. 날짜별 고유 수의 합이 아님.
- 기간 히트율 = Σorder line ÷ Σtote. 날짜별 히트율의 평균(AVG)과 구분.
- AVG = 0·빈 값을 제외한 날짜별 값의 단순 평균.
- tote/h = ceil(tote ÷ 작업시간), PCS/H = pcs ÷ 작업시간.

이 파일은 LLM이 바꿀 수 없다. 규칙 변경은 PRD 개정 사항이다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np
import pandas as pd

from . import ENGINE_VERSION

ORDER_UNITS = ("배송처차수", "배송처")

# 정규화된 입력 프레임의 필수 열
REQUIRED_COLS = ("date", "order_key", "sku", "order_line", "pcs")

# 지표 열 정의: (열 이름, 한국어 표시명, 수식 설명)
METRIC_DEFS = [
    ("order", "order", "고유 주문 수"),
    ("sku", "sku", "고유 상품코드 수"),
    ("order_line", "order line", "Order Line 합"),
    ("pcs", "pcs", "PCS 합"),
    ("tote", "tote", "ROUNDUP(PCS합/토트용량)의 합"),
    ("ol_per_tote", "HIT RATE OL/TOTE", "order line ÷ tote"),
    ("pcs_per_tote", "HIT RATE PCS/TOTE", "pcs ÷ tote"),
    ("ol_per_order", "주문당 OL", "order line ÷ order"),
    ("pcs_per_order", "주문당 PCS", "pcs ÷ order"),
    ("pcs_per_ol", "OL당 PCS", "pcs ÷ order line"),
    ("ol_per_sku", "SKU당 OL", "order line ÷ sku"),
    ("tote_per_h", "tote/h", "ROUNDUP(tote ÷ 작업시간)"),
    ("pcs_per_h", "PCS/H", "pcs ÷ 작업시간"),
]
RATIO_COLS = ["ol_per_tote", "pcs_per_tote", "ol_per_order", "pcs_per_order",
              "pcs_per_ol", "ol_per_sku"]
AVG_COLS = ["order", "sku", "order_line", "pcs", "tote"] + RATIO_COLS + ["tote_per_h", "pcs_per_h"]


@dataclass(frozen=True)
class Scenario:
    name: str = "기준"
    cells: int = 16
    tote_capacity: int = 15
    order_unit: str = "배송처차수"
    hours_per_day: float = 10.0

    def validate(self) -> None:
        if isinstance(self.cells, (bool, np.bool_)) or not isinstance(self.cells, (int, np.integer)) or not 1 <= self.cells <= 999:
            raise ValueError(f"DAS 셀 수는 1 이상의 정수여야 합니다: {self.cells!r}")
        if isinstance(self.tote_capacity, (bool, np.bool_)) or not isinstance(self.tote_capacity, (int, np.integer)) or not 1 <= self.tote_capacity <= 9999:
            raise ValueError(f"토트 용량은 1 이상의 정수여야 합니다: {self.tote_capacity!r}")
        if self.order_unit not in ORDER_UNITS:
            raise ValueError(f"주문 단위는 {ORDER_UNITS} 중 하나여야 합니다: {self.order_unit!r}")
        if isinstance(self.hours_per_day, bool) or not isinstance(self.hours_per_day, (int, float)) or not math.isfinite(self.hours_per_day) or not 0 < self.hours_per_day <= 24:
            raise ValueError(f"작업시간은 0보다 크고 24 이하의 유한한 수여야 합니다: {self.hours_per_day!r}")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("시나리오 이름이 비어 있습니다.")

    def key(self) -> str:
        return f"{self.cells}c_{self.tote_capacity}t_{self.order_unit}_{self.hours_per_day:g}h"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Scenario":
        for k in ("cells", "tote_capacity"):
            if isinstance(d[k], bool) or not math.isfinite(float(d[k])) or float(d[k]) != int(float(d[k])):
                raise ValueError(f"{k}는 정수여야 합니다.")
        result = cls(
            name=str(d.get("name", "")) or f"{d.get('cells')}셀·{d.get('tote_capacity')}PCS",
            cells=int(d["cells"]),
            tote_capacity=int(d["tote_capacity"]),
            order_unit=str(d.get("order_unit", "배송처차수")),
            hours_per_day=float(d.get("hours_per_day", 10)),
        )
        result.validate()
        return result


@dataclass(frozen=True)
class Filters:
    start: Optional[str] = None      # 'YYYY-MM-DD'
    end: Optional[str] = None
    category: Optional[str] = None   # 검토기준 값. None=전체

    def validate(self):
        import datetime
        for k in ("start", "end"):
            value = getattr(self, k)
            if value is not None:
                try:
                    if datetime.date.fromisoformat(value).isoformat() != value:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise ValueError(f"{k}: 올바른 YYYY-MM-DD 날짜가 필요합니다.")
        if self.start and self.end and self.start > self.end:
            raise ValueError("시작일은 종료일보다 늦을 수 없습니다.")
        if self.category is not None and not isinstance(self.category, str):
            raise ValueError("검토기준은 문자열이어야 합니다.")

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        parts = []
        if self.start or self.end:
            parts.append(f"{self.start or '처음'} ~ {self.end or '끝'}")
        if self.category:
            parts.append(f"검토기준={self.category}")
        return ", ".join(parts) if parts else "전체"


@dataclass
class RunResult:
    scenario: Scenario
    filters: Filters
    daily: pd.DataFrame           # 날짜별 지표 (date 오름차순)
    monthly: pd.DataFrame         # 월별 지표 (월 합계 기준 비율)
    period: dict                  # 기간 합계 기준
    avg: dict                     # 날짜별 값의 평균(0·빈 값 제외)
    n_days: int
    engine_version: str = ENGINE_VERSION
    waves: Optional[pd.DataFrame] = None   # (date, wave, sku, pcs, tote) 드릴다운
    flags: dict = field(default_factory=dict)
    avg_ex: dict = field(default_factory=dict)   # 특이일을 뺀 AVG (P2). 합계·기간 비율에는 영향 없음

    def summary_row(self) -> dict:
        row = {"name": self.scenario.name, **self.scenario.to_dict()}
        row.update({k: self.period.get(k) for k in [c for c, _, _ in METRIC_DEFS]})
        row["avg_ol_per_tote"] = self.avg.get("ol_per_tote")
        row["avg_pcs_per_tote"] = self.avg.get("pcs_per_tote")
        row["avg_tote"] = self.avg.get("tote")
        row["n_days"] = self.n_days
        return row


# ---------------------------------------------------------------- helpers
def order_key_for_unit(order_key: pd.Series, order_unit: str) -> pd.Series:
    """주문 단위에 맞는 키를 돌려준다. 배송처 = 첫 공백 앞 부분."""
    s = order_key.astype("string")
    if order_unit == "배송처차수":
        return s
    if order_unit == "배송처":
        return s.str.split(" ", n=1).str[0]
    raise ValueError(f"알 수 없는 주문 단위: {order_unit}")


def _safe_div(num, den):
    num = np.asarray(num, dtype="float64")
    den = np.asarray(den, dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(den != 0, num / den, np.nan)
    return out


def apply_filters(df: pd.DataFrame, filters: Optional[Filters]) -> pd.DataFrame:
    if filters is None:
        return df
    filters.validate()
    out = df
    if filters.start:
        out = out[out["date"] >= pd.Timestamp(filters.start).date()]
    if filters.end:
        out = out[out["date"] <= pd.Timestamp(filters.end).date()]
    if filters.category:
        if "category" not in out.columns:
            raise ValueError("검토기준 열이 없어 category 필터를 적용할 수 없습니다.")
        out = out[out["category"].astype("string") == filters.category]
    return out


def assign_waves(df: pd.DataFrame, cells: int) -> pd.DataFrame:
    """df에 'unit_key' 열이 있어야 한다. 'wave' 열을 추가해 돌려준다."""
    pairs = (
        df[["date", "unit_key"]]
        .drop_duplicates()
        .sort_values(["date", "unit_key"], kind="mergesort")  # 문자 오름차순(코드포인트)
        .reset_index(drop=True)
    )
    pairs["wave"] = (pairs.groupby("date", sort=False).cumcount() // cells + 1).astype("int64")
    return df.merge(pairs, on=["date", "unit_key"], how="left")


def _metrics_from_parts(
    order: pd.Series | int, sku: pd.Series | int, order_line, pcs, tote, hours: float
) -> dict | pd.DataFrame:
    m = {
        "order": order,
        "sku": sku,
        "order_line": order_line,
        "pcs": pcs,
        "tote": tote,
    }
    m["ol_per_tote"] = _safe_div(order_line, tote)
    m["pcs_per_tote"] = _safe_div(pcs, tote)
    m["ol_per_order"] = _safe_div(order_line, order)
    m["pcs_per_order"] = _safe_div(pcs, order)
    m["pcs_per_ol"] = _safe_div(pcs, order_line)
    m["ol_per_sku"] = _safe_div(order_line, sku)
    m["tote_per_h"] = np.ceil(np.asarray(tote, dtype="float64") / hours)
    m["pcs_per_h"] = np.asarray(pcs, dtype="float64") / hours
    return m


# ---------------------------------------------------------------- main
def compute(
    df: pd.DataFrame,
    scenario: Scenario,
    filters: Optional[Filters] = None,
    keep_waves: bool = False,
) -> RunResult:
    """정규화된 출고 프레임으로 시나리오 1개를 계산한다.

    df 열: date(datetime.date), order_key(str), sku(str), order_line(int), pcs(int)[, category]
    """
    scenario.validate()
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"필수 열이 없습니다: {missing}")

    data = apply_filters(df, filters)
    if data.empty:
        raise ValueError(f"필터({(filters or Filters()).describe()}) 적용 후 데이터가 없습니다.")

    data = data[list(REQUIRED_COLS)].copy()
    data["unit_key"] = order_key_for_unit(data["order_key"], scenario.order_unit)
    data = assign_waves(data, scenario.cells)

    # 토트: (date, wave, sku) PCS 합 → 올림
    cap = scenario.tote_capacity
    wave_sku = (
        data.groupby(["date", "wave", "sku"], sort=True, as_index=False)["pcs"].sum()
    )
    wave_sku["tote"] = wave_sku["pcs"] // cap + (wave_sku["pcs"] % cap != 0).astype("int64")

    # 날짜별
    g = data.groupby("date", sort=True)
    daily = pd.DataFrame({
        "order": g["unit_key"].nunique(),
        "sku": g["sku"].nunique(),
        "order_line": g["order_line"].sum(),
        "pcs": g["pcs"].sum(),
        "waves": g["wave"].max(),
    })
    daily["tote"] = wave_sku.groupby("date")["tote"].sum().reindex(daily.index).fillna(0).astype("int64")
    h = float(scenario.hours_per_day)
    m = _metrics_from_parts(daily["order"], daily["sku"], daily["order_line"], daily["pcs"], daily["tote"], h)
    for k in RATIO_COLS + ["tote_per_h", "pcs_per_h"]:
        daily[k] = m[k]
    daily = daily.reset_index()
    daily["date"] = pd.to_datetime(daily["date"])

    # 월별 (월 합계 기준, 고유 수는 그 달 전체)
    data["month"] = pd.to_datetime(data["date"]).dt.to_period("M").astype(str)
    gm = data.groupby("month", sort=True)
    monthly = pd.DataFrame({
        "order": gm["unit_key"].nunique(),
        "sku": gm["sku"].nunique(),
        "order_line": gm["order_line"].sum(),
        "pcs": gm["pcs"].sum(),
        "days": gm["date"].nunique(),
    })
    ws_month = wave_sku.assign(month=pd.to_datetime(wave_sku["date"]).dt.to_period("M").astype(str))
    monthly["tote"] = ws_month.groupby("month")["tote"].sum().reindex(monthly.index).fillna(0).astype("int64")
    mm = _metrics_from_parts(monthly["order"], monthly["sku"], monthly["order_line"], monthly["pcs"], monthly["tote"], h)
    for k in RATIO_COLS:
        monthly[k] = mm[k]
    # 월 시간당: 월 토트 ÷ (출고일수 × 작업시간)
    monthly["tote_per_h"] = np.ceil(monthly["tote"] / (monthly["days"] * h))
    monthly["pcs_per_h"] = monthly["pcs"] / (monthly["days"] * h)
    monthly = monthly.reset_index()

    # 기간 합계
    n_days = int(daily.shape[0])
    tot_order = int(data["unit_key"].nunique())
    tot_sku = int(data["sku"].nunique())
    tot_ol = int(daily["order_line"].sum())
    tot_pcs = int(daily["pcs"].sum())
    tot_tote = int(daily["tote"].sum())
    period = {
        "order": tot_order,
        "sku": tot_sku,
        "order_line": tot_ol,
        "pcs": tot_pcs,
        "tote": tot_tote,
        "ol_per_tote": tot_ol / tot_tote if tot_tote else None,
        "pcs_per_tote": tot_pcs / tot_tote if tot_tote else None,
        "ol_per_order": tot_ol / tot_order if tot_order else None,
        "pcs_per_order": tot_pcs / tot_order if tot_order else None,
        "pcs_per_ol": tot_pcs / tot_ol if tot_ol else None,
        "ol_per_sku": tot_ol / tot_sku if tot_sku else None,
        # 기간 tote/h = ceil(총 토트 ÷ (출고일 수 × 일 작업시간))  — 설계안 5장 제안
        "tote_per_h": math.ceil(tot_tote / (n_days * h)) if n_days else None,
        "pcs_per_h": tot_pcs / (n_days * h) if n_days else None,
        "waves": int(daily["waves"].sum()),
    }

    # AVG: 0·빈 값 제외한 날짜별 값의 단순 평균
    avg = {}
    for k in AVG_COLS:
        s = pd.to_numeric(daily[k], errors="coerce")
        s = s[(s != 0) & s.notna()]
        avg[k] = float(s.mean()) if len(s) else None

    # 특이일 표시 (P2): 주문 1건인 날, OL/TOTE < 1인 날
    flags = {
        "single_order_days": daily.loc[daily["order"] == 1, "date"].dt.strftime("%Y-%m-%d").tolist(),
        "low_hit_days": daily.loc[daily["ol_per_tote"] < 1, "date"].dt.strftime("%Y-%m-%d").tolist(),
    }
    # 특이일 제외 AVG (설계안 5장): AVG 계산에만 적용. 합계·기간 히트율은 그대로.
    outliers = sorted(set(flags["single_order_days"]) | set(flags["low_hit_days"]))
    flags["outlier_days"] = outliers
    keep = ~daily["date"].dt.strftime("%Y-%m-%d").isin(outliers)
    avg_ex = {}
    for k in AVG_COLS:
        s = pd.to_numeric(daily.loc[keep, k], errors="coerce")
        s = s[(s != 0) & s.notna()]
        avg_ex[k] = float(s.mean()) if len(s) else None
    avg_ex["days_included"] = int(keep.sum())
    avg_ex["days_excluded"] = int((~keep).sum())

    return RunResult(
        scenario=scenario,
        filters=filters or Filters(),
        daily=daily,
        monthly=monthly,
        period=period,
        avg=avg,
        n_days=n_days,
        waves=wave_sku if keep_waves else None,
        flags=flags,
        avg_ex=avg_ex,
    )


def compute_many(df: pd.DataFrame, scenarios: list[Scenario], filters: Optional[Filters] = None,
                 progress=None) -> list[RunResult]:
    out = []
    for i, sc in enumerate(scenarios):
        out.append(compute(df, sc, filters))
        if progress:
            progress(i + 1, len(scenarios), sc)
    return out


def compare(results: list[RunResult], baseline_index: int = 0) -> pd.DataFrame:
    """기준 시나리오 대비 증감 표. 같은 필터·데이터로 계산된 결과만 넣어야 한다."""
    if not results:
        return pd.DataFrame()
    base = results[baseline_index]
    base_f = base.filters.to_dict()
    rows = []
    for r in results:
        if r.filters.to_dict() != base_f:
            raise ValueError(f"'{r.scenario.name}'의 필터가 기준과 달라 비교할 수 없습니다.")
        p, bp = r.period, base.period
        row = {
            "시나리오": r.scenario.name,
            "DAS 셀": r.scenario.cells,
            "토트 용량": r.scenario.tote_capacity,
            "주문 단위": r.scenario.order_unit,
            "작업시간": r.scenario.hours_per_day,
            "총 토트": p["tote"],
            "토트 증감": p["tote"] - bp["tote"],
            "토트 증감률(%)": (p["tote"] - bp["tote"]) / bp["tote"] * 100 if bp["tote"] else None,
            "OL/TOTE(기간)": p["ol_per_tote"],
            "OL/TOTE 증감": (p["ol_per_tote"] - bp["ol_per_tote"]) if p["ol_per_tote"] is not None and bp["ol_per_tote"] is not None else None,
            "PCS/TOTE(기간)": p["pcs_per_tote"],
            "PCS/TOTE 증감": (p["pcs_per_tote"] - bp["pcs_per_tote"]) if p["pcs_per_tote"] is not None and bp["pcs_per_tote"] is not None else None,
            "tote/h(기간)": p["tote_per_h"],
            "tote/h 증감": (p["tote_per_h"] - bp["tote_per_h"]) if p["tote_per_h"] is not None and bp["tote_per_h"] is not None else None,
            "OL/TOTE(일평균)": r.avg.get("ol_per_tote"),
            "PCS/TOTE(일평균)": r.avg.get("pcs_per_tote"),
            "OL/TOTE(일평균·특이일 제외)": r.avg_ex.get("ol_per_tote"),
            "PCS/TOTE(일평균·특이일 제외)": r.avg_ex.get("pcs_per_tote"),
            "특이일 수": len(r.flags.get("outlier_days", [])),
            "총 웨이브": p["waves"],
            "기준": "●" if r is base else "",
        }
        rows.append(row)
    df = pd.DataFrame(rows)
    # 최소 토트 순위 (동률 표시)
    df["토트 순위"] = df["총 토트"].rank(method="min").astype(int)
    return df


def golden_example() -> pd.DataFrame:
    """PRD 4장 계산 예시 입력 (테스트·샘플용)."""
    import datetime as _dt
    d = _dt.date(2025, 1, 2)
    return pd.DataFrame({
        "date": [d, d, d, d],
        "order_key": ["A 101", "A 102", "A 102", "B 201"],
        "sku": ["X", "X", "Y", "X"],
        "order_line": [1, 2, 1, 1],
        "pcs": [10, 20, 1, 16],
        "category": ["의류용품"] * 4,
    })
