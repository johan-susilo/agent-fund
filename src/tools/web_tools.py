"""Internet / data tools exposed to the LLM agents.

Fact tools register their results as sourced observations through `emit`; the LLM never
writes observations itself. No tool here can place an order.
"""
from __future__ import annotations

import ipaddress
import itertools
import os
import re
import socket
from urllib.parse import urljoin, urlparse

import requests

from agent_loop import Tool
from observations import chunks
from util import iso, now_tw, save_raw_bytes

MAX_PAGE_BYTES = 600_000


def fragment_emit(frag):
    from observations import frag_add
    def emit(src, stmt, vals, topic, level="market", tickers=()):
        frag_add(frag, src, stmt, vals, topic, level, tickers)
        return None                      # ids are assigned later by the EvidenceBook
    return emit


def book_emit(book):
    def emit(src, stmt, vals, topic, level="market", tickers=()):
        return book.add_obs(stmt, vals, [book.add_source(src)], topic, level, tickers)
    return emit


def _safe_get(url: str, timeout: int = 20, hops: int = 3):
    """GET with SSRF guard: public http(s) hosts only, manual redirects, size cap."""
    for _ in range(hops + 1):
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise ValueError("only http(s) URLs are allowed")
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValueError("blocked address")
        r = requests.get(url, timeout=timeout, stream=True, allow_redirects=False,
                         headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, r.headers.get("Location", ""))
            r.close()
            continue
        r.raise_for_status()
        body = b"".join(itertools.islice(r.iter_content(65536), MAX_PAGE_BYTES // 65536 + 1))
        return url, r, body
    raise ValueError("too many redirects")


def _authority(host: str) -> str:
    if host.endswith("mops.twse.com.tw"):
        return "mops"
    if host.endswith("twse.com.tw"):
        return "twse"
    if host.endswith("tpex.org.tw"):
        return "tpex"
    return "media"


def build_tools(emit, *, trade_date: str, as_of: str, universe=None, counter=None, obs_cap: int = 5,
                include=("search", "fetch", "price", "fundamentals")) -> list:
    used = {"n": 0}

    def inc():
        if counter:
            counter.inc()

    def register(src, stmt, vals, topic, level="market", tickers=()):
        if emit is None:
            return None
        if used["n"] >= obs_cap:
            raise RuntimeError(f"evidence budget ({obs_cap}) used up; work with what you already have")
        used["n"] += 1
        return emit(src, stmt, vals, topic, level, tickers)

    def web_search(query: str, max_results: int = 8):
        from tools.macro_tools import fetch_gdelt_news
        inc()
        rows = fetch_gdelt_news([query], max_records=min(int(max_results), 10))
        if rows and "error" in rows[0]:
            return {"error": rows[0]["error"]}
        return {"results": [{"title": r["title"], "url": r["url"], "domain": r["domain"],
                             "seen": r["published_at"]} for r in rows],
                "note": "leads only; call fetch_url to read a page you want to rely on"}

    def fetch_url(url: str):
        inc()
        final, r, body = _safe_get(url)
        ctype = r.headers.get("Content-Type", "")
        if not re.search(r"text|json|xml|html", ctype, re.I):
            return {"error": f"unsupported content-type {ctype}"}
        raw = body.decode(r.encoding or "utf-8", "ignore")
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
        title = re.sub(r"\s+", " ", m.group(1)).strip() if m else final
        text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        host = urlparse(final).hostname or ""
        save_raw_bytes(trade_date, "web_" + host, body)
        archive = None
        if os.environ.get("ARCHIVE_NEWS", "1") != "0":
            from agents.macro_agent import _archive
            archive = _archive(final)
        now = iso(now_tw())
        src = {"authority": _authority(host), "name": title[:200], "url": final,
               "archive_url": archive, "content_as_of": now, "fetched_at": now}
        oid = register(src, f"網頁《{title[:80]}》({host}) 內文摘錄：{text[:280]}",
                       {"page_chars": len(text)}, "web_page")
        return {"obs_id": oid, "title": title, "text": text[:3500]}

    def get_price_data(symbol: str):
        import pandas as pd
        import yfinance as yf
        from agents.macro_agent import _us_close_to_tw
        from tools.factors import price_factors_from_ohlcv

        sym = str(symbol).strip()
        code = sym if re.fullmatch(r"\d{4}", sym) else None
        if code:
            twse = universe is None or universe.markets.get(code, "twse") == "twse"
            sym = f"{code}.TW" if twse else f"{code}.TWO"
        tw = sym.endswith((".TW", ".TWO")) or sym == "^TWII"
        inc()
        df = yf.Ticker(sym).history(period="6mo", interval="1d", auto_adjust=True)
        if df is None or df.empty:
            return {"error": f"no data for {sym}"}
        idx = df.index.tz_localize(None) if getattr(df.index, "tz", None) is not None else df.index
        cutoff = pd.Timestamp(as_of if tw else now_tw().date().isoformat())
        df = df[idx <= cutoff]
        if len(df) < 2:
            return {"error": "not enough history"}
        c = df["Close"].astype(float)
        d0 = str(df.index[-1].date())
        out = {"symbol": sym, "last_date": d0, "close": round(float(c.iloc[-1]), 4),
               "chg_1d_pct": round((c.iloc[-1] / c.iloc[-2] - 1) * 100, 2)}
        for n in (5, 20, 60):
            if len(c) > n:
                out[f"ret_{n}d_pct"] = round((c.iloc[-1] / c.iloc[-1 - n] - 1) * 100, 2)
        f = price_factors_from_ohlcv(df) if "Volume" in df else None
        if f:
            out["vol20_ann_pct"], out["adv20_twd_m"] = round(f["vol20"], 1), round(f["adv20"], 0)
        k = code or re.sub(r"[^a-z0-9]", "", sym.lower())
        vals = {f"{k}_close": out["close"], f"{k}_chg_pct": out["chg_1d_pct"]}
        if code:
            for a, b in (("ret_20d_pct", "mom20_pct"), ("ret_60d_pct", "mom60_pct"),
                         ("vol20_ann_pct", "vol20_pct"), ("adv20_twd_m", "adv20_twd_m")):
                if a in out:
                    vals[f"{code}_{b}"] = out[a]
        else:
            for n in (5, 20, 60):
                if f"ret_{n}d_pct" in out:
                    vals[f"{k}_ret{n}d_pct"] = out[f"ret_{n}d_pct"]
        now_str = iso(now_tw())
        expected_as_of = f"{d0}T13:30:00+08:00" if tw else _us_close_to_tw(d0)
        is_intraday = expected_as_of > now_str
        
        actual_as_of = now_str if is_intraday else expected_as_of
        disclaimer = " (未定時盤中快照)" if is_intraday else ""

        src = {"authority": "vendor", 
               "name": f"Yahoo Finance ({sym}) 日線{disclaimer}", 
               "fetched_at": now_str,
               "url": f"https://finance.yahoo.com/quote/{sym}/",
               "content_as_of": actual_as_of}
               
        status_label = "盤中(報價時間未知)" if is_intraday else "收"
        stmt = (f"{sym} {d0} {status_label} {out['close']:g}（1日{out['chg_1d_pct']:+.2f}%"
                + "".join(f"、{n}日{out[f'ret_{n}d_pct']:+.1f}%" for n in (5, 20, 60) if f"ret_{n}d_pct" in out) + "）")
        out["obs_id"] = register(src, stmt, vals, "agent_price", "stock" if code else "market",
                                 [code] if code else [])
        return out

    def get_fundamentals(tickers: list):
        from agents import fundamental_agent as fa
        infos = fa.official_fundamentals(trade_date, universe, counter)
        want = [str(t) for t in tickers][:20]
        ids = []
        for g in chunks([t for t in want if infos.get(t)], 10):
            stmt, vals = fa.fmt_fundamentals(infos, g)
            ids.append(register(fa.official_source(as_of), stmt, vals, "factor_fundamental", "stock", g))
        return {"data": {t: infos.get(t) for t in want}, "obs_ids": ids,
                "note": "rev_g / margin are fractions"}

    S = lambda d: {"type": "STRING", "description": d}
    tools = []
    if "search" in include:
        tools.append(Tool("web_search", "Search recent news (GDELT). Returns leads (title/url) only.",
                          {"query": S("short English or Chinese query"),
                           "max_results": {"type": "INTEGER", "description": "1-10"}}, web_search, ["query"]))
    if "fetch" in include:
        tools.append(Tool("fetch_url", "Read a public web page; it is recorded as citable evidence.",
                          {"url": S("http(s) URL")}, fetch_url, ["url"]))
    if "price" in include:
        tools.append(Tool("get_price_data",
                          "Daily price stats (1/5/20/60d return, volatility, liquidity) for a 4-digit Taiwan ticker "
                          "or a Yahoo symbol such as ^SOX, TSM, ^TNX, DX-Y.NYB, ^VIX, TWD=X.",
                          {"symbol": S("e.g. 2330 or ^SOX")}, get_price_data, ["symbol"]))
    if "fundamentals" in include and universe is not None:
        tools.append(Tool("get_fundamentals",
                          "Official TWSE/TPEx/MOPS P/E, P/B, monthly-revenue YoY and margin for up to 20 tickers.",
                          {"tickers": {"type": "ARRAY", "items": {"type": "STRING"}}}, get_fundamentals, ["tickers"]))
    return tools
