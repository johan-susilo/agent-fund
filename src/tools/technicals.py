"""Deterministic technical indicators (pure pandas/numpy: no network, no LLM).

Used by agents/technical_agent.py (evidence) and, through the technical stash, by risk_agent.py and the
portfolio prompt. Everything here is arithmetic on OHLCV, so every number can be re-derived from the
cited Yahoo Finance source. The composite `tech_score` is a TIMING overlay (0-100). It is NOT part of the
40/30/20/10 factor score, which keeps the strategy description unchanged.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

MIN_ROWS = 70          # need at least this many daily bars (MA60 + warm-up)


def _fin(x, nd: int = 2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    return rsi.where(dn != 0, 100.0)               # no losing days in the window -> 100


def _atr(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _score(d: dict) -> int:
    """Transparent 0-100 timing score: trend 30, RSI zone 20, MACD 20, volume 10, Bollinger 10, high proximity 10."""
    s = {1: 30, 0: 15, -1: 0}[d["trend"]]
    r = d["rsi14"]
    s += 20 if 50 <= r <= 70 else 12 if 40 <= r < 50 else 10 if r > 70 else 8 if 30 <= r < 40 else 5
    h, rising = d["macd_hist_pct"], d["_hist_rising"]
    s += 20 if h > 0 and rising else 12 if h > 0 else 8 if rising else 0
    s += 10 if d["vol_ratio"] >= 1.2 and d["ret1d_pct"] > 0 else 5 if d["vol_ratio"] >= 0.8 else 2
    s += 10 if 0.4 <= d["pct_b"] <= 0.9 else 4
    s += 10 if d["dd_high_pct"] > -10 else 5 if d["dd_high_pct"] > -20 else 0
    return int(s)


def indicators(sub: pd.DataFrame) -> dict | None:
    """sub: daily OHLCV (Open/High/Low/Close/Volume), ascending, last row = settlement day."""
    if not {"Open", "High", "Low", "Close", "Volume"}.issubset(sub.columns):
        return None
    sub = sub.dropna(subset=["Close"])
    if len(sub) < MIN_ROWS:
        return None
    o, h, l, c, v = (sub[k].astype(float) for k in ("Open", "High", "Low", "Close", "Volume"))
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    m20, m60 = float(ma20.iloc[-1]), float(ma60.iloc[-1])
    if last <= 0 or prev <= 0 or not (math.isfinite(m20) and math.isfinite(m60)):
        return None

    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    hl, hp, recent = float(hist.iloc[-1]), float(hist.iloc[-2]), hist.iloc[-4:-1]
    cross = 1 if hl > 0 and (recent <= 0).any() else (-1 if hl < 0 and (recent >= 0).any() else 0)

    sd = float(c.rolling(20).std(ddof=0).iloc[-1])
    pct_b = (last - (m20 - 2 * sd)) / (4 * sd) if sd > 0 else 0.5
    atr = float(_atr(h, l, c).iloc[-1])
    hi20, lo20 = float(h.iloc[-21:-1].max()), float(l.iloc[-21:-1].min())
    hi252 = float(h.iloc[-252:].max())
    v20 = float(v.iloc[-20:].mean())
    ret20 = c.pct_change().dropna().iloc[-20:]

    d = {
        "close": last,
        "ret1d_pct": (last / prev - 1) * 100,
        "dist_ma20_pct": (last / m20 - 1) * 100,
        "dist_ma60_pct": (last / m60 - 1) * 100,
        "trend": 1 if last > m20 > m60 else (-1 if last < m20 < m60 else 0),
        "rsi14": float(_rsi(c).iloc[-1]),
        "macd_hist_pct": hl / last * 100,
        "macd_cross": cross,
        "pct_b": pct_b,
        "atr14_pct": atr / last * 100,
        "dd_high_pct": (last / hi252 - 1) * 100,
        "vol_ratio": float(v.iloc[-5:].mean()) / v20 if v20 > 0 else 1.0,
        "gap_pct": (float(o.iloc[-1]) / prev - 1) * 100,
        "breakout20": int(last > hi20),
        "breakdown20": int(last < lo20),
        "vol20": float(ret20.std() * math.sqrt(252) * 100),
        "adv20": float((c * v).iloc[-20:].mean() / 1e6),      # NT$ million per day
        "_hist_rising": hl > hp,
    }
    if not all(math.isfinite(x) for x in d.values() if isinstance(x, float)):
        return None
    d["tech_score"] = _score(d)
    d.pop("_hist_rising")

    flags = []
    if d["rsi14"] > 75: flags.append("overbought")
    if d["rsi14"] < 25: flags.append("oversold")
    if d["breakout20"]: flags.append("breakout")
    if d["breakdown20"]: flags.append("breakdown")
    if last < m60: flags.append("below_ma60")
    if d["gap_pct"] <= -3: flags.append("gap_down")
    if d["gap_pct"] >= 3: flags.append("gap_up")
    if d["atr14_pct"] > 6: flags.append("high_atr")
    if cross == -1: flags.append("macd_dead_cross")
    if cross == 1: flags.append("macd_golden_cross")

    out = {k: _fin(x, 2 if k != "close" else 4) for k, x in d.items() if isinstance(x, float)}
    out.update({k: int(x) for k, x in d.items() if isinstance(x, (int, np.integer)) and not isinstance(x, bool)})
    out["flags"] = flags
    return out


def breadth(techs: dict) -> dict:
    """Market-level read of the whole 150-stock universe."""
    n = len(techs)
    if not n:
        return {}
    pct = lambda f: round(100.0 * sum(1 for d in techs.values() if f(d)) / n, 1)
    scores = sorted(d["tech_score"] for d in techs.values())
    return {
        "n": n,
        "pct_above_ma20": pct(lambda d: d["dist_ma20_pct"] > 0),
        "pct_above_ma60": pct(lambda d: d["dist_ma60_pct"] > 0),
        "pct_rsi_gt70": pct(lambda d: d["rsi14"] > 70),
        "pct_rsi_lt30": pct(lambda d: d["rsi14"] < 30),
        "pct_macd_pos": pct(lambda d: d["macd_hist_pct"] > 0),
        "n_breakout20": sum(d["breakout20"] for d in techs.values()),
        "n_breakdown20": sum(d["breakdown20"] for d in techs.values()),
        "median_tech_score": float(scores[n // 2]),
    }


def line(t: str, d: dict, margin_chg_pct=None) -> str:
    """One compact text line per stock for observation statements."""
    s = (f"{t} 分{d['tech_score']} RSI{d['rsi14']:.0f} MA20{d['dist_ma20_pct']:+.0f}% "
         f"MA60{d['dist_ma60_pct']:+.0f}% ATR{d['atr14_pct']:.1f} 量{d['vol_ratio']:.1f}")
    if margin_chg_pct is not None:
        s += f" 融資{margin_chg_pct:+.0f}%"
    if d.get("flags"):
        s += " [" + ",".join(d["flags"]) + "]"
    return s
