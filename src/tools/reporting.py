from __future__ import annotations
from pathlib import Path
import json
import pandas as pd

def write_json_report(data: dict, path: str | Path) -> Path:
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return p

def write_backtest_outputs(result: dict, output_dir: str | Path = "outputs") -> dict:
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    result["daily"].to_csv(out / "backtest_daily.csv", index_label="date")
    summary = {"metrics": result["metrics"], "assumptions": result["assumptions"]}
    write_json_report(summary, out / "backtest_summary.json")
    return summary
