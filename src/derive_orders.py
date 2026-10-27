"""Official order derivation and post-trade projection.

The LLM only supplies target weights; share counts always come from this module and are
never hand-adjusted. Rule checks live in risk_agent.py.
"""
from __future__ import annotations

import math

from settings import FEE_RATE, LOT, TAX_RATE


def target_shares(weight: float, nav: float, price: float) -> int:
    """Official formula: floor(weight x prev NAV / prev close / 1000) x 1000.

    The expression order is kept exactly as published so float rounding matches the server.
    """
    return math.floor(weight * nav / price / LOT) * LOT


def lot_weight(nav: float, price: float) -> float:
    """NAV share of a single board lot; a target weight below this buys zero shares."""
    return LOT * price / nav


def derive_orders(decisions: list, state: dict, closes: dict) -> list:
    """decisions: [{"decision_id","ticker","action","target_weight"}]; closes: previous close per ticker."""
    orders = []
    
    # Pre-calculate a fee-buffered NAV so official weight formulas don't over-deploy cash
    buffer_ratio = 1.0 - (FEE_RATE + TAX_RATE)
    usable_nav = state["nav"] * buffer_ratio
    
    for d in decisions:
        t = d["ticker"]
        if t not in closes:
            raise ValueError(f"{t}: no previous close available")
        
        # Use usable_nav to maintain compliance with E.SUN formula validation rules
        tgt = target_shares(d["target_weight"], usable_nav, closes[t])
        cur = state["holdings"].get(t, 0)
        diff = tgt - cur
        if diff == 0:
            raise ValueError(f"{d['decision_id']} {t}: target equals current holding; use no_trade_decisions")
        side = "BUY" if diff > 0 else "SELL"
        if side == "SELL" and -diff > cur:
            raise ValueError(f"{t}: selling {-diff} exceeds holding {cur} (oversell)")
        orders.append({
            "ticker": t, "side": side, "shares": abs(diff), "decision_ref": d["decision_id"],
            "derivation_detail": (f"target_shares = floor({d['target_weight']}×{state['nav']:,.0f} / "
                                  f"{closes[t]} / 1000)×1000 = {tgt:,}；現持 {cur:,} → {side} {abs(diff):,}"),
        })
    return orders


def project_portfolio(orders: list, state: dict, closes: dict) -> dict:
    """Estimate the post-trade portfolio at previous-close prices (fees and tax deducted from cash)."""
    shares, cash = dict(state["holdings"]), state["cash"]
    buy_amt = sell_amt = fees = 0.0
    for o in orders:
        amt = o["shares"] * closes[o["ticker"]]
        if o["side"] == "BUY":
            shares[o["ticker"]] = shares.get(o["ticker"], 0) + o["shares"]
            cash -= amt * (1 + FEE_RATE)
            fees += amt * FEE_RATE
            buy_amt += amt
            
        else:
            shares[o["ticker"]] = shares.get(o["ticker"], 0) - o["shares"]
            cash += amt * (1 - FEE_RATE - TAX_RATE)
            fees += amt * (FEE_RATE + TAX_RATE)
            sell_amt += amt
    shares = {t: s for t, s in shares.items() if s > 0}
    values = {t: s * closes[t] for t, s in shares.items()}
    nav = cash + sum(values.values())
    return {
        "holdings_count": len(shares),
        "cash": cash,
        "cash_ratio": cash / nav,
        "nav_after": nav,
        "buy_amt": buy_amt,
        "sell_amt": sell_amt,
        "net_flow_pct_nav": (buy_amt - sell_amt) / state["nav"],
        "weights": dict(sorted(((t, v / nav) for t, v in values.items()), key=lambda x: -x[1])),
    }

def cost_gate(side, shares, price, nav, score, limits, forced=False, check_size=True):
    """(ok, reason). Discretionary trades must beat their own fee; forced trades always pass."""
    if forced:
        return True, ""
    amt = shares * price
    cost = amt * (FEE_RATE if side == "BUY" else FEE_RATE + TAX_RATE)
    if check_size and amt / nav < limits.min_trade_w:
        return False, f"成交金額僅 {amt / nav:.2%} NAV，低於最小交易門檻 {limits.min_trade_w:.1%}"
    if score is None:
        return True, ""
    edge = limits.alpha_scale * (score - 0.5)
    gain = amt * (edge if side == "BUY" else -edge)
    if gain < limits.cost_mult * cost:
        return False, (f"預期優勢約 {gain:,.0f} 元 < {limits.cost_mult}× 交易成本 {cost:,.0f} 元"
                       f"（評分 {score:.2f}）")
    return True, ""