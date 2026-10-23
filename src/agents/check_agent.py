"""Check agent: deterministic rule engine (no LLM) shared by the whole pipeline.

Two layers:
  * pre-trade checks used inside the portfolio agent's retry loop (check_portfolio / posture / theme)
  * final_gate(): an independent re-validation of the finished D-Plan against the JSON schema,
    the reference chain, the official order formula and the official limits. If it fails,
    the D-Plan must not be submitted (a bad submission costs a warning; three = disqualified).
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta

from derive_orders import derive_orders, project_portfolio
from settings import CONFIG_DIR, OFFICIAL, SUBMIT_CLOSE, SUBMIT_OPEN, TW, Limits
from util import parse_ts


# ===================================================== pre-trade checks (internal, buffered limits)
def check_portfolio(report: dict, limits: Limits) -> list:
    errs = []
    n, cash = report["holdings_count"], report["cash_ratio"]
    if not limits.min_n <= n <= limits.max_n:
        errs.append(f"projected holdings {n}, must be {limits.min_n}-{limits.max_n}")
    if cash < 0:
        errs.append(f"projected cash is negative ({report['cash']:,.0f})")
    elif not limits.cash_lo <= cash <= limits.cash_hi:
        errs.append(f"projected cash ratio {cash:.2%} outside policy window [{limits.cash_lo:.0%}, {limits.cash_hi:.0%}]")
    for t, w in report["weights"].items():
        if w > limits.cap(t):
            errs.append(f"{t} projected weight {w:.2%} exceeds internal cap {limits.cap(t):.1%}")
    return errs


def check_posture(mv: dict, report: dict, limits: Limits) -> list:
    """The declared posture is a commitment: orders must match it."""
    errs, flow = [], report["net_flow_pct_nav"]
    intent = mv["net_exposure_intent"]
    if intent == "increase" and not flow > 0:
        errs.append(f"declared increase but net flow is {flow:+.2%} (must be net buying)")
    if intent == "reduce" and not flow < 0:
        errs.append(f"declared reduce but net flow is {flow:+.2%} (must be net selling)")
    if intent == "hold" and abs(flow) > limits.hold_tol:
        errs.append(f"declared hold but net flow {flow:+.2%} exceeds ±{limits.hold_tol:.1%}")
    lo, hi = mv["target_cash_pct_range"]
    if not lo <= report["cash_ratio"] <= hi:
        errs.append(f"projected cash {report['cash_ratio']:.2%} outside declared range [{lo:.2%}, {hi:.2%}]")
    return errs


def check_theme(report: dict, universe, limits: Limits) -> list:
    core = sum(w for t, w in report["weights"].items() if universe.is_core(t))
    if core < limits.core_min:
        return [f"core AI/semiconductor themes total {core:.1%}, below {limits.core_min:.0%}: "
                "inconsistent with the declared ETF theme"]
    return []


def check_submission_window(now: datetime, trade_date: str, as_of_date) -> list:
    """Warn (not fail) when outside 19:30 on the previous trading day .. 08:55 on the trade date."""
    close = datetime.fromisoformat(trade_date).replace(hour=SUBMIT_CLOSE[0], minute=SUBMIT_CLOSE[1], tzinfo=TW)
    warns = []
    if now > close:
        warns.append(f"now is after the submission deadline ({close:%Y-%m-%d %H:%M}); the server will reject or ignore it")
    if as_of_date:
        opens = datetime.fromisoformat(as_of_date).replace(hour=SUBMIT_OPEN[0], minute=SUBMIT_OPEN[1], tzinfo=TW)
        if now < opens:
            warns.append(f"submission window opens {opens:%Y-%m-%d %H:%M}")
    return warns


# ===================================================== final gate on the finished D-Plan
def _seq(items, key, prefix):
    return [i[key] for i in items] == [f"{prefix}{k}" for k in range(1, len(items) + 1)]


def _chain(d: dict) -> list:
    errs = []
    for name, key, p in (("sources", "source_id", "S"), ("observations", "obs_id", "O"),
                         ("inferences", "inf_id", "I"), ("decisions", "decision_id", "D")):
        if not _seq(d[name], key, p):
            errs.append(f"{name}: ids must be {p}1..{p}n consecutive without gaps")
    S = {s["source_id"] for s in d["sources"]}
    O = {o["obs_id"] for o in d["observations"]}
    I = {i["inf_id"] for i in d["inferences"]}
    D = {x["decision_id"] for x in d["decisions"]}
    for o in d["observations"]:
        if not o["source_ref"] or set(o["source_ref"]) - S:
            errs.append(f"{o['obs_id']}: broken source_ref")
    if set(d["market_view"]["basis_refs"]) - O or not d["market_view"]["basis_refs"]:
        errs.append("market_view.basis_refs broken")
    for i in d["inferences"]:
        if not i["premise_refs"] or set(i["premise_refs"]) - O:
            errs.append(f"{i['inf_id']}: broken premise_refs")
    for x in d["decisions"]:
        if not x["inference_refs"] or set(x["inference_refs"]) - I:
            errs.append(f"{x['decision_id']}: broken inference_refs (orphan decision)")
    for n in d["no_trade_decisions"]:
        if not n["reason_refs"] or set(n["reason_refs"]) - I:
            errs.append(f"no_trade {n['ticker']}: broken reason_refs")
    for o in d["orders"]:
        if o["decision_ref"] not in D:
            errs.append(f"order {o['ticker']}: broken decision_ref")
    return errs


def _coverage(d: dict, state: dict, universe) -> list:
    errs = []
    dec = [x["ticker"] for x in d["decisions"]]
    nt = [x["ticker"] for x in d["no_trade_decisions"]]
    if len(set(dec)) != len(dec) or len(set(nt)) != len(nt) or set(dec) & set(nt):
        errs.append("a ticker is duplicated within or across decisions / no_trade_decisions")
    missing = set(state["holdings"]) - set(dec) - set(nt)
    if missing:
        errs.append(f"yesterday's holdings not accounted for: {sorted(missing)}")
    for t in set(dec) | set(nt):
        if t not in universe.names:
            errs.append(f"{t} is outside the 150-stock universe")
    for t in set(nt) - set(state["holdings"]):
        errs.append(f"no_trade {t} is not a current holding")
    for x in d["decisions"]:
        cur = state["holdings"].get(x["ticker"], 0)
        a = x["action"]
        if a == "BUY" and cur:
            errs.append(f"{x['decision_id']} BUY on an existing holding")
        if a in ("ADD", "TRIM", "SELL_ALL") and not cur:
            errs.append(f"{x['decision_id']} {a} on a stock not held")
        if a == "SELL_ALL" and x["target_weight"] != 0:
            errs.append(f"{x['decision_id']} SELL_ALL must have target_weight 0")
    return errs


def _times(d: dict) -> list:
    errs, meta = [], d["agent_metadata"]
    done = parse_ts(meta["run_completed_at"])
    if done < parse_ts(meta["run_started_at"]):
        errs.append("run_completed_at earlier than run_started_at")
    if done > datetime.now(TW) + timedelta(seconds=5):
        errs.append("run_completed_at is in the future")
    for s in d["sources"]:        # no look-ahead: nothing the run could not have seen yet
        for k in ("content_as_of", "published_at", "fetched_at"):
            if s.get(k) and parse_ts(s[k]) > done:
                errs.append(f"{s['source_id']}.{k} is later than run_completed_at (look-ahead)")
    return errs


def _schema(d: dict):
    try:
        import jsonschema
    except ImportError:
        return [], ["jsonschema not installed: schema validation skipped (pip install jsonschema)"]
    schema = json.loads((CONFIG_DIR / "D-Plan.schema.json").read_text(encoding="utf-8"))
    errs = [f"schema: {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:150]}"
            for e in jsonschema.Draft202012Validator(schema).iter_errors(d)]
    return errs[:10], []


def final_gate(d: dict, state: dict, closes: dict, universe, limits: Limits, filename: str = ""):
    """Return (errors, warnings). Any error means: do NOT submit this file."""
    schema_errs, schema_warns = _schema(d)
    errs, warns = list(schema_errs), list(schema_warns)
    errs += _chain(d) + _coverage(d, state, universe) + _times(d)

    if d["team_id"] != state["team_id"] or d["trade_date"] != state["trade_date"]:
        errs.append("team_id / trade_date do not match the DataPack")
    if filename and filename != f"D-Plan_{d['team_id']}_{d['trade_date']}.json":
        errs.append(f"file name '{filename}' does not match D-Plan_<team_id>_<trade_date>.json")
    for o in d["orders"]:
        if o["shares"] % 1000 or o["shares"] < 1000:
            errs.append(f"order {o['ticker']}: shares must be a positive multiple of 1000")
        if o["side"] == "SELL" and o["shares"] > state["holdings"].get(o["ticker"], 0):
            errs.append(f"order {o['ticker']}: oversell")

    # Re-derive every order with the official formula and compare ticker / side / shares.
    try:
        want = {(o["ticker"], o["side"], o["shares"]) for o in derive_orders(d["decisions"], state, closes)}
        have = {(o["ticker"], o["side"], o["shares"]) for o in d["orders"]}
        if want != have:
            errs.append(f"orders differ from the official derivation: only-derived={sorted(want - have)} "
                        f"only-submitted={sorted(have - want)}")
        if len(have) != len(d["orders"]):
            errs.append("duplicate orders for the same ticker/side/shares")
    except ValueError as e:
        errs.append(f"order derivation failed: {e}")
        return errs, warns

    # Post-trade projection against the internal (buffered) limits, then the official ones.
    rep = project_portfolio(d["orders"], state, closes)
    mv = d["market_view"]
    errs += check_portfolio(rep, limits)
    errs += check_posture({"net_exposure_intent": mv["posture"]["net_exposure_intent"],
                           "target_cash_pct_range": mv["posture"]["target_cash_pct_range"]}, rep, limits)
    errs += check_theme(rep, universe, limits)
    if not OFFICIAL["min_n"] <= rep["holdings_count"] <= OFFICIAL["max_n"] or rep["cash_ratio"] > OFFICIAL["max_cash"]:
        errs.append("OFFICIAL limit breached (holdings count or cash ratio)")
    for t, w in rep["weights"].items():
        if w > (OFFICIAL["cap_2330"] if t == "2330" else OFFICIAL["cap_other"]):
            errs.append(f"OFFICIAL cap breached by {t}: {w:.2%}")
    if any(not math.isfinite(v) for v in rep["weights"].values()):
        errs.append("non-finite weight in projection")
    return errs, warns

