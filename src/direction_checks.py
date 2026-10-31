"""Final-gate checks that decisions, orders and narrative agree. Imported by check_agent.final_gate()."""
from __future__ import annotations
import re

_ADD = r"(?:加碼|買進|建倉|納入|新增)"
_EXIT = r"(?:出清|排除|剔除|清倉)"
_NEG = r"(?:不|未|勿|避免|暫緩|無需|不予)"


def check_direction(d: dict) -> list:
    """ERRORS: action label must match the order side; every decision needs exactly one order."""
    errs, by_id = [], {x["decision_id"]: x for x in d["decisions"]}
    n_orders = {}
    for o in d["orders"]:
        dec = by_id.get(o["decision_ref"])
        if not dec:
            continue
        n_orders[dec["decision_id"]] = n_orders.get(dec["decision_id"], 0) + 1
        buy_action = dec["action"] in ("BUY", "ADD")
        if buy_action != (o["side"] == "BUY"):
            errs.append(f"DIRECTION: {dec['decision_id']} {dec['ticker']} action={dec['action']} but order side={o['side']}")
    for did, dec in by_id.items():
        if n_orders.get(did, 0) != 1:
            errs.append(f"{did} {dec['ticker']} has {n_orders.get(did, 0)} orders (expected exactly 1)")
    return errs


def check_narrative_vs_orders(d: dict, state: dict) -> list:
    """WARNINGS (heuristic, never blocks): inference text promises a move the orders do not make."""
    warns = []
    sells = {o["ticker"] for o in d["orders"] if o["side"] == "SELL"}
    buys = {o["ticker"] for o in d["orders"] if o["side"] == "BUY"}
    for inf in d["inferences"]:
        text = f"{inf.get('logic', '')} {inf.get('conclusion', '')}"
        if "執行調整" in text:
            continue                                   # already reconciled by reconcile.apply
        for m in re.finditer(rf"({_NEG})?\s*{_ADD}[^0-9。；]{{0,12}}?(\d{{4}})", text):
            t = m.group(2)
            if not m.group(1) and t in sells:
                warns.append(f"NARRATIVE: {inf['inf_id']} says add/buy {t} but the order is SELL")
        for m in re.finditer(rf"({_NEG})?\s*{_EXIT}[^0-9。；]{{0,12}}?(\d{{4}})", text):
            t = m.group(2)
            if not m.group(1) and t in state["holdings"] and t not in sells:
                warns.append(f"NARRATIVE: {inf['inf_id']} says exit {t} but there is no SELL order")
    return warns
