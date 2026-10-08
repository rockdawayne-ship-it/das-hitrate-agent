"""운영 분석 에이전트 — 자연어 요청 → 계획(JSON) → 검증 → 도구 실행 → 근거 설명 (설계안 4·8장).

계획은 두 단계로 만든다.
1. 규칙 파서(RuleParser): "16·24·32셀 × 토트 15·20" 같은 정형 요청은 모델 없이 해석한다.
2. 언어모델 플래너: 규칙 파서가 확신하지 못하면 LLM에 JSON 계획을 요청한다(없으면 되묻는다).

숫자는 항상 계산 엔진·도구 결과에서 나온다. 모델 출력은 계획과 해설에만 쓴다.
"""
from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd

from .engine import Scenario, Filters, ORDER_UNITS
from .llm import LLMProvider, NullProvider, get_provider
from .tools import Toolbox, ToolError, MAX_SCENARIOS

ACTIONS = ("compare", "compare+export", "export", "drilldown", "explain", "clarify", "chat")

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "cells": {"type": ["integer", "null"]},
                    "tote_capacity": {"type": ["integer", "null"]},
                    "order_unit": {"type": ["string", "null"], "enum": list(ORDER_UNITS) + [None]},
                    "hours_per_day": {"type": ["number", "null"]},
                },
                "required": ["name", "cells", "tote_capacity", "order_unit", "hours_per_day"],
                "additionalProperties": False,
            },
        },
        "include_baseline": {"type": "boolean"},
        "clear_filters": {"type": "array", "items": {"type": "string", "enum": ["start", "end", "category"]}},
        "filters": {
            "type": "object",
            "properties": {
                "start": {"type": ["string", "null"]},
                "end": {"type": ["string", "null"]},
                "category": {"type": ["string", "null"]},
            },
            "required": ["start", "end", "category"],
            "additionalProperties": False,
        },
        "date": {"type": ["string", "null"]},
        "message": {"type": "string"},
    },
    "required": ["action", "scenarios", "include_baseline", "filters", "date", "message"],
    "additionalProperties": False,
}

PLANNER_SYSTEM = """너는 DAS(분배 설비) 히트율 시뮬레이터의 요청 해석기다. 사용자의 한국어 요청을 JSON 계획으로만 변환한다.
규칙:
- 계산·숫자 추정 금지. 계획만 만든다. 숫자는 계산 도구가 낸다.
- action: compare(시나리오 계산·비교), export(엑셀 내보내기), drilldown(특정 날짜 웨이브·SKU 상세), explain(직전 결과 설명), clarify(정보 부족으로 되묻기), chat(기능과 무관한 일반 질문).
- scenarios: 사용자가 말한 DAS 셀 수(cells)와 토트 용량(tote_capacity) 조합을 모두 나열한다. 예: "16·24·32셀 × 15·20PCS" → 6개.
  생략된 값은 null로 둔다(현재 설정을 계승한다). order_unit은 "배송처차수" 또는 "배송처"만 가능. "배송처로 묶어"=배송처.
- include_baseline: 기준과 비교해야 하면 true.
- filters: 기간(start,end는 YYYY-MM-DD)·검토기준. 없으면 null. 현재 데이터 기간은 사용자 메시지에 주어진다.
- 데이터 기간과 검토기준 목록은 메타데이터일 뿐, 사용자가 요청한 필터가 아니다. 셀/용량만 바꾸는 요청에서는 filters의 모든 값을 null로 둔다.
- clear_filters: 명시적으로 해제하라는 필터만 ["start","end","category"] 중 선택. 생략은 현재 값 유지.
- "더 좋은 설정"처럼 목적이 불명확하면 action=clarify, message에 확인 질문(예: 토트 최소? OL/TOTE 최대?)을 쓴다.
- message: 사용자에게 보여줄 한 줄 요약."""

NARRATE_SYSTEM = """너는 물류 DAS 운영 분석가다. 아래 '도구 결과'에 있는 숫자만 사용해 한국어로 3~6문장 해설을 쓴다.
- 도구 결과에 없는 숫자·사실을 만들지 않는다. 비용·인원·설비 투자는 이 계산에 없으므로 결론으로 단정하지 않는다.
- "토트가 가장 적은 설정"과 "최적 설비"를 구분한다. 동률이면 동률이라고 쓴다.
- 과장 없이 실무 보고 문체. 마크다운 표는 쓰지 않는다."""


# ---------------------------------------------------------------- 계획
@dataclass
class Plan:
    action: str = "clarify"
    scenarios: list[dict] = field(default_factory=list)
    include_baseline: bool = True
    filters: dict = field(default_factory=lambda: {"start": None, "end": None, "category": None})
    date: Optional[str] = None
    message: str = ""
    source: str = "rules"   # rules | llm | fallback
    clear_filters: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgentContext:
    dataset_id: Optional[str] = None
    baseline: Scenario = field(default_factory=Scenario)
    filters: Filters = field(default_factory=Filters)
    date_min: Optional[str] = None
    date_max: Optional[str] = None
    categories: list[str] = field(default_factory=list)
    last_run_ids: list[str] = field(default_factory=list)
    last_baseline_run_id: Optional[str] = None
    last_scenarios: list[Scenario] = field(default_factory=list)


@dataclass
class ToolCall:
    tool: str
    args: dict
    ok: bool
    summary: str
    result: Optional[dict] = None


@dataclass
class AgentReply:
    text: str
    plan: Optional[Plan]
    tool_calls: list[ToolCall]
    run_ids: list[str] = field(default_factory=list)
    baseline_run_id: Optional[str] = None
    compare_table: Optional[pd.DataFrame] = None
    export_path: Optional[str] = None
    drilldown: Optional[pd.DataFrame] = None
    narration: Optional[str] = None
    needs_input: bool = False


# ---------------------------------------------------------------- 규칙 파서
_NUM = r"-?\d+(?:\.\d+)?"
_NUMLIST = rf"({_NUM}(?:\s*(?:[·,/、]|와|과|및|,\s*그리고|랑|이랑|x|X|×)\s*{_NUM})*)"


def _ints(s: str) -> list[int]:
    return [float(x) if "." in x else int(x) for x in re.findall(_NUM, s)]


class RuleParser:
    """정형 요청을 모델 없이 계획으로 바꾼다. 확신이 없으면 None."""

    EXPORT = re.compile(r"엑셀|xlsx|excel|내보내|보고서|리포트|export|다운로드", re.I)
    DRILL = re.compile(r"상세|드릴|웨이브별|sku별|어떤 웨이브|웨이브 구성", re.I)
    COMPARE = re.compile(r"비교|대비|나아|나을|좋아|좋을|어때|얼마나|계산|시뮬|돌려|하면|이면|라면|바꾸|줄어|늘어|변경|해줘", re.I)
    EXPLAIN = re.compile(r"설명|왜|이유|해석|요약해", re.I)
    AMBIGUOUS_BEST = re.compile(r"(가장|제일|최적|최고|best)\s*(좋|나은|효율|설정|옵션|구성)|최적", re.I)
    HELP = re.compile(r"도움|사용법|뭘 할 수|무엇을 할 수|help|기능", re.I)
    DEFAULT_SET = re.compile(r"기본\s*(6|여섯)\s*개|기본\s*(세트|비교)")
    # 상대 표현(절반·두 배·%)은 현재 설정 기준 계산이 필요하므로 규칙 파서가 숫자만 떼어 쓰지 않고 언어모델에 넘긴다.
    RELATIVE = re.compile(r"절반|반으로|[두세네]\s*배|\d+\s*배|배로|%|퍼센트|씩", re.I)

    def parse(self, text: str, ctx: AgentContext) -> Optional[Plan]:
        t = text.strip()
        if self.RELATIVE.search(t) and not self.EXPORT.search(t):
            return None
        low = t.lower()
        plan = Plan(source="rules")

        # 1) 셀 수 / 토트 용량 / 주문 단위 / 작업시간
        cells: list[int] = []
        for m in re.finditer(_NUMLIST + r"\s*(?:개\s*)?(?:셀|cell|칸)", t, re.I):
            cells += _ints(m.group(1))
        rest = re.sub(_NUMLIST + r"\s*(?:개\s*)?(?:셀|cell|칸)", " ", t, flags=re.I)
        totes: list[int] = []
        for m in re.finditer(_NUMLIST + r"\s*(?:pcs|개|피스)?\s*(?:짜리\s*)?(?:토트|tote)", rest, re.I):
            totes += _ints(m.group(1))
        for m in re.finditer(r"(?:토트|tote)\s*(?:용량|사이즈|크기)?\s*(?:을|를|은|는|이|가)?\s*" + _NUMLIST + r"\s*(?:pcs|개|피스)?", rest, re.I):
            totes += _ints(m.group(1))
        if not totes:
            for m in re.finditer(_NUMLIST + r"\s*(?:pcs|피스)", rest, re.I):
                totes += _ints(m.group(1))
        cells = sorted(set(cells))
        totes = sorted(set(totes))

        unit: Optional[str] = None
        if re.search(r"배송처\s*차수", t):
            unit = "배송처차수"
        elif re.search(r"배송처(?:로|별|\s*단위|\s*기준|만)", t):
            unit = "배송처"

        hours: Optional[float] = None
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*시간", t)
        if m:
            hours = float(m.group(1))

        # 2) 필터
        plan.filters = self._filters(t, ctx)
        if re.search(r"전체\s*기간|전기간", t):
            plan.clear_filters = ["start", "end"]
        if re.search(r"필터\s*(해제|없이)", t):
            plan.clear_filters = ["start", "end", "category"]
        if re.search(r"전체\s*(검토기준|상품그룹)", t):
            plan.clear_filters.append("category")

        # 3) 행동
        if self.HELP.search(low) and not cells and not totes:
            plan.action = "chat"
            plan.message = "help"
            return plan
        if self.EXPORT.search(low) and not (cells or totes or unit or hours is not None or any(plan.filters.values())
                                            or plan.clear_filters or self.DEFAULT_SET.search(t)):
            plan.action = "export"
            plan.message = "직전 결과를 엑셀로 내보냅니다."
            return plan
        date = self._date(t, ctx)
        if self.DRILL.search(low) and date:
            plan.action = "drilldown"
            plan.date = date
            plan.scenarios = [self._sc(cells[:1], totes[:1], unit, hours)] if (cells or totes or unit or hours) else []
            plan.message = f"{date} 웨이브·SKU별 토트 상세"
            return plan
        if self.AMBIGUOUS_BEST.search(low) and not (cells or totes):
            plan.action = "clarify"
            plan.message = ("'더 좋은 설정'의 기준을 정해 주세요. 총 토트 최소? OL/TOTE 최대? "
                            "그리고 비교할 DAS 셀 수·토트 용량 후보를 알려 주세요 (예: 16·24·32셀 × 15·20PCS).")
            return plan
        if cells or totes or unit or hours is not None:
            plan.action = "compare"
            plan.scenarios = self._grid(cells, totes, unit, hours)
            plan.include_baseline = True
            if self.EXPORT.search(low):
                plan.message = "시나리오 계산 → 비교 → 엑셀 내보내기"
                plan.action = "compare+export"
            else:
                plan.message = f"시나리오 {len(plan.scenarios)}개 계산·비교"
            return plan
        if self.EXPLAIN.search(low) and ctx.last_run_ids:
            plan.action = "explain"
            plan.message = "직전 비교 결과 설명"
            return plan
        if any(plan.filters.values()) or plan.clear_filters:
            plan.action = "compare"
            if self.EXPORT.search(low):
                plan.action = "compare+export"
            plan.scenarios = []
            plan.message = "현재 설정으로 필터 적용 재계산"
            return plan
        if self.DEFAULT_SET.search(t):
            action = "compare+export" if self.EXPORT.search(low) else "compare"
            return Plan(action=action, scenarios=self._grid([16, 24, 32], [15, 20], None, None),
                        message="기본 6개 세트 계산·비교" + (" → 엑셀 내보내기" if action != "compare" else ""))
        if re.search(r"다시\s*계산|현재\s*(설정|조건).*계산", t):
            return Plan(action="compare")
        return None

    @staticmethod
    def _sc(cells, totes, unit, hours) -> dict:
        return {"name": None, "cells": cells[0] if cells else None, "tote_capacity": totes[0] if totes else None,
                "order_unit": unit, "hours_per_day": hours}

    @staticmethod
    def _grid(cells, totes, unit, hours) -> list[dict]:
        cs = cells or [None]
        ts = totes or [None]
        return [{"name": None, "cells": c, "tote_capacity": tt, "order_unit": unit, "hours_per_day": hours}
                for c in cs for tt in ts]

    @staticmethod
    def _filters(t: str, ctx: AgentContext) -> dict:
        f = {"start": None, "end": None, "category": None}
        if re.search(r"전체\s*기간|전기간|필터\s*(해제|없이)", t):
            return f
        year_hint = int(ctx.date_min[:4]) if ctx.date_min else None
        # YYYY-MM-DD ~ YYYY-MM-DD
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s*(?:~|부터|에서|-)\s*(\d{4}-\d{2}-\d{2})", t)
        if m:
            f["start"], f["end"] = m.group(1), m.group(2)
        else:
            # N월 ~ M월 / N월
            months = re.findall(r"(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월(?!\s*\d{1,2}\s*일)", t)
            if months:
                y0 = int(months[0][0]) if months[0][0] else year_hint
                if y0:
                    m0 = int(months[0][1])
                    y1 = int(months[-1][0]) if months[-1][0] else y0
                    m1 = int(months[-1][1])
                    if 1 <= m0 <= 12 and 1 <= m1 <= 12:
                        f["start"] = f"{y0:04d}-{m0:02d}-01"
                        f["end"] = f"{y1:04d}-{m1:02d}-{calendar.monthrange(y1, m1)[1]:02d}"
            else:
                m = re.search(r"(\d{4})[-./](\d{1,2})(?![-./\d])", t)
                if m:
                    y, mo = int(m.group(1)), int(m.group(2))
                    if not 1 <= mo <= 12:
                        raise ValueError("월은 1~12 범위여야 합니다.")
                    f["start"] = f"{y:04d}-{mo:02d}-01"
                    f["end"] = f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
        for c in ctx.categories:
            if c and c != "미분류" and c in t:
                f["category"] = c
        return f

    @staticmethod
    def _date(t: str, ctx: AgentContext) -> Optional[str]:
        m = re.search(r"(\d{4})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})\s*일?", t)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", t)
        if m and ctx.date_min:
            return f"{ctx.date_min[:4]}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        return None


# ---------------------------------------------------------------- 에이전트
class Agent:
    def __init__(self, toolbox: Toolbox, provider: Optional[LLMProvider] = None, max_tool_calls: int = 6):
        self.tb = toolbox
        self.provider = provider or get_provider()
        self.parser = RuleParser()
        self.max_tool_calls = max_tool_calls
        self.history: list[dict] = []   # {"role","content"} 화면 표시용

    # ---------------- 계획
    def make_plan(self, text: str, ctx: AgentContext) -> Plan:
        plan = self.parser.parse(text, ctx)
        if plan is not None:
            return plan
        if self.provider.available():
            llm_plan = self._llm_plan(text, ctx)
            if llm_plan is not None:
                return llm_plan
        return Plan(action="clarify", source="fallback",
                    message=("요청을 해석하지 못했습니다. 예: '16·24·32셀과 토트 15·20PCS를 비교해줘', "
                             "'배송처로 묶어서 다시 계산', '1월만 계산', '결과를 엑셀로 내보내줘', '2025-01-02 웨이브별 상세'."
                             + ("" if self.provider.available() else f" (언어모델 미사용: {getattr(self.provider, 'reason', '')})")))

    def _llm_plan(self, text: str, ctx: AgentContext) -> Optional[Plan]:
        user = (
            f"현재 설정: DAS {ctx.baseline.cells}셀, 토트 {ctx.baseline.tote_capacity}PCS, 주문 단위 {ctx.baseline.order_unit}, "
            f"작업시간 {ctx.baseline.hours_per_day:g}h. 데이터 기간 {ctx.date_min}~{ctx.date_max}. "
            f"검토기준 값: {ctx.categories}. 직전 실행 {len(ctx.last_run_ids)}건.\n\n사용자 요청: {text}"
        )
        for attempt in range(2):
            try:
                raw = self.provider.plan(PLANNER_SYSTEM, user, PLAN_SCHEMA)
            except Exception as e:
                return Plan(action="clarify", source="llm", message=f"언어모델 호출 실패: {e}")
            if not raw:
                return None
            try:
                # Metadata must never become an implicit user filter.
                if not isinstance(raw, dict):
                    raise ValueError("계획은 객체여야 합니다.")
                raw = dict(raw)
                raw_filters = raw.get("filters") or {}
                if not isinstance(raw_filters, dict):
                    raise ValueError("filters는 객체여야 합니다.")
                raw["filters"] = dict(raw_filters)
                if not re.search(r"\d{4}[-./]|기간|날짜|\d+\s*[년월일]|이번\s*(달|주)|지난\s*(달|주)|마지막\s*(날|주)|분기", text):
                    raw["filters"]["start"] = raw["filters"]["end"] = None
                if not any(c in text for c in ctx.categories) and not re.search(r"검토기준|상품그룹", text):
                    raw["filters"]["category"] = None
                if not re.search(r"전체|전기간|필터\s*해제|필터\s*없이", text):
                    raw["clear_filters"] = []
                if raw.get("action") != "drilldown":
                    raw["date"] = None
                plan = self._validate_llm_plan(raw)
                for scenario in plan.scenarios:
                    scenario["name"] = None  # Labels must describe the actual validated settings.
                return plan
            except ValueError as e:
                user += f"\n\n이전 계획 오류: {e}. 수정한 계획을 다시 만들어라."
        return None

    @staticmethod
    def _validate_llm_plan(raw: dict) -> Plan:
        if not isinstance(raw, dict):
            raise ValueError("계획은 JSON 객체여야 합니다.")
        action = raw.get("action")
        if action not in ACTIONS:
            raise ValueError(f"action 값이 잘못됨: {action}")
        scs = raw.get("scenarios") or []
        if not isinstance(scs, list):
            raise ValueError("scenarios는 배열이어야 합니다.")
        if len(scs) > MAX_SCENARIOS:
            raise ValueError(f"시나리오 {len(scs)}개는 상한 {MAX_SCENARIOS} 초과")
        clean = []
        for s in scs:
            if not isinstance(s, dict):
                raise ValueError("scenarios 항목은 객체여야 함")
            ou = s.get("order_unit")
            if ou not in (None, *ORDER_UNITS):
                raise ValueError(f"order_unit 잘못됨: {ou}")
            clean.append({"name": s.get("name"), "cells": s.get("cells"), "tote_capacity": s.get("tote_capacity"),
                          "order_unit": ou, "hours_per_day": s.get("hours_per_day")})
        f = raw.get("filters") or {}
        if not isinstance(f, dict):
            raise ValueError("filters는 객체여야 합니다.")
        filters = {"start": f.get("start"), "end": f.get("end"), "category": f.get("category")}
        for k in ("start", "end"):
            if filters[k] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(filters[k])):
                raise ValueError(f"filters.{k}는 YYYY-MM-DD 형식이어야 함: {filters[k]}")
        Filters(**filters).validate()
        clear = raw.get("clear_filters", [])
        if not isinstance(clear, list) or any(x not in ("start", "end", "category") for x in clear):
            raise ValueError("해제할 필터가 잘못되었습니다.")
        return Plan(action=action, scenarios=clean, include_baseline=bool(raw.get("include_baseline", True)),
                    filters=filters, date=raw.get("date"), message=str(raw.get("message", "")), source="llm", clear_filters=clear)

    # ---------------- 실행
    def handle(self, text: str, ctx: AgentContext, progress=None) -> AgentReply:
        self.history.append({"role": "user", "content": text})
        try:
            plan = self.make_plan(text, ctx)
        except (ValueError, TypeError, OverflowError) as e:
            plan = Plan(action="clarify", message=f"요청 값을 확인해주세요: {e}")
        needed_calls = 3 if plan.action == "compare+export" else 2 if plan.action == "compare" else 1
        if self.max_tool_calls < needed_calls:
            return AgentReply(text="이 요청에 필요한 도구 호출 수가 실행 한도를 초과합니다.", plan=plan, tool_calls=[], needs_input=True)
        reply = self._execute(plan, ctx, progress)
        reply.plan = plan
        self.history.append({"role": "assistant", "content": reply.text})
        return reply

    def _execute(self, plan: Plan, ctx: AgentContext, progress=None) -> AgentReply:
        calls: list[ToolCall] = []
        act = plan.action
        if act == "chat":
            return AgentReply(text=self._help_text(ctx), plan=plan, tool_calls=calls)
        if act == "clarify":
            return AgentReply(text=plan.message, plan=plan, tool_calls=calls, needs_input=True)
        if ctx.dataset_id is None:
            return AgentReply(text="먼저 '데이터 준비' 탭에서 출고 파일을 올려 저장하세요. 등록된 데이터셋이 없습니다.",
                              plan=plan, tool_calls=calls, needs_input=True)

        if act == "explain":
            if not ctx.last_run_ids:
                return AgentReply(text="설명할 직전 결과가 없습니다. 먼저 시나리오를 계산하세요.", plan=plan, tool_calls=calls, needs_input=True)
            return self._compare_and_reply(ctx.last_run_ids, ctx.last_baseline_run_id, ctx, calls, plan)

        if act == "export":
            if not ctx.last_run_ids:
                return AgentReply(text="내보낼 결과가 없습니다. 먼저 시나리오를 계산하세요.", plan=plan, tool_calls=calls, needs_input=True)
            return self._export(ctx.last_run_ids, ctx.last_baseline_run_id, calls, plan)

        if act == "drilldown":
            sc = (plan.scenarios[0] if plan.scenarios else {})
            args = {"dataset_id": ctx.dataset_id, "scenario": {**ctx.baseline.to_dict(), **{k: v for k, v in sc.items() if v}},
                    "date": plan.date, "filters": ctx.filters.to_dict()}
            try:
                res = self.tb.drilldown(**args)
                calls.append(ToolCall("drilldown", args, True, f"{plan.date} 토트 {res['tote']}", res))
                df = pd.DataFrame(res["rows"])
                txt = (f"{plan.date} · {res['scenario']['cells']}셀 · {res['scenario']['tote_capacity']}PCS · {res['scenario']['order_unit']}: "
                       f"웨이브 {df['wave'].nunique() if len(df) else 0}개, 토트 {res['tote']}.")
                if not len(df):
                    txt = f"{plan.date}에는 출고 데이터가 없습니다."
                return AgentReply(text=txt, plan=plan, tool_calls=calls, drilldown=df)
            except (ToolError, ValueError, TypeError) as e:
                calls.append(ToolCall("drilldown", args, False, str(e)))
                return AgentReply(text=f"상세 조회 실패: {e}", plan=plan, tool_calls=calls, needs_input=True)

        # compare / compare+export
        scenarios = [dict(s) for s in plan.scenarios]
        # An order-unit/hour/filter follow-up patches the previous comparison grid.
        if ctx.last_scenarios and (not scenarios or (len(scenarios) == 1 and scenarios[0].get("cells") is None and scenarios[0].get("tote_capacity") is None)):
            patch = scenarios[0] if scenarios else {}
            scenarios = [{**s.to_dict(), **{k: v for k, v in patch.items() if v is not None}, "name": None}
                         for s in ctx.last_scenarios]
        base_dict = ctx.baseline.to_dict()
        base_dict["name"] = ctx.baseline.name or "기준"
        base_key = ctx.baseline.key()
        for s in scenarios:   # 생략값(None)은 현재 설정 계승. 0 같은 잘못된 값은 그대로 두어 검증에서 걸리게 한다.
            for k, dv in (("cells", ctx.baseline.cells), ("tote_capacity", ctx.baseline.tote_capacity),
                          ("order_unit", ctx.baseline.order_unit), ("hours_per_day", ctx.baseline.hours_per_day)):
                if s.get(k) is None:
                    s[k] = dv
            try:
                if not s.get("name") and Scenario.from_dict(s).key() == base_key:
                    s["name"] = base_dict["name"]
            except (TypeError, ValueError):
                pass
        if not scenarios:
            scenarios = [dict(base_dict)]
        # 필터: 계획에 있으면 적용, 없으면 현재 필터 계승
        filters = {k: (plan.filters.get(k) if plan.filters.get(k) else getattr(ctx.filters, k)) for k in ("start", "end", "category")}
        for k in plan.clear_filters:
            filters[k] = None
        if plan.include_baseline:
            def _k(s):
                try:
                    return Scenario.from_dict(s).key()
                except (TypeError, ValueError):
                    return None
            if not any(_k(s) == base_key for s in scenarios):
                scenarios = [dict(base_dict)] + scenarios
        args = {"dataset_id": ctx.dataset_id, "scenarios": scenarios, "filters": filters}
        try:
            sim = self.tb.simulate_scenarios(**args, defaults=ctx.baseline, progress=progress)
        except ToolError as e:
            calls.append(ToolCall("simulate_scenarios", args, False, str(e)))
            return AgentReply(text=f"계산할 수 없습니다: {e}", plan=plan, tool_calls=calls, needs_input=True)
        run_ids = [r["run_id"] for r in sim["runs"]]
        calls.append(ToolCall("simulate_scenarios", args, True, f"{len(run_ids)}개 시나리오 계산 (필터 {Filters(**filters).describe()})", sim))
        base_run = next((r["run_id"] for r in sim["runs"] if Scenario.from_dict(r["scenario"]).key() == base_key), run_ids[0])
        ctx.last_run_ids = run_ids
        ctx.last_baseline_run_id = base_run
        ctx.last_scenarios = [Scenario.from_dict(r["scenario"]) for r in sim["runs"]]
        ctx.filters = Filters(**filters)
        reply = self._compare_and_reply(run_ids, base_run, ctx, calls, plan)
        if plan.action == "compare+export":
            exp = self._export(run_ids, base_run, calls, plan)
            reply.export_path = exp.export_path
            reply.text += f"\n\n{exp.text}"
            reply.needs_input = exp.needs_input
        return reply

    def _compare_and_reply(self, run_ids, base_run, ctx, calls, plan) -> AgentReply:
        args = {"run_ids": run_ids, "baseline_run_id": base_run}
        try:
            cmp_ = self.tb.compare_results(**args)
        except ToolError as e:
            calls.append(ToolCall("compare_results", args, False, str(e)))
            return AgentReply(text=f"비교 실패: {e}", plan=plan, tool_calls=calls, run_ids=run_ids, needs_input=True)
        calls.append(ToolCall("compare_results", args, True, f"최소 토트 {cmp_['min_tote']:,}: {cmp_['winners']}", cmp_))
        text = cmp_["explanation"]
        # Numeric explanation is rendered exclusively from verified tool results.
        narration = None
        return AgentReply(text=text, plan=plan, tool_calls=calls, run_ids=run_ids, baseline_run_id=base_run,
                          compare_table=pd.DataFrame(cmp_["table"]), narration=narration)

    def _export(self, run_ids, base_run, calls, plan) -> AgentReply:
        args = {"run_ids": run_ids, "baseline_run_id": base_run}
        try:
            exp = self.tb.export_report(**args)
        except ToolError as e:
            calls.append(ToolCall("export_report", args, False, str(e)))
            return AgentReply(text=f"내보내기 실패: {e}", plan=plan, tool_calls=calls, needs_input=True)
        calls.append(ToolCall("export_report", args, True, exp["path"], exp))
        return AgentReply(text=f"엑셀 저장: {exp['path']} (시트 {exp['sheets']}개)", plan=plan, tool_calls=calls,
                          run_ids=run_ids, baseline_run_id=base_run, export_path=exp["path"])

    def _narrate(self, cmp_: dict) -> Optional[str]:
        try:
            payload = {"table": cmp_["table"], "winners": cmp_["winners"], "min_tote": cmp_["min_tote"]}
            return self.provider.narrate(NARRATE_SYSTEM, "도구 결과:\n" + json.dumps(payload, ensure_ascii=False, default=str))
        except Exception as e:
            return f"(AI 해설 생략: {e})"

    def _help_text(self, ctx: AgentContext) -> str:
        return (
            "할 수 있는 요청 예시:\n"
            "- `16·24·32셀과 토트 15·20PCS를 비교해줘` → 6개 시나리오 + 기준 비교\n"
            "- `24셀에 토트 20개면 기준보다 나아?`\n"
            "- `배송처로 묶어서 다시 계산` (주문 단위 변경)\n"
            "- `1월만 계산`, `2025-01-01 ~ 2025-03-31 기간으로`\n"
            "- `결과를 엑셀로 내보내줘`\n"
            "- `2025-01-02 웨이브별 상세`\n"
            f"\n현재 설정: {ctx.baseline.cells}셀 · {ctx.baseline.tote_capacity}PCS · {ctx.baseline.order_unit} · {ctx.baseline.hours_per_day:g}h, "
            f"필터 {ctx.filters.describe()}. 언어모델: {self.provider.name}"
            + (f" ({self.provider.model})" if self.provider.available() else f" 미사용 — {getattr(self.provider, 'reason', '')}")
        )
