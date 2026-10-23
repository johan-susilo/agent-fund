"""Compare yesterday's agent result with today's DataPack and summarise realised performance."""
import json
from settings import ROOT

def review(prev_date, state, market):
    p = ROOT / "outputs" / f"agent_result_{prev_date}.json"
    if not p.exists():
        return None
    prev = json.loads(p.read_text(encoding="utf-8"))
    exp = {}                                   # expected holdings = yesterday's preview weights
    # 1) reconcile expected vs actual
    actual = state["holdings"]
    ordered = {o["ticker"] for o in prev["orders"]}
    drift = {t: actual.get(t, 0) for t in ordered if t not in actual}   # ordered but not held
    # 2) per-stock contribution since yesterday's close
    contrib = {}
    for t, sh in actual.items():
        px0, px1 = state["last_close"].get(t), market["prices"].get(t, {}).get("close")
        if px0 and px1:
            contrib[t] = sh * (px1 - px0) / state["nav"]
    # 3) benchmark: equal-weight basket of the 150
    chg = [v["change_pct"] for v in market["prices"].values() if v.get("change_pct") is not None]
    bench = sum(chg) / len(chg) if chg else 0.0
    return {"unfilled_or_missing": drift, "port_ret_pct": sum(contrib.values()) * 100,
            "bench_ret_pct": bench, "top_contrib": sorted(contrib.items(), key=lambda x: -x[1])[:3],
            "bottom_contrib": sorted(contrib.items(), key=lambda x: x[1])[:3],
            "violations": state["violations"]}