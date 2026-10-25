import math

import numpy as np
import pandas as pd
import pytest

from helpers import BEAR, BULL, FakeUniverse, macro_frag, make_frag
from tools import scoring
from tools.factors import price_factors_from_ohlcv

AS_OF, TRADE = "2026-10-23", "2026-10-26"
U = FakeUniverse()


def build(frag=None, macro=None, regime=None, **kw):
    return scoring.build_score_table(U, {"prices": {}}, frag or make_frag(U), macro, regime=regime,
                                     as_of=AS_OF, trade_date=TRADE, **kw)


def test_weights_are_the_required_split():
    w = scoring.load_config()["component_weights"]
    assert w == {"momentum": 0.35, "risk": 0.25, "earnings": 0.20, "macro": 0.10, "valuation": 0.10}
    assert abs(sum(w.values()) - 1) < 1e-12


def test_bad_weights_rejected(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"component_weights": {"momentum": 0.5, "risk": 0.5, "earnings": 0.5, "macro": 0, "valuation": 0}}')
    with pytest.raises(ValueError):
        scoring.load_config(p)


def test_table_shape_range_order_determinism():
    a, b = build(), build()
    assert len(a) == 150 and a["ticker"].is_unique
    assert a["score"].between(0, 1).all()
    assert a["score"].is_monotonic_decreasing
    pd.testing.assert_frame_equal(a, b)
    for c in ("s_momentum", "s_risk", "s_earnings", "s_macro", "s_valuation", "coverage", "data_flags",
              "mom20", "mom60", "vol20", "adv20", "roe", "pe", "rev_g", "core", "theme"):
        assert c in a.columns


def test_score_is_weighted_sum_of_components():
    df = build()
    w = scoring.load_config()["component_weights"]
    calc = sum(w[k] * df[f"s_{n}"] for k, n in (("momentum", "momentum"), ("risk", "risk"), ("earnings", "earnings"),
                                                  ("macro", "macro"), ("valuation", "valuation")))
    clean = ~df["data_flags"].str.contains("valuation_stretched|no_price")
    assert (calc[clean] - df["score"][clean]).abs().max() < 2e-3


def test_rank01_properties():
    s = pd.Series([1.0, 2.0, np.nan, 3.0, 3.0])
    r = scoring.rank01(s)
    assert r.isna().tolist() == [False, False, True, False, False]
    assert r.min() == 0 and r.max() <= 1 and r.iloc[3] == r.iloc[4] > r.iloc[1]   # ties share the average rank
    assert scoring.rank01(s, False).iloc[0] == 1
    assert scoring.rank01(pd.Series([5.0, np.nan, np.nan])).dropna().iloc[0] == 0.5   # <3 points -> neutral


def test_missing_price_data_is_penalised_and_flagged():
    f = make_frag(U)
    victim = "2454" if "2454" in U.names else "2001"
    del f["price_factors"][victim]
    df = build(f).set_index("ticker")
    assert "no_price_data" in df.loc[victim, "data_flags"]
    assert df.loc[victim, "s_momentum"] == 0.5 and df.loc[victim, "s_risk"] == 0.5
    assert list(df.index).index(victim) > 10          # never surfaces as an "average" top name


def test_metric_missing_for_everyone_is_dropped_not_penalising():
    f = make_frag(U)
    for v in f["fundamentals"].values():
        v.pop("rev_mom")
    df = build(f)
    assert "rev_mom" in df.attrs["dropped_metrics"]["earnings"]
    assert df["s_earnings"].between(0, 1).all() and df["s_earnings"].std() > 0.05   # dispersion intact


def test_partial_missing_shrinks_toward_neutral():
    f = make_frag(U)
    t = "2001"
    base = build(f).set_index("ticker").loc[t, "s_earnings"]
    for k in ("roe", "margin", "rev_mom"):
        f["fundamentals"][t].pop(k)
    after = build(f).set_index("ticker").loc[t, "s_earnings"]
    assert abs(after - 0.5) <= abs(base - 0.5) + 0.25   # still bounded; more neutral or same direction
    assert 0 <= after <= 1


def test_outliers_become_missing():
    f = make_frag(U)
    f["fundamentals"]["2001"]["pe"] = 900
    f["fundamentals"]["2002"]["roe"] = 500
    df = build(f).set_index("ticker")
    assert math.isnan(df.loc["2001", "pe"]) and math.isnan(df.loc["2002", "roe"])


# ------------------------------------------------------------------ look-ahead / staleness
def _ohlcv(n=90, last="2026-10-23", drift=0.002, seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=last, periods=n)
    c = 100 * np.cumprod(1 + rng.normal(drift, 0.015, n))
    return pd.DataFrame({"Close": c, "High": c * 1.01, "Low": c * 0.99, "Volume": rng.uniform(1e6, 2e6, n)}, index=idx)


def test_price_factors_ignore_future_bars():
    full = _ohlcv(95, last="2026-10-30")
    asof_slice = full[full.index <= pd.Timestamp(AS_OF)]
    a = price_factors_from_ohlcv(full, as_of=AS_OF)
    b = price_factors_from_ohlcv(asof_slice, as_of=AS_OF)
    assert a is not None and a == b
    assert a["last_date"] == AS_OF


def test_price_factors_stale_or_short_returns_none():
    assert price_factors_from_ohlcv(_ohlcv(last="2026-10-21"), as_of=AS_OF) is None
    assert price_factors_from_ohlcv(_ohlcv(n=40), as_of=AS_OF) is None


def test_price_factors_extended_metrics_sane():
    f = price_factors_from_ohlcv(_ohlcv(), as_of=AS_OF)
    for k in ("mom20", "mom60", "vol20", "adv20", "ma20_gap", "ma60_gap", "ma20_vs_ma60", "ud_vol20", "mdd60", "dd_dev20", "hl20"):
        assert f[k] is not None and math.isfinite(f[k])
    assert 0 <= f["mdd60"] <= 100 and f["hl20"] > 0


def test_stale_and_future_stash_entries_dropped():
    f = make_frag(U)
    f["price_factors"]["2001"]["last_date"] = "2026-10-16"          # a week old
    f["price_factors"]["2002"]["last_date"] = "2026-10-27"          # from the future
    f["fundamentals"]["2003"]["as_of"] = "2026-11-10"
    df = build(f).set_index("ticker")
    assert "stale_price" in df.loc["2001", "data_flags"]
    assert "future_price_dropped" in df.loc["2002", "data_flags"]
    assert "future_fundamental_dropped" in df.loc["2003", "data_flags"]
    assert math.isnan(df.loc["2002", "mom20"]) and math.isnan(df.loc["2003", "roe"])


def test_future_dated_macro_is_ignored():
    clean = build(macro=macro_frag(BULL))
    poisoned = build(macro=macro_frag({**BULL, **{"vix_yf": 60.0}}, ts="2026-10-26T10:00:00+08:00"))   # after 08:55 cut-off
    assert clean.attrs["macro"]["score"] > 0.6
    assert poisoned.attrs["macro"]["status"] == "insufficient_signals_neutral" and poisoned.attrs["macro"]["score"] == 0.5
    assert any("look-ahead" in w for w in poisoned.attrs["warnings"])


def test_stale_macro_is_ignored():
    df = build(macro=macro_frag(BULL, ts="2026-10-15T05:00:00+08:00"))
    assert df.attrs["macro"]["score"] == 0.5 and any("stale" in w for w in df.attrs["warnings"])


# ------------------------------------------------------------------ macro behaviour
def test_macro_direction_and_beta_sensitivity():
    bull, bear = build(macro=macro_frag(BULL)), build(macro=macro_frag(BEAR))
    assert bull.attrs["macro"]["score"] > 0.65 > 0.35 > bear.attrs["macro"]["score"]
    hi_beta = bull.sort_values("vol20", ascending=False).head(10)["s_macro"].mean()
    lo_beta = bull.sort_values("vol20").head(10)["s_macro"].mean()
    assert hi_beta > lo_beta
    hi_b, lo_b = bear.sort_values("vol20", ascending=False).head(10)["s_macro"].mean(), bear.sort_values("vol20").head(10)["s_macro"].mean()
    assert lo_b > hi_b                                              # risk-off favours defensives


def test_regime_clamp_and_neutral_without_macro():
    assert build(macro=macro_frag(BULL), regime="risk_off").attrs["macro"]["score"] <= 0.35
    none = build()
    assert none.attrs["macro"]["score"] == 0.5 and (none["s_macro"] == 0.5).all()


def test_valuation_guardrail_discounts_stretched_names():
    f = make_frag(U)
    t = "2001"
    f["fundamentals"][t]["pe"] = 110
    f["price_factors"][t]["ma60_gap"] = 60
    df = build(f).set_index("ticker")
    assert "valuation_stretched" in df.loc[t, "data_flags"]
