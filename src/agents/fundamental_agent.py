"""Fundamental / factor agent: deterministic official data + an LLM research loop with tools.

Deterministic part (no LLM):
  * price factors for the 150 stocks (20d/60d momentum, 20d volatility, 20d traded value)
  * official TWSE/TPEx/MOPS P/E, P/B, monthly-revenue YoY, net margin
  * market-level observation: median monthly-revenue YoY of the core AI/semiconductor themes
  * the FULL-universe fundamentals are stashed in frag["fundamentals"] so the factor table ranks every
    stock on the same data; only the best-ranked stocks (plus holdings) get citable observations.
LLM part: `research()` lets the model call tools to fill gaps (events, estimate revisions, outliers).
"""
from __future__ import annotations

import json
import math
import os
import statistics
from concurrent.futures import ThreadPoolExecutor

from observations import chunks, frag_add_obs, frag_add_source, new_fragment
from settings import PROMPT_DIR, ROOT
from tools.factors import price_factors_from_ohlcv as _price_factors
from util import Counter, http_get_json, iso, now_tw, read_cache, save_raw_bytes, write_cache

CACHE_TTL_MIN = 360
MAX_FUNDAMENTALS = int(os.environ.get("FUNDAMENTAL_MAX", 60))
YAHOO_URL = "https://finance.yahoo.com/"
TWSE_VAL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TPEX_VAL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
MOPS_REV_URLS = ("https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
                 "https://openapi.twse.com.tw/v1/opendata/t187ap05_O")
MOPS_PROF_URLS = ("https://openapi.twse.com.tw/v1/opendata/t187ap17_L",
                  "https://openapi.twse.com.tw/v1/opendata/t187ap17_O")
TWSE_INST_URL = "https://openapi.twse.com.tw/v1/fund/T86_ALL"
TPEX_INST_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
_OFFICIAL: dict = {}


def _symbol(universe, t):
    return f"{t}.TW" if universe.markets.get(t, "twse") == "twse" else f"{t}.TWO"


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
        sub = sub[idx <= pd.Timestamp(as_of)]
        if sub.empty or str(sub.index[-1].date()) != as_of:
            stale += 1
            continue
        f = _price_factors(sub, as_of)  # [upgrade-2026-10]
        if f:
            out[code] = f
    return out, stale


def _num_clean(v):
    try:
        val = float(str(v).replace(",", "").strip())
        return val if math.isfinite(val) else None
    except (ValueError, TypeError):
        return None


def _find(r, *subs):
    """First non-empty value whose key contains a substring (MOPS keys carry '(%)' suffixes)."""
    for k, v in r.items():
        if v not in (None, "") and any(s in str(k) for s in subs):
            return v
    return None


def _code(r):
    for k in ("Code", "SecuritiesCompanyCode", "公司代號", "股票代號"):
        if r.get(k):
            return str(r[k]).strip()
    return ""


def _fetch_official_fundamentals(trade_date, universe, counter) -> dict:
    infos = {t: {} for t in universe.names}
    TWSE_MARGIN_URL = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"
    TPEX_MARGIN_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_trading"
    
    jobs = [("twse_val", TWSE_VAL_URL), ("tpex_val", TPEX_VAL_URL),
            ("twse_inst", TWSE_INST_URL), ("tpex_inst", TPEX_INST_URL),
            ("twse_margin", TWSE_MARGIN_URL), ("tpex_margin", TPEX_MARGIN_URL)] \
        + [(f"mops_rev{i}", u) for i, u in enumerate(MOPS_REV_URLS)] \
        + [(f"mops_prof{i}", u) for i, u in enumerate(MOPS_PROF_URLS)]
    with ThreadPoolExecutor(8) as ex:
        futs = [(n, ex.submit(http_get_json, u, None, None, 25, 2, counter)) for n, u in jobs]
        for name, fut in futs:
            try:
                rows = fut.result()
                save_raw_bytes(trade_date, name, json.dumps(rows, ensure_ascii=False).encode("utf-8"))
            except Exception:
                continue                      # one dataset failing must not stop the others
            for r in rows or []:
                code = _code(r)
                if code not in infos:
                    continue
                if name.endswith("_val"):
                    pe = _num_clean(_find(r, "PEratio", "PriceEarningRatio", "本益比"))
                    pb = _num_clean(_find(r, "PBratio", "PriceBookRatio", "股價淨值比"))
                    dy = _num_clean(_find(r, "DividendYield", "殖利率"))
                    if pe and 4.0 <= pe <= 150.0:
                        infos[code]["pe"] = pe
                    if pb and 0.2 <= pb <= 50.0:
                        infos[code]["pb"] = pb
                    if dy and 0.0 <= dy <= 25.0:
                        infos[code]["dy"] = dy
                    # Derive ROE (%) = (P/B / P/E) * 100 as reliable baseline when MOPS ROE is absent
                    if infos[code].get("pe") and infos[code].get("pb"):
                        infos[code].setdefault("roe", round((infos[code]["pb"] / infos[code]["pe"]) * 100.0, 2))
                elif name.startswith("mops_rev"):
                    yoy = _num_clean(_find(r, "去年同月增減(%)", "YoY"))
                    mom = _num_clean(_find(r, "上月比較增減(%)", "MoM"))
                    if yoy is not None:
                        infos[code]["rev_g"] = max(-0.80, min(2.00, yoy / 100.0))
                    if mom is not None:
                        infos[code]["rev_mom"] = max(-0.50, min(1.00, mom / 100.0))
                elif name.startswith("mops_prof"):
                    m = _num_clean(_find(r, "稅後純益率", "NetIncome"))
                    gm = _num_clean(_find(r, "毛利率", "Gross"))
                    opm = _num_clean(_find(r, "營業利益率", "Operating"))
                    roe_mops = _num_clean(_find(r, "權益報酬率", "ROE"))
                    if m is not None:
                        infos[code]["margin"] = max(-0.50, min(0.80, m / 100.0))
                    if gm is not None:
                        infos[code]["gross_margin"] = max(-0.50, min(0.95, gm / 100.0))
                    if opm is not None:
                        infos[code]["op_margin"] = max(-0.50, min(0.85, opm / 100.0))
                    if roe_mops is not None:
                        infos[code]["roe"] = max(-50.0, min(80.0, roe_mops))
                elif name.endswith("_inst"):
                    # Net buy shares of Foreign + Investment Trust -> convert to board lots (張)
                    f_net = _num_clean(_find(r, "ForeignInvestorsNetBuySell", "外陸資買賣超股數", "外資及陸資買賣超股數")) or 0.0
                    it_net = _num_clean(_find(r, "InvestmentTrustNetBuySell", "投信買賣超股數")) or 0.0
                    tot_net = _num_clean(_find(r, "TotalNetBuySell", "三大法人買賣超股數合計", "三大法人買賣超"))
                    net_sh = tot_net if tot_net is not None else (f_net + it_net)
                    if net_sh != 0.0:
                        infos[code]["inst_lots"] = round(net_sh / 1000.0, 0)
    return {t: d for t, d in infos.items() if d}


def official_fundamentals(trade_date, universe, counter):
    if trade_date not in _OFFICIAL:
        _OFFICIAL[trade_date] = _fetch_official_fundamentals(trade_date, universe, counter or Counter())
    return _OFFICIAL[trade_date]


def official_source(as_of_date):
    return {"authority": "twse", "url": TWSE_VAL_URL, "fetched_at": iso(now_tw()),
            "name": "TWSE/TPEx/MOPS 公開資料：本益比、月營收年增、淨利率",
            "content_as_of": f"{as_of_date}T18:30:00+08:00"}


def fmt_fundamentals(infos, group):
    parts, vals = [], {}
    for t in group:
        r, bits = infos.get(t, {}), []
        if "roe" in r: bits.append(f"ROE {r['roe']:.1f}%"); vals[f"{t}_roe_pct"] = round(r["roe"], 2)
        if "pe" in r: bits.append(f"PE {r['pe']:.1f}"); vals[f"{t}_pe"] = round(r["pe"], 2)
        if "pb" in r: bits.append(f"PB {r['pb']:.1f}"); vals[f"{t}_pb"] = round(r["pb"], 2)
        if "rev_g" in r: bits.append(f"營收年增 {r['rev_g']*100:.1f}%"); vals[f"{t}_rev_growth_pct"] = round(r["rev_g"]*100, 2)
        if "margin" in r: bits.append(f"淨利率 {r['margin']*100:.1f}%"); vals[f"{t}_net_margin_pct"] = round(r["margin"]*100, 2)
        if "inst_lots" in r: bits.append(f"法人買賣超 {r['inst_lots']:+.0f}張"); vals[f"{t}_inst_lots"] = r["inst_lots"]
        parts.append(f"{t} " + " ".join(bits))
    return "財務與籌碼指標：" + "；".join(parts), vals


def research(trade_date, as_of_date, universe, holdings, frag, counter):
    from agent_loop import run_agent_loop
    from tools import web_tools
    tools = web_tools.build_tools(web_tools.fragment_emit(frag), trade_date=trade_date, as_of=as_of_date,
                                  universe=universe, counter=counter, obs_cap=4)
    user = json.dumps({"trade_date": trade_date, "as_of_date": as_of_date, "current_holdings": list(holdings),
                       "core_tickers": [t for th in universe.core_ids for t in universe.themes[th]["tickers"]],
                       "already_collected": [o["statement"][:90] for o in frag["observations"]]},
                      ensure_ascii=False)
    text, usage, _, models = run_agent_loop((PROMPT_DIR / "fundamental_agent.md").read_text(encoding="utf-8"),
                                            user, tools, max_steps=10, max_calls=12)
    frag["agent_notes"] = text[:1500]
    frag["llm_usage"] = {**usage, "models": models}


def _focus(holdings, universe, factors, infos) -> list:
    """Which stocks get citable fundamental observations: holdings first, then the best-looking core
    names (quick momentum + growth + ROE pre-score), plus a few non-core names for diversification."""
    def quick(t):
        f, r = factors.get(t, {}), infos.get(t, {})
        return (0.4 * f.get("mom60", 0.0) + 0.2 * f.get("mom20", 0.0)
                + 0.2 * r.get("rev_g", 0.0) * 100.0 + 0.2 * min(r.get("roe", 0.0), 40.0))
    ranked = sorted((t for t in universe.names if t in infos), key=quick, reverse=True)
    core = [t for t in ranked if universe.is_core(t)]
    rest = [t for t in ranked if not universe.is_core(t)]
    keep_rest = min(12, MAX_FUNDAMENTALS // 5)
    order = list(holdings) + rest[:keep_rest] + core
    return [t for t in dict.fromkeys(order) if t in infos][:MAX_FUNDAMENTALS]


def collect(trade_date: str, as_of_date: str, universe, holdings, refresh: bool = False) -> dict:
    cache = ROOT / "inputs" / trade_date / "fundamental_fragment.json"
    if not refresh and (hit := read_cache(cache, CACHE_TTL_MIN)):
        hit["warnings"] = hit.get("warnings", []) + ["factor evidence loaded from cache (use --refresh)"]
        return hit

    frag, counter = new_fragment(), Counter()
    try:
        import yfinance  # noqa: F401
    except ImportError:
        frag["warnings"].append("yfinance not installed: momentum evidence skipped")
        return frag

    now = iso(now_tw())
    factors, stale = {}, 0
    try:
        factors, stale = _factors(universe, as_of_date, counter)
    except Exception as e:
        frag["warnings"].append(f"price factors failed: {type(e).__name__}: {str(e)[:120]}")
    try:
        infos = official_fundamentals(trade_date, universe, counter)
    except Exception as e:
        infos = {}
        frag["warnings"].append(f"official fundamentals failed: {type(e).__name__}: {str(e)[:120]}")
    frag["fundamentals"] = infos
    frag["price_factors"] = factors   # [upgrade-2026-10] full feature stash for tools/scoring.py

    px_src = frag_add_source(frag, {"authority": "vendor", "url": YAHOO_URL, "fetched_at": now,
                                    "name": "Yahoo Finance 日線歷史價量（20/60日動能、波動、成交值）",
                                    "content_as_of": f"{as_of_date}T13:30:00+08:00"})
    fd_src = frag_add_source(frag, official_source(as_of_date))

    for g in chunks([t for t in universe.ordered() if t in factors], 10):
        parts, vals = [], {}
        for t in g:
            f = factors[t]
            parts.append(f"{t} 20日{f['mom20']:+.1f}% 60日{f['mom60']:+.1f}% 波動{f['vol20']:.0f}% 日均值{f['adv20']:,.0f}M")
            vals.update({f"{t}_mom20_pct": round(f["mom20"], 2), f"{t}_mom60_pct": round(f["mom60"], 2),
                         f"{t}_vol20_pct": round(f["vol20"], 1), f"{t}_adv20_twd_m": round(f["adv20"], 0)})
        frag_add_obs(frag, px_src, "價量因子（截至結算日）：" + "；".join(parts), vals,
                     "factor_momentum", "stock", g)

    # Market-level read of semiconductor monthly revenue (strategy: 半導體月營收)
    core = [t for th in universe.core_ids for t in universe.themes[th]["tickers"]]
    revs = [infos[t]["rev_g"] * 100 for t in core if t in infos and "rev_g" in infos[t]]
    if len(revs) >= 5:
        frag_add_obs(frag, fd_src,
                     f"核心 AI/半導體主題 {len(revs)} 檔最新月營收年增：中位數 {statistics.median(revs):+.1f}%、"
                     f"{sum(r > 0 for r in revs)}/{len(revs)} 檔為正成長",
                     {"core_rev_yoy_median_pct": round(statistics.median(revs), 2), "n": len(revs),
                      "positive_count": sum(r > 0 for r in revs)}, "macro_semi_revenue", "market")

    for g in chunks(_focus(holdings, universe, factors, infos), 10):
        stmt, vals = fmt_fundamentals(infos, g)
        frag_add_obs(frag, fd_src, stmt, vals, "factor_fundamental", "stock", g)

    if stale:
        frag["warnings"].append(f"{stale} stocks had no usable price history for {as_of_date} (halted or stale)")
    if not factors:
        frag["warnings"].append("no momentum factors available")
    if not infos:
        frag["warnings"].append("no official fundamentals returned: check endpoint names on the OpenAPI swagger")
    if os.environ.get("AGENT_AUTONOMY", "1") != "0" and os.environ.get("EVIDENCE_MODE", "merged") == "legacy":  # [upgrade-2026-10]
        try:
            research(trade_date, as_of_date, universe, holdings, frag, counter)
        except Exception as e:
            frag["warnings"].append(f"fundamental research agent failed: {type(e).__name__}: {str(e)[:120]}")
    frag["tool_calls"] = counter.n
    if frag["observations"]:
        write_cache(cache, frag)
    return frag
