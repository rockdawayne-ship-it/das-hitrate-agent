"""ClaudeProvider — Claude 구독(Claude Code 로그인) 경로.

CLI 호출은 가짜 subprocess로 검증한다. 실제 호출 검증은 DAS_TEST_CLAUDE=1 일 때만 실행한다.
"""
import json
import os
import subprocess

import pytest

from das_agent.agent import PLAN_SCHEMA, PLANNER_SYSTEM
from das_agent.llm import ClaudeProvider, NullProvider, get_provider


class FakeRun:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        out = self.responses.pop(0)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out, ensure_ascii=False), stderr="")


def test_available_requires_login(monkeypatch):
    fake = FakeRun([{"loggedIn": False}])
    monkeypatch.setattr(subprocess, "run", fake)
    p = ClaudeProvider(exe="claude-fake")
    assert p.available() is False
    assert "claude auth login" in p.reason


def test_plan_uses_lean_headless_flags_and_structured_output(monkeypatch):
    fake = FakeRun([
        {"loggedIn": True, "authMethod": "claude.ai", "email": "x@y"},
        {"is_error": False, "structured_output": {"action": "compare", "scenarios": [{"cells": 24, "tote_capacity": 20}], "message": "ok"}},
    ])
    monkeypatch.setattr(subprocess, "run", fake)
    p = ClaudeProvider(exe="claude-fake", model="sonnet")
    assert p.available() and p.account == "claude.ai x@y"
    plan = p.plan(PLANNER_SYSTEM, "24셀 20PCS 비교", PLAN_SCHEMA)
    assert plan["action"] == "compare" and plan["scenarios"][0]["cells"] == 24
    cmd, kw = fake.calls[1]
    assert cmd[:2] == ["claude-fake", "-p"]
    for flag in ("--json-schema", "--strict-mcp-config", "--setting-sources", "--tools", "--max-turns", "--system-prompt"):
        assert flag in cmd
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--max-turns") + 1] == "1"
    assert cmd[-1] == "24셀 20PCS 비교" and cmd[cmd.index("--system-prompt") + 1] == PLANNER_SYSTEM
    assert kw["cwd"] and "das_claude_" in kw["cwd"]          # 프로젝트 CLAUDE.md 자동 탐색 차단
    assert "ANTHROPIC_API_KEY" not in kw["env"]               # 구독 로그인만 사용


def test_plan_surfaces_cli_error(monkeypatch):
    fake = FakeRun([{"loggedIn": True}, {"is_error": True, "result": "Not logged in · Please run /login"}])
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(ValueError, match="Not logged in"):
        ClaudeProvider(exe="claude-fake").plan("s", "u", PLAN_SCHEMA)


def test_get_provider_claude_falls_back_to_null_when_cli_missing(monkeypatch):
    monkeypatch.setattr("das_agent.llm.shutil.which", lambda _: None)
    monkeypatch.delenv("DAS_LLM", raising=False)
    p = get_provider("claude")
    assert isinstance(p, NullProvider) and "claude auth login" in p.reason


@pytest.mark.skipif(os.environ.get("DAS_TEST_CLAUDE") != "1", reason="DAS_TEST_CLAUDE=1 일 때만 실제 Claude 구독 호출")
def test_real_claude_plan():
    p = ClaudeProvider()
    assert p.available(), p.reason
    user = "현재 설정: DAS 16셀, 토트 15PCS, 주문 단위 배송처차수, 작업시간 10h. 데이터 기간 2025-01-02~2025-02-14. 검토기준 값: ['의류용품'].\n\n사용자 요청: 16·24·32셀과 토트 15·20PCS를 비교해줘"
    plan = p.plan(PLANNER_SYSTEM, user, PLAN_SCHEMA)
    assert plan["action"] == "compare"
    assert {(s["cells"], s["tote_capacity"]) for s in plan["scenarios"]} >= {(16, 15), (24, 20), (32, 20)}
