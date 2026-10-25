"""Evidence research agent: ONE tool-calling LLM loop that replaces the former macro-research and
fundamental-research loops (they shared tools, had overlapping gap-filling jobs and each paid its own prompt).

It runs after the deterministic data and the factor table exist, so it can aim its few tool calls at what the
scores cannot see: current holdings and top candidates with data gaps, event risk, estimate revisions /
guidance / monthly-revenue surprises, and macro items the baseline feeds missed.  Facts only enter the
EvidenceBook through the fact tools (obs ids are citable); the agent's text is a short, non-citable note.

Set EVIDENCE_MODE=legacy to restore the two old loops.  AGENT_AUTONOMY=0 disables this agent.
"""
from __future__ import annotations

import json
import os

from agent_loop import run_agent_loop
from settings import PROMPT_DIR
from tools import web_tools
from util import Counter

MAX_STEPS = int(os.environ.get("EVIDENCE_MAX_STEPS", 10))
MAX_CALLS = int(os.environ.get("EVIDENCE_MAX_CALLS", 12))
OBS_CAP = int(os.environ.get("EVIDENCE_OBS_CAP", 6))


def enabled() -> bool:
    return os.environ.get("AGENT_AUTONOMY", "1") != "0" and os.environ.get("EVIDENCE_MODE", "merged") != "legacy"


def _gaps(factor_df, holdings, n_top: int = 12) -> list:
    """Holdings and top-ranked names whose score rests on incomplete or flagged data."""
    if factor_df is None or len(factor_df) == 0:
        return []
    want = list(dict.fromkeys(list(holdings) + factor_df["ticker"].head(n_top).tolist()))
    f = factor_df.set_index("ticker")
    out = []
    for t in want:
        if t in f.index:
            r = f.loc[t]
            if r.get("data_flags") or (r.get("coverage") is not None and r["coverage"] < 0.85):
                out.append({"t": t, "flags": r.get("data_flags") or "", "coverage": float(r["coverage"])})
    return out[:15]


def research(trade_date, as_of, state, universe, book, factor_df, regime) -> dict:
    counter = Counter()
    tools = web_tools.build_tools(web_tools.book_emit(book), trade_date=trade_date, as_of=as_of,
                                  universe=universe, counter=counter, obs_cap=OBS_CAP)
    macro = (getattr(factor_df, "attrs", {}) or {}).get("macro", {})
    holdings = sorted(state["holdings"])
    top = [] if factor_df is None else factor_df.head(10)[["ticker", "score", "s_momentum", "s_risk", "s_earnings"]] \
        .round(3).to_dict("records")
    user = json.dumps({
        "trade_date": trade_date, "as_of_date": as_of, "regime_hint": regime,
        "macro_score": {k: macro.get(k) for k in ("score", "coverage", "signals", "missing")},
        "current_holdings": holdings,
        "top_ranked": top,
        "data_gaps": _gaps(factor_df, holdings),
        "core_tickers": [t for th in universe.core_ids for t in universe.themes[th]["tickers"]],
        "already_collected": [o["statement"][:80] for o in book.observations[-25:]],
    }, ensure_ascii=False)
    system = (PROMPT_DIR / "evidence_agent.md").read_text(encoding="utf-8")
    text, usage, _, models = run_agent_loop(system, user, tools, max_steps=MAX_STEPS, max_calls=MAX_CALLS)
    return {"notes": text[:1500], "usage": usage, "tool_calls": counter.n, "models": models}
