"""Post-gate reconciliation: keep decisions, orders and narrative telling the SAME story.

Root cause this fixes (2026-11-02 run): derive_decisions() applies the no-trade band, cost gate,
holding-period rule and turnover scaling AFTER the LLM wrote its plan. The old
repair_post_band_cash() then rewrote the target_weight of an unrelated "ADD" decision downward
to fix the cash window. Result: action=ADD, order=SELL, inference text says "加碼", and the
decision still carried the stale risk_check.

Invariants enforced here
  1. action is ALWAYS recomputed from the real share delta (BUY/ADD => BUY order, TRIM/SELL_ALL => SELL order).
  2. The cash window is repaired only in the direction the plan already points:
       cash too low  -> cancel discretionary buys, then promote the LLM's OWN trims/exits, then (last resort)
                        a rule-based pro-rata trim documented by an auto-generated inference;
       cash too high -> scale up existing BUY/ADD only.
     A BUY/ADD is never turned into a SELL.
  3. Whenever the executed result differs from what an inference said (blocked by a gate, cancelled by the cash
     window, ...) the inference text is annotated with 【執行調整】 so narrative == orders.
"""
from __future__ import annotations

from derive_orders import derive_orders, project_portfolio, target_shares
from settings import FEE_RATE, TAX_RATE

AUTO = "__AUTO__"          # placeholder inference id for rule-based synthetic trims
CASH_MARGIN = 0.003        # land this far inside the lower cash bound (prices drift before the fill)
HOLD_DAYS = 5              # same minimum holding period used in portfolio_agent.derive_decisions


def _cut(s, n):
    return s if s is None or len(s) <= n else s[: n - 1] + "…"


def _sign(x):
    return (x > 0) - (x < 0)


def _score(score_map, t):
    if not score_map:
        return 0.5
    v = score_map.get(t)
    return 0.5 if v is None or v != v else float(v)


# ---------------------------------------------------------------- plan container
class _Plan:
    def __init__(self, decisions, no_trade, state, closes):
        self.decisions, self.no_trade = list(decisions), list(no_trade)
        self.state, self.closes = state, closes
        self.orders, self.rep = [], {}

    @property
    def cash(self):
        return self.rep["cash_ratio"]

    def refresh(self):
        """Relabel every decision from its real share delta, drop no-ops, re-simulate."""
        nav, held = self.state["nav"], self.state["holdings"]
        keep = []
        for d in self.decisions:
            t = d["ticker"]
            cur = held.get(t, 0)
            tgt = target_shares(d["target_weight"], nav, self.closes[t])
            if tgt == cur:
                if cur > 0:
                    self.no_trade.append({"ticker": t, "reason_refs": d["inference_refs"][:5],
                                          "reason": "調整後目標股數與現有持股相同，維持不動"})
                continue
            d["action"] = "SELL_ALL" if tgt == 0 else ("BUY" if cur == 0 else ("ADD" if tgt > cur else "TRIM"))
            if tgt == 0:
                d["target_weight"] = 0.0
            keep.append(d)
        self.decisions = keep
        traded = {d["ticker"] for d in keep}
        seen, nt = set(), []
        for x in self.no_trade:                      # a ticker may not be in both lists, nor twice
            if x["ticker"] in traded or x["ticker"] in seen:
                continue
            seen.add(x["ticker"])
            nt.append(x)
        self.no_trade = nt
        for i, d in enumerate(self.decisions, 1):    # derive_orders needs unique ids
            d["decision_id"] = f"D{i}"
        self.orders = derive_orders(self.decisions, self.state, self.closes)
        self.rep = project_portfolio(self.orders, self.state, self.closes)
        return self.cash

    def snapshot(self):
        return list(self.decisions), list(self.no_trade)

    def restore(self, snap):
        self.decisions, self.no_trade = list(snap[0]), list(snap[1])
        self.refresh()


# ---------------------------------------------------------------- pre-check for the LLM retry loop
def precheck_plan(portfolio, exits, state, closes, limits):
    """Raw LLM plan must be self-funded: planned buys cannot push cash below the window."""
    nav, held = state["nav"], state["holdings"]
    cur_w = lambda t: held.get(t, 0) * closes.get(t, 0) / nav
    buy = sum(max(0.0, p["target_weight"] - cur_w(p["ticker"])) for p in portfolio)
    sell = sum(max(0.0, cur_w(p["ticker"]) - p["target_weight"]) for p in portfolio)
    sell += sum(cur_w(e["ticker"]) for e in exits)
    proj = state["cash"] / nav + sell * (1 - FEE_RATE - TAX_RATE) - buy * (1 + FEE_RATE)
    if proj < limits.cash_lo - 0.002:
        return [f"your plan projects cash {proj:.2%} (buys {buy:.2%} NAV, sells {sell:.2%} NAV) which is below the "
                f"cash window floor {limits.cash_lo:.0%}. Do NOT add/buy unless you fund it with explicit trims in the "
                f"same plan; reduce buys by about {limits.cash_lo - proj:.2%} NAV."]
    return []


# ---------------------------------------------------------------- main entry
def apply(decisions, no_trade, infs, mv, portfolio_raw, exits, state, closes, limits,
          score_map=None, rm=None, skipped=None):
    """Returns (decisions, no_trade, orders, report, notes). Mutates `infs` (annotates / appends one auto inference)."""
    rm = rm or {"VETO": set()}
    nav, held = state["nav"], state["holdings"]
    dh = state.get("days_held", {})
    why = {}
    for s in skipped or []:
        t, _, r = s.partition(":")
        why[t.strip()] = r.strip()

    plan = _Plan(decisions, no_trade, state, closes)
    plan.refresh()
    lo, hi = limits.cash_lo, limits.cash_hi
    goal = min(lo + CASH_MARGIN, (lo + hi) / 2)
    events, notes, auto_used = [], [], False
    cw = lambda t: held.get(t, 0) * closes[t] / nav
    intents = {p["ticker"]: p for p in portfolio_raw}
    c_start = plan.cash

    def add_decision(t, action, w, refs):
        plan.decisions.append({"decision_id": "D0", "ticker": t, "action": action, "target_weight": w,
                               "inference_refs": list(refs)[:8], "risk_check": ""})
        plan.no_trade = [x for x in plan.no_trade if x["ticker"] != t]
        plan.refresh()
        return any(x["ticker"] == t for x in plan.decisions)

    # ---------------- cash too LOW: raise cash without ever turning a buy into a sell
    if c_start < lo:
        # (a) cancel discretionary buys, weakest score first
        for d in sorted([x for x in plan.decisions if x["action"] in ("BUY", "ADD")],
                        key=lambda x: _score(score_map, x["ticker"])):
            if plan.cash >= goal:
                break
            t = d["ticker"]
            if held.get(t, 0) == 0 and plan.rep["holdings_count"] - 1 < limits.min_n:
                continue                                   # needed to reach the minimum holding count
            before = plan.cash
            plan.decisions.remove(d)
            if held.get(t, 0) > 0:
                plan.no_trade.append({"ticker": t, "reason_refs": d["inference_refs"][:5],
                                      "reason": _cut(f"加碼後現金比將為 {before:.2%}，低於現金窗下限 {lo:.0%}，取消加碼並維持不動", 300)})
            plan.refresh()
            events.append({"ticker": t, "refs": d["inference_refs"],
                           "text": f"{t} 原擬{'加碼' if held.get(t, 0) else '建倉'}，惟執行後現金比 {before:.2%} 低於現金窗下限 "
                                   f"{lo:.0%}，依規則取消，{'維持現有部位' if held.get(t, 0) else '不建倉'}。"})

        # (b) promote the LLM's OWN exits / trims that the band or cost gate had swallowed
        if plan.cash < goal:
            sold = {x["ticker"] for x in plan.decisions}
            cands = []
            for e in exits:
                t = e["ticker"]
                if held.get(t, 0) > 0 and t not in sold:
                    cands.append((0, dh.get(t, 0) < HOLD_DAYS, _score(score_map, t), t, "SELL_ALL", 0.0, e["inference_refs"]))
            for t, p in intents.items():
                cur = held.get(t, 0)
                tgt = target_shares(p["target_weight"], nav, closes[t])
                if cur > 0 and t not in sold and 0 < tgt < cur:
                    cands.append((1, dh.get(t, 0) < HOLD_DAYS, -(cur - tgt) * closes[t] / nav, t, "TRIM",
                                  p["target_weight"], p["inference_refs"]))
            cands.sort(key=lambda c: (c[0], c[1], c[2]))
            for _, _, _, t, act, w, refs in cands:
                if plan.cash >= goal:
                    break
                snap, before = plan.snapshot(), plan.cash
                ok = add_decision(t, act, w, refs)
                if not ok or plan.cash > hi:
                    plan.restore(snap)
                    continue
                events.append({"ticker": t, "refs": refs,
                               "text": f"{t} 依原推論意圖{'出清' if act == 'SELL_ALL' else '減碼'}，並因現金比 {before:.2%} "
                                       f"低於現金窗下限 {lo:.0%} 而確實執行（不受無交易帶／成本檢核攔阻）。"})

        # (c) last resort: rule-based pro-rata trim, documented by one auto inference
        if plan.cash < goal:
            sold = {x["ticker"] for x in plan.decisions}
            pool = sorted([t for t in held if t != "2330" and t not in sold],
                          key=lambda t: (dh.get(t, 0) < HOLD_DAYS, _score(score_map, t)))
            for t in pool:
                if plan.cash >= goal:
                    break
                snap = plan.snapshot()
                if not add_decision(t, "TRIM", round(cw(t) * 0.75, 4), [AUTO]) or plan.cash > hi:
                    plan.restore(snap)
                    continue
                auto_used = True
            if plan.cash < lo:
                notes.append(f"WARNING: cash {plan.cash:.2%} still below window floor {lo:.0%} after reconciliation")

    # ---------------- cash too HIGH: deploy only through existing BUY/ADD (direction preserved)
    elif c_start > hi:
        tgt_cash = hi - CASH_MARGIN if hi - CASH_MARGIN >= lo else (lo + hi) / 2
        for d in sorted([x for x in plan.decisions if x["action"] in ("BUY", "ADD")],
                        key=lambda x: -_score(score_map, x["ticker"])):
            if plan.cash <= tgt_cash:
                break
            t = d["ticker"]
            room = limits.cap(t) - 0.002 - d["target_weight"]
            if room <= 0.001:
                continue
            d["target_weight"] = round(d["target_weight"] + min(plan.cash - tgt_cash, room), 4)
            plan.refresh()

    if abs(plan.cash - c_start) > 1e-9:
        notes.append(f"reconciled cash ratio {c_start:.2%} -> {plan.cash:.2%} (window [{lo:.0%}, {hi:.0%}])")

    # ---------------- finalise: ids, realised risk_check, orders
    decisions, no_trade = plan.decisions, plan.no_trade
    for i, d in enumerate(decisions, 1):
        d["decision_id"] = f"D{i}"
    orders = derive_orders(decisions, state, closes)
    report = project_portfolio(orders, state, closes)
    delta = {o["ticker"]: (o["shares"] if o["side"] == "BUY" else -o["shares"]) for o in orders}
    for d in decisions:
        t = d["ticker"]
        w_real = (held.get(t, 0) + delta[t]) * closes[t] / nav
        official = "25%" if t == "2330" else "10%"
        d["risk_check"] = ("出清持股，釋出資金" if d["action"] == "SELL_ALL" else
                           f"{d['action']}：實際執行後權重 {w_real:.2%} ≤ 內部上限 {limits.cap(t):.1%}（官方 {official}）")

    # ---------------- auto inference for rule-based trims
    if auto_used:
        auto_id = f"I{len(infs) + 1}"
        trimmed = [d["ticker"] for d in decisions if AUTO in d["inference_refs"]]
        for d in decisions:
            d["inference_refs"] = [auto_id if r == AUTO else r for r in d["inference_refs"]]
        infs.append({"inf_id": auto_id, "premise_refs": list(mv.get("basis_refs", []))[:3],
                     "logic": _cut(f"執行層現金窗調節：計畫經無交易帶與成本檢核後，預估現金比 {c_start:.2%} 低於現金窗下限 {lo:.0%}，"
                                   f"且原推論所指減碼不足以補足；依預先定義之量化規則按評分由低至高、各減碼約四分之一持股："
                                   f"{'、'.join(trimmed)}。此為機械式現金調節，非新增市場判斷。", 1000),
                     "conclusion": _cut(f"減碼 {'、'.join(trimmed)} 以回到現金窗。", 300),
                     "counter_evidence": "減碼將降低既有部位的趨勢暴露，並產生交易成本。", "confidence": 0.6})

    # ---------------- residual mismatches: intended vs executed
    evented = {e["ticker"] for e in events}
    for p in portfolio_raw:
        t, w = p["ticker"], p["target_weight"]
        cur = held.get(t, 0)
        want = _sign(target_shares(w, nav, closes[t]) - cur)
        material = cur == 0 or abs(w - cw(t)) > 0.002   # 0.2% NAV: a stated trim/add must show up in the narrative
        got = _sign(delta.get(t, 0))
        if want and material and got != want and t not in evented:
            reason = why.get(t) or "未通過無交易帶／成本／持有期／現金窗檢核"
            events.append({"ticker": t, "refs": p["inference_refs"],
                           "text": f"{t} 原擬{'加碼' if want > 0 else '減碼'}，實際未執行（{reason}），維持現有部位。"})
    for e in exits:
        t = e["ticker"]
        if held.get(t, 0) > 0 and t not in evented and delta.get(t, 0) != -held[t]:
            reason = why.get(t) or "未通過成本／持有期檢核"
            events.append({"ticker": t, "refs": e["inference_refs"],
                           "text": f"{t} 原擬出清，實際未執行（{reason}），續抱。"})

    for ev in events:
        for ref in dict.fromkeys(ev["refs"]):
            inf = next((i for i in infs if i["inf_id"] == ref), None)
            if not inf or ref.startswith("I") is False:
                continue
            if auto_used and inf["inf_id"] == infs[-1]["inf_id"] and "現金窗調節" in inf["logic"]:
                continue
            inf["logic"] = _cut((inf["logic"] or "") + " 【執行調整】" + ev["text"], 1000)
            suffix = "｜執行調整：" + ev["text"]
            suffix = suffix[:299]
            inf["conclusion"] = (inf.get("conclusion") or "")[: 300 - len(suffix)] + suffix
        notes.append("narrative aligned: " + ev["text"])
    return decisions, no_trade, orders, report, notes
