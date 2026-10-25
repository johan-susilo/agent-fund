"""Risk manager agent: exchange event scan + rule-based overrides + portfolio risk maths + stress facts + LLM loop.

Layers (each one survives the failure of the next, so a broken LLM or a dead endpoint never drops a veto):
  1. exchange events (no LLM): 處置股 -> VETO, 注意股 -> WARN, 除權息 -> EXDIV flag; unreadable endpoints are reported
  2. rule overrides (no LLM) from the technical stash: high volatility/ATR -> REDUCE, held position above its liquidity
     limit -> REDUCE, breakdown / overbought / margin trap -> WARN
  3. portfolio risk facts (no LLM): 60-day historical VaR95, volatility, beta, correlation, effective N, max drawdown
     (hypothetical equal-weight book on day 1) registered as market-level observations
  4. stress facts (no LLM, free sources): WTI, US dollar index, US 10Y (Yahoo), HY credit spread and 10Y-2Y (FRED, key optional)
  5. LLM tool loop: may ADD or ESCALATE overrides (never downgrade) and writes the summary
"""
from __future__ import annotations

import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor

from agent_loop import run_agent_loop
from settings import PROMPT_DIR
from tools import web_tools
from util import Counter, http_get_json, iso, now_tw, prev_weekday

VOL_REDUCE = float(os.environ.get("RISK_VOL_REDUCE", 80))        # 20d annualised volatility (%) above which -> REDUCE
ATR_REDUCE = float(os.environ.get("RISK_ATR_REDUCE", 7))         # ATR14 as % of price above which -> REDUCE
REDUCE_CAP = 0.04                                                # must equal check_agent.REDUCE_CAP
EXIT_DAYS, PARTICIPATION = 3, 0.10                               # liquidity limit: exit in 3 days at 10% of ADV
YAHOO_URL = "https://finance.yahoo.com/"
SEV = {"WARN": 1, "REDUCE": 2, "VETO": 3}

# (tag, authority, label, url, kind). Endpoint names: verify against the OpenAPI swagger pages; a dead one is
# reported in event_flags["failed_sources"] instead of silently reading as "no stocks on the list".
EVENT_URLS = (
    ("twse_punish", "twse", "TWSE 公布處置有價證券", "https://openapi.twse.com.tw/v1/announcement/punish", "punish"),
    ("tpex_punish", "tpex", "TPEx 公布處置有價證券", "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information", "punish"),
    ("twse_notice", "twse", "TWSE 公布注意累計次數異常資訊", "https://openapi.twse.com.tw/v1/announcement/notice", "notice"),
    ("tpex_notice", "tpex", "TPEx 公布注意交易資訊", "https://www.tpex.org.tw/openapi/v1/tpex_trading_warning_information", "notice"),
    ("twse_exdiv", "twse", "TWSE 除權除息預告表", "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL", "exdiv"),
    ("tpex_exdiv", "tpex", "TPEx 除權除息預告表", "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_exright_preannounce", "exdiv"),
)
FRED_STRESS = {"BAMLH0A0HYM2": ("hy_oas", "美國高收益債信用利差 OAS(%)"),
               "T10Y2Y": ("t10y2y", "美國 10Y-2Y 殖利率差(%)")}


def _extract_code(row: dict) -> str:
    for k in ("Code", "SecuritiesCompanyCode", "股票代號", "證券代號", "CompanyCode"):
        if row.get(k):
            return str(row[k]).strip()
    return ""


# ------------------------------------------------------------------ 1. exchange events
def _scan_exchange_events(as_of: str, universe, book, counter: Counter) -> dict:
    found = {"punish": [], "notice": [], "exdiv": []}
    failed, new_ids, now = [], [], iso(now_tw())
    with ThreadPoolExecutor(4) as ex:
        futs = [(spec, ex.submit(http_get_json, spec[3], None, None, 30, 3, counter)) for spec in EVENT_URLS]
        for (tag, auth, label, url, kind), fut in futs:
            try:
                rows = fut.result()
                if not isinstance(rows, list):
                    raise ValueError("unexpected payload")
            except Exception:
                failed.append(tag)
                continue
            matched = sorted({c for r in rows if isinstance(r, dict) and (c := _extract_code(r)) in universe.names})
            if not matched:
                continue
            sid = book.add_source({"authority": auth, "name": label, "url": url,
                                   "content_as_of": f"{as_of}T18:30:00+08:00", "fetched_at": now})
            shown = matched[:15]
            word, topic, key = {"punish": ("名單包含標的池個股", "risk_disposition", "disposition_flag"),
                                "notice": ("注意股名單包含標的池個股", "risk_attention", "attention_flag"),
                                "exdiv": ("近期即將除權息標的", "risk_ex_dividend", "exdiv_flag")}[kind]
            oid = book.add_obs(f"{label}{word}：{', '.join(shown)}", {f"{c}_{key}": 1.0 for c in shown},
                               [sid], topic, level="stock", tickers=shown)
            new_ids += [oid] if oid else []
            found[kind].extend(matched)
    return {"disposition_tickers": sorted(set(found["punish"])), "attention_tickers": sorted(set(found["notice"])),
            "ex_dividend_tickers": sorted(set(found["exdiv"])), "failed_sources": failed, "obs_ids": new_ids}


# ------------------------------------------------------------------ 2. rule overrides + per-stock risk
def _stock_risk(tech: dict | None, nav: float) -> dict:
    """Per-stock beta vs TAIEX (60d) and liquidity limit `liq_w` (largest weight exitable in EXIT_DAYS at PARTICIPATION)."""
    import pandas as pd
    out = {}
    techs = (tech or {}).get("technicals") or {}
    betas = {}
    ret = (tech or {}).get("returns60")
    if ret:
        R, m = pd.DataFrame(ret["ret"]), pd.Series(ret["bench"], dtype=float)
        mc = m - m.mean()
        den = float((mc ** 2).sum())
        if den > 0:
            betas = ((R - R.mean()).mul(mc, axis=0).sum() / den).to_dict()
    for t, d in techs.items():
        liq_w = PARTICIPATION * d["adv20"] * 1e6 * EXIT_DAYS / nav
        b = betas.get(t)
        out[t] = {"beta": round(float(b), 2) if b is not None and math.isfinite(b) else None,
                  "liq_w": round(liq_w, 4)}
    return out


def _auto_overrides(held_w: dict, tech: dict | None, stock_risk: dict, flags: dict) -> list:
    techs = (tech or {}).get("technicals") or {}
    out = []
    add = lambda t, a, r: out.append({"ticker": t, "action": a, "reason": r, "obs_ref": None})
    for t, d in techs.items():
        if t != "2330" and (d["vol20"] > VOL_REDUCE or d["atr14_pct"] > ATR_REDUCE):
            add(t, "REDUCE", f"20日年化波動 {d['vol20']:.0f}%、ATR {d['atr14_pct']:.1f}% 偏高，權重上限降至 {REDUCE_CAP:.0%}")
        if t in held_w:
            lw = stock_risk.get(t, {}).get("liq_w")
            if t != "2330" and lw is not None and held_w[t] > lw:
                add(t, "REDUCE", f"持股 {held_w[t]:.1%} 超過 {EXIT_DAYS} 日可出清上限 {lw:.1%}（日均成交值 {d['adv20']:,.0f}M）")
            fl = set(d["flags"])
            if "breakdown" in fl or (d["trend"] == -1 and d["dd_high_pct"] < -20):
                add(t, "WARN", f"技術面轉弱：距MA60 {d['dist_ma60_pct']:+.1f}%、距52週高 {d['dd_high_pct']:+.1f}%")
            if "overbought" in fl:
                add(t, "WARN", f"RSI {d['rsi14']:.0f} 過熱，勿追加")
            if (d.get("margin_chg5d_pct") or 0) > 10 and d["dist_ma20_pct"] < 0:
                add(t, "WARN", f"融資5日增 {d['margin_chg5d_pct']:+.0f}% 且跌破MA20（融資套牢風險）")
    for t in flags.get("attention_tickers", []):
        add(t, "WARN", "列入交易所注意股，留意處置風險")
    return out


def _merge_overrides(*lists) -> list:
    """Per ticker keep the most severe action (VETO > REDUCE > WARN); 2330 is never REDUCE-capped."""
    best = {}
    for lst in lists:
        for o in lst or []:
            t, a = str(o.get("ticker", "")).strip(), o.get("action")
            if not re.fullmatch(r"\d{4}", t) or a not in SEV or (t == "2330" and a == "REDUCE"):
                continue
            if t not in best or SEV[a] > SEV[best[t]["action"]]:
                best[t] = {"ticker": t, "action": a, "reason": str(o.get("reason", ""))[:200], "obs_ref": o.get("obs_ref")}
    return sorted(best.values(), key=lambda x: (-SEV[x["action"]], x["ticker"]))


# ------------------------------------------------------------------ 3. portfolio risk facts
def _portfolio_stats(weights: dict, tech: dict | None) -> dict | None:
    import numpy as np
    import pandas as pd
    ret = (tech or {}).get("returns60")
    if not ret or not weights:
        return None
    R = pd.DataFrame(ret["ret"])
    ts = [t for t in weights if t in R.columns]
    if len(ts) < 3 or len(R) < 30:
        return None
    w, X, m = np.array([weights[t] for t in ts]), R[ts].to_numpy(), np.array(ret["bench"])
    rp = X @ w
    var_p = float(np.var(rp, ddof=1))
    n = len(ts)
    corr = np.nan_to_num(np.corrcoef(X.T))
    contrib = sorted(((t, float(w[i] * np.cov(X[:, i], rp)[0, 1] / var_p * 100)) for i, t in enumerate(ts)),
                     key=lambda x: -x[1]) if var_p > 0 else []
    cum = np.cumprod(1 + rp)
    wn = w / w.sum()
    return {
        "n": n, "invested_w": round(float(w.sum()), 4),
        "var95_pct_nav": round(float(-np.percentile(rp, 5) * 100), 2),
        "ann_vol_pct": round(float(np.std(rp, ddof=1) * math.sqrt(252) * 100), 1),
        "beta": round(float(np.cov(rp, m)[0, 1] / np.var(m, ddof=1)), 2) if np.var(m) > 0 else None,
        "avg_corr": round(float((corr.sum() - n) / (n * (n - 1))), 2),
        "eff_n": round(float(1 / np.sum(wn ** 2)), 1),
        "max_dd60_pct": round(float((cum / np.maximum.accumulate(cum) - 1).min() * 100), 1),
        "top_risk": [(t, round(c, 1)) for t, c in contrib[:3]],
    }


def _register_portfolio_obs(book, stats: dict, label: str, as_of: str) -> str | None:
    sid = book.add_source({"authority": "vendor", "name": "Yahoo Finance 日線（投組風險試算）", "url": YAHOO_URL,
                           "content_as_of": f"{as_of}T13:30:00+08:00", "fetched_at": iso(now_tw())})
    vals = {k: stats[k] for k in ("var95_pct_nav", "ann_vol_pct", "beta", "avg_corr", "eff_n", "max_dd60_pct") if stats.get(k) is not None}
    top = "、".join(f"{t} {c:.0f}%" for t, c in stats["top_risk"])
    return book.add_obs(
        f"{label}（{stats['n']} 檔、投入 {stats['invested_w']:.0%}）：60日歷史模擬單日 VaR95 {stats['var95_pct_nav']:.2f}% NAV、"
        f"年化波動 {stats['ann_vol_pct']:.1f}%、對加權指數 Beta {stats.get('beta')}、持股平均相關係數 {stats['avg_corr']:.2f}、"
        f"有效持股數 {stats['eff_n']:.1f}、60日最大回檔 {stats['max_dd60_pct']:.1f}%；風險貢獻前三：{top}",
        vals, [sid], "risk_portfolio", level="market")


# ------------------------------------------------------------------ 4. stress facts (free sources)
def _stress(book, as_of: str, trade_date: str, counter: Counter, warns: list) -> list:
    ids, now = [], iso(now_tw())
    try:
        import yfinance as yf
        from agents.macro_agent import _us_close_to_tw
        counter.inc()
        syms = {"CL=F": ("wti", "WTI 原油"), "DX-Y.NYB": ("dxy", "美元指數 DXY"), "^TNX": ("tnx", "美國10年期殖利率(%)")}
        df = yf.download(list(syms), period="3mo", interval="1d", auto_adjust=True, group_by="ticker",
                         progress=False, threads=True)
        parts, vals, last_day = [], {}, None
        for sym, (k, label) in syms.items():
            try:
                c = df[sym]["Close"].dropna().astype(float)
            except KeyError:
                continue
            if len(c) < 21:
                continue
            v0, v5, ma20 = float(c.iloc[-1]), float(c.iloc[-6]), float(c.iloc[-20:].mean())
            last_day = str(c.index[-1].date())
            parts.append(f"{label} {v0:g}（5日 {(v0 / v5 - 1) * 100:+.1f}%、較20日均線 {(v0 / ma20 - 1) * 100:+.1f}%）")
            vals.update({k: round(v0, 3), f"{k}_chg5d_pct": round((v0 / v5 - 1) * 100, 2),
                         f"{k}_vs_ma20_pct": round((v0 / ma20 - 1) * 100, 2)})
        if parts:
            sid = book.add_source({"authority": "vendor", "name": "Yahoo Finance：原油、美元指數、美債殖利率",
                                   "url": YAHOO_URL, "content_as_of": _us_close_to_tw(last_day), "fetched_at": now})
            oid = book.add_obs("總經壓力指標：" + "；".join(parts), vals, [sid], "risk_stress", level="market")
            ids += [oid] if oid else []
    except Exception as e:
        warns.append(f"stress (Yahoo) failed: {type(e).__name__}: {str(e)[:100]}")

    key = os.environ.get("FRED_API_KEY")
    if key:
        try:
            from agents.macro_agent import _us_close_to_tw
            parts, vals, last_day = [], {}, None
            for sid_name, (k, label) in FRED_STRESS.items():
                js = http_get_json("https://api.stlouisfed.org/fred/series/observations",
                                   {"series_id": sid_name, "api_key": key, "file_type": "json", "sort_order": "desc",
                                    "limit": 10, "observation_end": prev_weekday(trade_date)}, counter=counter)
                obs = [(o["date"], float(o["value"])) for o in js.get("observations", []) if o["value"] not in (".", "")]
                if len(obs) < 2:
                    continue
                (d0, v0), (_, v1) = obs[0], obs[1]
                last_day = max(last_day or d0, d0)
                parts.append(f"{label} {d0} 為 {v0:g}（前值 {v1:g}，變動 {(v0 - v1) * 100:+.0f}bp）")
                vals.update({k: v0, f"{k}_prev": v1, f"{k}_chg_bp": round((v0 - v1) * 100, 1)})
            if parts:
                sid = book.add_source({"authority": "vendor", "name": "FRED：高收益債利差與殖利率曲線",
                                       "url": "https://fred.stlouisfed.org/series/BAMLH0A0HYM2",
                                       "content_as_of": _us_close_to_tw(last_day), "fetched_at": now})
                oid = book.add_obs("信用與利率結構：" + "；".join(parts), vals, [sid], "risk_credit", level="market")
                ids += [oid] if oid else []
        except Exception as e:
            warns.append(f"stress (FRED) failed: {type(e).__name__}: {str(e)[:100]}")
    return ids


# ------------------------------------------------------------------ main entry
def review(trade_date, as_of, state, universe, book, factor_df, regime, tech=None, use_llm: bool = True):
    counter, warns, nav = Counter(), [], state["nav"]

    # 1. exchange events (a failure here must not abort the review)
    try:
        event_flags = _scan_exchange_events(as_of, universe, book, counter)
    except Exception as e:
        event_flags = {"disposition_tickers": [], "attention_tickers": [], "ex_dividend_tickers": [],
                       "failed_sources": ["scan_crashed"], "obs_ids": []}
        warns.append(f"exchange scan crashed: {type(e).__name__}: {str(e)[:100]}")
    if event_flags["failed_sources"]:
        print(f"  !! risk: exchange endpoints unreadable (lists treated as UNKNOWN): {event_flags['failed_sources']}")

    # 2. per-stock risk + rule overrides
    held_w = {t: round(sh * state["last_close"].get(t, 0) / nav, 4) for t, sh in state["holdings"].items()}
    stock_risk = _stock_risk(tech, nav)
    rule_ov = _auto_overrides(held_w, tech, stock_risk, event_flags)

    # 3. portfolio risk facts (current book, or a hypothetical equal-weight book on day 1)
    new_ids = list(event_flags.get("obs_ids", []))
    stats, label = None, "目前投組"
    try:
        weights = dict(held_w)
        if not weights and factor_df is not None and not factor_df.empty:
            top = factor_df[factor_df["core"] == True].sort_values("score", ascending=False).head(20)["ticker"].tolist()  # noqa: E712
            weights, label = {t: 0.92 / len(top) for t in top}, "假想等權組合（前20名核心候選，空倉首日參考）"
        stats = _portfolio_stats(weights, tech)
        if stats:
            oid = _register_portfolio_obs(book, stats, label, as_of)
            new_ids += [oid] if oid else []
    except Exception as e:
        warns.append(f"portfolio risk stats failed: {type(e).__name__}: {str(e)[:100]}")

    # 4. stress facts
    new_ids += _stress(book, as_of, trade_date, counter, warns)

    # 5. LLM loop
    parsed, text, usage, models = {}, "", {"input_tokens": 0, "output_tokens": 0}, []
    if use_llm:
        try:
            techs = (tech or {}).get("technicals") or {}
            held = []
            for t, w in held_w.items():
                d = techs.get(t, {})
                held.append({"t": t, "name": universe.names.get(t), "w": w, "theme": universe.theme_of(t),
                             "rsi": d.get("rsi14"), "d20": d.get("dist_ma20_pct"), "atr": d.get("atr14_pct"),
                             "flags": d.get("flags"), **stock_risk.get(t, {})})
            f = factor_df.set_index("ticker") if factor_df is not None and not factor_df.empty else None
            theme_w: dict = {}
            for h in held:
                theme_w[h["theme"]] = round(theme_w.get(h["theme"], 0.0) + h["w"], 4)
            top_c = ([] if f is None else
                     f[f["core"] == True].sort_values("score", ascending=False).head(15)  # noqa: E712
                     [["theme", "score", "mom20", "mom60", "vol20", "adv20"]].round(2).reset_index().to_dict("records"))
            tools = web_tools.build_tools(web_tools.book_emit(book), trade_date=trade_date, as_of=as_of,
                                          universe=universe, counter=counter, obs_cap=3,
                                          include=("search", "fetch", "price"))
            user = json.dumps({
                "trade_date": trade_date, "regime_hint": regime,
                "exchange_event_flags": {k: v for k, v in event_flags.items() if k != "obs_ids"},
                "rule_overrides": [{k: o[k] for k in ("ticker", "action", "reason")} for o in rule_ov][:40],
                "portfolio_risk": {"label": label, **(stats or {})} if stats else None,
                "stress": [{"id": o["obs_id"], "text": o["statement"]} for o in book.observations
                           if o["obs_id"] in new_ids and o["topic"] in ("risk_stress", "risk_credit")],
                "new_obs_ids": new_ids,
                "current_theme_weights": theme_w, "current_holdings": held, "top_core_candidates": top_c,
            }, ensure_ascii=False)
            text, usage, _, models = run_agent_loop((PROMPT_DIR / "risk_agent.md").read_text(encoding="utf-8"),
                                                    user, tools, max_steps=6, max_calls=8)
            clean = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
            parsed = json.loads(clean)
        except json.JSONDecodeError:
            parsed = {"portfolio_risk_summary": text[:1800], "overrides": []}
        except Exception as e:
            warns.append(f"risk LLM loop failed (rule-based overrides still apply): {type(e).__name__}: {str(e)[:100]}")

    # 6. merge: rule overrides + LLM additions + hard VETO for every 處置股
    hard = [{"ticker": t, "action": "VETO", "reason": "TWSE/TPEx 處置股，流動性受限予以排除", "obs_ref": None}
            for t in event_flags["disposition_tickers"]]
    merged = _merge_overrides(hard, rule_ov, parsed.get("overrides"))
    summary = parsed.get("portfolio_risk_summary") or (
        f"規則式風控：{len(merged)} 檔覆寫；" + (
            f"投組 VaR95 {stats['var95_pct_nav']}% NAV、Beta {stats.get('beta')}、平均相關 {stats['avg_corr']}、有效持股 {stats['eff_n']}。"
            if stats else "無法計算投組風險（缺少報酬資料）。"))
    if event_flags["failed_sources"]:
        summary += f"（交易所端點讀取失敗：{','.join(event_flags['failed_sources'])}，名單視為未知）"
    for w in warns:
        print("  !! risk:", w)

    return {
        "structured_risk": {"portfolio_risk_summary": summary, "overrides": merged},
        "event_flags": event_flags, "notes": str(summary)[:1800], "usage": usage,
        "tool_calls": counter.n, "models": models,
        "portfolio_risk": ({"label": label, **stats} if stats else None),
        "stock_risk": stock_risk, "new_obs_ids": new_ids, "warnings": warns,
    }
