"""Quantitative hard risk gate.  Pure functions, no LLM, no network.

Why separate from the score: a high aggregate score must never buy its way past a risk limit.  The gate reads
raw risk metrics only (volatility, drawdown, liquidity, spread proxy, data availability) and emits
  * VETO / REDUCE overrides in the same shape the LLM risk manager uses, so check_agent.risk_maps(),
    portfolio_agent.build_shortlist()/weight_caps() and the retry loop already enforce them;
  * per-name weight caps scaled by volatility ("CAPS");
  * portfolio-level limits (weighted volatility, top-5 concentration) checked on every proposed portfolio.
It runs even when AGENT_AUTONOMY=0 or the LLM risk agent fails (the exchange disposition scan is re-run then).
"""
from __future__ import annotations

import math

from tools.scoring import load_config

_EPS = 5e-5          # weights are rounded to 4 d.p.


def _f(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def compute_gate(factor_df, limits, regime: str | None, cfg: dict | None = None) -> dict:
    """Return {"overrides": [...], "caps": {t: cap}, "vol": {t: vol20}, "portfolio": {...}}."""
    hr = (cfg or load_config())["hard_risk"]
    exempt = set(hr.get("exempt_from_caps", ["2330"]))
    cap_base = float(getattr(limits, "cap_other", 0.08))
    floor = float(hr["cap_floor"])
    overrides, caps, vols = [], {}, {}
    if factor_df is None or len(factor_df) == 0:
        return {"overrides": [], "caps": {}, "vol": {}, "portfolio": _portfolio_limits(hr, regime),
                "warnings": ["risk gate: no factor table, only exchange flags apply"]}
    for r in factor_df.to_dict("records"):
        t = str(r["ticker"])
        vol, mdd, adv, hl = _f(r.get("vol20")), _f(r.get("mdd60")), _f(r.get("adv20")), _f(r.get("hl20"))
        if vol is not None:
            vols[t] = vol
        if t in exempt:
            continue                                   # anchor stock: only exchange VETOs can touch it
        reasons, veto = [], False
        if adv is not None and adv < hr["veto_adv20_twd_m_below"]:
            veto, _ = True, reasons.append(f"20日均成交值 {adv:.0f}M 低於 {hr['veto_adv20_twd_m_below']}M，流動性不足")
        if vol is not None and vol > hr["reduce_vol20_above"]:
            reasons.append(f"20日年化波動 {vol:.0f}% 超過 {hr['reduce_vol20_above']}%")
        if mdd is not None and mdd > hr["reduce_mdd60_above"]:
            reasons.append(f"近60日最大回檔 {mdd:.0f}% 超過 {hr['reduce_mdd60_above']}%")
        if hl is not None and hl > hr["reduce_hl20_above"]:
            reasons.append(f"20日平均日內振幅 {hl:.1f}% 偏高（價差/衝擊成本代理）")
        if adv is not None and not veto and adv < hr["reduce_adv20_twd_m_below"]:
            reasons.append(f"20日均成交值 {adv:.0f}M 低於 {hr['reduce_adv20_twd_m_below']}M")
        if hr.get("reduce_on_missing_price_data") and vol is None and adv is None:
            reasons.append("缺少價量資料（停牌或資料過期），無法評估風險")
        if veto:
            overrides.append({"ticker": t, "action": "VETO", "reason": "；".join(reasons), "source": "quant_gate"})
            continue
        if reasons:
            overrides.append({"ticker": t, "action": "REDUCE", "reason": "；".join(reasons), "source": "quant_gate"})
        cap = cap_base
        if vol is not None and vol > hr["vol_target"]:
            cap = cap_base * hr["vol_target"] / vol
        if mdd is not None and mdd > hr["mdd_extra_haircut_above"]:
            cap *= hr["mdd_extra_haircut"]
        cap = round(max(floor, min(cap_base, cap)), 4)
        if cap < cap_base - _EPS:
            caps[t] = cap
    return {"overrides": overrides, "caps": caps, "vol": vols, "portfolio": _portfolio_limits(hr, regime), "warnings": []}


def _portfolio_limits(hr: dict, regime) -> dict:
    p = hr["portfolio"]
    return {"max_weighted_vol20": float(p["max_weighted_vol20"].get(regime or "neutral", p["max_weighted_vol20"]["neutral"])),
            "unknown_vol": float(p["unknown_vol_assumption"]), "max_top5": float(p["max_top5_weight"])}


def check_portfolio_risk(weights: dict, port: dict | None, vol: dict | None = None) -> list:
    """Portfolio-level limits on a proposed {ticker: weight} map.  Independent of any score."""
    if not port or not weights:
        return []
    vol = vol or {}
    tot = sum(weights.values())
    errs = []
    if tot > 0:
        wv = sum(w * vol.get(t, port["unknown_vol"]) for t, w in weights.items()) / tot
        if wv > port["max_weighted_vol20"] + 1e-9:
            errs.append(f"weighted 20d volatility {wv:.1f}% exceeds the {port['max_weighted_vol20']:.0f}% portfolio limit: "
                        "shift weight to lower-volatility holdings or raise cash within the window")
    top5 = sum(sorted(weights.values(), reverse=True)[:5])
    if top5 > port["max_top5"] + 1e-9:
        errs.append(f"top-5 weight {top5:.1%} exceeds the {port['max_top5']:.0%} concentration limit")
    return errs


def merge_into_ctx(risk_ctx: dict | None, gate: dict) -> dict:
    """Add the gate to risk_ctx without ever weakening an LLM / exchange override (VETO > REDUCE > WARN)."""
    ctx = risk_ctx or {"structured_risk": {"portfolio_risk_summary": "", "overrides": []}, "event_flags": {},
                       "notes": "", "usage": {"input_tokens": 0, "output_tokens": 0}, "tool_calls": 0, "models": []}
    ctx.setdefault("structured_risk", {}).setdefault("overrides", [])
    ctx.setdefault("event_flags", {})
    ctx.setdefault("usage", {"input_tokens": 0, "output_tokens": 0})
    ctx.setdefault("tool_calls", 0)
    ctx.setdefault("models", [])
    ctx.setdefault("notes", "")
    rank = {"WARN": 1, "REDUCE": 2, "VETO": 3}
    have = {}
    for o in ctx["structured_risk"]["overrides"]:
        have[str(o.get("ticker"))] = max(have.get(str(o.get("ticker")), 0), rank.get(o.get("action"), 0))
    for o in gate["overrides"]:
        if rank[o["action"]] > have.get(o["ticker"], 0):
            ctx["structured_risk"]["overrides"].append(o)
    ctx["quant_gate"] = {"caps": gate["caps"], "vol": gate["vol"], "portfolio": gate["portfolio"]}
    return ctx


def ensure_risk_ctx(risk_ctx, factor_df, limits, regime, universe=None, as_of=None, book=None) -> dict:
    """Entry point used by run_daily.  Guarantees a risk_ctx with exchange flags + quantitative gate, even if the
    LLM risk agent was skipped or crashed (known gap: the disposition veto used to vanish in that case)."""
    has_flags = bool(risk_ctx) and "disposition_tickers" in (risk_ctx.get("event_flags") or {})
    ctx = merge_into_ctx(risk_ctx, compute_gate(factor_df, limits, regime))
    if not has_flags and None not in (universe, as_of, book):
        try:
            from agents import risk_agent
            from util import Counter
            ctx["event_flags"] = risk_agent._scan_exchange_events(as_of, universe, book, Counter())
        except Exception as e:                          # exchange API down / offline: quantitative gate still applies
            ctx["notes"] = (ctx.get("notes") or "") + f" [exchange event scan unavailable: {type(e).__name__}]"
    return ctx
