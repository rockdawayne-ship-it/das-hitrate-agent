"""Reproducible 650,000-row synthetic XLSX benchmark; never uses real shipments."""
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from das_agent.engine import Scenario
from das_agent.ingestion import inspect_file
from das_agent.storage import Storage
from das_agent.tools import Toolbox

size = 650000
rng = np.random.default_rng(17)
dates = pd.bdate_range("2025-01-02", periods=245).strftime("%Y%m%d").to_numpy()
raw = pd.DataFrame({
    "실확정일자": dates[np.arange(size) % 245],
    "검토기준": "의류용품",
    "배송처차수": [f"{x:05d} {y}" for x, y in zip(rng.integers(1, 1000, size), rng.integers(301, 304, size))],
    "상품코드": [f"SKU{x:05d}" for x in rng.integers(0, 300, size)],
    "Order Line": np.ones(size, dtype="int64"),
    "PCS": rng.integers(1, 25, size),
})
root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DAS_HitRate_Agent"
with tempfile.TemporaryDirectory(prefix="das_benchmark_") as directory:
    path = Path(directory) / "synthetic_650000.xlsx"
    print("Generating 650,000 synthetic rows...", flush=True)
    raw.to_excel(path, sheet_name="data", index=False, engine="xlsxwriter")
    del raw
    storage = Storage(Path(directory) / "data")
    tools = Toolbox(storage)
    start = time.perf_counter()
    inspection = tools.inspect_file(str(path))
    result = tools.register_dataset(str(path), inspection["sheet"], inspection["mapping"], keep_copy=False)
    elapsed_import = time.perf_counter() - start
    assert result["dataset_id"], result
    # Cold load from stored DuckDB, not from the just-imported DataFrame.
    storage._frames.clear()
    start = time.perf_counter()
    run_id, simulation = storage.run_scenario(result["dataset_id"], Scenario(), reuse=False)
    elapsed_compute = time.perf_counter() - start
    measurements = {"data": "synthetic_only", "rows": size, "days": simulation.n_days,
                    "xlsx_size_mb": round(path.stat().st_size / 1024**2, 2),
                    "read_validate_store_seconds": round(elapsed_import, 3),
                    "cold_load_simulate_seconds": round(elapsed_compute, 3),
                    "import_under_90s": elapsed_import <= 90,
                    "simulate_under_15s": elapsed_compute <= 15,
                    "python": platform.python_version(), "engine": simulation.engine_version}
    storage.close()
root.mkdir(parents=True, exist_ok=True)
(root / "benchmark.json").write_text(json.dumps(measurements, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(measurements, ensure_ascii=False, indent=2), flush=True)
