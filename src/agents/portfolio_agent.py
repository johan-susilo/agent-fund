"""Portfolio agent: the only LLM step. Turns sourced evidence into target weights.

The LLM supplies market view, inferences and target weights. Everything mechanical is code:
weight normalisation, BUY/ADD/TRIM/SELL_ALL/no-trade classification, share counts, and all
rule checks (via check_agent). A failed check is fed back to the LLM for a retry.

Usage (normally called by run_daily.py):
    run_agent(trade_date, state, market, universe, book, strategy, limits, started_at)
"""
from __future__ import annotations

import json
import os
import traceback

from agents import check_agent
from derive_orders import derive_orders, lot_weight, project_portfolio, target_shares
from settings import PROMPT_DIR, ROOT, Limits
from util import iso, now_tw

MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", 3))


# ===================================================================== prompt construction
def build_system_prompt(limits: Limits, universe) -> str:
    text = (PROMPT_DIR / "system_prompt.md").read_text(encoding="utf-8")
    core = ", ".join(universe.themes[t]["label"].split(" — ")[0] for t in universe.core_ids)
    rep = {"MIN_N": limits.min_n, "MAX_N": limits.max_n, "CAP_2330": limits.cap_2330,
           "CAP_OTHER": limits.cap_other, "SUM_LO": limits.sum_lo, "SUM_HI": limits.sum_hi,
           "CASH_LO": limits.cash_lo, "CASH_HI": limits.cash_hi,
           "CASH_LO_PCT": f"{limits.cash_lo:.0%}", "CASH_HI_PCT": f"{limits.cash_hi:.0%}",
           "HOLD_TOL_PCT": f"{limits.hold_tol:.1%}", "CORE_MIN": f"{limits.core_min:.0%}",
           "BAND_PCT": f"{limits.no_trade_band:.1%}", "CORE_THEMES": core}
    for k, v in rep.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def build_user_prompt(strategy, state, universe, book, candidates, closes, feedback=None) -> str:
    nav, tobs = state["nav"], book.ticker_obs()
    current = [{"ticker": t, "shares": s, "weight": round(s * closes[t] / nav, 4)}
               for t, s in sorted(state["holdings"].items()) if t in closes]
    payload = {
        "trade_date": state["trade_date"],
        "etf_strategy": strategy,
        "account": {"nav": nav, "cash": state["cash"], "cash_ratio": round(state["cash"] / nav, 4),
                    "holdings_count": len(state["holdings"]), "current_holdings": current},
        "market_obs_ids": book.market_level_ids(),
        # compact keys keep ~150 rows cheap: t=ticker n=name theme lot_w=one-lot NAV share obs=evidence ids
        "candidates": [{"t": t, "n": universe.names.get(t, ""), "theme": universe.theme_of(t),
                        "core": universe.is_core(t), "lot_w": round(lot_weight(nav, closes[t]), 4),
                        "obs": tobs.get(t, [])} for t in candidates],
        # statements carry the numbers; the duplicate `values` dict is omitted to save tokens
        "observations": [{"id": o["obs_id"], "topic": o["topic"], "text": o["statement"]}
                         for o in book.observations],
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if feedback:
        text += ("\n\n[Your previous output failed the program's checks. Fix every item and return the full JSON again]\n"
                 + "\n".join(f"- {m}" for m in feedback))
    return text


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
    errs, lim = [], ctx["limits"]
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
            errs.append(f"{t} is not in the candidate universe")
            continue
        if not isinstance(w, (int, float)) or not 0 < w <= lim.cap(t) + 0.05:
            errs.append(f"{t} target_weight={w} invalid (cap {lim.cap(t)})")
        elif w < ctx["lot_w"][t]:
            errs.append(f"{t} target_weight {w} is below one lot ({ctx['lot_w'][t]:.4f}); raise it or drop the stock")
        if not p.get("inference_refs") or set(p["inference_refs"]) - inf_ids:
            errs.append(f"{t} inference_refs empty or unknown")
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
    if not lim.min_n <= len(portfolio) <= lim.max_n:
        errs.append(f"portfolio has {len(portfolio)} stocks, must be {lim.min_n}-{lim.max_n}")
    return errs


def normalize_weights(portfolio: list, limits: Limits):
    """Deterministic repair: clip to caps, then rescale the sum into [sum_lo, sum_hi].

    Saves an LLM retry for what is only arithmetic. Returns (portfolio, note_or_None).
    """
    w = {p["ticker"]: min(float(p["target_weight"]), limits.cap(p["ticker"])) for p in portfolio}
    before, s = sum(w.values()), sum(w.values())
    if not limits.sum_lo <= s <= limits.sum_hi:
        goal = limits.sum_hi - 0.002 if s > limits.sum_hi else limits.sum_lo + 0.002
        for _ in range(30):                       # proportional rescale; capped names stay capped
            s = sum(w.values())
            if abs(s - goal) < 1e-4:
                break
            free = [t for t in w if w[t] < limits.cap(t) - 1e-9] if s < goal else list(w)
            if not free:
                break
            k = (goal - s) / sum(w[t] for t in free)
            for t in free:
                w[t] = min(limits.cap(t), w[t] * (1 + k))
    out = [{**p, "target_weight": round(w[p["ticker"]], 4)} for p in portfolio]
    after = sum(p["target_weight"] for p in out)
    note = f"weights normalised: sum {before:.3f} -> {after:.3f}" if abs(after - before) > 0.002 else None
    return out, note


def derive_decisions(portfolio, exits, state, closes, limits: Limits):
    """Classify every stock as BUY / ADD / TRIM / SELL_ALL or no-trade (all derived in code)."""
    decisions, no_trade, n = [], [], 0
    nav = state["nav"]
    for p in portfolio:
        t, w, cur = p["ticker"], p["target_weight"], state["holdings"].get(p["ticker"], 0)
        tgt = target_shares(w, nav, closes[t])
        cur_w = cur * closes[t] / nav
        in_band = cur > 0 and cur_w <= limits.cap(t) and abs(w - cur_w) <= limits.no_trade_band
        if tgt == cur or in_band:
            no_trade.append({"ticker": t, "reason_refs": p["inference_refs"][:5],
                             "reason": f"目標權重 {w:.2%} 與現權重 {cur_w:.2%} 差距在 {limits.no_trade_band:.1%} 無交易帶內，維持不動"})
            continue
        n += 1
        action = "BUY" if cur == 0 else ("ADD" if tgt > cur else "TRIM")
        official = "25%" if t == "2330" else "10%"
        decisions.append({"decision_id": f"D{n}", "ticker": t, "action": action, "target_weight": w,
                          "inference_refs": p["inference_refs"][:8],
                          "risk_check": f"目標權重 {w:.2%} ≤ 內部上限 {limits.cap(t):.1%}（官方 {official}）"})
    for e in exits:
        n += 1
        decisions.append({"decision_id": f"D{n}", "ticker": e["ticker"], "action": "SELL_ALL",
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


# ===================================================================== main loop
def run_agent(trade_date, state, market, universe, book, strategy, limits, started_at, ask=None):
    prices = market["prices"]
    closes = {t: p["close"] for t, p in prices.items() if t in universe.names}
    closes.update(state["last_close"])           # the server's own close is authoritative for held stocks
    candidates = [t for t in universe.ordered() if t in closes]
    missing_held = [t for t in state["holdings"] if t not in closes]
    if missing_held:
        raise RuntimeError(f"no previous close for held stocks {missing_held}; cannot size orders")

    ctx = {"limits": limits, "obs_ids": {o["obs_id"] for o in book.observations},
           "market_ids": set(book.market_level_ids()), "candidates": set(candidates),
           "holdings": state["holdings"], "lot_w": {t: lot_weight(state["nav"], closes[t]) for t in candidates}}
    system_prompt = build_system_prompt(limits, universe)

    if ask is None:
        from llm import ask_json_meta as ask     # lazy import: offline tests and --replay need no SDK
    (ROOT / "logs").mkdir(exist_ok=True)

    feedback, tot_in, tot_out, models, notes = None, 0, 0, [], []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        user = build_user_prompt(strategy, state, universe, book, candidates, closes, feedback)
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
        attempt_notes = []
        if not errs:
            portfolio, note = normalize_weights(portfolio, limits)
            attempt_notes += [note] if note else []
            errs += [f"{p['ticker']} weight {p['target_weight']} buys zero shares after normalisation"
                     for p in portfolio if target_shares(p["target_weight"], state["nav"], closes[p["ticker"]]) == 0]
        if not errs:
            decisions, no_trade = derive_decisions(portfolio, exits, state, closes, limits)
            try:
                orders = derive_orders(decisions, state, closes)
            except ValueError as e:
                errs.append(str(e))
        if not errs:
            report = project_portfolio(orders, state, closes)
            note = widen_cash_range(mv, report, limits)
            attempt_notes += [note] if note else []
            errs = (check_agent.check_portfolio(report, limits) + check_agent.check_posture(mv, report, limits)
                    + check_agent.check_theme(report, universe, limits))
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
                         "output_tokens": tot_out, "attempts": attempt},
            "run_started_at": started_at, "generated_at": iso(now_tw()),
        }
        out_path = ROOT / "outputs" / f"agent_result_{trade_date}.json"
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        result["_closes"] = closes               # in-memory only (not written): reused by the final gate
        return result

    raise RuntimeError(f"all {MAX_ATTEMPTS} attempts failed the checks; last problems: {feedback}. "
                       "Do NOT submit an unchecked file today.")


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
