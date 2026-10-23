from __future__ import annotations

def estimate_trade_cost(trade_value_twd: float, side: str, rules: dict) -> float:
    """Estimate commission, sell tax (sell side only), and configured slippage."""
    value = abs(float(trade_value_twd))
    commission = value * float(rules.get("commission_rate_per_side", 0.0))
    tax = value * float(rules.get("sell_tax_rate", 0.0)) if side.lower() == "sell" else 0.0
    slippage = value * float(rules.get("slippage_bps_per_side", 0.0)) / 10000.0
    if side.lower() not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")
    return commission + tax + slippage

def estimate_turnover_cost(turnover_fraction: float, nav_twd: float, rules: dict,
                           include_sell_tax: bool = False) -> float:
    value = abs(float(turnover_fraction)) * float(nav_twd)
    side = "sell" if include_sell_tax else "buy"
    return estimate_trade_cost(value, side, rules)
