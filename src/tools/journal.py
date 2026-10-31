"""Journal built from the SERVER's own record: inputs/<date>/datapack.json for every trading day.

What it reports (all non-citable context for the portfolio manager):
  * performance from the DataPack NAV series (daily, cumulative, max drawdown) vs an equal-weight benchmark
  * plan vs reality: previous DataPack holdings + the orders of the archived agent result (outputs/agent_past/ and
    outputs/) are compared with today's DataPack holdings -> voided / excluded / unfilled orders show up
  * per-stock contribution from DataPack close prices
"""
from __future__ import annotations

import json

from settings import ROOT

AGENT_DIRS = (ROOT / "outputs" / "agent_past", ROOT / "outputs")


def _load_datapacks(upto: str) -> list:
    """[(trade_date, datapack)] with a usable account, ascending, trade_date <= upto."""
    out = []
    for p in (ROOT / "inputs").glob("*/datapack.json"):
        try:
            dp = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        td = str(dp.get("trade_date") or "")
        if dp.get("doc_type") == "DataPack" and dp.get("account") and td and td <= upto:
            out.append((td, dp))
    return sorted(out, key=lambda x: x[0])


def _nav(dp: dict) -> float:
    a = dp["account"]
    return float(a.get("aum") or a.get("nav_tradable"))      # aum includes receivable dividends / stock dividends


def _holdings(dp: dict) -> dict:
    return {h["code"]: {"shares": int(h["shares"]), "close": h.get("close_price")}
            for h in (dp.get("holdings") or []) if (h.get("shares") or 0) > 0}


def _plan_candidates(date: str) -> list:
    """[(path, parsed)] for every archived plan (agent_result_* or D-Plan_*) whose trade_date is `date`.
    Matched by file CONTENT, not by name. D-Plan files (what was actually submitted) sort last = win ties."""
    out, seen = [], set()
    for d in AGENT_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.json")):
            key = f.resolve()
            if key in seen or "REJECTED" in f.name or "REPLAY" in f.name:
                continue
            seen.add(key)
            try:
                res = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(res, dict) and isinstance(res.get("orders"), list) and str(res.get("trade_date")) == date:
                out.append((f, res))
    out.sort(key=lambda x: (x[1].get("doc_type") == "D-Plan", x[0].name))
    return out


def _expected(prev_hold: dict, orders: list) -> dict:
    exp = {t: h["shares"] for t, h in prev_hold.items()}
    for o in orders:
        exp[o["ticker"]] = exp.get(o["ticker"], 0) + (o["shares"] if o["side"] == "BUY" else -o["shares"])
    return {t: s for t, s in exp.items() if s > 0}


def _diff(expected: dict, actual: dict) -> dict:
    return {t: {"expected": expected.get(t, 0), "actual": actual.get(t, 0)}
            for t in sorted(set(expected) | set(actual)) if expected.get(t, 0) != actual.get(t, 0)}


def review(prev_date: str, state: dict, market: dict) -> dict | None:
    """prev_date is unused (kept for the old call signature); history comes from the DataPacks."""
    series = _load_datapacks(state["trade_date"])
    if not series or series[-1][0] != state["trade_date"]:
        return None
    if len(series) < 2:
        return None                                           # first trading day: no history yet
    (p_date, p_dp), (t_date, t_dp) = series[-2], series[-1]
    navs = [(d, _nav(dp)) for d, dp in series]
    nav_prev, nav_now = navs[-2][1], navs[-1][1]

    # ---- performance from the server's NAV series
    peak, mdd = navs[0][1], 0.0
    for _, v in navs:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)

    # ---- plan vs reality
    prev_hold, now_hold = _holdings(p_dp), _holdings(t_dp)
    actual = {t: h["shares"] for t, h in now_hold.items()}
    best = None
    for f, res in _plan_candidates(p_date):
        diff = _diff(_expected(prev_hold, res.get("orders", [])), actual)
        if best is None or len(diff) <= len(best[1]):          # later files win ties
            best = (f, diff)
    outcome = (t_dp.get("prev_day_orders") or {})

    # ---- per-stock contribution (shares held through the day x DataPack close change, % of previous NAV)
    contrib = {}
    for t, h in now_hold.items():
        p = prev_hold.get(t)
        if p and p["close"] and h["close"]:
            contrib[t] = min(p["shares"], h["shares"]) * (h["close"] - p["close"]) / nav_prev

    chg = [v["change_pct"] for v in market["prices"].values() if v.get("change_pct") is not None]
    bench = sum(chg) / len(chg) if chg else 0.0

    return {
        "source": "datapack_history",
        "datapacks_used": len(series),
        "plan_file": best[0].name if best else None,
        "plan_vs_actual": {"matched": bool(best) and not best[1], "mismatches": best[1] if best else None},
        "order_outcome": {"outcome": outcome.get("outcome"), "void_reasons": outcome.get("void_reasons", []),
                          "excluded_codes": outcome.get("excluded_codes", [])},
        "nav_prev": round(nav_prev), "nav_now": round(nav_now),
        "port_ret_pct": round((nav_now / nav_prev - 1) * 100, 2),
        "cum_ret_pct": round((nav_now / navs[0][1] - 1) * 100, 2),
        "mdd_pct": round(mdd * 100, 2),
        "bench_ret_pct": round(bench, 2),
        "top_contrib": [(t, round(v * 100, 3)) for t, v in sorted(contrib.items(), key=lambda x: -x[1])[:3]],
        "bottom_contrib": [(t, round(v * 100, 3)) for t, v in sorted(contrib.items(), key=lambda x: x[1])[:3]],
        "violations": state["violations"],
    }
