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
    """Per-ticker metrics -> sanitised -> scored 40 fundamentals / 30 momentum / 20 risk / 10 valuation.

    Fundamentals come from the full-universe stash `factor_frag["fundamentals"]` (so every stock is
    ranked on the same data, not only the ones that got an evidence observation), then observation
    values override. Stocks with missing price factors are penalised instead of silently scored 0.5.
    """
    rows = {t: {"ticker": t, "theme": universe.theme_of(t), "core": universe.is_core(t)} for t in universe.names}
    if factor_frag:
        for t, i in (factor_frag.get("fundamentals") or {}).items():
            if t not in rows:
                continue
            if i.get("roe") is not None:
                rows[t]["roe"] = i["roe"]
            if i.get("pe") is not None:
                rows[t]["pe"] = i["pe"]
            if i.get("rev_g") is not None:
                rows[t]["rev_g"] = i["rev_g"] * 100.0        # stash holds fractions
            if i.get("margin") is not None:
                rows[t]["margin"] = i["margin"] * 100.0
            if i.get("inst_lots") is not None:
                rows[t]["inst_lots"] = i["inst_lots"]
            if i.get("rev_mom") is not None:
                rows[t]["rev_mom"] = i["rev_mom"] * 100.0
            if i.get("margin_inc") is not None:
                rows[t]["margin_inc"] = i["margin_inc"]
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
                elif metric == "inst_lots":
                    rows[t]["inst_lots"] = v
    df = pd.DataFrame(list(rows.values()))
    for col in ("roe", "rev_g", "rev_mom", "margin", "mom20", "mom60", "vol20", "adv20", "pe", "inst_lots", "margin_inc"):
        if col not in df.columns:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # Sanity-filter extreme outliers before ranking
    df["rev_g"]  = df["rev_g"].clip(lower=-80.0, upper=200.0)
    df["roe"]    = df["roe"].clip(lower=-50.0, upper=80.0)
    df["margin"] = df["margin"].clip(lower=-50.0, upper=70.0)
    df["pe"]     = df["pe"].where((df["pe"] >= 5.0) & (df["pe"] <= 120.0), np.nan)
    r = lambda s, asc=True: s.rank(pct=True, ascending=asc).fillna(0.5)
    df["rev_mom"] = df["rev_mom"].clip(lower=-50.0, upper=100.0)
    df["fund"]  = (r(df["roe"]) + (0.5 * r(df["rev_g"]) + 0.5 * r(df["rev_mom"])) + r(df["margin"])) / 3.0
    # Blend 20d/60d momentum with institutional net buying rank
    df["mom"]   = 0.35 * r(df["mom20"]) + 0.50 * r(df["mom60"]) + 0.15 * r(df["inst_lots"])
    base_risk = 0.5 * r(df["vol20"], False) + 0.5 * r(df["adv20"], True)
    df["margin_inc"] = pd.to_numeric(df.get("margin_inc", 0), errors="coerce")
    margin_trap = ((df["margin_inc"].fillna(0) > 0) & (df["mom20"].fillna(0) < 0)).astype(float)
    df["risk"] = (base_risk - 0.3 * margin_trap).clip(lower=0.01)
    df["val"]   = r(df["pe"].where(df["pe"] > 0), False)
    raw = 0.4 * df["fund"] + 0.3 * df["mom"] + 0.2 * df["risk"] + 0.1 * df["val"]
    # Missing price history = unknown momentum/risk: shrink the score so such names don't surface as "average".
    cov = df[["mom20", "mom60", "vol20", "adv20"]].notna().mean(axis=1)
    df["score"] = (raw * (0.5 + 0.5 * cov)).round(4)
    return df.sort_values("score", ascending=False, kind="mergesort").reset_index(drop=True)


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
