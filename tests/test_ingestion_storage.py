import datetime as dt

import pandas as pd
import pytest

from das_agent.engine import Scenario, Filters, golden_example
from das_agent.ingestion import suggest_mapping, normalize, inspect_file, read_raw, make_sample_file
from das_agent.storage import Storage


def test_suggest_mapping_aliases():
    m = suggest_mapping(["실확정일자", "검토기준", "배송처차수", "상품코드", "Order Line", "PCS"])
    assert m == {"date": "실확정일자", "order_key": "배송처차수", "sku": "상품코드",
                 "order_line": "Order Line", "pcs": "PCS", "category": "검토기준"}
    m2 = suggest_mapping(["출고일", "배송처코드", "SKU", "O/L", "수량"])
    assert m2["order_key"] == "배송처코드" and m2["order_line"] == "O/L" and m2["pcs"] == "수량"
    assert m2["category"] is None


def test_normalize_validation_errors_and_leading_zero():
    raw = pd.DataFrame({
        "실확정일자": [20250102, "2025-01-03", "bad", 20250104],
        "배송처차수": ["00123 301", "00123 302", "", "9 1"],
        "상품코드": ["X", "Y", "Z", "X"],
        "Order Line": [1, 1, 1, -1],
        "PCS": [4, 2.5, 1, 3],
    })
    m = suggest_mapping(list(raw.columns))
    df, val = normalize(raw, m)
    assert not val.ok
    reasons = {(e["row"], e["reason"]) for e in val.errors}
    assert (4, "날짜 해석 불가") in reasons
    assert (4, "배송처차수(주문 키) 비어 있음") in reasons
    assert (5, "Order Line는 0 이상의 정수여야 함") in reasons
    assert any(r == 3 and "PCS" in why for r, why in reasons)
    assert df.loc[0, "order_key"] == "00123 301"      # 앞자리 0 보존
    assert df.loc[1, "date"] == dt.date(2025, 1, 3)
    assert (df["category"] == "미분류").all()


def test_sample_file_roundtrip_and_storage(tmp_path):
    xlsx = make_sample_file(tmp_path / "sample.xlsx", days=10)
    insp = inspect_file(xlsx)
    assert insp.sheet == "data" and not insp.missing_required
    raw = read_raw(xlsx, insp.sheet)
    df, val = normalize(raw, insp.mapping)
    assert val.ok and val.row_count == len(raw)

    st = Storage(tmp_path / "data")
    ds = st.register_dataset(df, name="sample", file_name="sample.xlsx", file_hash=insp.file_hash,
                             sheet=insp.sheet, mapping=insp.mapping, validation=val.to_dict())
    assert st.find_dataset(insp.file_hash, insp.sheet, insp.mapping) == ds
    assert st.list_datasets().iloc[0]["row_count"] == len(df)

    sc = Scenario("기준", 16, 15)
    rid, res = st.run_scenario(ds, sc)
    rid2, res2 = st.run_scenario(ds, sc)          # 동일 조건 재사용
    assert rid == rid2 and res2.period == res.period
    assert res2.daily["tote"].sum() == res.period["tote"]

    # 저장소 재오픈 후 로드
    st.close()
    st2 = Storage(tmp_path / "data")
    loaded = st2.load_run(rid)
    assert loaded.period["tote"] == res.period["tote"]
    assert loaded.scenario == sc
    assert len(st2.load_frame(ds)) == len(df)
    dd = st2.drilldown(ds, sc, str(res.daily.loc[0, "date"].date()))
    assert dd["tote"].sum() == res.daily.loc[0, "tote"]
    st2.delete_dataset(ds)
    assert st2.list_datasets().empty


def test_storage_golden(tmp_path):
    st = Storage(tmp_path / "d")
    g = golden_example()
    g.insert(0, "src_row", range(2, 6))
    ds = st.register_dataset(g, name="g", file_name="g", file_hash="h", sheet="data", mapping={}, validation={})
    _, r = st.run_scenario(ds, Scenario("g", 2, 15))
    assert r.period["tote"] == 5 and r.period["order"] == 3
    _, r2 = st.run_scenario(ds, Scenario("g2", 2, 15, "배송처"))
    assert r2.period["order"] == 2 and r2.period["tote"] == 5
