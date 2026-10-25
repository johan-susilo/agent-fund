"""Technical agent: deterministic chart/flow facts for the 150 stocks + an LLM research loop.

Deterministic part (no LLM, every number re-derivable from the cited sources):
  * RSI / MACD / Bollinger %B / ATR / MA20-60 trend / 52w drawdown / volume ratio / breakout flags
    for all 150 stocks from Yahoo Finance daily bars (1 bulk download), plus a 0-100 timing `tech_score`
  * market-level breadth (share of stocks above MA20/MA60, RSI extremes, breakouts vs breakdowns)
  * FinMind (needs FINMIND_TOKEN, optional): foreign net open interest in TAIEX futures (fixes the old
    `foreign_noi` gap) and 5-day margin-balance change for the focus names (feeds `margin_inc`)
  * stashes for downstream code (NOT observations): frag["technicals"], frag["returns60"], frag["margin_inc"]
LLM part: `research()` lets the model verify chart events (gaps, breakdowns, earnings gaps) with tools.
Its free-text note goes to frag["agent_notes"] and reaches the PM only as non-citable desk notes.
"""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from observations import chunks, frag_add_obs, frag_add_source, new_fragment
from settings import PROMPT_DIR, ROOT
from tools import finmind, technicals
from util import Counter, iso, now_tw, read_cache, write_cache

CACHE_TTL_MIN = 360
TECH_FOCUS = int(os.environ.get("TECH_FOCUS", 40))                 # stocks that get citable observations
MIN_ADV20_TWD_M = float(os.environ.get("MIN_ADV20_TWD_M", 200))
MAX_STALE_DAYS = int(os.environ.get("TECH_MAX_STALE_DAYS", 0))      # 0 = last bar must be the settlement day
YAHOO_URL = "https://finance.yahoo.com/"


def _symbol(universe, t: str) -> str:
    return f"{t}.TW" if universe.markets.get(t, "twse") == "twse" else f"{t}.TWO"


# ------------------------------------------------------------------ data
def _download(universe, as_of: str, counter):
    """One bulk Yahoo download. Returns (techs, closes DataFrame, benchmark Series | None, stale_count)."""
    import pandas as pd
    import yfinance as yf

    sym2code = {_symbol(universe, t): t for t in universe.names}
    syms = list(sym2code) + ["^TWII"]
    counter.inc()
    df = yf.download(syms, period="1y", interval="1d", auto_adjust=True,
                     group_by="ticker", progress=False, threads=True)
    cutoff, as_of_d = pd.Timestamp(as_of), date.fromisoformat(as_of)
    techs, closes, bench, stale = {}, {}, None, 0
    for sym in syms:
        try:
            sub = df[sym].dropna(subset=["Close"]).copy()
        except KeyError:
            continue
        idx = pd.DatetimeIndex(sub.index)
        sub.index = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()
        sub = sub[sub.index <= cutoff]
        code = sym2code.get(sym)
        if sub.empty or (as_of_d - sub.index[-1].date()).days > MAX_STALE_DAYS:
            stale += code is not None
            continue
        if code is None:
            bench = sub["Close"].astype(float)
            continue
        f = technicals.indicators(sub)
        if f:
            techs[code] = f
            closes[code] = sub["Close"].astype(float)
    return techs, closes, bench, stale


def _returns60(closes: dict, bench):
    """Aligned last-60-day simple returns for risk maths (stash, not an observation)."""
    import pandas as pd
    if not closes or bench is None:
        return None
    cl = pd.DataFrame(closes).sort_index()
    rets = cl.pct_change(fill_method=None).iloc[-60:].fillna(0.0)
    b = bench.pct_change().reindex(rets.index).fillna(0.0)
    if len(rets) < 30:
        return None
    return {"dates": [d.strftime("%Y-%m-%d") for d in rets.index],
            "ret": {t: [round(float(x), 5) for x in rets[t]] for t in rets.columns},
            "bench": [round(float(x), 5) for x in b]}


def _focus(techs: dict, universe, holdings) -> list:
    quick = lambda t: techs[t]["tech_score"] + (10 if universe.is_core(t) else 0)
    liquid = sorted((t for t in techs if t not in holdings and techs[t]["adv20"] >= MIN_ADV20_TWD_M),
                    key=quick, reverse=True)
    order = [t for t in holdings if t in techs] + (["2330"] if "2330" in techs else []) + liquid
    return list(dict.fromkeys(order))[:max(TECH_FOCUS, len(holdings))]


def _margins(tickers, as_of: str, counter):
    out, first_err = {}, None

    def one(t):
        try:
            return t, finmind.margin_change(t, as_of, counter=counter), None
        except Exception as e:                       # one bad ticker must not stop the rest
            return t, None, f"{type(e).__name__}: {str(e)[:100]}"

    with ThreadPoolExecutor(4) as ex:
        for t, m, err in ex.map(one, tickers):
            first_err = first_err or err
            if m and m.get("chg_frac") is not None:
                out[t] = m
    return out, first_err


# ------------------------------------------------------------------ evidence
def _emit(frag, techs, focus, margins, noi, as_of: str, px_src: int, fm_src: int | None):
    b = technicals.breadth(techs)
    if b:
        frag["breadth"] = b
        frag_add_obs(
            frag, px_src,
            (f"技術面廣度（{b['n']} 檔）：{b['pct_above_ma20']:.0f}% 站上 MA20、{b['pct_above_ma60']:.0f}% 站上 MA60、"
             f"{b['pct_rsi_gt70']:.0f}% RSI>70、{b['pct_rsi_lt30']:.0f}% RSI<30、MACD 柱狀為正 {b['pct_macd_pos']:.0f}%；"
             f"創20日高 {b['n_breakout20']} 檔 vs 破20日低 {b['n_breakdown20']} 檔；技術分數中位數 {b['median_tech_score']:.0f}"),
            {"pct_above_ma20": b["pct_above_ma20"], "pct_above_ma60": b["pct_above_ma60"],
             "pct_rsi_gt70": b["pct_rsi_gt70"], "pct_rsi_lt30": b["pct_rsi_lt30"],
             "pct_macd_pos": b["pct_macd_pos"], "n_breakout20": b["n_breakout20"],
             "n_breakdown20": b["n_breakdown20"], "median_tech_score": b["median_tech_score"]},
            "tech_breadth", "market")
    if noi and fm_src is not None:
        vals = {"foreign_noi": noi["noi"]}
        txt = f"外資台股期貨(TX)淨未平倉 {noi['date']} 為 {noi['noi']:+,.0f} 口"
        if noi.get("prev") is not None:
            vals["foreign_noi_prev"], vals["foreign_noi_chg"] = noi["prev"], noi["noi"] - noi["prev"]
            txt += f"（前一日 {noi['prev']:+,.0f}，變動 {noi['noi'] - noi['prev']:+,.0f}）"
        frag_add_obs(frag, fm_src, txt, vals, "macro_foreign_noi", "market")
    for g in chunks(focus, 8):   # keeps each statement under the 500-char schema cut
        parts, vals = [], {}
        for t in g:
            d = techs[t]
            m = margins.get(t)
            parts.append(technicals.line(t, d, None if not m else m["chg_frac"] * 100))
            vals.update({f"{t}_tech_score": d["tech_score"], f"{t}_rsi14": d["rsi14"],
                         f"{t}_dist_ma20_pct": d["dist_ma20_pct"], f"{t}_dist_ma60_pct": d["dist_ma60_pct"],
                         f"{t}_atr14_pct": d["atr14_pct"], f"{t}_vol_ratio": d["vol_ratio"]})
            if m:
                vals[f"{t}_margin_chg5d_pct"] = round(m["chg_frac"] * 100, 2)
        used_fm = fm_src is not None and any(t in margins for t in g)
        frag["observations"].append({
            "src": [px_src] + ([fm_src] if used_fm else []),
            "statement": "技術指標（截至結算日）：" + "；".join(parts),
            "values": vals, "topic": "tech_stock", "level": "stock", "tickers": list(g)})


def research(trade_date, as_of, universe, holdings, frag, counter):
    from agent_loop import run_agent_loop
    from tools import web_tools
    tools = web_tools.build_tools(web_tools.fragment_emit(frag), trade_date=trade_date, as_of=as_of,
                                  universe=universe, counter=counter, obs_cap=3,
                                  include=("search", "fetch", "price"))
    techs = frag.get("technicals", {})
    user = json.dumps({
        "trade_date": trade_date, "as_of_date": as_of, "breadth": frag.get("breadth"),
        "current_holdings": {t: {k: techs[t].get(k) for k in ("tech_score", "rsi14", "dist_ma20_pct", "atr14_pct", "flags")}
                             for t in holdings if t in techs},
        "flagged": {t: d["flags"] for t, d in techs.items()
                    if {"breakdown", "gap_down", "overbought", "macd_dead_cross"} & set(d["flags"])
                    and d["adv20"] >= MIN_ADV20_TWD_M},
        "already_collected": [o["statement"][:90] for o in frag["observations"]]}, ensure_ascii=False)
    text, usage, _, models = run_agent_loop((PROMPT_DIR / "technical_agent.md").read_text(encoding="utf-8"),
                                            user, tools, max_steps=8, max_calls=10)
    frag["agent_notes"] = text[:1500]
    frag["llm_usage"] = {**usage, "models": models}


# ------------------------------------------------------------------ public entry
def collect(trade_date: str, as_of_date: str, universe, holdings, refresh: bool = False) -> dict:
    cache = ROOT / "inputs" / trade_date / "technical_fragment.json"
    if not refresh and (hit := read_cache(cache, CACHE_TTL_MIN)):
        hit["warnings"] = hit.get("warnings", []) + ["technical evidence loaded from cache (use --refresh)"]
        return hit

    frag, counter, now = new_fragment(), Counter(), iso(now_tw())
    try:
        import yfinance  # noqa: F401
    except ImportError:
        frag["warnings"].append("yfinance not installed: technical evidence skipped")
        return frag

    techs, closes, bench, stale = {}, {}, None, 0
    try:
        techs, closes, bench, stale = _download(universe, as_of_date, counter)
    except Exception as e:
        frag["warnings"].append(f"technical download failed: {type(e).__name__}: {str(e)[:120]}")
    if stale:
        frag["warnings"].append(f"{stale} stocks had no bars for {as_of_date} (halted, stale or date not yet traded)")
    if not techs:
        frag["warnings"].append("no technical indicators available")
        return frag

    focus = _focus(techs, universe, list(holdings))
    px_src = frag_add_source(frag, {"authority": "vendor", "url": YAHOO_URL, "fetched_at": now,
                                    "name": "Yahoo Finance 日線歷史價量（技術指標）",
                                    "content_as_of": f"{as_of_date}T13:30:00+08:00"})
    fm_src, noi, margins = None, None, {}
    if finmind.have_token():
        try:
            noi = finmind.foreign_futures_noi(as_of_date, counter)
        except Exception as e:
            frag["warnings"].append(f"FinMind foreign futures OI failed: {type(e).__name__}: {str(e)[:100]}")
        margins, err = _margins(list(dict.fromkeys(list(holdings) + focus))[:40], as_of_date, counter)
        if err and not margins:
            frag["warnings"].append(f"FinMind margin data failed: {err}")
        if noi or margins:
            fm_src = frag_add_source(frag, {"authority": "vendor", "url": finmind.SITE, "fetched_at": now,
                                            "name": "FinMind 開放資料：台指期外資未平倉、個股融資餘額",
                                            "content_as_of": f"{(noi or {}).get('date', as_of_date)}T18:30:00+08:00"})
    else:
        frag["warnings"].append("FINMIND_TOKEN missing: foreign futures OI and margin signals skipped")

    for t, m in margins.items():
        if t in techs:
            techs[t]["margin_chg5d_pct"] = round(m["chg_frac"] * 100, 2)
    _emit(frag, techs, focus, margins, noi, as_of_date, px_src, fm_src)

    frag["technicals"] = techs
    frag["returns60"] = _returns60(closes, bench)
    frag["margin_inc"] = {t: round(m["chg_frac"], 4) for t, m in margins.items()}
    if os.environ.get("AGENT_AUTONOMY", "1") != "0":
        try:
            research(trade_date, as_of_date, universe, holdings, frag, counter)
        except Exception as e:
            frag["warnings"].append(f"technical research agent failed: {type(e).__name__}: {str(e)[:120]}")
    frag["tool_calls"] = counter.n
    if frag["observations"]:
        write_cache(cache, frag)
    return frag


if __name__ == "__main__":
    import sys
    from universe import load_universe
    td, ao = sys.argv[1], sys.argv[2]
    out = collect(td, ao, load_universe(), [], True)
    print(json.dumps({k: out[k] for k in ("warnings", "breadth", "agent_notes") if k in out}, ensure_ascii=False, indent=1))
    for o in out["observations"]:
        print("-", o["statement"][:160])
