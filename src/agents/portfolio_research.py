"""Research phase for the portfolio agent: the LLM may call tools and dry-run its draft plan
(validate_plan) before it produces the final JSON. Tool facts become citable observations."""
from __future__ import annotations

from agent_loop import Tool, run_agent_loop
from derive_orders import derive_orders, project_portfolio
from tools import web_tools
from util import Counter

ADDENDUM = """

RESEARCH MODE (this turn only): do NOT output the final JSON. You may call tools to verify evidence,
check events (ex-dividend, halts, earnings, export controls), pull price data for any ticker, and call
validate_plan with a draft [{ticker, target_weight}] to test it against the rules (holding count, caps,
cash window, core-theme minimum, per-theme caps, turnover). Fact tools return an obs_id you may cite later.
End with a short plain-text note: draft idea, key evidence, open risks."""


def _validate_tool(state, universe, closes, limits):
    from agents import check_agent, portfolio_agent as pa

    def fn(portfolio: list):
        p = [{"ticker": str(x["ticker"]), "target_weight": float(x["target_weight"]), "inference_refs": ["I1"]}
             for x in portfolio]
        bad = [x["ticker"] for x in p if x["ticker"] not in closes or x["ticker"] not in universe.names]
        if bad:
            return {"errors": [f"not in universe / no price: {bad}"]}
        p, _ = pa.normalize_weights(p, limits)
        keep = {x["ticker"] for x in p}
        exits = [{"ticker": t, "inference_refs": ["I1"]} for t in state["holdings"] if t not in keep]
        decisions, _ = pa.derive_decisions(p, exits, state, closes, limits)
        rep = project_portfolio(derive_orders(decisions, state, closes), state, closes)
        errs = (check_agent.check_portfolio(rep, limits, state) + check_agent.check_theme(rep, universe, limits)
                + check_agent.check_theme_caps(rep, universe, limits))
        return {"errors": errs, "holdings_count": rep["holdings_count"], "cash_ratio": round(rep["cash_ratio"], 4),
                "net_flow_pct_nav": round(rep["net_flow_pct_nav"], 4),
                "core_weight": round(sum(w for t, w in rep["weights"].items() if universe.is_core(t)), 4),
                "normalized": {x["ticker"]: x["target_weight"] for x in p}}

    return Tool("validate_plan", "Dry-run a draft portfolio against the rule engine. Returns rule errors and projected stats.",
                {"portfolio": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                    "ticker": {"type": "STRING"}, "target_weight": {"type": "NUMBER"}}}}}, fn, ["portfolio"])


def run(system_prompt, user_prompt, state, universe, book, closes, limits, as_of):
    counter = Counter()
    tools = web_tools.build_tools(web_tools.book_emit(book), trade_date=state["trade_date"], as_of=as_of,
                                  universe=universe, counter=counter, obs_cap=3)
    tools.append(_validate_tool(state, universe, closes, limits))
    text, usage, _, models = run_agent_loop(system_prompt + ADDENDUM, user_prompt, tools, max_steps=8, max_calls=12)
    return text[:2000], usage, counter.n, models
