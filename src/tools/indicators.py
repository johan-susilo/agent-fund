from __future__ import annotations
import numpy as np
import pandas as pd

def technical_features(prices: pd.DataFrame) -> pd.DataFrame:
    """Calculate cross-sectional features at each date. Prices: date index x ticker columns."""
    close = prices.sort_index().astype(float)
    ret20 = close / close.shift(20) - 1
    ret60 = close / close.shift(60) - 1
    daily = close.pct_change()
    vol20 = daily.rolling(20, min_periods=15).std() * np.sqrt(252)
    ma20, ma60 = close.rolling(20).mean(), close.rolling(60).mean()
    trend = (close > ma20).astype(float) + (close > ma60).astype(float)
    rows = []
    for dt in close.index:
        for ticker in close.columns:
            rows.append({"date": dt, "ticker": str(ticker),
                         "momentum_20d": ret20.at[dt, ticker],
                         "momentum_60d": ret60.at[dt, ticker],
                         "volatility_20d_ann": vol20.at[dt, ticker],
                         "trend_score": trend.at[dt, ticker] / 2})
    return pd.DataFrame(rows)

def cross_sectional_rank(features: pd.DataFrame) -> pd.DataFrame:
    f = features.copy()
    for col, ascending in [("momentum_20d", True), ("momentum_60d", True)]:
        f[col + "_pct"] = f.groupby("date")[col].rank(pct=True, ascending=ascending)
    # Lower volatility receives a higher rank.
    f["low_vol_pct"] = f.groupby("date")["volatility_20d_ann"].rank(pct=True, ascending=False)
    f["composite_score"] = (
        0.20 * f["momentum_20d_pct"].fillna(0.5) +
        0.30 * f["momentum_60d_pct"].fillna(0.5) +
        0.20 * f["trend_score"].fillna(0.5) +
        0.30 * f["low_vol_pct"].fillna(0.5)
    )
    f["score_data_coverage"] = f[["momentum_20d", "momentum_60d", "volatility_20d_ann"]].notna().mean(axis=1)
    return f
