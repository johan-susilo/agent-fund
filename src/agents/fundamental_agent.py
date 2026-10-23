"""Fundamental / factor agent: Yahoo Finance data turned into sourced observations (no LLM).

Produces the factors named in config/strategy.json that the exchange feed does not carry:
  * price factors for all 150 stocks from ONE batch download: 20d / 60d momentum,
    20d annualised volatility, 20d average traded value
  * key statistics (ROE, P/E, revenue growth, net margin) for the core-theme stocks and current holdings
Stocks are grouped 10 per observation to stay inside the 100-observation schema limit.
"""
from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor

from observations import chunks, frag_add_obs, frag_add_source, new_fragment
from settings import ROOT
from util import Counter, iso, now_tw, read_cache, write_cache

CACHE_TTL_MIN = 360
MAX_FUNDAMENTALS = int(os.environ.get("FUNDAMENTAL_MAX", 90))
YAHOO_URL = "https://finance.yahoo.com/"


def _symbol(universe, t):
    return f"{t}.TW" if universe.markets.get(t, "twse") == "twse" else f"{t}.TWO"


def _price_factors(sub):
    """sub: DataFrame (Close, Volume) up to and including the settlement date."""
    close, vol = sub["Close"].astype(float), sub["Volume"].astype(float)
    if len(close) < 61:
        return None
    ret = close.pct_change().dropna().iloc[-20:]
    f = {"mom20": (close.iloc[-1] / close.iloc[-21] - 1) * 100,
         "mom60": (close.iloc[-1] / close.iloc[-61] - 1) * 100,
         "vol20": ret.std() * math.sqrt(252) * 100,
         "adv20": (close * vol).iloc[-20:].mean() / 1e6}      # NT$ million
    return f if all(math.isfinite(v) for v in f.values()) else None


def _factors(universe, as_of, counter):
    import pandas as pd
    import yfinance as yf
    sym2code = {_symbol(universe, t): t for t in universe.names}
    counter.inc()
    df = yf.download(list(sym2code), period="6mo", interval="1d", auto_adjust=True,
                     group_by="ticker", progress=False, threads=True)
    out, stale = {}, 0
    for sym, code in sym2code.items():
        try:
            sub = df[sym].dropna(subset=["Close"])
        except KeyError:
            continue
        idx = sub.index.tz_localize(None) if getattr(sub.index, "tz", None) is not None else sub.index
        sub = sub[idx <= pd.Timestamp(as_of)]        # drop any partial bar of the trade date
        if sub.empty or str(sub.index[-1].date()) != as_of:   # halted or stale -> no factor
            stale += 1
            continue
        f = _price_factors(sub)
        if f:
            out[code] = f
    return out, stale


def _info(sym, counter):
    import yfinance as yf
    counter.inc()
    try:
        i = yf.Ticker(sym).info
    except Exception:
        return None
    pe = i.get("forwardPE") or i.get("trailingPE")
    row = {"roe": i.get("returnOnEquity"), "pe": pe, "rev_g": i.get("revenueGrowth"),
           "margin": i.get("profitMargins")}
    row = {k: v for k, v in row.items() if isinstance(v, (int, float)) and math.isfinite(v)}
    return row or None


def collect(trade_date: str, as_of_date: str, universe, holdings, refresh: bool = False) -> dict:
    cache = ROOT / "inputs" / trade_date / "fundamental_fragment.json"
    if not refresh and (hit := read_cache(cache, CACHE_TTL_MIN)):
        hit["warnings"] = hit.get("warnings", []) + ["factor evidence loaded from cache (use --refresh)"]
        return hit

    frag, counter = new_fragment(), Counter()
    try:
        import yfinance  # noqa: F401
    except ImportError:
        frag["warnings"].append("yfinance not installed: momentum / fundamental evidence skipped")
        return frag

    now = iso(now_tw())
    factors, stale = {}, 0
    try:
        factors, stale = _factors(universe, as_of_date, counter)
    except Exception as e:
        frag["warnings"].append(f"price factors failed: {type(e).__name__}: {str(e)[:120]}")

    focus = list(dict.fromkeys(list(holdings) + [t for th in universe.core_ids
                                                 for t in universe.themes[th]["tickers"]]))[:MAX_FUNDAMENTALS]
    infos = {}
    with ThreadPoolExecutor(8) as ex:
        for t, r in zip(focus, ex.map(lambda t: _info(_symbol(universe, t), counter), focus)):
            if r:
                infos[t] = r

    px_src = frag_add_source(frag, {"authority": "vendor", "url": YAHOO_URL, "fetched_at": now,
                                    "name": "Yahoo Finance 日線歷史價量（20/60日動能、波動、成交值）",
                                    "content_as_of": f"{as_of_date}T13:30:00+08:00"})
    fd_src = frag_add_source(frag, {"authority": "vendor", "url": YAHOO_URL + "quote/", "fetched_at": now,
                                    "name": "Yahoo Finance 關鍵財務指標（ROE、本益比、營收成長、淨利率）",
                                    "content_as_of": now})

    order = [t for t in universe.ordered() if t in factors]
    for g in chunks(order, 10):
        parts, vals = [], {}
        for t in g:
            f = factors[t]
            parts.append(f"{t} 20日{f['mom20']:+.1f}% 60日{f['mom60']:+.1f}% 波動{f['vol20']:.0f}% 日均值{f['adv20']:,.0f}M")
            vals.update({f"{t}_mom20_pct": round(f["mom20"], 2), f"{t}_mom60_pct": round(f["mom60"], 2),
                         f"{t}_vol20_pct": round(f["vol20"], 1), f"{t}_adv20_twd_m": round(f["adv20"], 0)})
        frag_add_obs(frag, px_src, "價量因子（截至結算日）：" + "；".join(parts), vals,
                     "factor_momentum", "stock", g)

    order = [t for t in focus if t in infos]
    for g in chunks(order, 10):
        parts, vals = [], {}
        for t in g:
            r, bits = infos[t], []
            if "roe" in r: bits.append(f"ROE {r['roe'] * 100:.1f}%"); vals[f"{t}_roe_pct"] = round(r["roe"] * 100, 2)
            if "pe" in r: bits.append(f"PE {r['pe']:.1f}"); vals[f"{t}_pe"] = round(r["pe"], 2)
            if "rev_g" in r: bits.append(f"營收年增 {r['rev_g'] * 100:.1f}%"); vals[f"{t}_rev_growth_pct"] = round(r["rev_g"] * 100, 2)
            if "margin" in r: bits.append(f"淨利率 {r['margin'] * 100:.1f}%"); vals[f"{t}_net_margin_pct"] = round(r["margin"] * 100, 2)
            parts.append(f"{t} " + " ".join(bits))
        frag_add_obs(frag, fd_src, "財務指標：" + "；".join(parts), vals, "factor_fundamental", "stock", g)

    if stale:
        frag["warnings"].append(f"{stale} stocks had no usable price history for {as_of_date} (halted or stale)")
    if not factors:
        frag["warnings"].append("no momentum factors available")
    frag["tool_calls"] = counter.n
    if frag["observations"]:
        write_cache(cache, frag)
    return frag
