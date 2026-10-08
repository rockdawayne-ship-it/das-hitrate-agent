"""Browser-independent Streamlit user journeys with an isolated local database."""
from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def click_label(app, label):
    next(b for b in app.button if b.label == label).click().run(timeout=30)
    assert not app.exception, [e.message for e in app.exception]


def test_sample_compare_export_and_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("DAS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DAS_LLM", "none")
    st.cache_resource.clear()
    st.cache_data.clear()
    app = AppTest.from_file(APP).run(timeout=30)
    assert not app.exception
    click_label(app, "합성 샘플 파일 생성")
    click_label(app, "검증하고 저장")
    assert len(app.metric) == 12
    click_label(app, "계산")
    assert len(app.session_state["compare_runs"]) == 6
    click_label(app, "엑셀 생성")
    assert Path(app.session_state["last_export"]).is_file()
    app.chat_input[0].set_value("배송처로 다시 계산").run(timeout=30)
    assert not app.exception
    assert len(app.session_state["compare_runs"]) == 7
    app.chat_input[0].set_value("엑셀로 내보내줘").run(timeout=30)
    assert not app.exception
    assert Path(app.session_state["chat"][-1]["export"]).is_file()
    next(n for n in app.number_input if n.label == "DAS 셀 수").set_value(8).run(timeout=30)
    assert not app.exception
    assert app.session_state["compare_runs"] == []
    assert app.session_state["last_export"] is None
    st.cache_resource.clear()
