from __future__ import annotations
from settings import FEE_RATE, TAX_RATE

def estimate_trade_cost(trade_value_twd: float, side: str, rules: dict | None = None) -> float:
    rules = rules or {}
    value = abs(float(trade_value_twd))
    commission = value * float(rules.get("commission_rate_per_side", FEE_RATE))
    tax = value * float(rules.get("sell_tax_rate", TAX_RATE)) if side.lower() == "sell" else 0.0
    slippage = value * float(rules.get("slippage_bps_per_side", 0.0)) / 10000.0
    if side.lower() not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")
    return commission + tax + slippage
    
def estimate_turnover_cost(turnover_fraction: float, nav_twd: float, rules: dict,
                           include_sell_tax: bool = False) -> float:
    value = abs(float(turnover_fraction)) * float(nav_twd)
    side = "sell" if include_sell_tax else "buy"
    return estimate_trade_cost(value, side, rules)
