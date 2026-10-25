"""Shared deterministic factor scoring and regime detection.

Scoring is delegated to tools/scoring.py (35 momentum / 25 risk / 20 earnings / 10 macro / 10 valuation,
weights in config/scoring.json).  This module keeps the public names the rest of the pipeline imports:
`price_factors_from_ohlcv`, `build_factor_table`, `detect_regime`.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _fin(x):
    return float(x) if x is not None and math.isfinite(x) else None


def price_factors_from_ohlcv(sub: pd.DataFrame, as_of: str | None = None) -> dict | None:
    """sub: DataFrame (DatetimeIndex) with Close, Volume and optionally High/Low, up to the settlement date.

    No look-ahead: bars after `as_of` are discarded, and if `as_of` is given the last bar must be that day
    (otherwise the stock is stale/halted and None is returned).  Returns the four legacy metrics plus
    trend, volume-confirmation, drawdown, downside-risk and spread-proxy features.
    """
    if as_of is not None:
        idx = sub.index.tz_localize(None) if getattr(sub.index, "tz", None) is not None else sub.index
        sub = sub[idx <= pd.Timestamp(as_of)]
        if sub.empty or str(sub.index[-1].date()) != str(as_of):
            return None
    close, vol = sub["Close"].astype(float), sub["Volume"].astype(float)
    if len(close) < 61:
        return None
    ret = close.pct_change().dropna()
    r20 = ret.iloc[-20:]
    c0 = close.iloc[-1]
    ma20, ma60 = close.iloc[-20:].mean(), close.iloc[-60:].mean()
    up = vol.iloc[-20:][ret.iloc[-20:] > 0].sum()
    dn = vol.iloc[-20:][ret.iloc[-20:] < 0].sum()
    run_max = close.iloc[-60:].cummax()
    f = {
        "mom20": (c0 / close.iloc[-21] - 1) * 100,
        "mom60": (c0 / close.iloc[-61] - 1) * 100,
        "vol20": r20.std() * math.sqrt(252) * 100,
        "adv20": (close * vol).iloc[-20:].mean() / 1e6,                       # NT$ million
        "ma20_gap": (c0 / ma20 - 1) * 100,
        "ma60_gap": (c0 / ma60 - 1) * 100,
        "ma20_vs_ma60": (ma20 / ma60 - 1) * 100,
        "ud_vol20": math.log((up + 1.0) / (dn + 1.0)),                        # >0: volume on up days dominates
        "mdd60": float(((run_max - close.iloc[-60:]) / run_max).max() * 100),
        "dd_dev20": float(np.sqrt(np.mean(np.minimum(r20.values, 0.0) ** 2)) * math.sqrt(252) * 100),
    }
    if "High" in sub and "Low" in sub:
        hl = ((sub["High"].astype(float) - sub["Low"].astype(float)) / close).iloc[-20:].mean() * 100
        f["hl20"] = hl                                                         # daily range = spread/impact proxy
    f = {k: _fin(v) for k, v in f.items()}
    if any(f[k] is None for k in ("mom20", "mom60", "vol20", "adv20")):
        return None
    f["last_date"] = str(sub.index[-1].date())
    return f


def build_factor_table(universe, market: dict, factor_frag: dict | None, macro_frag: dict | None = None,
                       regime: str | None = None, as_of: str | None = None, trade_date: str | None = None) -> pd.DataFrame:
    """Score table for the whole universe, sorted best first (see tools/scoring.py).

    Backwards compatible: the original 3-argument call still works (macro then scores neutral and is flagged).
    """
    from tools import scoring
    return scoring.build_score_table(universe, market, factor_frag, macro_frag, regime=regime,
                                     as_of=as_of, trade_date=trade_date)


def detect_regime(market: dict, macro_frag: dict | None, factor_df: pd.DataFrame, universe) -> str:
    """Deterministic regime classification from VIX, Nasdaq/SOX (1-day and vs 20d average),
    TAIEX trend, market breadth, and core 20d momentum."""
    mvals = {}
    if macro_frag:
        for o in macro_frag.get("observations", []):
            mvals.update(o.get("values", {}))

    vix = mvals.get("vix", 18.0)
    vix_chg = mvals.get("vix_chg_pct", 0.0)
    nasdaq_chg = mvals.get("nasdaq_chg_pct", 0.0)
    sox_chg = mvals.get("sox_chg_pct", 0.0)
    sox_ma = mvals.get("sox_vs_ma20_pct", 0.0)         # % vs 20d average (trend damage)
    taiex_ma = mvals.get("taiex_vs_ma20_pct", 0.0)

    prices = market.get("prices", {})
    chgs = [p["change_pct"] for p in prices.values() if p.get("change_pct") is not None]
    adv_ratio = sum(c > 0 for c in chgs) / len(chgs) if chgs else 0.5

    core_df = factor_df[factor_df["core"] == True]  # noqa: E712
    core_neg_mom20 = float((core_df["mom20"] < 0).mean()) if not core_df.empty else 0.5

    tsm_adr_chg = mvals.get("tsm_adr_chg_pct", 0.0)
    vix_spot = mvals.get("vix_yf", vix)
    bear_points = 0
    # Require VIX level >= 20.5 even on a % spike, so a bounce from 16->19 doesn't false-trigger panic
    if vix_spot >= 23.0 or (vix_spot >= 20.5 and vix_chg >= 12.0):
        bear_points += 1
    foreign_noi = mvals.get("foreign_noi", 0.0)
    if foreign_noi <= -30000:
        bear_points += 1
    if sox_chg <= -2.0 or (nasdaq_chg <= -1.8 and sox_chg < 0):
        bear_points += 1
    if adv_ratio < 0.25 and core_neg_mom20 > 0.65:
        bear_points += 1
    if sox_ma <= -10.0 or taiex_ma <= -5.0:             # sustained trend damage, not a one-day move
        bear_points += 1
    # Overnight US semiconductor lead override: if both SOX and TSM ADR closed green overnight, cap at 'neutral'
    if sox_chg > 0.0 and tsm_adr_chg >= 0.5 and bear_points >= 2:
        bear_points = 1
    if bear_points >= 2:
        return "risk_off"
    if bear_points == 1 or adv_ratio < 0.45 or core_neg_mom20 > 0.50 or sox_ma <= -5.0:
        return "neutral"
    return "risk_on"
