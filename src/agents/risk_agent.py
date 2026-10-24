"""Risk manager agent: deterministic TWSE/TPEx event checks (處置股, 除權息) + LLM tool loop."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from agent_loop import run_agent_loop
from settings import PROMPT_DIR
from tools import web_tools
from util import Counter, http_get_json, iso, now_tw

EVENT_URLS = (
    ("twse_punish", "twse", "TWSE 公布處置有價證券", "https://openapi.twse.com.tw/v1/announcement/punish"),
    ("tpex_punish", "tpex", "TPEx 公布處置有價證券", "https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"),
    ("twse_exdiv",  "twse", "TWSE 除權除息預告表",   "https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL"),
    ("tpex_exdiv",  "tpex", "TPEx 除權除息預告表",   "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_exright_preannounce"),
)


def _extract_code(row: dict) -> str:
    for k in ("Code", "SecuritiesCompanyCode", "股票代號", "證券代號", "CompanyCode"):
        if row.get(k):
            return str(row[k]).strip()
    return ""


def _scan_exchange_events(as_of: str, universe, book, counter: Counter) -> dict:
    """Zero-LLM batch check for 處置股 (disposition) and 除權息 (ex-dividend) across the 150 universe."""
    punished, exdiv = [], []
    now = iso(now_tw())
    with ThreadPoolExecutor(4) as ex:
        futs = [(tag, auth, label, url, ex.submit(http_get_json, url, None, None, 20, 1, counter))
                for tag, auth, label, url in EVENT_URLS]
        for tag, auth, label, url, fut in futs:
            try:
                rows = fut.result() or []
            except Exception:
                continue
            matched = [c for r in rows if (c := _extract_code(r)) in universe.names]
            matched = sorted(set(matched))
            if not matched:
                continue
            sid = book.add_source({
                "authority": auth, "name": label, "url": url,
                "content_as_of": f"{as_of}T18:30:00+08:00", "fetched_at": now,
            })
            if "punish" in tag:
                punished.extend(matched)
                vals = {f"{c}_disposition_flag": 1.0 for c in matched}
                oid = book.add_obs(
                    f"{label}名單包含標的池個股：{', '.join(matched)}（流動性受限）",
                    vals, [sid], "risk_disposition", level="stock", tickers=matched
                )
            else:
                exdiv.extend(matched)
                vals = {f"{c}_exdiv_flag": 1.0 for c in matched[:15]}
                oid = book.add_obs(
                    f"{label}近期即將除權息標的：{', '.join(matched[:15])}",
                    vals, [sid], "risk_ex_dividend", level="stock", tickers=matched[:15]
                )
    return {"disposition_tickers": sorted(set(punished)), "ex_dividend_tickers": sorted(set(exdiv))}


def review(trade_date, as_of, state, universe, book, factor_df, regime):
    counter = Counter()
    # 1. Deterministic exchange event scan (處置股 / 除權息) -> automatically added to EvidenceBook
    event_flags = _scan_exchange_events(as_of, universe, book, counter)

    # 2. Compute current theme concentration for the risk agent
    nav = state["nav"]
    f = factor_df.set_index("ticker") if factor_df is not None and not factor_df.empty else None
    held, theme_weights = [], {}
    for t, sh in state["holdings"].items():
        w = round(sh * state["last_close"].get(t, 0) / nav, 4)
        th = universe.theme_of(t)
        theme_weights[th] = round(theme_weights.get(th, 0.0) + w, 4)
        row = f.loc[t].to_dict() if f is not None and t in f.index else {}
        held.append({"t": t, "name": universe.names.get(t), "w": w, "theme": th,
                     **{k: row.get(k) for k in ("mom20", "mom60", "vol20", "adv20")}})

    top = ([] if f is None else
           f[f["core"] == True].sort_values("score", ascending=False).head(15)  # noqa: E712
           [["theme", "score", "mom20", "mom60", "vol20", "adv20"]].round(2).reset_index().to_dict("records"))

    tools = web_tools.build_tools(web_tools.book_emit(book), trade_date=trade_date, as_of=as_of,
                                  universe=universe, counter=counter, obs_cap=3,
                                  include=("search", "fetch", "price"))
    user = json.dumps({
        "trade_date": trade_date,
        "regime_hint": regime,
        "exchange_event_flags": event_flags,
        "current_theme_weights": theme_weights,
        "current_holdings": held,
        "top_core_candidates": top,
        "existing_obs_ids": [o["obs_id"] for o in book.observations[-10:]],
    }, ensure_ascii=False)

    system = (PROMPT_DIR / "risk_agent.md").read_text(encoding="utf-8")
    text, usage, _, models = run_agent_loop(system, user, tools, max_steps=6, max_calls=8)

    parsed_risk_data = {}
    try:
        clean_text = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
        parsed_risk_data = json.loads(clean_text)
        notes = parsed_risk_data.get("portfolio_risk_summary", text[:1800])
    except json.JSONDecodeError:
        parsed_risk_data = {
            "portfolio_risk_summary": text[:1800],
            "overrides": [],
        }
        notes = text[:1800]

    # Auto-append hard VETO overrides for any TWSE/TPEx disposition (處置) stock
    existing_ov = {o.get("ticker") for o in parsed_risk_data.get("overrides", [])}
    for dt in event_flags["disposition_tickers"]:
        if dt not in existing_ov:
            parsed_risk_data.setdefault("overrides", []).append({
                "ticker": dt, "action": "VETO", "reason": "TWSE/TPEx 處置股，流動性受限予以排除"
            })

    return {
        "structured_risk": parsed_risk_data,
        "event_flags": event_flags,
        "notes": notes,
        "usage": usage,
        "tool_calls": counter.n,
        "models": models,
    }