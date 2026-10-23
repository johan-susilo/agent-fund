"""Compare yesterday's agent result with today's DataPack and summarise realised performance."""
from __future__ import annotations

import json
from settings import ROOT


def _latest_agent_result(prev_date: str):
    out_dir = ROOT / "outputs"
    stamped = sorted(out_dir.glob(f"agent_result_{prev_date}_*.json"))
    if stamped:
        return stamped[-1]
    p = out_dir / f"agent_result_{prev_date}.json"
    return p if p.exists() else None


def review(prev_date: str, state: dict, market: dict) -> dict | None:
    p = _latest_agent_result(prev_date)
    if not p:
        return None
    prev = json.loads(p.read_text(encoding="utf-8"))

    # 1) Reconcile expected vs actual (only flag BUY orders or positive preview weights missing from actual)
    actual = state["holdings"]
    expected_positive = set(prev.get("preview", {}).get("weights", {}).keys())
    if not expected_positive:
        expected_positive = {o["ticker"] for o in prev.get("orders", []) if o.get("side") == "BUY"}
    drift = {t: actual.get(t, 0) for t in expected_positive if t not in actual}

    # 2) Per-stock contribution using change_pct to recover prior close c0
    contrib = {}
    for t, sh in actual.items():
        p_row = market["prices"].get(t, {})
        c1 = state["last_close"].get(t) or p_row.get("close")
        chg = p_row.get("change_pct")
        if c1 and chg is not None and (1 + chg / 100.0) > 0:
            c0 = c1 / (1.0 + chg / 100.0)
            contrib[t] = sh * (c1 - c0) / state["nav"]

    # 3) Benchmark: equal-weight basket of the 150 universe
    chg_all = [v["change_pct"] for v in market["prices"].values() if v.get("change_pct") is not None]
    bench = sum(chg_all) / len(chg_all) if chg_all else 0.0

    return {
        "source_file": p.name,
        "unfilled_or_missing": drift,
        "port_ret_pct": round(sum(contrib.values()) * 100, 2),
        "bench_ret_pct": round(bench, 2),
        "top_contrib": [(t, round(v * 100, 3)) for t, v in sorted(contrib.items(), key=lambda x: -x[1])[:3]],
        "bottom_contrib": [(t, round(v * 100, 3)) for t, v in sorted(contrib.items(), key=lambda x: x[1])[:3]],
        "violations": state["violations"],
    }