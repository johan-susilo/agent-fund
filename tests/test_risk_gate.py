import pandas as pd

from helpers import FakeUniverse, make_frag
from tools import risk_gate, scoring


class Lim:
    cap_other = 0.08

    def cap(self, t):
        return 0.20 if t == "2330" else 0.08


U = FakeUniverse()


def table(mutate):
    f = make_frag(U)
    mutate(f)
    return scoring.build_score_table(U, {"prices": {}}, f, None, as_of="2026-10-23", trade_date="2026-10-26")


def test_high_score_cannot_buy_past_risk_limit():
    def m(f):
        t = f["price_factors"]["2001"]
        t.update({"mom20": 80, "mom60": 140, "ma20_gap": 20, "ma60_gap": 40, "vol20": 95, "mdd60": 38})
        f["fundamentals"]["2001"].update({"rev_g": 1.5, "roe": 70})
    df = table(m)
    row = df[df.ticker == "2001"].iloc[0]
    assert row["s_momentum"] > 0.7                                   # score likes it ...
    g = risk_gate.compute_gate(df, Lim(), "neutral")
    acts = {o["ticker"]: o["action"] for o in g["overrides"]}
    assert acts["2001"] == "REDUCE"                                  # ... the gate does not care
    assert g["caps"]["2001"] == 0.04                                 # 8% * 45/95 * 0.75 -> floored at 4%


def test_veto_on_illiquid_and_2330_exempt():
    def m(f):
        f["price_factors"]["2002"]["adv20"] = 10
        f["price_factors"]["2330"].update({"vol20": 90, "adv20": 10, "mdd60": 50})
    g = risk_gate.compute_gate(table(m), Lim(), "neutral")
    acts = {o["ticker"]: o["action"] for o in g["overrides"]}
    assert acts["2002"] == "VETO"
    assert "2330" not in acts and "2330" not in g["caps"]


def test_missing_price_data_reduces_not_ignored():
    def m(f):
        del f["price_factors"]["2003"]
    g = risk_gate.compute_gate(table(m), Lim(), "neutral")
    assert any(o["ticker"] == "2003" and o["action"] == "REDUCE" for o in g["overrides"])


def test_volatility_cap_scales_and_respects_floor():
    def m(f):
        f["price_factors"]["2004"]["vol20"] = 60      # 8% * 45/60 = 6%
    g = risk_gate.compute_gate(table(m), Lim(), "neutral")
    assert abs(g["caps"]["2004"] - 0.06) < 1e-9
    assert all(c >= 0.04 for c in g["caps"].values())


def test_merge_never_downgrades_and_adds_quant_gate():
    ctx = {"structured_risk": {"overrides": [{"ticker": "2001", "action": "VETO", "reason": "x"}]},
           "event_flags": {"disposition_tickers": ["2005"]}, "usage": {"input_tokens": 1, "output_tokens": 1}}
    gate = {"overrides": [{"ticker": "2001", "action": "REDUCE", "reason": "y", "source": "quant_gate"}],
            "caps": {"2006": 0.05}, "vol": {"2006": 80.0}, "portfolio": {"max_weighted_vol20": 58, "unknown_vol": 60, "max_top5": 0.62}}
    out = risk_gate.merge_into_ctx(ctx, gate)
    assert [o["action"] for o in out["structured_risk"]["overrides"]] == ["VETO"]
    assert out["quant_gate"]["caps"] == {"2006": 0.05} and out["event_flags"]["disposition_tickers"] == ["2005"]


def test_ensure_ctx_works_without_llm_agent():
    df = table(lambda f: None)
    ctx = risk_gate.ensure_risk_ctx(None, df, Lim(), "neutral")          # AGENT_AUTONOMY=0 / agent crashed
    assert ctx["usage"] == {"input_tokens": 0, "output_tokens": 0} and "quant_gate" in ctx


def test_portfolio_limits():
    port = {"max_weighted_vol20": 50.0, "unknown_vol": 60.0, "max_top5": 0.5}
    assert risk_gate.check_portfolio_risk({"a": 0.1, "b": 0.1}, port, {"a": 40, "b": 45}) == []
    assert "weighted 20d volatility" in risk_gate.check_portfolio_risk({"a": 0.1, "b": 0.1}, port, {"a": 70, "b": 70})[0]
    assert any("top-5" in e for e in risk_gate.check_portfolio_risk({str(i): 0.12 for i in range(6)}, port, {str(i): 30 for i in range(6)}))
    assert risk_gate.check_portfolio_risk({"a": 0.1}, None) == []
    # unknown volatility is assumed high, not zero
    assert risk_gate.check_portfolio_risk({"a": 0.1, "b": 0.1}, {**port, "max_weighted_vol20": 55.0}, {})


def test_regime_tightens_portfolio_limit():
    f = table(lambda f: None)
    on, off = (risk_gate.compute_gate(f, Lim(), r)["portfolio"]["max_weighted_vol20"] for r in ("risk_on", "risk_off"))
    assert off < on
