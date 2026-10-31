"""Portfolio agent: the only LLM step. Turns sourced evidence into target weights.

The LLM supplies market view, inferences and target weights. Everything mechanical is code:
weight normalisation, BUY/ADD/TRIM/SELL_ALL/no-trade classification, share counts, and all
rule checks (via check_agent). A failed check is fed back to the LLM for a retry.

Decision-quality upgrades (no new modules):
  * the LLM sees a SHORTLIST (~45 ranked, liquid, non-vetoed names + current holdings), not 150 rows
  * a deterministic baseline portfolio and per-stock max weights are given as anchors
  * risk-manager VETO / REDUCE are enforced in code, not just suggested
  * bulk per-stock evidence text is hidden (its numbers are already in the candidate rows); ids stay citable
  * net_exposure_intent is auto-corrected from the real net flow (pure arithmetic, saves a retry)

Usage (normally called by run_daily.py):
    run_agent(trade_date, state, market, universe, book, strategy, limits, started_at)
"""
from __future__ import annotations

import json
import math
import os
import traceback

from agents import check_agent
import reconcile
from derive_orders import derive_orders, lot_weight, project_portfolio, target_shares, cost_gate  
from settings import PROMPT_DIR, ROOT, Limits
from util import iso, now_tw


MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", 3))
SHORTLIST_N = int(os.environ.get("SHORTLIST_N", 45))            # non-held names shown to the LLM
MIN_ADV20_TWD_M = float(os.environ.get("MIN_ADV20_TWD_M", 200))  # liquidity floor for NEW buys (NT$ million/day)
CORE_SHORTLIST_SHARE = 0.8
BULK_TOPICS = {"factor_momentum", "stock_close", "tech_stock"}                 # numbers already live in candidate rows


def _num(v, nd=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return round(x, nd) if nd is not None else x


# ===================================================================== risk caps / shortlist / baseline
def weight_caps(limits: Limits, rm: dict) -> dict:
    """Per-ticker caps tighter than the normal cap: risk manager REDUCE and the quantitative volatility caps.  # [upgrade-2026-10]"""
    caps = {t: min(limits.cap(t), check_agent.REDUCE_CAP) for t in rm["REDUCE"] if t != "2330"}
    for t, c in (rm.get("CAPS") or {}).items():
        if t != "2330":
            caps[t] = min(caps.get(t, limits.cap(t)), c)
    return caps


def build_shortlist(candidates, holdings, factor_df, universe, rm, n: int = SHORTLIST_N) -> list:
    """Current holdings + the best-ranked liquid, non-vetoed names (~80% core themes), in factor-rank order."""
    cand = set(candidates)
    held = [t for t in holdings if t in cand]
    have_df = factor_df is not None and not factor_df.empty
    order = [t for t in factor_df["ticker"]] if have_df else list(candidates)
    adv = factor_df.set_index("ticker")["adv20"].to_dict() if have_df else {}
    rank = {t: i for i, t in enumerate(order)}

    def ok(t):
        if t not in cand or t in rm["VETO"] or t in held:
            return False
        a = _num(adv.get(t))
        return a is None or a >= MIN_ADV20_TWD_M

    ranked = [t for t in order if ok(t)]
    slots = max(n, 10)
    core_slots = round(slots * CORE_SHORTLIST_SHARE)
    core = [t for t in ranked if universe.is_core(t)]
    rest = [t for t in ranked if not universe.is_core(t)]
    pick = core[:core_slots] + rest[:slots - core_slots]
    if len(pick) < slots:                                  # one side ran dry: fill from the other
        pick += [t for t in ranked if t not in pick][:slots - len(pick)]
    if "2330" in cand and "2330" not in rm["VETO"] and "2330" not in held and "2330" not in pick:
        pick.append("2330")                                # the benchmark anchor must always be visible
    return sorted(set(held) | set(pick), key=lambda t: rank.get(t, 10 ** 6))


def baseline_weights(shortlist, factor_df, universe, limits: Limits, state, closes, caps) -> dict:
    """Deterministic score-tilted starting portfolio (a hint, never an instruction)."""
    try:
        from tools.portfolio import build_target_weights
        if factor_df is None or factor_df.empty:
            return {}
        elig = factor_df[factor_df["ticker"].isin(shortlist)][["ticker", "score"]].copy()
        elig["composite_score"] = (elig["score"].fillna(0.5)
                                   + elig["ticker"].map(lambda t: 0.15 if universe.is_core(t) else 0.0)
                                   + elig["ticker"].map(lambda t: 0.03 if t in state["holdings"] else 0.0)) ** 4
        w = build_target_weights(elig[["ticker", "composite_score"]],
                                 n_holdings=(limits.min_n + limits.max_n) // 2,
                                 max_weight=limits.cap_other - 0.005,
                                 cash_target=(limits.cash_lo + limits.cash_hi) / 2.0,
                                 tsmc_cap=limits.cap_2330 - 0.02)
        return {t: round(min(v, caps.get(t, 1.0)), 4) for t, v in w.items()
                if v >= lot_weight(state["nav"], closes[t])}
    except Exception:
        return {}


# ===================================================================== prompt construction
def build_system_prompt(limits: Limits, universe) -> str:
    text = (PROMPT_DIR / "system_prompt.md").read_text(encoding="utf-8")
    core = ", ".join(universe.themes[t]["label"].split(" — ")[0] for t in universe.core_ids)
    rep = {
        "APPROVED_UNIVERSE": "the tickers listed in `candidates` (a ranked shortlist of the 150-stock approved universe; any other ticker is forbidden)",
        "MIN_N": limits.min_n, "MAX_N": limits.max_n, "CAP_2330": limits.cap_2330,
        "CAP_OTHER": limits.cap_other, "SUM_LO": limits.sum_lo, "SUM_HI": limits.sum_hi,
        "CASH_LO": limits.cash_lo, "CASH_HI": limits.cash_hi,
        "CASH_LO_PCT": f"{limits.cash_lo:.0%}", "CASH_HI_PCT": f"{limits.cash_hi:.0%}",
        "HOLD_TOL_PCT": f"{limits.hold_tol:.1%}", "CORE_MIN": f"{limits.core_min:.0%}",
        "BAND_PCT": f"{limits.no_trade_band:.1%}", "CORE_THEMES": core,
        "THEME_CAP": f"{limits.theme_cap:.0%}", "THEME_CAP_SEMIS": f"{limits.theme_cap_semis:.0%}",
        "MIN_TRADE_PCT": f"{limits.min_trade_w:.1%}", "HURDLE_BUY": limits.hurdle_buy, "HURDLE_SELL": limits.hurdle_sell,
    }
    for k, v in rep.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def build_user_prompt(
    strategy, state, universe, book, candidates, closes,
    factor_df=None, baseline_weights=None, regime="neutral", journal_ctx=None, feedback=None,
    risk_ctx=None, research_notes=None, limits: Limits | None = None, caps: dict | None = None,
    tech_ctx=None, desk_notes=None
) -> str:
    nav, tobs = state["nav"], book.ticker_obs()
    rm, caps, base_w = check_agent.risk_maps(risk_ctx), caps or {}, baseline_weights or {}
    cur_w_map = {t: round(s * closes[t] / nav, 4) for t, s in state["holdings"].items() if t in closes}
    current = [{"ticker": t, "shares": s, "weight": cur_w_map[t]}
               for t, s in sorted(state["holdings"].items()) if t in closes]
    f_map = factor_df.set_index("ticker").to_dict("index") if factor_df is not None and not factor_df.empty else {}
    tech_map = (tech_ctx or {}).get("technicals") or {}

    rows = []
    for rank, t in enumerate(candidates, 1):
        f = f_map.get(t, {})
        row = {
            "t": t, "n": universe.names.get(t, ""), "theme": universe.theme_of(t), "core": universe.is_core(t),
            "rank": rank, "score": _num(f.get("score"), 3),
            "comp": [_num(f.get(k), 2) for k in ("s_momentum", "s_risk", "s_earnings", "s_macro", "s_valuation")],  # [upgrade-2026-10]
            "mom20": _num(f.get("mom20"), 1), "mom60": _num(f.get("mom60"), 1),
            "vol20": _num(f.get("vol20"), 0), "adv20": _num(f.get("adv20"), 0),
            "roe": _num(f.get("roe"), 1), "pe": _num(f.get("pe"), 1), "rev_g": _num(f.get("rev_g"), 1),
            "cur_w": cur_w_map.get(t, 0.0), "base_w": round(base_w.get(t, 0.0), 4),
            "max_w": round(caps.get(t, limits.cap(t)), 4) if limits else None,
            "lot_w": round(lot_weight(nav, closes[t]), 4), "obs": tobs.get(t, [])[:7],
        }
        sc_ = _num(f.get("score"))
        if limits is not None:
            row["gate"] = {"buy_ok": sc_ is not None and sc_ >= limits.hurdle_buy,
                           "sell_ok": sc_ is not None and sc_ <= limits.hurdle_sell,
                           "days_held": state.get("days_held", {}).get(t, 0)}
        tk = tech_map.get(t)
        if tk:
            row["tech"] = {k: tk[s] for k, s in (("score", "tech_score"), ("rsi", "rsi14"), ("d20", "dist_ma20_pct"),
                                                 ("d60", "dist_ma60_pct"), ("atr", "atr14_pct"), ("vr", "vol_ratio"),
                                                 ("ddh", "dd_high_pct"), ("trend", "trend"), ("flags", "flags"))
                           if tk.get(s) not in (None, [])}
        sr = ((risk_ctx or {}).get("stock_risk") or {}).get(t) or {}
        if sr.get("beta") is not None:
            row["beta"] = sr["beta"]
        if sr.get("liq_w") is not None:
            row["liq_w"] = sr["liq_w"]
        flags = [k for k in ("VETO", "REDUCE", "WARN", "EXDIV") if t in rm[k]]
        if flags:
            row["flags"] = flags
        rows.append({k: v for k, v in row.items() if v is not None})

    short = set(candidates)
    obs_view = []
    for o in book.observations:
        m = book.meta.get(o["obs_id"], {})
        if m.get("level") == "market" or (set(m.get("tickers", [])) & short and o["topic"] not in BULK_TOPICS):
            obs_view.append({"id": o["obs_id"], "topic": o["topic"], "text": o["statement"][:400]})

    theme_now: dict = {}
    for t, w in cur_w_map.items():
        th = universe.theme_of(t)
        theme_now[th] = round(theme_now.get(th, 0.0) + w, 4)

    payload = {
        "trade_date": state["trade_date"],
        "computed_regime_hint": regime,
        "constraints": None if not limits else {
            "holdings": [limits.min_n, limits.max_n], "cap_2330": limits.cap_2330, "cap_other": limits.cap_other,
            "cash_window": [limits.cash_lo, limits.cash_hi], "core_min": limits.core_min,
            "theme_cap": limits.theme_cap, "theme_cap_semis": limits.theme_cap_semis,
            "no_trade_band": limits.no_trade_band, "max_one_way_turnover": limits.max_turnover},
        "etf_strategy": strategy,
        "account": {
            "nav": nav, "cash": state["cash"], "cash_ratio": round(state["cash"] / nav, 4),
            "holdings_count": len(state["holdings"]), "current_holdings": current,
            "theme_weights": theme_now,
            "core_weight": round(sum(w for t, w in cur_w_map.items() if universe.is_core(t)), 4),
        },
        "trading_costs": None if not limits else {
            "buy_cost_pct": 0.1425, "sell_cost_pct": 0.4425, "round_trip_pct": 0.585,
            "min_trade_w": limits.min_trade_w, "score_hurdle_buy": limits.hurdle_buy,
            "score_hurdle_sell": limits.hurdle_sell,
            "rule": "discretionary trades failing the hurdle are cancelled in code and the stock keeps its current weight"},
        "cash_status": None if not limits else {
            "current_cash_ratio": round(state["cash"] / nav, 4),
            "window": [limits.cash_lo, limits.cash_hi],
            "must_net_sell_pct_nav": round(max(0.0, limits.cash_lo + 0.003 - state["cash"] / nav), 4),
            "max_net_buy_pct_nav": round(max(0.0, state["cash"] / nav - limits.cash_lo - 0.003), 4)},
        "baseline_portfolio": base_w,
        "risk_manager": {
            "summary": risk_ctx["notes"] if risk_ctx else None,
            "overrides": (risk_ctx.get("structured_risk", {}).get("overrides", []) if risk_ctx else []),
            "exchange_flags": (risk_ctx.get("event_flags", {}) if risk_ctx else {}),
            "considerations": (risk_ctx or {}).get("considerations", []),
        },
        "non_citable_journal_context": journal_ctx,   # informational only; do NOT cite as O#
        "research_notes": research_notes,
        "market_obs_ids": book.market_level_ids(),
        "portfolio_risk": (risk_ctx or {}).get("portfolio_risk"),
        "desk_notes_non_citable": desk_notes,
        "weight_budget": None if not limits else {"sum_min": limits.sum_lo, "sum_max": limits.sum_hi,
                                                "aim": round((limits.sum_lo + limits.sum_hi) / 2, 3),
                                                "rule": "sum of portfolio target_weight must land in [sum_min, sum_max]; it is validated, not rescaled"},
        "candidates": rows,
        "observations": obs_view,
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if feedback:
        text += (
            "\n\n[Your previous output failed the program's checks. Fix every item and return the full JSON again]\n"
            + "\n".join(f"- {m}" for m in feedback)
        )
    return text


def repair_post_band_cash(decisions, no_trade, state, closes, limits: Limits):
    """If no-trade-band skips cause projected cash to drift outside [cash_lo, cash_hi],
    promote no-trade holdings or scale unconstrained BUY/ADD decisions to restore cash inside bounds."""
    orders = derive_orders(decisions, state, closes)
    rep = project_portfolio(orders, state, closes)
    c = rep["cash_ratio"]
    if limits.cash_lo <= c <= limits.cash_hi:
        return decisions, orders, rep, None

    target_cash = (limits.cash_lo + limits.cash_hi) / 2.0
    delta_cash = c - target_cash  # >0 means too much cash (need to buy more); <0 means too little cash
    adjustable = [d for d in decisions if d["action"] in ("BUY", "ADD", "TRIM")]
    if not adjustable:
        return decisions, orders, rep, None

    for d in adjustable:
        t = d["ticker"]
        cur_w = d["target_weight"]
        room_up = limits.cap(t) - cur_w - 0.002
        room_dn = cur_w - lot_weight(state["nav"], closes[t]) - 0.002
        if delta_cash > 0 and room_up > 0.002:
            step = min(delta_cash, room_up)
            d["target_weight"] = round(cur_w + step, 4)
            delta_cash -= step
        elif delta_cash < 0 and room_dn > 0.002:
            step = min(-delta_cash, room_dn)
            d["target_weight"] = round(cur_w - step, 4)
            delta_cash += step
        if abs(delta_cash) < 0.003:
            break

    # Remove any decision whose new target_shares equals current holding
    kept = []
    for d in decisions:
        t = d["ticker"]
        if d["action"] == "SELL_ALL" or target_shares(d["target_weight"], state["nav"], closes[t]) != state["holdings"].get(t, 0):
            kept.append(d)
    for idx, d in enumerate(kept, 1):
        d["decision_id"] = f"D{idx}"
    orders = derive_orders(kept, state, closes)
    rep = project_portfolio(orders, state, closes)
    return kept, orders, rep, f"repaired post-band cash ratio {c:.2%} -> {rep['cash_ratio']:.2%}"


def _build_fallback_llm_output(state, universe, book, candidates, closes, limits: Limits, factor_df, regime: str,
                               caps: dict | None = None) -> dict:
    """Deterministic score-based fallback when all LLM attempts fail (prevents missed submission)."""
    from tools.portfolio import build_target_weights
    import pandas as pd

    caps = caps or {}
    cash_target = round((limits.cash_lo + limits.cash_hi) / 2.0, 4)
    n_hold = (limits.min_n + limits.max_n) // 2

    if factor_df is not None and not factor_df.empty:
        elig = factor_df[factor_df["ticker"].isin(candidates)].copy()
        elig = elig.rename(columns={"score": "composite_score"})
    else:
        elig = pd.DataFrame({"ticker": list(candidates), "composite_score": 1.0})

    # Ensure >= 65% core weight by boosting core scores slightly in baseline
    elig["composite_score"] = elig["composite_score"].fillna(0.5) + elig["ticker"].map(
        lambda t: 0.25 if universe.is_core(t) else 0.0
    )
    weights = build_target_weights(
        elig, ticker_col="ticker", score_col="composite_score",
        n_holdings=n_hold, max_weight=limits.cap_other - 0.005,
        cash_target=cash_target, tsmc_cap=min(0.14, limits.cap_2330 - 0.02),
    )
    # Filter out any weight below 1 board lot; respect REDUCE caps
    weights = {t: round(min(w, caps.get(t, 1.0)), 4) for t, w in weights.items()
               if w >= lot_weight(state["nav"], closes[t])}

    m_ids = book.market_level_ids()[:4] or [book.observations[0]["obs_id"]]
    t_obs = book.ticker_obs()
    infs, portfolio = [], []
    for idx, (t, w) in enumerate(sorted(weights.items(), key=lambda x: -x[1]), 1):
        inf_id = f"I{idx}"
        prem = (t_obs.get(t) or m_ids)[:3]
        infs.append({
            "inf_id": inf_id,
            "premise_refs": prem,
            "logic": f"依據量化多因子模型（35%動能與趨勢、25%風險與流動性、20%獲利與催化、10%總經與市場情勢、10%估值）與台股收盤數據，{t} ({universe.names.get(t, '')}) 綜合評分位居候選池前列且符合單一持股上限。",
            "conclusion": f"納入 {t} 為核心投資組合持股（權重見 portfolio 與委託）。",
            "counter_evidence": "短期市場波動可能影響個股表現，已透過部位上限控制風險。",
            "confidence": 0.65,
        })
        portfolio.append({"ticker": t, "target_weight": w, "inference_refs": [inf_id]})

    exits = []
    for t in sorted(state["holdings"]):
        if t not in weights:
            inf_id = f"I{len(infs) + 1}"
            prem = (t_obs.get(t) or m_ids)[:2]
            infs.append({
                "inf_id": inf_id,
                "premise_refs": prem,
                "logic": f"依據每日量化因子重新排序與部位集中度控管，{t} 綜合評分低於目前入選標的或遭風險控管排除，故予以出清以優化投組效率。",
                "conclusion": f"全數出清 {t} 持股。",
                "counter_evidence": None,
                "confidence": 0.65,
            })
            exits.append({"ticker": t, "inference_refs": [inf_id]})

    stance = "defensive" if regime == "risk_off" else ("aggressive" if regime == "risk_on" else "neutral")
    cur_invested = sum(s * closes[t] for t, s in state["holdings"].items() if t in closes) / state["nav"]
    tgt_invested = sum(weights.values())
    intent = "increase" if tgt_invested - cur_invested > limits.hold_tol else (
        "reduce" if cur_invested - tgt_invested > limits.hold_tol else "hold"
    )
    return {
        "market_view": {
            "basis_refs": m_ids,
            "logic": f"系統根據總經指標（VIX、美股科技指數）與台股廣度量化判定當前市場狀態為 {regime}，採取 {stance} 配置立場並維持現金比例於 [{limits.cash_lo:.0%}, {limits.cash_hi:.0%}] 區間。",
            "regime": regime,
            "stance": stance,
            "net_exposure_intent": intent,
            "target_cash_pct_range": [limits.cash_lo, limits.cash_hi],
            "counter_evidence": "部分次產業仍具防禦韌性，故保留核心AI與半導體配置。",
            "confidence": 0.65,
        },
        "inferences": infs,
        "portfolio": portfolio,
        "exits": exits,
    }


# ===================================================================== LLM output handling
def _split_llm(out: dict):
    mv = out.get("market_view") or {}
    infs = [{"inf_id": i.get("inf_id"), "premise_refs": i.get("premise_refs") or [],
             "logic": i.get("logic", ""), "conclusion": (i.get("conclusion") or "")[:300],
             "counter_evidence": i.get("counter_evidence"),
             "confidence": i.get("confidence") if isinstance(i.get("confidence"), (int, float)) else None}
            for i in out.get("inferences") or []]
    return mv, infs, out.get("portfolio") or [], out.get("exits") or []


def check_structure(mv, infs, portfolio, exits, ctx) -> list:
    """Validate shape and references of the LLM output (everything the schema or the chain would reject)."""
    errs, lim, caps = [], ctx["limits"], ctx.get("caps", {})
    cap_of = lambda t: min(lim.cap(t), caps.get(t, 1.0))
    inf_ids = {i["inf_id"] for i in infs}
    if not infs:
        errs.append("inferences must not be empty")
    if [i["inf_id"] for i in infs] != [f"I{k}" for k in range(1, len(infs) + 1)]:
        errs.append("inf_id must be I1, I2, ... consecutive")
    for i in infs:
        if len(i["logic"]) < 20:
            errs.append(f"{i['inf_id']} logic shorter than 20 characters")
        if not i["premise_refs"] or set(i["premise_refs"]) - ctx["obs_ids"]:
            errs.append(f"{i['inf_id']} premise_refs empty or unknown: {sorted(set(i['premise_refs']) - ctx['obs_ids'])}")
    basis = mv.get("basis_refs") or []
    if not basis or set(basis) - ctx["market_ids"]:
        errs.append("market_view.basis_refs must be non-empty and drawn from market_obs_ids")
    if len(mv.get("logic", "")) < 20:
        errs.append("market_view.logic shorter than 20 characters")
    for k, allowed in (("regime", ("risk_on", "neutral", "risk_off")),
                       ("stance", ("aggressive", "neutral", "defensive")),
                       ("net_exposure_intent", ("increase", "hold", "reduce"))):
        if mv.get(k) not in allowed:
            errs.append(f"market_view.{k} must be one of {allowed}")
    r = mv.get("target_cash_pct_range")
    if not (isinstance(r, list) and len(r) == 2 and all(isinstance(x, (int, float)) for x in r) and 0 <= r[0] <= r[1] <= 0.25):
        errs.append("target_cash_pct_range must be [low, high] with 0 <= low <= high <= 0.25")

    seen = set()
    for p in portfolio:
        t, w = p.get("ticker"), p.get("target_weight")
        if t in seen:
            errs.append(f"{t} listed twice in portfolio")
        seen.add(t)
        if t not in ctx["candidates"]:
            errs.append(f"{t} is not in the candidate list")
            continue
        if not isinstance(w, (int, float)) or not 0 < w <= cap_of(t) + 0.05:
            errs.append(f"{t} target_weight={w} invalid (max {cap_of(t)})")
        elif w < ctx["lot_w"][t]:
            errs.append(f"{t} target_weight {w} is below one lot ({ctx['lot_w'][t]:.4f}); raise it or drop the stock")
        if not p.get("inference_refs") or set(p["inference_refs"]) - inf_ids:
            errs.append(f"{t} inference_refs empty or unknown")
    errs += check_agent.check_risk_overrides({p.get("ticker"): p.get("target_weight") for p in portfolio},
                                             ctx["holdings"], ctx["rm"])
    exit_t = {e.get("ticker") for e in exits}
    for e in exits:
        if e.get("ticker") not in ctx["holdings"]:
            errs.append(f"exit {e.get('ticker')} is not a current holding")
        if e.get("ticker") in seen:
            errs.append(f"{e.get('ticker')} appears in both portfolio and exits")
        if not e.get("inference_refs") or set(e["inference_refs"]) - inf_ids:
            errs.append(f"exit {e.get('ticker')} inference_refs empty or unknown")
    lost = [t for t in ctx["holdings"] if t not in seen and t not in exit_t]
    if lost:
        errs.append(f"held stocks {lost} are in neither portfolio nor exits")
    # --- weight budget + narrative consistency (the LLM must do its own arithmetic; code does not rescale it) ---
    import re as _re
    raw_sum = sum(float(p["target_weight"]) for p in portfolio if isinstance(p.get("target_weight"), (int, float)))
    aim = (lim.sum_lo + lim.sum_hi) / 2
    if not lim.sum_lo - 0.01 <= raw_sum <= lim.sum_hi + 0.01:
        errs.append(f"sum of portfolio target_weight is {raw_sum:.1%}; it must be between {lim.sum_lo:.1%} and {lim.sum_hi:.1%} "
                    f"(aim {aim:.1%}). Re-allocate the weights yourself and add them up before answering; "
                    "the program will NOT scale them for you.")
    _w = _re.compile(r"(權重|配置|目標|比重|佔|占)[^。；;，,]{0,12}?\d+(?:\.\d+)?\s*%")
    for i in infs:
        if _w.search(i["logic"] or "") or _w.search(i["conclusion"] or ""):
            errs.append(f"{i['inf_id']} states a weight percentage in its text; put weights ONLY in `portfolio` "
                        "(describe tilts with words such as overweight / underweight / anchor)")
    if not lim.min_n <= len(portfolio) <= lim.max_n:
        errs.append(f"portfolio has {len(portfolio)} stocks, must be {lim.min_n}-{lim.max_n}")
    
    errs += check_agent.check_risk_citations(infs, ctx.get("risk_considerations", []))
    return errs


def normalize_weights(portfolio: list, limits: Limits, caps: dict | None = None):
    """Deterministic repair: clip to caps, then rescale the sum into [sum_lo, sum_hi].

    Saves an LLM retry for what is only arithmetic. Returns (portfolio, note_or_None).
    """
    caps = caps or {}
    cap = lambda t: min(limits.cap(t), caps.get(t, 1.0))
    w = {p["ticker"]: min(float(p["target_weight"]), cap(p["ticker"])) for p in portfolio}
    before, s = sum(w.values()), sum(w.values())
    if not limits.sum_lo <= s <= limits.sum_hi:
        goal = limits.sum_hi - 0.002 if s > limits.sum_hi else limits.sum_lo + 0.002
        for _ in range(30):                       # proportional rescale; capped names stay capped
            s = sum(w.values())
            if abs(s - goal) < 1e-4:
                break
            free = [t for t in w if w[t] < cap(t) - 1e-9] if s < goal else list(w)
            if not free:
                break
            k = (goal - s) / sum(w[t] for t in free)
            for t in free:
                w[t] = min(cap(t), w[t] * (1 + k))
    out = [{**p, "target_weight": round(w[p["ticker"]], 4)} for p in portfolio]
    after = sum(p["target_weight"] for p in out)
    note = f"weights normalised: sum {before:.3f} -> {after:.3f}" if abs(after - before) > 0.002 else None
    return out, note

def enforce_turnover_limit(decisions, state, closes, rm, max_turnover=0.08):
    """Req 3: Limits daily one-way turnover to max_turnover by scaling down discretionary trades."""
    if not state["holdings"]:      # day 1 build has no turnover limit (check_turnover also skips it)
        return decisions, None
    nav = state["nav"]
    turnover = 0.0
    discretionary_diffs = {}
    
    for d in decisions:
        t = d["ticker"]
        cur_w = (state["holdings"].get(t, 0) * closes[t]) / nav
        diff = abs(d["target_weight"] - cur_w)
        turnover += diff / 2.0
        
        if t not in rm.get("VETO", set()):
            discretionary_diffs[t] = diff
            
    if turnover <= max_turnover:
        return decisions, None
        
    excess = turnover - max_turnover
    discretionary_pool = sum(discretionary_diffs.values()) / 2.0
    
    if discretionary_pool <= 0:
        return decisions, "turnover limit exceeded but all trades are mandatory VETO exits"
        
    scale = max(0.0, 1.0 - (excess / discretionary_pool))
    for d in decisions:
        t = d["ticker"]
        if t in discretionary_diffs:
            cur_w = (state["holdings"].get(t, 0) * closes[t]) / nav
            new_diff = (d["target_weight"] - cur_w) * scale
            d["target_weight"] = round(cur_w + new_diff, 4)
            
    return decisions, f"discretionary trades scaled by {scale:.1%} to meet {max_turnover:.1%} turnover limit"

def derive_decisions(portfolio, exits, state, closes, limits: Limits, caps=None,
                     scores=None, rm=None, skipped=None):
    caps, rm = caps or {}, rm or {"VETO": set()}
    skipped = skipped if skipped is not None else []
    decisions, no_trade, n = [], [], 0
    nav, n_held = state["nav"], len(state["holdings"])
    gate_on = scores is not None and n_held > 0          # day 1 builds freely
    for p in portfolio:
        t, w, cur = p["ticker"], p["target_weight"], state["holdings"].get(p["ticker"], 0)
        cap_t = min(limits.cap(t), caps.get(t, 1.0))
        tgt = target_shares(w, nav, closes[t])
        cur_w = cur * closes[t] / nav
        
        # Dynamic band: 20% relative drift, floored at 0.3% (to cover fees), capped by the global absolute limit
        allowed_drift = max(0.003, min(w * 0.20, limits.no_trade_band))
        in_band = cur > 0 and cur_w <= cap_t and abs(w - cur_w) <= allowed_drift
        
        if tgt == cur or in_band:
            rel_dev = abs(w - cur_w) / max(w, 0.0001)
            no_trade.append({"ticker": t, "reason_refs": p["inference_refs"][:5],
                             "reason": f"目標權重 {w:.2%} 與現權重 {cur_w:.2%} 差距在動態無交易帶內 (允許偏差 {allowed_drift:.2%} / 相對 {rel_dev:.1%})，維持不動"})
            continue
        if gate_on:
            # --- Req 1 & 5: Minimum holding period ---
            days_held = state.get("days_held", {}).get(t, 0)
            if tgt < cur and days_held < 5 and t not in rm["VETO"]:
                skipped.append(f"{t}: Minimum 5-day holding period not met (held {days_held} days)")
                no_trade.append({"ticker": t, "reason_refs": p["inference_refs"][:5],
                                 "reason": f"未滿5日最低持有期限制 (已持有 {days_held} 日)"})
                continue

            forced = cur_w > cap_t + 1e-9 or (cur == 0 and n_held < limits.min_n)
            ok, why = cost_gate("BUY" if tgt > cur else "SELL", abs(tgt - cur), closes[t], nav,
                                scores.get(t), limits, forced)
            if not ok:
                skipped.append(f"{t}: {why}")
                no_trade.append({"ticker": t, "reason_refs": p["inference_refs"][:5],
                                 "reason": f"交易成本檢核：{why}，維持現有部位"[:300]})
                continue
        n += 1
        action = "BUY" if cur == 0 else ("ADD" if tgt > cur else "TRIM")
        official = "25%" if t == "2330" else "10%"
        decisions.append({"decision_id": f"D{n}", "ticker": t, "action": action, "target_weight": w,
                          "inference_refs": p["inference_refs"][:8],
                          "risk_check": f"目標權重 {w:.2%} ≤ 內部上限 {cap_t:.1%}（官方 {official}）"})
    for e in exits:
        t, cur = e["ticker"], state["holdings"].get(e["ticker"], 0)
        if gate_on and t not in rm["VETO"]:
            # --- Req 1 & 5: Minimum holding period ---
            days_held = state.get("days_held", {}).get(t, 0)
            if days_held < 5:
                skipped.append(f"{t}: Minimum 5-day holding period not met (held {days_held} days)")
                no_trade.append({"ticker": t, "reason_refs": e["inference_refs"][:5],
                                 "reason": f"未滿5日最低持有期限制 (已持有 {days_held} 日)"})
                continue

            ok, why = cost_gate("SELL", cur, closes[t], nav, scores.get(t), limits, check_size=False)
            if not ok:
                skipped.append(f"{t}: {why}")
                no_trade.append({"ticker": t, "reason_refs": e["inference_refs"][:5],
                                 "reason": f"出清未通過交易成本檢核：{why}，續抱"[:300]})
                continue
        n += 1
        decisions.append({"decision_id": f"D{n}", "ticker": t, "action": "SELL_ALL",
                          "target_weight": 0.0, "inference_refs": e["inference_refs"][:8],
                          "risk_check": "出清持股，釋出資金"})
    return decisions, no_trade


def widen_cash_range(mv: dict, report: dict, limits: Limits):
    """If the projected cash is inside the policy window but outside the LLM's declared range,
    widen the declared range to include it (pure arithmetic; avoids a pointless retry)."""
    lo, hi = mv["target_cash_pct_range"]
    c = report["cash_ratio"]
    if lo <= c <= hi or not limits.cash_lo <= c <= limits.cash_hi:
        return None
    mv["target_cash_pct_range"] = [round(min(lo, c - 0.005), 3), round(max(hi, c + 0.005), 3)]
    return f"declared cash range widened to {mv['target_cash_pct_range']} (projected {c:.2%})"


def sync_intent(mv: dict, report: dict, limits: Limits):
    """The declared net_exposure_intent must match the real net flow; correct it when it does not."""
    flow, cur = report["net_flow_pct_nav"], mv.get("net_exposure_intent")
    if (cur == "increase" and flow > 0) or (cur == "reduce" and flow < 0) or (cur == "hold" and abs(flow) <= limits.hold_tol):
        return None
    want = "increase" if flow > limits.hold_tol else ("reduce" if flow < -limits.hold_tol else "hold")
    mv["net_exposure_intent"] = want
    return f"net_exposure_intent corrected {cur} -> {want} (net flow {flow:+.2%} NAV)"


# ===================================================================== main loop
def run_agent(
    trade_date, state, market, universe, book, strategy, limits, started_at,
    ask=None, factor_df=None, regime="neutral", journal_ctx=None, as_of=None, risk_ctx=None,
    tech_ctx=None, desk_notes=None
):
    prices = market["prices"]
    closes = {t: p["close"] for t, p in prices.items() if t in universe.names}
    closes.update(state["last_close"])           # the server's own close is authoritative for held stocks
    all_candidates = [t for t in universe.ordered() if t in closes]
    missing_held = [t for t in state["holdings"] if t not in closes]
    if missing_held:
        raise RuntimeError(f"no previous close for held stocks {missing_held}; cannot size orders")

    # --- Req 4: Smooth market regime changes (max 3% cash drift per day) ---
    import dataclasses
    
    prev_cash_ratio = state["cash"] / state["nav"]
    max_cash_shift = 0.03
    
    new_cash_lo = max(limits.cash_lo, prev_cash_ratio - max_cash_shift)
    new_cash_hi = min(limits.cash_hi, prev_cash_ratio + max_cash_shift)
    
    # Ensure window is valid if limits inverted
    if new_cash_lo > new_cash_hi:
        new_cash_lo = min(new_cash_lo, new_cash_hi)
        
    limits = dataclasses.replace(limits, cash_lo=new_cash_lo, cash_hi=new_cash_hi)

    rm = check_agent.risk_maps(risk_ctx)
    caps = weight_caps(limits, rm)
    candidates = build_shortlist(all_candidates, state["holdings"], factor_df, universe, rm)
    baseline = baseline_weights(candidates, factor_df, universe, limits, state, closes, caps)
    print(f"  shortlist: {len(candidates)} of {len(all_candidates)} stocks | baseline: {len(baseline)} names"
          f" | veto {sorted(rm['VETO'])} reduce {sorted(rm['REDUCE'])}")

    feedback, tot_in, tot_out, models, notes = None, 0, 0, [], []
    system_prompt = build_system_prompt(limits, universe)

    def prompt(fb=None, rn=None):
        return build_user_prompt(strategy, state, universe, book, candidates, closes, factor_df=factor_df,
                                 baseline_weights=baseline, regime=regime, journal_ctx=journal_ctx, feedback=fb,
                                 risk_ctx=risk_ctx, research_notes=rn, limits=limits, caps=caps,
                                 tech_ctx=tech_ctx, desk_notes=desk_notes)

    research_notes, extra_calls = None, 0
    if ask is None and os.environ.get("AGENT_AUTONOMY", "1") != "0":     # live run only (not --replay)
        try:
            from agents import portfolio_research
            research_notes, u, extra_calls, ms = portfolio_research.run(
                system_prompt, prompt(), state, universe, book, closes, limits, as_of or state["as_of_date"],
                risk_ctx=risk_ctx)
            tot_in, tot_out, models = u["input_tokens"], u["output_tokens"], list(ms)
        except Exception as e:
            notes.append(f"portfolio research skipped: {type(e).__name__}: {str(e)[:120]}")
    ctx = {"limits": limits, "obs_ids": {o["obs_id"] for o in book.observations},
           "market_ids": set(book.market_level_ids()), "candidates": set(candidates),
           "holdings": state["holdings"], "rm": rm, "caps": caps,
           "lot_w": {t: lot_weight(state["nav"], closes[t]) for t in candidates},
           "risk_considerations": (risk_ctx or {}).get("considerations", [])}
           
    score_map = (factor_df.set_index("ticker")["score"].to_dict() 
                 if factor_df is not None and not factor_df.empty else None)
    if ask is None:
        from llm import ask_json_meta as ask
    (ROOT / "logs").mkdir(exist_ok=True)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        user = prompt(feedback, research_notes)
        try:
            out, meta, raw = ask(system_prompt, user)
        except Exception as e:                   # broken JSON / API error -> let the LLM try again
            feedback = [f"output could not be parsed or the call failed: {type(e).__name__}: {str(e)[:200]}"]
            print(f"[attempt {attempt}] {feedback[0]}\n{traceback.format_exc()}")
            continue
        tot_in += meta["input_tokens"]; tot_out += meta["output_tokens"]
        if meta["model"] not in models:
            models.append(meta["model"])
        (ROOT / "logs" / f"{trade_date}_attempt{attempt}.txt").write_text(raw, encoding="utf-8")

        mv, infs, portfolio, exits = _split_llm(out)
        errs = check_structure(mv, infs, portfolio, exits, ctx)
        weights_map = {p.get("ticker"): p.get("target_weight") for p in portfolio if p.get("ticker")}
        errs += check_agent.check_narrative_consistency(infs, weights_map)
        if not errs:
            errs += reconcile.precheck_plan(portfolio, exits, state, closes, limits)
      
        
        attempt_notes = []
        if not errs:
            portfolio, note = normalize_weights(portfolio, limits, caps)
            attempt_notes += [note] if note else []
            errs += [f"{p['ticker']} weight {p['target_weight']} buys zero shares after normalisation"
                     for p in portfolio if target_shares(p["target_weight"], state["nav"], closes[p["ticker"]]) == 0]
        if not errs:
            skipped = []
            decisions, no_trade = derive_decisions(portfolio, exits, state, closes, limits, caps,
                                                   scores=score_map, rm=rm, skipped=skipped)
            # --- Req 3: Stricter turnover limit ---
            decisions, turn_note = enforce_turnover_limit(decisions, state, closes, rm, limits.max_turnover)
            if turn_note: attempt_notes.append(turn_note)

            try:
                decisions, no_trade, orders, _, rec_notes = reconcile.apply(
                    decisions, no_trade, infs, mv, portfolio, exits, state, closes, limits,
                    score_map, rm, skipped)
                attempt_notes += rec_notes + [f"cost gate kept: {s}" for s in skipped]
            except ValueError as e:
                errs.append(str(e))
        if not errs:
            report = project_portfolio(orders, state, closes)
            for fix in (widen_cash_range(mv, report, limits), sync_intent(mv, report, limits)):
                attempt_notes += [fix] if fix else []
            errs = (check_agent.check_portfolio(report, limits, state) + check_agent.check_posture(mv, report, limits)
                    + check_agent.check_theme(report, universe, limits)
                    + check_agent.check_theme_caps(report, universe, limits)
                    + check_agent.check_projected_risk(report["weights"], tech_ctx))
        if errs:
            feedback = errs
            print(f"[attempt {attempt}] {len(errs)} check(s) failed:")
            for m in errs:
                print("   -", m)
            continue

        notes += attempt_notes
        result = {
            "trade_date": trade_date,
            "evidence": {"sources": book.sources, "observations": book.observations},
            "market_view": {
                "basis_refs": mv["basis_refs"], "logic": mv["logic"], "regime": mv["regime"], "stance": mv["stance"],
                "posture": {"net_exposure_intent": mv["net_exposure_intent"],
                            "target_cash_pct_range": mv["target_cash_pct_range"]},
                "counter_evidence": mv.get("counter_evidence"),
                **({"confidence": mv["confidence"]} if isinstance(mv.get("confidence"), (int, float)) else {}),
            },
            "inferences": [{k: v for k, v in i.items() if not (k == "confidence" and v is None)} for i in infs],
            "decisions": decisions, "no_trade_decisions": no_trade, "orders": orders,
            "preview": report, "notes": notes,
            "llm_meta": {"models": models, "model": models[-1], "input_tokens": tot_in,
                         "output_tokens": tot_out, "attempts": attempt,
                         "tool_calls": extra_calls},
            "run_started_at": started_at, "generated_at": iso(now_tw()),
        }
        out_dir = ROOT / "outputs"
        out_dir.mkdir(exist_ok=True)
        hhmm = now_tw().strftime("%H%M")
        payload_txt = json.dumps(result, ensure_ascii=False, indent=1)
        (out_dir / f"agent_result_{trade_date}_{hhmm}.json").write_text(payload_txt, encoding="utf-8")
        (out_dir / f"agent_result_{trade_date}.json").write_text(payload_txt, encoding="utf-8")
        result["_closes"] = closes
        return result

    # --- DETERMINISTIC FALLBACK WHEN ALL LLM ATTEMPTS FAIL ---
    print(f"[WARN] All {MAX_ATTEMPTS} LLM attempts failed ({feedback}); engaging deterministic score-based fallback.")
    safe_candidates = [t for t in all_candidates if t not in rm["VETO"]]   # vetoed holdings fall out -> exits
    fb_out = _build_fallback_llm_output(state, universe, book, safe_candidates, closes, limits, factor_df, regime, caps)
    mv, infs, portfolio, exits = _split_llm(fb_out)
    portfolio, _ = normalize_weights(portfolio, limits, caps)
    decisions, no_trade = derive_decisions(portfolio, exits, state, closes, limits, caps)
    
    # --- Req 3: Stricter turnover limit ---
    decisions, turn_note = enforce_turnover_limit(decisions, state, closes, rm, limits.max_turnover)
    if turn_note: notes.append(turn_note)
    
    decisions, no_trade, orders, report, rec_notes = reconcile.apply(
        decisions, no_trade, infs, mv, portfolio, exits, state, closes, limits,
        score_map, rm, None)
    notes += rec_notes
    widen_cash_range(mv, report, limits)
    # Sync net_exposure_intent with actual net_flow_pct_nav
    flow = report["net_flow_pct_nav"]
    mv["net_exposure_intent"] = "increase" if flow > limits.hold_tol else ("reduce" if flow < -limits.hold_tol else "hold")
    result = {
        "trade_date": trade_date,
        "evidence": {"sources": book.sources, "observations": book.observations},
        "market_view": {
            "basis_refs": mv["basis_refs"], "logic": mv["logic"], "regime": mv["regime"], "stance": mv["stance"],
            "posture": {"net_exposure_intent": mv["net_exposure_intent"],
                        "target_cash_pct_range": mv["target_cash_pct_range"]},
            "counter_evidence": mv.get("counter_evidence"), "confidence": mv.get("confidence", 0.65),
        },
        "inferences": infs, "decisions": decisions, "no_trade_decisions": no_trade,
        "orders": orders, "preview": report, "notes": notes + ["deterministic fallback used"],
        "llm_meta": {"models": models or ["fallback"], "model": (models or ["fallback"])[-1],
                     "input_tokens": tot_in, "output_tokens": tot_out, "attempts": MAX_ATTEMPTS,
                     "tool_calls": extra_calls},
        "run_started_at": started_at, "generated_at": iso(now_tw()),
    }
    out_dir = ROOT / "outputs"
    out_dir.mkdir(exist_ok=True)
    hhmm = now_tw().strftime("%H%M")
    payload_txt = json.dumps(result, ensure_ascii=False, indent=1)
    (out_dir / f"agent_result_{trade_date}_{hhmm}.json").write_text(payload_txt, encoding="utf-8")
    (out_dir / f"agent_result_{trade_date}.json").write_text(payload_txt, encoding="utf-8")
    result["_closes"] = closes
    return result


def print_result(r: dict, universe) -> None:
    p, mv = r["preview"], r["market_view"]
    print(f"\n✅ passed | attempts {r['llm_meta']['attempts']} | orders {len(r['orders'])} | holdings {p['holdings_count']} "
          f"| cash {p['cash_ratio']:.2%} | net flow {p['net_flow_pct_nav']:+.2%} NAV")
    print(f"market view {mv['regime']} / {mv['stance']} / {mv['posture']['net_exposure_intent']} "
          f"cash range {mv['posture']['target_cash_pct_range']}")
    print("top weights:", {t: f"{w:.1%}" for t, w in list(p["weights"].items())[:8]})
    print(f"core themes total {sum(w for t, w in p['weights'].items() if universe.is_core(t)):.1%}")
    for n in r["notes"]:
        print("  note:", n)
