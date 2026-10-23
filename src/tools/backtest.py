from __future__ import annotations
import pandas as pd
import numpy as np
from .costs import estimate_trade_cost
from .risk import risk_metrics

def backtest_weights(prices: pd.DataFrame, target_weights: pd.DataFrame,
                     initial_capital: float = 1_000_000_000,
                     commission_rate: float = 0.001425, sell_tax_rate: float = 0.003,
                     slippage_bps: float = 5, execution_lag: int = 1) -> dict:
    """
    Daily close-to-close portfolio backtest.
    prices: date x ticker adjusted-close panel.
    target_weights: date x ticker target weights known at that date.
    Positions take effect after `execution_lag` bars to reduce look-ahead risk.
    Transaction costs are approximated on changes in weights. This is not a
    lot-level simulator; do not treat it as official competition simulation.
    """
    px = prices.sort_index().astype(float).dropna(how="all")
    weights = target_weights.reindex(index=px.index, columns=px.columns).fillna(0.0)
    if execution_lag < 1: raise ValueError("execution_lag must be >= 1 to avoid same-bar look-ahead")
    weights = weights.shift(execution_lag).fillna(0.0)
    returns = px.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)
    prev = weights.shift(1).fillna(0.0)
    buys = (weights - prev).clip(lower=0).sum(axis=1)
    sells = (prev - weights).clip(lower=0).sum(axis=1)
    buy_cost = buys * (commission_rate + slippage_bps / 10000)
    sell_cost = sells * (commission_rate + sell_tax_rate + slippage_bps / 10000)
    turnover_cost = buy_cost + sell_cost
    gross_return = (weights * returns).sum(axis=1)
    net_return = gross_return - turnover_cost
    equity = initial_capital * (1 + net_return).cumprod()
    result = pd.DataFrame({"gross_return": gross_return, "turnover_buy": buys,
                           "turnover_sell": sells, "estimated_cost_return": turnover_cost,
                           "net_return": net_return, "equity": equity})
    return {"daily": result, "metrics": risk_metrics(result["equity"]),
            "assumptions": {"execution_lag_bars": execution_lag,
                            "commission_rate_per_side": commission_rate,
                            "sell_tax_rate": sell_tax_rate, "slippage_bps_per_side": slippage_bps,
                            "note": "Approximate weight-based costs; no lot rounding, bid-ask spread model, suspensions, or price impact."}}

def weekly_momentum_weights(prices: pd.DataFrame, n_holdings: int = 25,
                            cash_target: float = 0.08, max_weight: float = 0.08,
                            tsmc_cap: float = 0.20) -> pd.DataFrame:
    """Illustrative baseline: rebalance every 5 trading days using 60-day momentum."""
    px = prices.sort_index().astype(float)
    mom = px / px.shift(60) - 1
    rows = []
    last_weights = {}
    for i, dt in enumerate(px.index):
        if i % 5 == 0:
            vals = mom.loc[dt].dropna().sort_values(ascending=False).head(n_holdings)
            if len(vals):
                from .portfolio import build_target_weights
                temp = pd.DataFrame({"ticker": vals.index.astype(str), "composite_score": vals.values})
                last_weights = build_target_weights(temp, n_holdings=n_holdings,
                    max_weight=max_weight, cash_target=cash_target, tsmc_cap=tsmc_cap)
        rows.append(pd.Series(last_weights, name=dt))
    return pd.DataFrame(rows).fillna(0.0).reindex(columns=px.columns, fill_value=0.0)
