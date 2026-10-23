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

from tools.factors import price_factors_from_ohlcv as _price_factors
from util import Counter, http_get_json, iso, now_tw, read_cache, write_cache, save_raw_bytes


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


TWSE_VAL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TPEX_VAL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
MOPS_REV_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
MOPS_PROF_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap17_L"

def _num_clean(v):
    try:
        val = float(str(v).replace(",", "").strip())
        return val if math.isfinite(val) else None
    except (ValueError, TypeError):
        return None

def _fetch_official_fundamentals(trade_date: str, universe, counter: Counter) -> dict:
    infos = {t: {} for t in universe.names}
    with ThreadPoolExecutor(4) as ex:
        f_tw_val = ex.submit(http_get_json, TWSE_VAL_URL, None, None, 25, 2, counter)
        f_tp_val = ex.submit(http_get_json, TPEX_VAL_URL, None, None, 25, 2, counter)
        f_rev    = ex.submit(http_get_json, MOPS_REV_URL, None, None, 25, 2, counter)
        f_prof   = ex.submit(http_get_json, MOPS_PROF_URL, None, None, 25, 2, counter)

        for name, fut in (("twse_val", f_tw_val), ("tpex_val", f_tp_val), ("mops_rev", f_rev), ("mops_prof", f_prof)):
            try:
                rows = fut.result()
                save_raw_bytes(trade_date, name, json.dumps(rows, ensure_ascii=False).encode("utf-8"))
            except Exception:
                rows = []
            for r in rows or []:
                code = str(r.get("Code") or r.get("SecuritiesCompanyCode") or r.get("公司代號") or "").strip()
                if code not in infos:
                    continue
                if name in ("twse_val", "tpex_val"):
                    pe = _num_clean(r.get("PEratio") or r.get("PriceEarningRatio"))
                    pb = _num_clean(r.get("PBratio") or r.get("PriceBookRatio"))
                    if pe and 4.0 <= pe <= 150.0:
                        infos[code]["pe"] = pe
                    if pb and 0.2 <= pb <= 50.0:
                        infos[code]["pb"] = pb
                elif name == "mops_rev":
                    yoy = _num_clean(r.get("去年同月增減(%)"))
                    if yoy is not None:
                        infos[code]["rev_g"] = max(-0.80, min(2.00, yoy / 100.0))
                elif name == "mops_prof":
                    margin = _num_clean(r.get("稅後純益率") or r.get("營業利益率"))
                    if margin is not None:
                        infos[code]["margin"] = max(-0.50, min(0.80, margin / 100.0))
    return {t: d for t, d in infos.items() if d}


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
