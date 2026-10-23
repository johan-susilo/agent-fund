"""Shared deterministic factor scoring (40/30/20/10) and regime detection."""
from __future__ import annotations

import math
import numpy as np
import pandas as pd


def price_factors_from_ohlcv(sub: pd.DataFrame) -> dict | None:
    """sub: DataFrame with Close and Volume columns up to settlement date."""
    close, vol = sub["Close"].astype(float), sub["Volume"].astype(float)
    if len(close) < 61:
        return None
    ret = close.pct_change().dropna().iloc[-20:]
    f = {
        "mom20": (close.iloc[-1] / close.iloc[-21] - 1) * 100,
        "mom60": (close.iloc[-1] / close.iloc[-61] - 1) * 100,
        "vol20": ret.std() * math.sqrt(252) * 100,
        "adv20": (close * vol).iloc[-20:].mean() / 1e6,  # NT$ million
    }
    return f if all(math.isfinite(v) for v in f.values()) else None


def build_factor_table(universe, market: dict, factor_frag: dict | None) -> pd.DataFrame:
    """Extract per-ticker metrics from factor_frag observations, sanitize outliers, and score 40/30/20/10."""
    rows = {t: {"ticker": t, "theme": universe.theme_of(t), "core": universe.is_core(t)} for t in universe.names}
    if factor_frag:
        for obs in factor_frag.get("observations", []):
            vals = obs.get("values", {})
            for k, v in vals.items():
                parts = k.split("_", 1)
                if len(parts) != 2 or parts[0] not in rows:
                    continue
                t, metric = parts
                if metric == "mom20_pct":
                    rows[t]["mom20"] = v
                elif metric == "mom60_pct":
                    rows[t]["mom60"] = v
                elif metric == "vol20_pct":
                    rows[t]["vol20"] = v
                elif metric == "adv20_twd_m":
                    rows[t]["adv20"] = v
                elif metric == "roe_pct":
                    rows[t]["roe"] = v
                elif metric == "pe":
                    rows[t]["pe"] = v
                elif metric == "rev_growth_pct":
                    rows[t]["rev_g"] = v
                elif metric == "net_margin_pct":
                    rows[t]["margin"] = v

    df = pd.DataFrame(list(rows.values()))
    for col in ("roe", "rev_g", "margin", "mom20", "mom60", "vol20", "adv20", "pe"):
        if col not in df.columns:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Sanity-filter extreme outliers before ranking (e.g., 2408 611%, 5289 641%, 2337 PE 2.6)
    df["rev_g"]  = df["rev_g"].clip(lower=-80.0, upper=200.0)
    df["roe"]    = df["roe"].clip(lower=-50.0, upper=80.0)
    df["margin"] = df["margin"].clip(lower=-50.0, upper=70.0)
    df["pe"]     = df["pe"].where((df["pe"] >= 5.0) & (df["pe"] <= 120.0), np.nan)

    r = lambda s, asc=True: s.rank(pct=True, ascending=asc).fillna(0.5)
    df["fund"]  = (r(df["roe"]) + r(df["rev_g"]) + r(df["margin"])) / 3.0
    df["mom"]   = 0.4 * r(df["mom20"]) + 0.6 * r(df["mom60"])
    df["risk"]  = 0.5 * r(df["vol20"], False) + 0.5 * r(df["adv20"], True)
    df["val"]   = r(df["pe"].where(df["pe"] > 0), False)
    df["score"] = (0.4 * df["fund"] + 0.3 * df["mom"] + 0.2 * df["risk"] + 0.1 * df["val"]).round(4)

    return df.sort_values("score", ascending=False).reset_index(drop=True)


def detect_regime(market: dict, macro_frag: dict | None, factor_df: pd.DataFrame, universe) -> str:
    """Deterministic regime classification from VIX, Nasdaq/SOX, market breadth, and core 20d momentum."""
    mvals = {}
    if macro_frag:
        for o in macro_frag.get("observations", []):
            mvals.update(o.get("values", {}))

    vix = mvals.get("vix", 18.0)
    vix_chg = mvals.get("vix_chg_pct", 0.0)
    nasdaq_chg = mvals.get("nasdaq_chg_pct", 0.0)
    sox_chg = mvals.get("sox_chg_pct", 0.0)

    prices = market.get("prices", {})
    chgs = [p["change_pct"] for p in prices.values() if p.get("change_pct") is not None]
    adv_ratio = sum(c > 0 for c in chgs) / len(chgs) if chgs else 0.5

    core_df = factor_df[factor_df["core"] == True]
    core_neg_mom20 = float((core_df["mom20"] < 0).mean()) if not core_df.empty else 0.5

    bear_points = 0
    if vix >= 22.0 or vix_chg >= 10.0:
        bear_points += 1
    if nasdaq_chg <= -1.5 or sox_chg <= -2.0:
        bear_points += 1
    if adv_ratio < 0.30 or core_neg_mom20 > 0.65:
        bear_points += 1

    if bear_points >= 2:
        return "risk_off"
    if bear_points == 1 or adv_ratio < 0.45 or core_neg_mom20 > 0.50:
        return "neutral"
    return "risk_on"