"""설계안 9장 검증 7·8: 자연어 6개 비교, 주문 단위 변경, 잘못된 토트 용량, 모호한 요청, 미등록 데이터셋."""
import pytest

from das_agent.agent import Agent, AgentContext, RuleParser, PLAN_SCHEMA
from das_agent.engine import Scenario, Filters
from das_agent.ingestion import make_sample_file
from das_agent.llm import NullProvider
from das_agent.storage import Storage
from das_agent.tools import Toolbox, ToolError


class FakeLLM:
    """LLM 플래너를 흉내 낸다. 규칙 파서가 못 푼 요청만 받는다."""
    name = "fake"
    model = "fake-1"

    def __init__(self, plan=None):
        self._plan = plan
        self.calls = 0

    def available(self):
        return True

    def plan(self, system, user, schema):
        self.calls += 1
        assert schema is PLAN_SCHEMA
        return self._plan

    def narrate(self, system, user):
        return "AI 해설: 도구 결과 기준."


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("agent")
    xlsx = make_sample_file(tmp / "sample.xlsx", days=14)
    st = Storage(tmp / "data")
    tb = Toolbox(st)
    insp = tb.inspect_file(str(xlsx))
    reg = tb.register_dataset(str(xlsx), insp["sheet"], insp["mapping"], name="sample")
    ctx = AgentContext(dataset_id=reg["dataset_id"], baseline=Scenario("기준", 16, 15),
                       date_min=reg["date_min"], date_max=reg["date_max"], categories=st.categories(reg["dataset_id"]))
    return st, tb, ctx


def test_rule_parser_grid_and_units():
    p = RuleParser()
    ctx = AgentContext(date_min="2025-01-02", date_max="2025-12-30", categories=["의류용품"])
    plan = p.parse("16·24·32셀과 토트 15·20PCS를 비교해줘", ctx)
    assert plan.action == "compare" and len(plan.scenarios) == 6
    assert {(s["cells"], s["tote_capacity"]) for s in plan.scenarios} == {(c, t) for c in (16, 24, 32) for t in (15, 20)}
    plan = p.parse("24셀에 토트 20개면 기준보다 나아?", ctx)
    assert plan.action == "compare" and plan.scenarios == [{"name": None, "cells": 24, "tote_capacity": 20, "order_unit": None, "hours_per_day": None}]
    plan = p.parse("배송처로 묶어서 다시 계산", ctx)
    assert plan.action == "compare" and plan.scenarios[0]["order_unit"] == "배송처"
    plan = p.parse("배송처차수 기준으로 8시간 작업이면?", ctx)
    assert plan.scenarios[0]["order_unit"] == "배송처차수" and plan.scenarios[0]["hours_per_day"] == 8
    plan = p.parse("1월 의류용품만 계산해줘", ctx)
    assert plan.filters == {"start": "2025-01-01", "end": "2025-01-31", "category": "의류용품"}
    plan = p.parse("결과를 엑셀로 내보내줘", ctx)
    assert plan.action == "export"
    plan = p.parse("2025-01-02 웨이브별 상세 보여줘", ctx)
    assert plan.action == "drilldown" and plan.date == "2025-01-02"
    plan = p.parse("가장 좋은 설정이 뭐야?", ctx)
    assert plan.action == "clarify"
    assert p.parse("오늘 날씨 어때", ctx) is None


def test_agent_six_scenarios_and_followups(env):
    st, tb, ctx = env
    ag = Agent(tb, NullProvider())
    r = ag.handle("16·24·32셀 × 토트 15·20PCS 비교", ctx)
    assert r.plan.source == "rules"
    assert [c.tool for c in r.tool_calls] == ["simulate_scenarios", "compare_results"]
    assert all(c.ok for c in r.tool_calls)
    assert len(r.run_ids) == 6                       # 기준(16·15)이 격자에 포함되므로 추가 없음
    assert r.compare_table.iloc[0]["기준"] == "●"
    assert "기준 '기준'" in r.text
    assert r.narration is None                       # LLM 없음

    # 후속: 직전 6개 조합을 유지하고 주문 단위만 변경 + 원래 기준
    r2 = ag.handle("배송처로 묶어서 다시 계산", ctx)
    assert len(r2.run_ids) == 7
    assert (r2.compare_table.iloc[1:]["주문 단위"] == "배송처").all()
    assert ctx.last_run_ids == r2.run_ids

    # 후속: 엑셀
    r3 = ag.handle("엑셀로 내보내줘", ctx)
    assert r3.export_path and r3.export_path.endswith(".xlsx")
    assert r3.tool_calls[-1].tool == "export_report" and r3.tool_calls[-1].ok

    # 필터 적용 재계산 (샘플 기간 안의 월)
    month = ctx.date_min[:7]
    r4 = ag.handle(f"{int(month[5:])}월만 계산해줘", ctx)
    assert r4.tool_calls[0].ok and ctx.filters.start == f"{month}-01"

    # 드릴다운
    r5 = ag.handle(f"{ctx.date_min} 웨이브별 상세", ctx)
    assert r5.drilldown is not None and r5.drilldown["tote"].sum() > 0


def test_agent_rejects_bad_inputs(env):
    st, tb, ctx = env
    ag = Agent(tb, NullProvider())
    r = ag.handle("토트 용량 0으로 계산", ctx)
    assert r.needs_input and not r.tool_calls[0].ok and "토트 용량 0" in r.tool_calls[0].summary
    r = ag.handle("5000셀이면?", ctx)
    assert r.needs_input and "범위" in r.text
    r = ag.handle("가장 효율적인 설정 찾아줘", ctx)
    assert r.needs_input and r.plan.action == "clarify" and not r.tool_calls
    r = ag.handle("오늘 날씨 어때", ctx)
    assert r.needs_input and r.plan.source == "fallback"

    # 미등록 데이터셋: 수치 생성 금지
    bad_ctx = AgentContext(dataset_id=None, baseline=Scenario("기준", 16, 15))
    r = ag.handle("24셀 20PCS 비교", bad_ctx)
    assert r.needs_input and not r.tool_calls and "데이터셋" in r.text
    with pytest.raises(ToolError):
        tb.simulate_scenarios("ds_없음", [{"cells": 16, "tote_capacity": 15}])


def test_agent_llm_plan_path(env):
    st, tb, ctx = env
    fake = FakeLLM(plan={"action": "compare",
                         "scenarios": [{"name": "큰 셀", "cells": 48, "tote_capacity": None, "order_unit": None, "hours_per_day": None}],
                         "include_baseline": True, "filters": {"start": None, "end": None, "category": None},
                         "date": None, "message": "48셀 비교"})
    ag = Agent(tb, fake)
    r = ag.handle("셀을 세 배로 늘리면 어떻게 돼?", ctx)   # 규칙 파서 불가 → LLM
    assert fake.calls == 1 and r.plan.source == "llm"
    assert len(r.run_ids) == 2 and r.compare_table.iloc[1]["DAS 셀"] == 48
    assert r.compare_table.iloc[1]["토트 용량"] == 15      # 생략값은 현재 설정 계승
    assert r.narration is None  # 숫자 설명은 도구 결과로만 생성

    bad = FakeLLM(plan={"action": "compare", "scenarios": [{"name": None, "cells": 1, "tote_capacity": 1, "order_unit": "SKU",
                                                              "hours_per_day": None}], "include_baseline": True,
                        "filters": {"start": None, "end": None, "category": None}, "date": None, "message": ""})
    ag2 = Agent(tb, bad)
    r = ag2.handle("뭔가 해봐", ctx)
    assert bad.calls == 2 and r.plan.action == "clarify"   # 1회 수정 요청 후 포기


def test_compare_rejects_mixed_datasets(env):
    st, tb, ctx = env
    import pandas as pd
    from das_agent.engine import golden_example
    g = golden_example(); g.insert(0, "src_row", range(2, 6))
    other = st.register_dataset(g, name="g", file_name="g", file_hash="h2", sheet="data", mapping={}, validation={})
    a = tb.simulate_scenarios(ctx.dataset_id, [{"cells": 16, "tote_capacity": 15}])["runs"][0]["run_id"]
    b = tb.simulate_scenarios(other, [{"cells": 2, "tote_capacity": 15}])["runs"][0]["run_id"]
    with pytest.raises(ToolError):
        tb.compare_results([a, b])


def test_rule_parser_defers_relative_requests_to_llm():
    ctx = AgentContext(baseline=Scenario("기준", 16, 15))
    rp = RuleParser()
    for text in ["셀을 지금의 절반으로 줄이고 토트는 25개로 하면 기준 대비 어떻게 되는지 봐줘",
                 "토트 용량을 지금의 두 배로 늘려서 비교해줘", "셀을 50% 늘리면?"]:
        assert rp.parse(text, ctx) is None, text
    # 절대값 요청은 여전히 규칙 파서가 처리한다
    assert rp.parse("16·24·32셀과 토트 15·20PCS를 비교하고, 기준보다 토트가 얼마나 줄어드는지 보고서로 만들어줘", ctx) is not None
