"""'기본 6개' 세트 요청이 엑셀 내보내기와 결합돼도 계산을 먼저 수행한다."""
from das_agent.agent import AgentContext, RuleParser


def test_default_set_with_export_is_compare_then_export():
    p = RuleParser()
    ctx = AgentContext(date_min="2025-01-02", date_max="2025-12-31")
    plan = p.parse("기본 6개 비교하고 엑셀로 내보내줘", ctx)
    assert plan.action == "compare+export"
    assert len(plan.scenarios) == 6
    plan = p.parse("기본 세트 돌려서 보고서로 뽑아줘", ctx)
    assert plan.action == "compare+export"
    plan = p.parse("기본 6개 비교해줘", ctx)
    assert plan.action == "compare" and len(plan.scenarios) == 6
    plan = p.parse("엑셀로 내보내줘", ctx)
    assert plan.action == "export"
