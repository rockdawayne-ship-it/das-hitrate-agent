"""PRD 4장 수작업 정답 + 설계안 9장 검증 시나리오."""
import datetime as dt
import math

import pandas as pd
import pytest

from das_agent.engine import (
    Scenario, Filters, compute, compare, golden_example, assign_waves, order_key_for_unit,
)


def test_prd_golden_order_unit_delivery_round():
    r = compute(golden_example(), Scenario("g", cells=2, tote_capacity=15, order_unit="배송처차수"), keep_waves=True)
    p = r.period
    assert p["order"] == 3
    assert p["sku"] == 2
    assert p["order_line"] == 5
    assert p["pcs"] == 47
    assert p["tote"] == 5
    assert p["ol_per_tote"] == pytest.approx(1.00)
    assert p["pcs_per_tote"] == pytest.approx(9.40)
    # 웨이브 1 = {A 101, A 102}, 웨이브 2 = {B 201}
    w = r.waves.set_index(["wave", "sku"])["tote"]
    assert w[(1, "X")] == 2 and w[(1, "Y")] == 1 and w[(2, "X")] == 2
    assert p["waves"] == 2


def test_prd_golden_order_unit_delivery():
    r = compute(golden_example(), Scenario("g", cells=2, tote_capacity=15, order_unit="배송처"), keep_waves=True)
    assert r.period["order"] == 2
    assert r.period["tote"] == 5
    assert r.period["waves"] == 1
    w = r.waves.set_index(["wave", "sku"])["tote"]
    assert w[(1, "X")] == 4 and w[(1, "Y")] == 1


def test_wave_resets_per_date_and_string_sort():
    d1, d2 = dt.date(2025, 1, 2), dt.date(2025, 1, 3)
    df = pd.DataFrame({
        "date": [d1, d1, d1, d2, d2],
        "order_key": ["A10", "A2", "A1", "A10", "A2"],
        "sku": ["X"] * 5, "order_line": [1] * 5, "pcs": [1] * 5,
    })
    df["unit_key"] = order_key_for_unit(df["order_key"], "배송처차수")
    out = assign_waves(df, cells=2).set_index(["date", "order_key"])["wave"]
    # 문자순: "A1" < "A10" < "A2"
    assert out[(d1, "A1")] == 1 and out[(d1, "A10")] == 1 and out[(d1, "A2")] == 2
    # 날짜 바뀌면 1부터
    assert out[(d2, "A10")] == 1 and out[(d2, "A2")] == 1


def test_sum_before_ceiling():
    d = dt.date(2025, 1, 2)
    # 같은 SKU가 3행으로 나뉨: 5+5+5=15 → ceil(15/15)=1 (행별 올림이면 3)
    df = pd.DataFrame({
        "date": [d] * 3, "order_key": ["A 1", "A 2", "A 3"], "sku": ["X"] * 3,
        "order_line": [1] * 3, "pcs": [5, 5, 5],
    })
    r = compute(df, Scenario("s", cells=10, tote_capacity=15))
    assert r.period["tote"] == 1
    r2 = compute(df, Scenario("s", cells=1, tote_capacity=15))  # 주문마다 웨이브 → 토트 3
    assert r2.period["tote"] == 3


def test_period_unique_not_summed_and_avg_vs_period_ratio():
    d1, d2 = dt.date(2025, 1, 2), dt.date(2025, 1, 3)
    df = pd.DataFrame({
        "date": [d1, d1, d2, d2],
        "order_key": ["A 1", "B 1", "A 1", "B 1"],
        "sku": ["X", "X", "X", "Y"],
        "order_line": [1, 1, 1, 1],
        "pcs": [15, 15, 1, 1],
    })
    r = compute(df, Scenario("s", cells=10, tote_capacity=15))
    assert r.period["order"] == 2 and r.period["sku"] == 2       # 기간 고유 수
    assert r.daily["order"].tolist() == [2, 2]
    # d1: tote = ceil(30/15)=2, OL/TOTE=1.0 ; d2: tote = 1+1=2, OL/TOTE=1.0
    assert r.period["tote"] == 4
    assert r.period["ol_per_tote"] == pytest.approx(4 / 4)
    assert r.avg["ol_per_tote"] == pytest.approx(1.0)
    # 월별: 1월 한 달 합계
    assert r.monthly.loc[0, "order"] == 2 and r.monthly.loc[0, "tote"] == 4


def test_avg_excludes_zero_and_tote_per_h_ceiling():
    d1, d2 = dt.date(2025, 1, 2), dt.date(2025, 1, 3)
    df = pd.DataFrame({
        "date": [d1, d2], "order_key": ["A 1", "B 1"], "sku": ["X", "Y"],
        "order_line": [1, 1], "pcs": [0, 31],
    })
    r = compute(df, Scenario("s", cells=16, tote_capacity=15, hours_per_day=10))
    # d1: pcs 0 → tote 0 → OL/TOTE NaN(분모 0) ; d2: tote 3
    assert math.isnan(r.daily.loc[0, "ol_per_tote"])
    assert r.daily.loc[1, "tote"] == 3
    assert r.daily.loc[1, "tote_per_h"] == 1          # ceil(3/10)
    assert r.avg["tote"] == pytest.approx(3.0)        # 0 제외
    assert r.avg["ol_per_tote"] == pytest.approx(1 / 3)
    assert r.period["tote_per_h"] == 1                # ceil(3/(2일×10h))


def test_filters_and_compare():
    d1, d2 = dt.date(2025, 1, 2), dt.date(2025, 2, 3)
    df = pd.DataFrame({
        "date": [d1, d1, d2],
        "order_key": ["A 1", "B 1", "C 1"],
        "sku": ["X", "X", "X"],
        "order_line": [1, 1, 1], "pcs": [16, 16, 16],
        "category": ["의류용품", "잡화", "의류용품"],
    })
    f = Filters(start="2025-01-01", end="2025-01-31", category="의류용품")
    r = compute(df, Scenario("s", cells=16, tote_capacity=15), filters=f)
    assert r.period["order"] == 1 and r.period["pcs"] == 16
    with pytest.raises(ValueError):
        compute(df, Scenario("s"), filters=Filters(category="없음"))

    base = compute(df, Scenario("16셀·15", cells=16, tote_capacity=15))
    alt = compute(df, Scenario("16셀·20", cells=16, tote_capacity=20))
    cmp_ = compare([base, alt])
    assert cmp_.loc[0, "기준"] == "●"
    # cap15: d1 ceil(32/15)=3 + d2 ceil(16/15)=2 = 5 ; cap20: 2 + 1 = 3
    assert base.period["tote"] == 5 and alt.period["tote"] == 3
    assert cmp_.loc[1, "토트 증감"] == -2 and cmp_.loc[1, "토트 순위"] == 1
    with pytest.raises(ValueError):
        compare([base, compute(df, Scenario("x"), filters=Filters(category="잡화"))])


def test_scenario_validation():
    with pytest.raises(ValueError):
        Scenario("bad", cells=0).validate()
    with pytest.raises(ValueError):
        Scenario("bad", tote_capacity=-1).validate()
    with pytest.raises(ValueError):
        Scenario("bad", order_unit="SKU").validate()
    with pytest.raises(ValueError):
        Scenario("bad", hours_per_day=0).validate()
