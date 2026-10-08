"""CLI (das_agent.cli) — 화면 없이 등록·KPI·비교·엑셀."""
import json
import os
import sys

import pytest

from das_agent import cli
from das_agent.ingestion import make_sample_file


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "db"
    xlsx = make_sample_file(tmp_path / "sample.xlsx", days=12)
    monkeypatch.setenv("DAS_LLM", "none")

    def run(*argv, as_json=False):
        args = ["--data-dir", str(data_dir)] + (["--json"] if as_json else []) + list(argv)
        cli.main(args)
        out = capsys.readouterr().out
        return json.loads(out) if as_json else out
    return run, xlsx


def test_register_kpi_compare_export(env, tmp_path):
    run, xlsx = env
    reg = run("register", str(xlsx), "--name", "cli_sample", as_json=True)
    assert reg["dataset_id"] and reg["row_count"] > 0 and reg["mapping"]["order_key"] == "배송처차수"
    ds = run("datasets", as_json=True)
    assert ds["datasets"][0]["name"] == "cli_sample"
    kpi = run("kpi", "--dataset", "cli_sample", "--avg-excl", as_json=True)
    assert kpi["period"]["tote"] > 0 and "days_included" in kpi["avg_excl_outliers"]
    cmp_ = run("compare", "--export", as_json=True)
    assert len(cmp_["run_ids"]) == 6 and cmp_["plan"]["source"] == "rules"
    assert cmp_["export_path"] and os.path.exists(cmp_["export_path"])
    assert cmp_["llm"] == "none"
    txt = run("compare", "--grid", "16:15,24:20")
    assert "24셀·20PCS" in txt or "24셀 20PCS" in txt
    assert "계획: rules" in txt


def test_ask_rejects_unknown_dataset(env):
    run, xlsx = env
    run("register", str(xlsx), as_json=True)
    with pytest.raises(SystemExit) as e:
        run("kpi", "--dataset", "없는이름")
    assert "찾을 수 없습니다" in str(e.value)


def test_ask_without_llm_handles_structured_text(env):
    run, xlsx = env
    run("register", str(xlsx), as_json=True)
    out = run("ask", "24셀에 토트 20PCS로 비교해줘", as_json=True)
    assert out["plan"]["source"] == "rules" and len(out["run_ids"]) == 2
    out = run("ask", "셀을 절반으로 줄이면?", as_json=True)
    assert out["needs_input"] and out["plan"]["source"] == "fallback"   # 모델 없으면 되묻는다
