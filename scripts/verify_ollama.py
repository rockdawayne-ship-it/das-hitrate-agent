"""Opt-in live local-model acceptance check, using synthetic data only."""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from das_agent.agent import Agent, AgentContext
from das_agent.engine import Scenario, golden_example
from das_agent.llm import OllamaProvider
from das_agent.storage import Storage
from das_agent.tools import Toolbox

class MeasuredProvider(OllamaProvider):
    """Record API timing without changing application behavior or recording prompts."""

    def _request(self, method, path, payload=None, timeout=120):
        result = super()._request(method, path, payload, timeout)
        if path == "/api/chat":
            self.metrics = {key: result.get(key) for key in (
                "model", "total_duration", "load_duration", "prompt_eval_count",
                "prompt_eval_duration", "eval_count", "eval_duration", "done_reason")}
        return result


provider = MeasuredProvider()
assert provider.available(), provider.reason
cases = [("셀을 두 배로 늘리면 어떻게 돼?", 32, 15),
         ("셀 수를 현재의 세 배로 바꾸고 토트는 그대로 유지하면?", 48, 15),
         ("토트 용량을 지금의 두 배로 늘려서 비교해줘", 16, 30)]
report = []
with tempfile.TemporaryDirectory(prefix="das_ollama_") as directory:
    storage = Storage(directory)
    frame = golden_example()
    frame["src_row"] = range(2, 6)
    dataset = storage.register_dataset(frame, name="PRD 정답 샘플", file_name="synthetic", file_hash="golden", sheet="data", mapping={}, validation={})
    for question, cells, capacity in cases:
        ctx = AgentContext(dataset_id=dataset, baseline=Scenario(), date_min="2025-01-02", date_max="2025-01-02", categories=["의류용품"])
        started = time.perf_counter()
        reply = Agent(Toolbox(storage), provider).handle(question, ctx)
        elapsed = time.perf_counter() - started
        success = (reply.plan.source == "llm" and not reply.needs_input and
                   reply.compare_table is not None and
                   any(r["DAS 셀"] == cells and r["토트 용량"] == capacity for _, r in reply.compare_table.iterrows()) and
                   ctx.filters.start is None and ctx.filters.end is None)
        report.append({"question": question, "passed": bool(success), "model": provider.model,
                       "checked_at": datetime.now(timezone.utc).isoformat(),
                       "elapsed_seconds": round(elapsed, 3), "api_metrics": getattr(provider, "metrics", {}),
                       "plan": reply.plan.to_dict(), "reply": reply.text})
        print(json.dumps(report[-1], ensure_ascii=False), flush=True)
    storage.close()
root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DAS_HitRate_Agent"
root.mkdir(exist_ok=True, parents=True)
(root / "ollama-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
assert all(r["passed"] for r in report), "Local model acceptance failed; inspect ollama-verification.json"
