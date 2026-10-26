"""Macro agent: collects macro / news / calendar FACTS with sources (no LLM involved).

Why no LLM here: D-Plan observations must be verifiable facts. An LLM summary is an
inference, so interpretation is left to the portfolio agent, which cites these observations.
All calls run in parallel; results are cached per trade date.

Env keys (all optional; a missing key just skips that data source):
    FRED_API_KEY, NEWS_API_KEY, FINNHUB_API_KEY, ARCHIVE_NEWS=0 to skip web.archive.org snapshots
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
import yfinance as yf

from observations import frag_add, new_fragment
from settings import ROOT, TW
from util import Counter, http_get_json, iso, now_tw, parse_ts, prev_weekday, read_cache, write_cache

load_dotenv()

CACHE_TTL_MIN = 180
# FRED series -> (obs key, label, unit of change). The series PAGE url is cited, never the API url (it holds the key).
FRED_SERIES = {
    "DGS10": ("us10y", "美國 10 年期公債殖利率(%)", "bp"),
    "VIXCLS": ("vix", "VIX 恐慌指數", "pct"),
    "DEXTAUS": ("usdtwd", "美元兌新台幣(TWD/USD)", "pct"),
    "NASDAQCOM": ("nasdaq", "那斯達克綜合指數", "pct"),
}
NEWS_QUERIES = {"news_semis": "Taiwan semiconductor OR TSMC OR AI server",
                "news_macro": "Federal Reserve interest rate OR Taiwan dollar"}


# Add to src/agents/macro_agent.py
YF_MACRO = {
    "^SOX":  ("sox", "費城半導體指數 (SOX)"),
    "TSM":   ("tsm_adr", "台積電 ADR (TSM)"),
    "NVDA":  ("nvda", "NVIDIA (NVDA) AI伺服器指標"),
    "MU":    ("mu", "Micron (MU) HBM與記憶體指標"),
    "^VIX":  ("vix_yf", "VIX 波動率指數 (含20日均線比)"),
    "^TWII": ("taiex", "台灣加權指數 (TAIEX)"),
    "TWD=X": ("usdtwd_spot", "美元兌台幣即期匯率 (USD/TWD)"),
}

NEWS_KEYWORDS = (
    "tsmc", "taiwan", "nvidia", "semiconductor", "chip", "wafer", "cowos",
    "ai server", "supermicro", "broadcom", "asml", "fed", "powell", "treasury",
    "tariff", "export", "rate", "inflation",
)

def _yf_macro(trade_date: str, counter: Counter):
    counter.inc()
    df = yf.download(list(YF_MACRO.keys()), period="2mo", interval="1d", auto_adjust=True,
                     group_by="ticker", progress=False, threads=True)
    now = iso(now_tw())
    out, warns = [], []
    us_syms = {"^SOX", "TSM", "NVDA", "MU", "^VIX"}
    for sym, (k, label) in YF_MACRO.items():
        try:
            sub = df[sym].dropna(subset=["Close"])
            if len(sub) < 2:
                continue
            closes = sub["Close"].astype(float)
            v0, v1 = float(closes.iloc[-1]), float(closes.iloc[-2])
            d0 = str(sub.index[-1].date())
            chg = (v0 / v1 - 1.0) * 100.0 if v1 else 0.0
            ma20 = float(closes.iloc[-20:].mean()) if len(closes) >= 20 else v0
            vs_ma20_pct = (v0 / ma20 - 1.0) * 100.0 if ma20 else 0.0
            src = {
                "authority": "vendor",
                "name": f"Yahoo Finance ({sym}): {label}",
                "url": f"https://finance.yahoo.com/quote/{sym}/",
                "content_as_of": _us_close_to_tw(d0) if sym in us_syms else f"{d0}T13:30:00+08:00",
                "fetched_at": now,
            }
            stmt = f"{label} {d0} 收 {v0:g}（前值 {v1:g}，單日 {chg:+.2f}%，較20日均線 {vs_ma20_pct:+.1f}%）"
            vals = {k: round(v0, 2), f"{k}_prev": round(v1, 2),
                    f"{k}_chg_pct": round(chg, 2), f"{k}_vs_ma20_pct": round(vs_ma20_pct, 2)}
            out.append((src, stmt, vals, f"macro_{k}"))
        except Exception as e:
            warns.append(f"yfinance {sym} failed: {e}")
    return out, warns

def _us_close_to_tw(date_str: str) -> str:
    """US market date -> the moment of its 16:00 New York close, expressed in Taipei time."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:                       # tzdata missing (e.g. Windows): fixed EST offset
        tz = timezone(timedelta(hours=-5))
    return iso(datetime.fromisoformat(date_str + "T16:00:00").replace(tzinfo=tz))


# ------------------------------------------------------------------ data tasks
def _fred(series_id, trade_date, counter):
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return [], ["FRED_API_KEY missing: US rates / VIX / FX / Nasdaq skipped"]
    k, label, unit = FRED_SERIES[series_id]
    js = http_get_json("https://api.stlouisfed.org/fred/series/observations",
                       {"series_id": series_id, "api_key": key, "file_type": "json", "sort_order": "desc",
                        "limit": 10, "observation_end": prev_weekday(trade_date)}, counter=counter)
    obs = [(o["date"], float(o["value"])) for o in js.get("observations", []) if o["value"] not in (".", "")]
    if len(obs) < 2:
        return [], [f"FRED {series_id}: fewer than 2 valid observations"]
    (d0, v0), (_, v1) = obs[0], obs[1]
    chg = (v0 - v1) * 100 if unit == "bp" else (v0 / v1 - 1) * 100 if v1 else 0.0
    values = {k: v0, f"{k}_prev": v1, f"{k}_chg_{unit}": round(chg, 2)}
    stmt = f"{label} {d0} 收 {v0:g}（前一筆 {v1:g}，變動 {chg:+.2f}{'bp' if unit == 'bp' else '%'}）"
    src = {"authority": "vendor", "name": f"FRED {series_id}: {label}",
           "url": f"https://fred.stlouisfed.org/series/{series_id}",
           "content_as_of": _us_close_to_tw(d0), "fetched_at": iso(now_tw())}
    return [(src, stmt, values, f"macro_{k}")], []


def _archive(url):
    """Best-effort web.archive.org snapshot; returns the snapshot URL or None."""
    try:
        r = requests.get("https://web.archive.org/save/" + url, timeout=40, headers={"User-Agent": "Mozilla/5.0"})
        loc = r.headers.get("Content-Location")
        if loc and loc.startswith("/web/"):
            return "https://web.archive.org" + loc
    except requests.RequestException:
        pass
    return None


def _news(topic, query, counter):
    key = os.environ.get("NEWS_API_KEY")
    if not key:
        return [], ["NEWS_API_KEY missing: news skipped"]
    now = now_tw()
    js = http_get_json("https://newsapi.org/v2/everything",
                       {"q": query, "language": "en", "sortBy": "publishedAt", "pageSize": 6,
                        "from": (now - timedelta(hours=36)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                        "apiKey": key}, counter=counter)
    seen_titles = set()
    items = []
    for a in js.get("articles", []):
        title = (a.get("title") or "").strip()
        if not title or not a.get("url") or not a.get("publishedAt"):
            continue
        norm_title = "".join(c for c in title.lower() if c.isalnum())[:50]
        if norm_title in seen_titles:
            continue
        blob = f"{title} {a.get('description') or ''}".lower()
        if not any(kw in blob for kw in NEWS_KEYWORDS):
            continue
        pub = parse_ts(a["publishedAt"])
        if pub > now:
            continue
        seen_titles.add(norm_title)
        items.append((a, pub))
        if len(items) == 3:
            break
    snaps = [None] * len(items)
    if items and os.environ.get("ARCHIVE_NEWS", "1") != "0":
        with ThreadPoolExecutor(3) as ex:
            snaps = list(ex.map(lambda ap: _archive(ap[0]["url"]), items))
    out = []
    for (a, pub), snap in zip(items, snaps):
        site = (a.get("source") or {}).get("name") or "news"
        src = {"authority": "media", "name": f"{site}: {a['title']}", "url": a["url"], "archive_url": snap,
               "published_at": iso(pub), "content_as_of": iso(pub), "fetched_at": iso(now)}
        stmt = f"{site} 報導：{a['title']}"
        out.append((src, stmt, {"published_hours_ago": round((now - pub).total_seconds() / 3600, 1)}, topic))
    return out, []


def _calendar(counter):
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return [], ["FINNHUB_API_KEY missing: economic calendar skipped"]
    now = now_tw()
    js = http_get_json("https://finnhub.io/api/v1/calendar/economic",
                       {"from": now.date().isoformat(), "to": (now + timedelta(days=4)).date().isoformat(),
                        "token": key}, counter=counter)
    out = []
    for e in js.get("economicCalendar", []):
        if e.get("country") in ("US", "TW") and str(e.get("impact", "")).lower() == "high" and e.get("time"):
            when = parse_ts(e["time"].replace(" ", "T") + "+00:00") if "T" not in e["time"] else parse_ts(e["time"])
            if when > now:
                src = {"authority": "vendor", "name": "Finnhub economic calendar",
                       "url": "https://finnhub.io/docs/api/economic-calendar",
                       "content_as_of": iso(now), "fetched_at": iso(now)}
                out.append((src, f"即將公布（高影響）：{e['country']} {e['event']}，預定 {iso(when)}",
                            {"hours_until": round((when - now).total_seconds() / 3600, 1)}, "macro_calendar"))
        if len(out) == 3:
            break
    return out, []


def research(trade_date: str, frag: dict, counter: Counter) -> None:
    """LLM decides which extra tools to call; every result is registered as an observation."""
    import json
    from agent_loop import run_agent_loop
    from settings import PROMPT_DIR
    from tools import web_tools
    tools = web_tools.build_tools(web_tools.fragment_emit(frag), trade_date=trade_date,
                                  as_of=prev_weekday(trade_date), counter=counter, obs_cap=5,
                                  include=("search", "fetch", "price"))
    user = json.dumps({"trade_date": trade_date, "now": iso(now_tw()),
                       "already_collected": [o["statement"][:90] for o in frag["observations"]]},
                      ensure_ascii=False)
    text, usage, _, models = run_agent_loop((PROMPT_DIR / "macro_agent.md").read_text(encoding="utf-8"),
                                            user, tools, max_steps=8, max_calls=10)
    frag["agent_notes"] = text[:1500]
    frag["llm_usage"] = {**usage, "models": models}


# ------------------------------------------------------------------ public entry
def collect(trade_date: str, refresh: bool = False) -> dict:
    cache = ROOT / "inputs" / trade_date / "macro_fragment.json"
    if not refresh and (hit := read_cache(cache, CACHE_TTL_MIN)):
        hit["warnings"] = hit.get("warnings", []) + ["macro evidence loaded from cache (use --refresh to refetch)"]
        return hit

    counter, frag = Counter(), new_fragment()
    with ThreadPoolExecutor(8) as ex:
        futs = [ex.submit(_fred, s, trade_date, counter) for s in FRED_SERIES]
        futs.append(ex.submit(_yf_macro, trade_date, counter))
        futs += [ex.submit(_news, t, q, counter) for t, q in NEWS_QUERIES.items()]
        futs.append(ex.submit(_calendar, counter))
        for f in futs:                       # fixed order -> deterministic observation ids
            try:
                items, warns = f.result()
            except Exception as e:           # one failing source must not stop the pipeline
                items, warns = [], [f"macro source failed: {type(e).__name__}: {str(e)[:120]}"]
            frag["warnings"] += warns
            for src, stmt, values, topic in items:
                frag_add(frag, src, stmt, values, topic, level="market")
    if os.environ.get("AGENT_AUTONOMY", "1") != "0" and os.environ.get("EVIDENCE_MODE", "merged") == "legacy":  # [upgrade-2026-10]
        try:
            research(trade_date, frag, counter)
        except Exception as e:                       # baseline evidence is still valid
            frag["warnings"].append(f"macro research agent failed: {type(e).__name__}: {str(e)[:120]}")
    frag["warnings"] = list(dict.fromkeys(frag["warnings"]))
    frag["tool_calls"] = counter.n
    if frag["observations"]:
        write_cache(cache, frag)
    return frag

if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(collect(sys.argv[1] if len(sys.argv) > 1 else now_tw().date().isoformat(), True),
                     ensure_ascii=False, indent=1))
