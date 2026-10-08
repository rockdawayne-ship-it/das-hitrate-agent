"""DAS 히트율 시뮬레이터 + 운영 분석 에이전트 — Streamlit 화면 (설계안 7장).

실행: streamlit run app.py
"""
from __future__ import annotations

import os
import hashlib
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from das_agent import ENGINE_VERSION
from das_agent.agent import Agent, AgentContext
from das_agent.engine import Scenario, Filters, ORDER_UNITS, compare
from das_agent.ingestion import REQUIRED, OPTIONAL, LABELS, make_sample_file
from das_agent.llm import get_provider
from das_agent.reporting import export_excel, explain_comparison
from das_agent.storage import Storage
from das_agent.tools import Toolbox, ToolError

st.set_page_config(page_title="DAS 히트율 시뮬레이터", page_icon="📦", layout="wide")
st.markdown("""<style>
.block-container {padding-top: 2rem; max-width: 1500px;}
[data-testid="stMetric"] {border:1px solid #dde7ec; border-radius:12px; padding:16px; background:#f5f9fa;}
[data-testid="stMetricLabel"] {color:#47616b;}
[data-testid="stMetricValue"] {font-size:1.8rem; color:#10343d;}
button[data-baseweb="tab"] {font-weight:600; padding:12px 18px;}
</style>""", unsafe_allow_html=True)
st.title("DAS 운영 분석")
st.caption("출고 데이터를 넣고, 셀 수와 토트 용량에 따른 작업량을 비교하세요.")
INPUT_FILE = Path.home() / "DAS_Data" / "input" / "data.xlsx"

DEFAULT_GRID = [(16, 15), (16, 20), (24, 15), (24, 20), (32, 15), (32, 20)]
COMPARE_COLS = {
    "작업시간": st.column_config.NumberColumn(format="%.1f"),
    "총 토트": st.column_config.NumberColumn(format="%d"),
    "토트 증감": st.column_config.NumberColumn(format="%+d"),
    "토트 증감률(%)": st.column_config.NumberColumn(format="%+.1f%%"),
    "OL/TOTE(기간)": st.column_config.NumberColumn(format="%.2f"),
    "OL/TOTE 증감": st.column_config.NumberColumn(format="%+.2f"),
    "PCS/TOTE(기간)": st.column_config.NumberColumn(format="%.2f"),
    "PCS/TOTE 증감": st.column_config.NumberColumn(format="%+.2f"),
    "tote/h(기간)": st.column_config.NumberColumn(format="%d"),
    "tote/h 증감": st.column_config.NumberColumn(format="%+d"),
    "OL/TOTE(일평균)": st.column_config.NumberColumn(format="%.2f"),
    "PCS/TOTE(일평균)": st.column_config.NumberColumn(format="%.2f"),
    "OL/TOTE(일평균·특이일 제외)": st.column_config.NumberColumn(format="%.2f"),
    "PCS/TOTE(일평균·특이일 제외)": st.column_config.NumberColumn(format="%.2f"),
}


# ---------------------------------------------------------------- 리소스
@st.cache_resource(show_spinner=False)
def get_storage() -> Storage:
    return Storage()


@st.cache_resource(show_spinner=False)
def get_toolbox() -> Toolbox:
    return Toolbox(get_storage())


def get_agent(pref: str = "none") -> Agent:
    key = "agent_" + pref
    if key not in st.session_state:
        st.session_state[key] = Agent(get_toolbox(), get_provider(pref))
    return st.session_state[key]


@st.cache_data(show_spinner=False, max_entries=4)
def cached_inspection(path, sheet, modified_ns):
    return get_toolbox().inspect_file(path, sheet)


storage = get_storage()
toolbox = get_toolbox()

ss = st.session_state
ss.setdefault("dataset_id", None)
ss.setdefault("baseline", {"cells": 16, "tote_capacity": 15, "order_unit": "배송처차수", "hours_per_day": 10.0})
ss.setdefault("filters", {"start": None, "end": None, "category": None})
ss.setdefault("scenarios", pd.DataFrame(
    [{"시나리오": f"{c}셀·{t}PCS", "DAS 셀": c, "토트 용량": t, "주문 단위": "배송처차수", "작업시간": 10.0} for c, t in DEFAULT_GRID]))
ss.setdefault("compare_runs", [])
ss.setdefault("chat", [])
ss.setdefault("agent_ctx", None)
ss.setdefault("upload", None)
ss.setdefault("llm_pref", os.environ.get("DAS_LLM", "ollama"))


def current_baseline() -> Scenario:
    b = ss["baseline"]
    return Scenario("기준", int(b["cells"]), int(b["tote_capacity"]), b["order_unit"], float(b["hours_per_day"]))


def current_filters() -> Filters:
    f = ss["filters"]
    return Filters(f.get("start"), f.get("end"), f.get("category"))


def fmt(x, nd=2):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "N/A"
    if isinstance(x, (int,)) or (isinstance(x, float) and float(x).is_integer() and abs(x) >= 100):
        return f"{int(x):,}"
    return f"{x:,.{nd}f}"


# ---------------------------------------------------------------- 사이드바
with st.sidebar:
    st.title("📦 DAS 히트율")
    st.caption("내 PC에서 계산 · API 키 불필요")
    with st.expander("저장 위치·계산 버전"):
        st.caption(f"엔진 v{ENGINE_VERSION} · 데이터 폴더: `{storage.data_dir}`")
    ds_df = storage.list_datasets()
    if ds_df.empty:
        st.info("등록된 데이터셋이 없습니다. '데이터 준비' 탭에서 파일을 올리세요.")
        ss["dataset_id"] = None
    else:
        labels = {r.dataset_id: f"{r.name} ({r.row_count:,}행, {r.date_min}~{r.date_max})" for r in ds_df.itertuples()}
        ids = list(labels)
        if ss["dataset_id"] not in ids:
            ss["dataset_id"] = ids[0]
        ss["dataset_id"] = st.selectbox("데이터셋", ids, index=ids.index(ss["dataset_id"]), format_func=lambda i: labels[i])
    if ss.get("active_dataset") != ss["dataset_id"]:
        ss["active_dataset"] = ss["dataset_id"]
        ss["filters"] = {"start": None, "end": None, "category": None}
        for k, value in (("compare_runs", []), ("chat", []), ("agent_ctx", None), ("last_export", None)):
            ss[k] = value

    st.subheader("기준 시나리오")
    b = ss["baseline"]
    b["cells"] = st.number_input("DAS 셀 수", 1, 999, int(b["cells"]))
    b["tote_capacity"] = st.number_input("토트 용량(PCS)", 1, 9999, int(b["tote_capacity"]))
    b["order_unit"] = st.selectbox("주문 단위", ORDER_UNITS, index=ORDER_UNITS.index(b["order_unit"]),
                                   help="배송처차수 = '14247 301' 전체, 배송처 = 공백 앞 '14247'")
    b["hours_per_day"] = st.number_input("일 작업시간(h)", 0.5, 24.0, float(b["hours_per_day"]), step=0.5)

    st.subheader("분석 범위")
    f = ss["filters"]
    meta = storage.get_dataset(ss["dataset_id"]) if ss["dataset_id"] else None
    use_period = st.checkbox("기간 지정", value=bool(f.get("start") or f.get("end")))
    if use_period and meta:
        dmin, dmax = pd.Timestamp(meta["date_min"]).date(), pd.Timestamp(meta["date_max"]).date()
        s0 = pd.Timestamp(f["start"]).date() if f.get("start") else dmin
        e0 = pd.Timestamp(f["end"]).date() if f.get("end") else dmax
        rng = st.date_input("기간", (s0, e0), min_value=dmin, max_value=dmax)
        if isinstance(rng, tuple) and len(rng) == 2:
            f["start"], f["end"] = str(rng[0]), str(rng[1])
    else:
        f["start"], f["end"] = None, None
    cats = storage.categories(ss["dataset_id"]) if ss["dataset_id"] else []
    if len(cats) > 1:
        c = st.selectbox("검토기준", ["(전체)"] + cats, index=(["(전체)"] + cats).index(f["category"]) if f.get("category") in cats else 0)
        f["category"] = None if c == "(전체)" else c
    else:
        f["category"] = None
    st.caption(f"적용 범위: {current_filters().describe()}")
    ss["avg_excl"] = st.checkbox("특이일을 일평균(AVG)에서 제외", value=bool(ss.get("avg_excl", False)),
                                 help="주문 1건인 날, OL/TOTE<1인 날을 AVG에서만 뺍니다. 합계·기간 히트율은 그대로입니다.")

    st.subheader("요청 처리 방식")
    modes = ["ollama", "claude", "none"]
    MODE_LABELS = {"ollama": "로컬 AI · Ollama (데이터 외부 전송 없음)", "claude": "Claude 구독 · Claude Code 로그인", "none": "정형 요청 · 모델 없음"}
    ss["llm_pref"] = st.selectbox("대화 모드", modes, index=modes.index(ss["llm_pref"]) if ss["llm_pref"] in modes else 0,
                                  format_func=lambda x: MODE_LABELS[x],
                                  help="Claude 구독: API 키 없이 Claude Code 로그인 계정으로 호출. 현재 설정·기간·검토기준 값·요청문만 전송하고 출고 원본 행은 보내지 않습니다.")
    provider = get_agent(ss["llm_pref"]).provider
    if provider.available():
        if provider.name == "claude":
            st.success(f"Claude 구독 연결됨 · {provider.model} · {getattr(provider, 'account', '')}")
            st.caption("전송: 설정·기간·검토기준 값·요청문. 미전송: 출고 원본 행, 집계 표.")
        else:
            st.success(f"로컬 AI 연결됨 · {provider.model}")
    else:
        st.info("정형 요청 모드 · AI 모델 없이 계산·비교·내보내기를 처리합니다.")
        if ss["llm_pref"] in ("ollama", "claude"):
            st.caption(getattr(provider, "reason", "모델 연결을 확인하세요."))
    if st.button("모델 연결 다시 확인"):
        ss.pop("agent_" + ss["llm_pref"], None)
        st.rerun()

scope = (ss["dataset_id"], current_baseline().key(), tuple(current_filters().to_dict().values()))
if ss.get("ui_scope") != scope:
    ss["ui_scope"] = scope
    ss["compare_runs"] = []
    ss["last_export"] = None
    ss["agent_ctx"] = None
if meta and ("sample" in meta["file_name"].lower() or "합성" in meta["name"]):
    st.info("현재 데이터는 기능 체험용 합성 샘플입니다. 실제 출고 실적이 아닙니다.")


tab1, tab2, tab3, tab4 = st.tabs(["1. 데이터 준비", "2. 결과 대시보드", "3. 시나리오 비교", "4. 에이전트와 대화"])

# ================================================================ 탭 1
with tab1:
    st.header("업로드 → 시트 선택 → 열 매핑 → 검증 → 저장")
    col_u, col_s = st.columns([3, 1])
    with col_u:
        up = st.file_uploader("출고 데이터 (.xlsx / .csv)", type=["xlsx", "xlsm", "csv"])
    with col_s:
        if st.button("합성 샘플 파일 생성", help="PRD 열 이름을 가진 30일치 합성 데이터 (실제 출고 아님)"):
            p = make_sample_file(storage.data_dir / "uploads" / "sample_synthetic.xlsx", days=45)
            ss["upload"] = str(p)
            st.success(f"생성: {p}")
    st.caption(f"추천 입력 경로: {INPUT_FILE}")
    if st.button("추천 폴더의 data.xlsx 불러오기", disabled=not INPUT_FILE.is_file()):
        ss["upload"] = str(INPUT_FILE)
    if up is not None:
        token = hashlib.sha256(up.getbuffer()).hexdigest()[:24]
        if ss.get("upload_token") != token:
            dest = storage.data_dir / "uploads" / f"{token}_{Path(up.name).name}"
            dest.write_bytes(up.getbuffer())
            ss["upload"] = str(dest)
            ss["upload_token"] = token

    if ss["upload"]:
        path = ss["upload"]
        st.write(f"파일: `{Path(path).name}`")
        try:
            insp0 = cached_inspection(path, None, Path(path).stat().st_mtime_ns)
        except Exception as e:
            st.error(f"파일을 읽을 수 없습니다: {e}")
            insp0 = None
        if insp0:
            sheet = st.selectbox("시트", insp0["sheets"], index=insp0["sheets"].index(insp0["sheet"]))
            insp = cached_inspection(path, sheet, Path(path).stat().st_mtime_ns) if sheet != insp0["sheet"] else insp0
            st.dataframe(pd.DataFrame(insp["sample"]), width="stretch", hide_index=True)
            st.subheader("열 매핑 (자동 인식 → 확인)")
            cols = ["(없음)"] + insp["columns"]
            mapping = {}
            mc = st.columns(3)
            for i, k in enumerate(REQUIRED + OPTIONAL):
                sug = insp["mapping"].get(k)
                with mc[i % 3]:
                    v = st.selectbox(LABELS[k], cols, index=cols.index(sug) if sug in cols else 0, key=f"map_{insp['file_hash']}_{sheet}_{k}")
                mapping[k] = None if v == "(없음)" else v
            for n in insp["notes"]:
                st.caption("ℹ️ " + n)
            if insp["existing_dataset_id"] and insp["mapping"] == mapping:
                st.info(f"같은 파일·시트·매핑이 이미 저장되어 있습니다: {insp['existing_dataset_id']}")
            name = st.text_input("데이터셋 이름", Path(path).stem.replace("_incoming_", ""))
            if st.button("검증하고 저장", type="primary"):
                with st.status("읽는 중 → 검증 → DuckDB 저장", expanded=True) as status:
                    try:
                        res = toolbox.register_dataset(path, sheet, mapping, name=name)
                    except (ToolError, ValueError, OSError) as e:
                        status.update(label="실패", state="error")
                        st.error(str(e))
                        res = None
                if res:
                    val = res.get("validation", {})
                    if res.get("dataset_id"):
                        status.update(label="저장 완료", state="complete")
                        st.success(f"{'재사용' if res['reused'] else '저장'}: {res['dataset_id']} · {res['row_count']:,}행 · "
                                   f"{res.get('date_min')}~{res.get('date_max')}" +
                                   (f" · {res.get('elapsed_sec', 0):.1f}초" if not res["reused"] else ""))
                        for w in val.get("warnings", []):
                            st.warning(w)
                        ss["dataset_id"] = res["dataset_id"]
                        ss["compare_runs"] = []
                        st.rerun()
                    else:
                        status.update(label="치명 오류로 저장 중단", state="error")
                        st.error(res.get("error"))
                        st.dataframe(pd.DataFrame(val.get("errors", [])), width="stretch", hide_index=True)

    st.divider()
    st.subheader("저장된 데이터셋")
    ds_df = storage.list_datasets()
    st.dataframe(ds_df, width="stretch", hide_index=True)
    if not ds_df.empty:
        del_id = st.selectbox("삭제할 데이터셋", ds_df["dataset_id"].tolist(), key="del_ds")
        delete_confirm = st.checkbox("선택한 데이터셋과 계산 기록을 삭제합니다")
        if st.button("데이터셋 삭제 (실행 기록 포함)", disabled=not delete_confirm):
            storage.delete_dataset(del_id)
            ss["dataset_id"] = None
            ss["compare_runs"] = []
            st.rerun()


# ================================================================ 공통 차트
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def daily_chart(res, title: str):
    """일별 OL/TOTE·PCS/TOTE 추이 + 기간 기준선 + 특이일 마커."""
    d = res.daily
    out = set(res.flags.get("outlier_days", []))
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=d["date"], y=d["ol_per_tote"], name="OL/TOTE", mode="lines+markers",
                             line=dict(color="#1f6f8b", width=2), marker=dict(size=5)))
    fig.add_trace(go.Scatter(x=d["date"], y=d["pcs_per_tote"], name="PCS/TOTE", mode="lines+markers", yaxis="y2",
                             line=dict(color="#e67e22", width=2, dash="dot"), marker=dict(size=5)))
    if out:
        od = d[d["date"].dt.strftime("%Y-%m-%d").isin(out)]
        fig.add_trace(go.Scatter(x=od["date"], y=od["ol_per_tote"], name="특이일", mode="markers",
                                 marker=dict(symbol="x", size=12, color="#c0392b", line=dict(width=2))))
    if res.period.get("ol_per_tote") is not None:
        fig.add_hline(y=res.period["ol_per_tote"], line=dict(color="#1f6f8b", dash="dash", width=1),
                      annotation_text=f"기간 OL/TOTE {res.period['ol_per_tote']:.2f}", annotation_position="top left")
    fig.update_layout(title=title, yaxis=dict(title="OL/TOTE"), yaxis2=dict(title="PCS/TOTE", overlaying="y", side="right"),
                      height=360, margin=dict(l=20, r=20, t=40, b=20), legend=dict(orientation="h"), hovermode="x unified")
    return fig


def workload_chart(res):
    """일별 작업량: 토트(막대) + 웨이브 수(선)."""
    d = res.daily
    fig = go.Figure()
    fig.add_trace(go.Bar(x=d["date"], y=d["tote"], name="토트", marker_color="#5dade2"))
    fig.add_trace(go.Scatter(x=d["date"], y=d["waves"], name="웨이브", mode="lines+markers", yaxis="y2",
                             line=dict(color="#7d3c98", width=2)))
    a = res.avg_ex if ss.get("avg_excl") else res.avg
    if a.get("tote"):
        fig.add_hline(y=a["tote"], line=dict(color="#5dade2", dash="dash", width=1),
                      annotation_text=f"일평균 토트 {a['tote']:.1f}", annotation_position="top left")
    fig.update_layout(title="일별 작업량 (토트·웨이브)", yaxis=dict(title="토트"), yaxis2=dict(title="웨이브", overlaying="y", side="right"),
                      height=340, margin=dict(l=20, r=20, t=40, b=20), legend=dict(orientation="h"), hovermode="x unified")
    return fig


def weekday_frame(res) -> pd.DataFrame:
    """요일별 평균 (0·빈 값 제외, 특이일 옵션 반영)."""
    d = res.daily.copy()
    if ss.get("avg_excl"):
        d = d[~d["date"].dt.strftime("%Y-%m-%d").isin(res.flags.get("outlier_days", []))]
    d["요일"] = d["date"].dt.weekday.map(lambda i: WEEKDAYS[i])
    g = d.groupby("요일")
    out = pd.DataFrame({
        "출고일 수": g.size(),
        "평균 토트": g["tote"].apply(lambda s: s[s != 0].mean()),
        "평균 OL/TOTE": g["ol_per_tote"].apply(lambda s: s[(s != 0) & s.notna()].mean()),
        "평균 PCS/TOTE": g["pcs_per_tote"].apply(lambda s: s[(s != 0) & s.notna()].mean()),
    }).reindex([w for w in WEEKDAYS if w in g.size().index]).reset_index()
    return out


def basis_table(res) -> pd.DataFrame:
    """합계 기준 vs 일평균 vs 특이일 제외 일평균 — 같은 지표의 세 가지 집계를 나란히."""
    p, a, x = res.period, res.avg, res.avg_ex
    rows = [("OL/TOTE", "ol_per_tote"), ("PCS/TOTE", "pcs_per_tote"), ("토트", "tote"), ("tote/h", "tote_per_h"), ("order", "order"), ("order line", "order_line")]
    return pd.DataFrame([{"지표": lbl, "기간 합계 기준": p.get(k), "일평균(AVG, 0 제외)": a.get(k),
                          "AVG(특이일 제외)": x.get(k)} for lbl, k in rows])


def outlier_table(res) -> pd.DataFrame:
    d = res.daily
    single, low = set(res.flags.get("single_order_days", [])), set(res.flags.get("low_hit_days", []))
    out = d[d["date"].dt.strftime("%Y-%m-%d").isin(single | low)].copy()
    out["날짜"] = out["date"].dt.strftime("%Y-%m-%d")
    out["사유"] = out["날짜"].map(lambda s: " · ".join([t for t, ok in (("주문 1건", s in single), ("OL/TOTE<1", s in low)) if ok]))
    return out[["날짜", "사유", "order", "order_line", "pcs", "tote", "ol_per_tote", "pcs_per_tote"]]


DAILY_COLS_CFG = {
    "date": st.column_config.TextColumn("날짜"),
    "ol_per_tote": st.column_config.NumberColumn("OL/TOTE", format="%.2f"),
    "pcs_per_tote": st.column_config.NumberColumn("PCS/TOTE", format="%.2f"),
    "ol_per_order": st.column_config.NumberColumn("주문당 OL", format="%.2f"),
    "pcs_per_order": st.column_config.NumberColumn("주문당 PCS", format="%.2f"),
    "pcs_per_ol": st.column_config.NumberColumn("OL당 PCS", format="%.2f"),
    "ol_per_sku": st.column_config.NumberColumn("SKU당 OL", format="%.2f"),
    "tote_per_h": st.column_config.NumberColumn("tote/h", format="%d"),
    "pcs_per_h": st.column_config.NumberColumn("PCS/H", format="%.1f"),
    "특이일": st.column_config.CheckboxColumn("특이일", disabled=True),
}


# ================================================================ 탭 2
with tab2:
    st.header("운영 대시보드")
    if not ss["dataset_id"]:
        st.info("데이터셋을 먼저 저장하세요.")
    else:
        base = current_baseline()
        meta = storage.get_dataset(ss["dataset_id"]) or {}
        try:
            with st.spinner("계산 중…"):
                rid, res = storage.run_scenario(ss["dataset_id"], base, current_filters())
        except ValueError as e:
            st.error(str(e))
            res = None
        if res:
            p = res.period
            use_ex = bool(ss.get("avg_excl"))
            a = res.avg_ex if use_ex else res.avg
            avg_label = "일평균·특이일 제외" if use_ex else "일평균"
            st.caption(f"데이터셋 {meta.get('name', '')} ({meta.get('row_count', 0):,}행) · 실행 {rid} · "
                       f"기준 {base.cells}셀 · {base.tote_capacity}PCS · {base.order_unit} · {base.hours_per_day:g}h · "
                       f"범위 {res.filters.describe()} · 출고일 {res.n_days}일 · 엔진 v{res.engine_version}")

            # --- KPI 카드
            k = st.columns(6)
            k[0].metric("OL/TOTE (기간)", fmt(p["ol_per_tote"]), help="Σorder line ÷ Σtote. 날짜별 평균과 다름")
            k[1].metric("PCS/TOTE (기간)", fmt(p["pcs_per_tote"]), help="Σpcs ÷ Σtote")
            k[2].metric("총 토트", fmt(p["tote"]), help="ROUNDUP(PCS합/토트용량)의 합")
            k[3].metric(f"토트 ({avg_label})", fmt(a["tote"], 1), help="0인 날 제외 평균" + (" · 특이일 제외" if use_ex else ""))
            k[4].metric(f"OL/TOTE ({avg_label})", fmt(a["ol_per_tote"]), help="날짜별 OL/TOTE의 평균")
            k[5].metric("tote/h (기간)", fmt(p["tote_per_h"]), help="ROUNDUP(총 토트 ÷ (출고일 수 × 작업시간))")
            k2 = st.columns(6)
            k2[0].metric("order (기간 고유)", fmt(p["order"])); k2[1].metric("sku (기간 고유)", fmt(p["sku"]))
            k2[2].metric("order line", fmt(p["order_line"])); k2[3].metric("pcs", fmt(p["pcs"]))
            k2[4].metric("총 웨이브", fmt(p["waves"])); k2[5].metric("PCS/H (기간)", fmt(p["pcs_per_h"], 1))

            fl = res.flags
            n_out = len(fl.get("outlier_days", []))
            if n_out:
                mode = (f"AVG에서 제외 (포함 {res.avg_ex.get('days_included')}일 / 제외 {res.avg_ex.get('days_excluded')}일)"
                        if use_ex else "AVG에 포함 (사이드바에서 제외 가능)")
                st.warning(f"특이일 {n_out}일 — 주문 1건: {len(fl.get('single_order_days', []))}일, "
                           f"OL/TOTE<1: {len(fl.get('low_hit_days', []))}일. {mode}. 합계·기간 히트율은 항상 포함.")

            # --- 추이
            c1, c2 = st.columns([3, 2])
            with c1:
                st.plotly_chart(daily_chart(res, "일별 히트율 추이"), width="stretch")
            with c2:
                m = res.monthly
                fig = px.bar(m, x="month", y=["ol_per_tote", "pcs_per_tote"], barmode="group", title="월별 히트율 (월 합계 기준)",
                             color_discrete_sequence=["#1f6f8b", "#e67e22"])
                fig.update_layout(height=360, margin=dict(l=20, r=20, t=40, b=20), legend=dict(orientation="h", title=None),
                                  yaxis_title=None, xaxis_title=None)
                st.plotly_chart(fig, width="stretch")

            # --- 작업량·요일 패턴
            c3, c4 = st.columns([3, 2])
            with c3:
                st.plotly_chart(workload_chart(res), width="stretch")
            with c4:
                wd = weekday_frame(res)
                fig = px.bar(wd, x="요일", y="평균 OL/TOTE", text="출고일 수", title=f"요일별 평균 OL/TOTE ({avg_label})",
                             color_discrete_sequence=["#1f6f8b"])
                fig.update_traces(texttemplate="%{text}일", textposition="outside")
                fig.update_layout(height=340, margin=dict(l=20, r=20, t=40, b=20), yaxis_title=None, xaxis_title=None)
                st.plotly_chart(fig, width="stretch")

            # --- 집계 기준 비교 · 특이일 목록
            c5, c6 = st.columns([2, 3])
            with c5:
                st.subheader("집계 기준 비교")
                st.caption("같은 지표라도 기간 합계 비율과 날짜별 평균은 다르다. 보고서에는 기준을 명시한다.")
                st.dataframe(basis_table(res), width="stretch", hide_index=True,
                             column_config={c: st.column_config.NumberColumn(format="%.2f") for c in
                                            ("기간 합계 기준", "일평균(AVG, 0 제외)", "AVG(특이일 제외)")})
            with c6:
                st.subheader(f"특이일 목록 ({n_out}일)")
                if n_out:
                    st.dataframe(outlier_table(res), width="stretch", hide_index=True,
                                 column_config={"ol_per_tote": st.column_config.NumberColumn("OL/TOTE", format="%.2f"),
                                                "pcs_per_tote": st.column_config.NumberColumn("PCS/TOTE", format="%.2f")})
                else:
                    st.caption("특이일(주문 1건인 날, OL/TOTE<1인 날)이 없습니다.")

            # --- 시나리오 비교 요약 (탭 3에서 계산한 결과가 있을 때)
            if ss.get("compare_runs"):
                try:
                    cres = [toolbox.result(r) for r in ss["compare_runs"]]
                    ctab = compare(cres, min(ss.get("compare_base", 0), len(cres) - 1))
                    best = ctab.sort_values("총 토트").head(3)
                    st.subheader("시나리오 비교 요약")
                    st.caption(f"최근 비교 {len(cres)}건 중 총 토트가 적은 순 3개. 전체 표는 '3. 시나리오 비교' 탭.")
                    st.dataframe(best[["시나리오", "DAS 셀", "토트 용량", "주문 단위", "총 토트", "토트 증감", "토트 증감률(%)",
                                       "OL/TOTE(기간)", "PCS/TOTE(기간)", "tote/h(기간)", "기준"]],
                                 width="stretch", hide_index=True, column_config=COMPARE_COLS)
                except (KeyError, ValueError):
                    pass

            # --- 상세 표
            with st.expander("날짜별 표", expanded=False):
                show = res.daily.copy()
                show["특이일"] = show["date"].dt.strftime("%Y-%m-%d").isin(fl.get("outlier_days", []))
                show["date"] = show["date"].dt.strftime("%Y-%m-%d")
                st.dataframe(show, width="stretch", hide_index=True, column_config=DAILY_COLS_CFG)
            with st.expander("월별 표"):
                st.dataframe(res.monthly, width="stretch", hide_index=True, column_config=DAILY_COLS_CFG)
            with st.expander("날짜 드릴다운 (웨이브별·SKU별 토트)"):
                dates = res.daily["date"].dt.strftime("%Y-%m-%d").tolist()
                dsel = st.selectbox("날짜", dates, key="drill_date")
                if st.button("상세 조회"):
                    dd = storage.drilldown(ss["dataset_id"], base, dsel, current_filters())
                    st.caption(f"{dsel}: 웨이브 {dd['wave'].nunique()}개, 토트 {int(dd['tote'].sum())}")
                    wsum = dd.groupby("wave", as_index=False).agg(sku수=("sku", "nunique"), pcs=("pcs", "sum"), tote=("tote", "sum"))
                    cc1, cc2 = st.columns([2, 3])
                    with cc1:
                        figw = px.bar(wsum, x="wave", y="tote", title="웨이브별 토트", text="sku수", color_discrete_sequence=["#5dade2"])
                        figw.update_traces(texttemplate="SKU %{text}", textposition="outside")
                        figw.update_layout(height=300, margin=dict(l=20, r=20, t=40, b=20), xaxis_title="웨이브", yaxis_title=None)
                        st.plotly_chart(figw, width="stretch")
                    with cc2:
                        st.dataframe(dd, width="stretch", hide_index=True)

# ================================================================ 탭 3
with tab3:
    st.header("시나리오 비교")
    if not ss["dataset_id"]:
        st.info("데이터셋을 먼저 저장하세요.")
    else:
        cA, cB, cC = st.columns([1, 1, 2])
        if cA.button("기본 6개 세트 (16/24/32셀 × 15/20PCS)"):
            b = ss["baseline"]
            ss["scenarios"] = pd.DataFrame([{"시나리오": f"{c}셀·{t}PCS", "DAS 셀": c, "토트 용량": t,
                                             "주문 단위": b["order_unit"], "작업시간": float(b["hours_per_day"])} for c, t in DEFAULT_GRID])
        if cB.button("기준 시나리오 행 추가"):
            b = ss["baseline"]
            row = {"시나리오": "기준", "DAS 셀": int(b["cells"]), "토트 용량": int(b["tote_capacity"]),
                   "주문 단위": b["order_unit"], "작업시간": float(b["hours_per_day"])}
            ss["scenarios"] = pd.concat([pd.DataFrame([row]), ss["scenarios"]], ignore_index=True)
        edited = st.data_editor(
            ss["scenarios"], num_rows="dynamic", width="stretch", hide_index=True,
            column_config={
                "DAS 셀": st.column_config.NumberColumn(min_value=1, max_value=999, step=1),
                "토트 용량": st.column_config.NumberColumn(min_value=1, max_value=9999, step=1),
                "주문 단위": st.column_config.SelectboxColumn(options=list(ORDER_UNITS), required=True),
                "작업시간": st.column_config.NumberColumn(min_value=0.5, max_value=24.0, step=0.5),
            }, key="scen_editor")
        ss["scenarios"] = edited
        names = edited["시나리오"].fillna("").tolist()
        base_name = cC.selectbox("기준 시나리오", names, index=0 if names else None,
                                 help="증감은 이 행 대비로 계산합니다.")
        if st.button("계산", type="primary", disabled=edited.empty):
            scs = []
            try:
                for r in edited.itertuples(index=False):
                    sc = Scenario.from_dict({"name": str(r[0]), "cells": r[1], "tote_capacity": r[2], "order_unit": str(r[3]), "hours_per_day": r[4]})
                    scs.append(sc)
                if len(scs) > 12:
                    raise ValueError("한 번에 최대 12개 시나리오를 비교할 수 있습니다.")
                if len(set(names)) != len(names):
                    raise ValueError("시나리오 이름을 서로 다르게 입력하세요.")
            except (ValueError, TypeError) as e:
                st.error(f"시나리오 입력 오류: {e}")
                scs = []
            if scs:
                prog = st.progress(0.0, "계산 중…")
                runs = []
                try:
                    for i, sc in enumerate(scs):
                        rid, _ = storage.run_scenario(ss["dataset_id"], sc, current_filters())
                        runs.append(rid)
                        prog.progress((i + 1) / len(scs), f"{sc.name} 완료 ({i + 1}/{len(scs)})")
                    ss["compare_runs"] = runs
                    ss["compare_base"] = names.index(base_name) if base_name in names else 0
                    ss["last_export"] = None
                    ctx = ss["agent_ctx"] or AgentContext()
                    ctx.last_run_ids = runs
                    ctx.last_baseline_run_id = runs[ss["compare_base"]]
                    ctx.last_scenarios = scs
                    ctx.filters = current_filters()
                    ss["agent_ctx"] = ctx
                except ValueError as e:
                    st.error(str(e))
                prog.empty()

        if ss["compare_runs"]:
            results = [toolbox.result(r) for r in ss["compare_runs"]]
            bi = min(ss.get("compare_base", 0), len(results) - 1)
            try:
                table = compare(results, bi)
            except ValueError as e:
                st.error(str(e))
                table = None
            if table is not None:
                st.caption(f"범위 {results[0].filters.describe()} · 출고일 {results[0].n_days}일 · 실행 {len(results)}건")
                st.dataframe(table, width="stretch", hide_index=True, column_config=COMPARE_COLS)
                st.text(explain_comparison(results, bi))
                mm = pd.concat([r.monthly.assign(시나리오=r.scenario.name) for r in results])
                fig = px.line(mm, x="month", y="ol_per_tote", color="시나리오", markers=True, title="월별 OL/TOTE 비교")
                fig.update_layout(height=380, margin=dict(l=20, r=20, t=40, b=20))
                st.plotly_chart(fig, width="stretch")
                fig2 = px.bar(table, x="시나리오", y="총 토트", color="주문 단위", title="총 토트")
                fig2.update_layout(height=320, margin=dict(l=20, r=20, t=40, b=20))
                st.plotly_chart(fig2, width="stretch")

                st.subheader("엑셀 내보내기")
                pick = st.multiselect("내보낼 시나리오", [r.scenario.name for r in results],
                                      default=[r.scenario.name for r in results])
                if st.button("엑셀 생성"):
                    sel = [r for r in results if r.scenario.name in pick]
                    if not sel:
                        st.error("시나리오를 선택하세요.")
                    else:
                        base_sel = next((i for i, r in enumerate(sel) if r is results[bi]), 0)
                        out = storage.data_dir / "exports" / f"DAS_hitrate_{pd.Timestamp.now():%Y%m%d_%H%M%S}.xlsx"
                        export_excel(sel, out, dataset=storage.get_dataset(ss["dataset_id"]), baseline_index=base_sel)
                        ss["last_export"] = str(out)
                if ss.get("last_export") and Path(ss["last_export"]).exists():
                    st.download_button("다운로드: " + Path(ss["last_export"]).name, Path(ss["last_export"]).read_bytes(),
                                       file_name=Path(ss["last_export"]).name,
                                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ================================================================ 탭 4
with tab4:
    st.header("에이전트와 대화")
    st.caption("요청 해석 → 계산 도구 실행 → 결과 설명. 수치는 계산 엔진의 검증된 결과만 표시합니다.")
    st.markdown("예: `기본 6개 비교해줘` · `24셀에 토트 20PCS로 비교` · `배송처로 다시 계산` · `엑셀로 내보내줘`")
    agent = get_agent(ss["llm_pref"])
    meta = storage.get_dataset(ss["dataset_id"]) if ss["dataset_id"] else None
    ctx: AgentContext = ss["agent_ctx"] or AgentContext()
    ctx.dataset_id = ss["dataset_id"]
    ctx.baseline = current_baseline()
    if meta:
        ctx.date_min, ctx.date_max = str(meta["date_min"]), str(meta["date_max"])
        ctx.categories = storage.categories(ss["dataset_id"])
    if ss["agent_ctx"] is None:
        ctx.filters = current_filters()
    ss["agent_ctx"] = ctx

    for msg in ss["chat"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("narration"):
                st.info(msg["narration"])
            if msg.get("table") is not None:
                st.dataframe(msg["table"], width="stretch", hide_index=True, column_config=COMPARE_COLS)
            if msg.get("drill") is not None:
                st.dataframe(msg["drill"], width="stretch", hide_index=True)
            if msg.get("export") and Path(msg["export"]).exists():
                st.download_button("엑셀 다운로드", Path(msg["export"]).read_bytes(), file_name=Path(msg["export"]).name,
                                   key=f"dl_{msg['export']}_{id(msg)}")
            if msg.get("calls"):
                with st.expander(f"실행 기록 — 계획: {msg.get('plan_src')} · 도구 {len(msg['calls'])}회"):
                    for c in msg["calls"]:
                        st.markdown(f"{'✅' if c['ok'] else '❌'} `{c['tool']}` — {c['summary']}")
                        st.json(c["args"], expanded=False)

    q = st.chat_input("예: 16·24·32셀과 토트 15·20PCS를 비교해줘")
    if q:
        ss["chat"].append({"role": "user", "content": q})
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            with st.spinner("계획 → 계산 → 비교…"):
                reply = agent.handle(q, ctx)
            st.markdown(reply.text)
            if reply.narration:
                st.info(reply.narration)
            if reply.compare_table is not None:
                st.dataframe(reply.compare_table, width="stretch", hide_index=True, column_config=COMPARE_COLS)
            if reply.drilldown is not None:
                st.dataframe(reply.drilldown, width="stretch", hide_index=True)
        ss["chat"].append({
            "role": "assistant", "content": reply.text, "narration": reply.narration,
            "table": reply.compare_table, "drill": reply.drilldown, "export": reply.export_path,
            "plan_src": reply.plan.source if reply.plan else "-",
            "calls": [{"tool": c.tool, "ok": c.ok, "summary": c.summary, "args": c.args} for c in reply.tool_calls],
        })
        if reply.run_ids:
            ss["compare_runs"] = reply.run_ids
            ss["compare_base"] = reply.run_ids.index(reply.baseline_run_id) if reply.baseline_run_id in reply.run_ids else 0
            ss["last_export"] = reply.export_path
        st.rerun()
    if st.button("대화 초기화"):
        ss["chat"] = []
        ss["agent_ctx"] = None
        get_agent(ss["llm_pref"]).history.clear()
        st.rerun()
