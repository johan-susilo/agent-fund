"""Deterministic 5-component scoring engine (no LLM, no network).

    score = 0.35 momentum/trend + 0.25 risk/liquidity + 0.20 earnings/catalysts
          + 0.10 macro/regime   + 0.10 valuation guardrail           (weights: config/scoring.json)

Design rules
  * One normalisation everywhere: cross-sectional rank mapped to [0, 1] (robust to outliers, ties averaged).
  * Missing is NaN, never 0 and never silently 0.5.  A metric that fewer than 30% of the universe has today
    is dropped for EVERYONE (so one absent feed does not compress a component for all stocks equally);
    a stock missing part of a component is shrunk toward neutral in proportion to the lost weight, and a
    component with < 50% coverage is neutral and flagged.
  * Stale / future data is treated as missing: price bars must end on `as_of`, macro observations must be
    dated at or before the submission cut-off and younger than `macro_max_age_hours`, fundamentals that carry
    an `as_of` field must not be newer than `as_of`.  Nothing here reads a clock; all dates are inputs.
  * Hard risk limits are NOT in this file's score.  See tools/risk_gate.py (applied independently of score).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

TW = timezone(timedelta(hours=8))
_CFG_PATH = Path(__file__).resolve().parents[2] / "config" / "scoring.json"

PRICE_KEYS = ("mom20", "mom60", "vol20", "adv20", "ma20_gap", "ma60_gap", "ma20_vs_ma60",
              "ud_vol20", "mdd60", "dd_dev20", "hl20")
CORE_PRICE_KEYS = ("mom20", "mom60", "vol20", "adv20")


# ------------------------------------------------------------------ config
@lru_cache(maxsize=4)
def _load(path: str) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    w = cfg["component_weights"]
    if abs(sum(w.values()) - 1.0) > 1e-9:
        raise ValueError(f"component_weights must sum to 1.0, got {sum(w.values())}")
    return cfg


def load_config(path=None) -> dict:
    return _load(str(path or _CFG_PATH))


# ------------------------------------------------------------------ helpers
def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return np.nan
    return x if math.isfinite(x) else np.nan


def rank01(s: pd.Series, higher_better: bool = True) -> pd.Series:
    """Cross-sectional rank in [0, 1]; NaN stays NaN; fewer than 3 valid points -> 0.5."""
    s = pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan)
    out = pd.Series(np.nan, index=s.index, dtype=float)
    n = int(s.notna().sum())
    if n == 0:
        return out
    if n < 3:
        out[s.notna()] = 0.5
        return out
    r = s.rank(method="average", ascending=higher_better)
    return (r - 1.0) / (n - 1.0)


def _lin(x, lo, hi):
    """Linear map of x from [lo, hi] to [0, 1], clipped; works for hi < lo (inverted)."""
    x = _num(x)
    if math.isnan(x) or lo == hi:
        return None
    return float(min(1.0, max(0.0, (x - lo) / (hi - lo))))


def _parse_ts(v):
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=TW)


def theme_median(df: pd.DataFrame, col: str, min_peers: int) -> pd.Series:
    """Median of `col` within the stock's theme; falls back to the universe median for thin themes."""
    g = df.groupby("theme")[col]
    med = g.transform("median")
    cnt = g.transform("count")
    return med.where(cnt >= min_peers, df[col].median())


def _blend(parts: dict, weights: dict, min_universe_cov: float, min_comp_cov: float, neutral: float = 0.5):
    """parts: {metric: Series of [0,1] scores or NaN}.  Returns (component Series, coverage Series, dropped metrics)."""
    idx = next(iter(parts.values())).index
    n = max(len(idx), 1)
    active = {m: s for m, s in parts.items()
              if weights.get(m, 0) > 0 and s.notna().sum() / n >= min_universe_cov}
    dropped = [m for m in parts if weights.get(m, 0) > 0 and m not in active]
    if not active:
        return pd.Series(neutral, index=idx), pd.Series(0.0, index=idx), dropped
    wsum = sum(weights[m] for m in active)
    num = pd.Series(0.0, index=idx)
    have = pd.Series(0.0, index=idx)
    for m, s in active.items():
        w = weights[m] / wsum
        ok = s.notna()
        num += s.fillna(0.0) * w
        have += ok.astype(float) * w
    raw = (num / have.where(have > 0)).clip(0, 1)
    comp = neutral + (raw - neutral) * have                      # shrink toward neutral by lost weight
    comp = comp.where(have >= min_comp_cov, neutral).fillna(neutral)
    return comp, have, dropped


# ------------------------------------------------------------------ macro
def _obs_values(macro_frag, trade_date, max_age_h, warnings):
    """Merge macro observation values, dropping anything dated after the submission cut-off or too old.
    Undated observations are kept (cannot be judged) but counted."""
    if not macro_frag:
        return {}
    cutoff = _parse_ts(f"{trade_date}T08:55:00+08:00") if trade_date else None
    sources = {}
    for s in macro_frag.get("sources", []) or []:
        for k in ("src_id", "id", "source_id"):
            if s.get(k) is not None:
                sources[str(s[k])] = s
    vals, future, stale, undated = {}, 0, 0, 0
    for o in macro_frag.get("observations", []) or []:
        ts = _parse_ts(o.get("content_as_of"))
        if ts is None:
            for sid in (o.get("src") or o.get("source_ids") or o.get("sources") or []):
                src = sources.get(str(sid))
                ts = _parse_ts(src.get("content_as_of")) if src else None
                if ts:
                    break
        if ts is None:
            undated += 1
        elif cutoff and ts > cutoff:
            future += 1
            continue
        elif cutoff and (cutoff - ts) > timedelta(hours=max_age_h):
            stale += 1
            continue
        vals.update(o.get("values", {}) or {})
    if future:
        warnings.append(f"macro: {future} observation(s) dated after the submission cut-off were ignored (look-ahead guard)")
    if stale:
        warnings.append(f"macro: {stale} observation(s) older than {max_age_h}h were ignored (stale)")
    if undated:
        warnings.append(f"macro: {undated} observation(s) carry no resolvable timestamp (kept)")
    return vals


def compute_macro(market: dict, macro_frag, df: pd.DataFrame, regime, trade_date, cfg: dict, warnings: list) -> dict:
    """Market-level macro/regime score M in [0,1] (0.5 = neutral) from independent signals."""
    mc = cfg["macro"]
    v = _obs_values(macro_frag, trade_date, cfg["normalization"]["macro_max_age_hours"], warnings)
    sig = {}
    vix = v.get("vix_yf", v.get("vix"))
    sig["vix_level"] = _lin(vix, 30, 14)
    sig["vix_trend"] = _lin(v.get("vix_yf_vs_ma20_pct", v.get("vix_chg_pct")), 20, -20)
    t, c = _lin(v.get("sox_vs_ma20_pct"), -10, 10), _lin(v.get("sox_chg_pct"), -3, 3)
    sig["sox_trend"] = (0.7 * t + 0.3 * c) if (t is not None and c is not None) else (t if t is not None else c)
    sig["tsm_adr"] = _lin(v.get("tsm_adr_chg_pct"), -3, 3)
    sig["us10y_move"] = _lin(v.get("us10y_chg_bp"), 15, -15)
    fx = _num(v.get("usdtwd_spot_chg_pct", v.get("usdtwd_chg_pct")))
    sig["twd_move"] = None if math.isnan(fx) else float(1.0 - min(abs(fx) / 1.5, 1.0))
    chgs = [p.get("change_pct") for p in (market or {}).get("prices", {}).values() if p.get("change_pct") is not None]
    sig["breadth"] = (sum(x > 0 for x in chgs) / len(chgs)) if len(chgs) >= 50 else None
    sig["taiex_trend"] = _lin(v.get("taiex_vs_ma20_pct"), -6, 6)
    inst = df["inst_lots"].dropna() if "inst_lots" in df else pd.Series(dtype=float)
    sig["fund_flow"] = float((inst > 0).mean()) if len(inst) >= 50 else None

    w = mc["signal_weights"]
    avail = {k: s for k, s in sig.items() if s is not None}
    cov = sum(w[k] for k in avail) / sum(w.values())
    missing = sorted(k for k, s in sig.items() if s is None)
    if cov < mc["min_signal_weight_coverage"]:
        m_raw, status = 0.5, "insufficient_signals_neutral"
        warnings.append(f"macro: only {cov:.0%} of signal weight available -> macro tilt disabled (neutral)")
    else:
        m_raw = sum(w[k] * s for k, s in avail.items()) / sum(w[k] for k in avail)
        status = "ok"
    lo, hi = mc["regime_clamp"].get(regime or "neutral", [0.0, 1.0])
    m = min(max(m_raw, lo), hi)
    return {"score": round(float(m), 4), "raw": round(float(m_raw), 4), "coverage": round(cov, 3), "status": status,
            "regime": regime, "clamped": bool(abs(m - m_raw) > 1e-12),
            "signals": {k: (None if s is None else round(s, 3)) for k, s in sig.items()}, "missing": missing}


# ------------------------------------------------------------------ table
def _assemble(universe, factor_frag, as_of, cfg, warnings) -> pd.DataFrame:
    rows = {t: {"ticker": t, "theme": universe.theme_of(t), "core": bool(universe.is_core(t)), "_flags": []}
            for t in universe.names}
    fr = factor_frag or {}
    max_age = int(cfg["normalization"]["price_max_age_days"])
    asof_dt = _parse_ts(f"{as_of}T13:30:00+08:00") if as_of else None

    # --- price features (stash first, then legacy observation values for the four base metrics)
    stash = fr.get("price_factors") or {}
    for t, f in stash.items():
        if t not in rows:
            continue
        last = f.get("last_date")
        if as_of and last:
            if str(last) > str(as_of):
                rows[t]["_flags"].append("future_price_dropped")
                continue
            if asof_dt and (asof_dt - _parse_ts(f"{last}T13:30:00+08:00")).days > max_age:
                rows[t]["_flags"].append("stale_price")
                continue
        for k in PRICE_KEYS:
            if f.get(k) is not None:
                rows[t][k] = f[k]
    for obs in fr.get("observations", []) or []:
        for k, val in (obs.get("values") or {}).items():
            tk, _, metric = k.partition("_")
            if tk not in rows:
                continue
            key = {"mom20_pct": "mom20", "mom60_pct": "mom60", "vol20_pct": "vol20", "adv20_twd_m": "adv20",
                   "roe_pct": "roe", "pe": "pe", "rev_growth_pct": "rev_g", "net_margin_pct": "margin",
                   "inst_lots": "inst_lots"}.get(metric)
            if key and key not in rows[tk]:                      # stash (full-universe) wins over observation text
                rows[tk][key] = val

    # --- fundamentals stash (fractions -> percent), with look-ahead / staleness guard
    for t, i in (fr.get("fundamentals") or {}).items():
        if t not in rows:
            continue
        fa = i.get("as_of") or i.get("published")
        if as_of and fa and str(fa)[:10] > str(as_of):
            rows[t]["_flags"].append("future_fundamental_dropped")
            continue
        r = rows[t]
        for src, dst, mult in (("roe", "roe", 1), ("pe", "pe", 1), ("rev_g", "rev_g", 100), ("margin", "margin", 100),
                               ("rev_mom", "rev_mom", 100), ("inst_lots", "inst_lots", 1),
                               ("margin_inc", "margin_inc", 1), ("eps_rev", "earn_rev", 1)):
            if i.get(src) is not None:
                r[dst] = _num(i[src]) * mult
    df = pd.DataFrame(list(rows.values()))
    cols = list(PRICE_KEYS) + ["roe", "pe", "rev_g", "rev_mom", "margin", "inst_lots", "margin_inc", "earn_rev"]
    for c in cols:
        if c not in df:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c, (lo, hi) in cfg["validity"].items():                  # out-of-range = bad data = missing
        if c in df:
            df[c] = df[c].where((df[c] >= lo) & (df[c] <= hi))
    df["pe"] = df["pe"].where(df["pe"] > 0)
    return df


def build_score_table(universe, market: dict, factor_frag: dict | None, macro_frag: dict | None = None, *,
                      regime: str | None = None, as_of: str | None = None, trade_date: str | None = None,
                      cfg: dict | None = None) -> pd.DataFrame:
    cfg = cfg or load_config()
    nz, W = cfg["normalization"], cfg["component_weights"]
    warnings: list = []
    df = _assemble(universe, factor_frag, as_of, cfg, warnings)
    peers, neutral = nz["min_theme_peers"], nz["neutral"]
    ucov, ccov = nz["min_metric_universe_coverage"], nz["min_component_coverage"]

    # ---------------- momentum & trend (35%)
    mc = cfg["momentum"]
    trend_raw = 0.3 * df["ma20_gap"] + 0.3 * df["ma60_gap"] + 0.4 * df["ma20_vs_ma60"]
    rel = 0.4 * (df["mom20"] - theme_median(df, "mom20", peers)) + 0.6 * (df["mom60"] - theme_median(df, "mom60", peers))
    s_mom, cov_mom, drop_mom = _blend({
        "mom20": rank01(df["mom20"]), "mom60": rank01(df["mom60"]), "ma_trend": rank01(trend_raw),
        "rel_strength": rank01(rel), "volume_confirm": rank01(df["ud_vol20"]), "inst_flow": rank01(df["inst_lots"]),
    }, mc, ucov, ccov, neutral)

    # ---------------- risk & liquidity (25%)
    rc = cfg["risk"]
    s_risk, cov_risk, drop_risk = _blend({
        "vol20": rank01(df["vol20"], False), "dd_dev20": rank01(df["dd_dev20"], False),
        "mdd60": rank01(df["mdd60"], False), "adv20": rank01(df["adv20"], True),
        "spread_proxy": rank01(df["hl20"], False),
    }, rc, ucov, ccov, neutral)
    trap = ((df["margin_inc"].fillna(0) > 0) & (df["mom20"].fillna(0) < 0)).astype(float)
    s_risk = (s_risk - rc["margin_trap_penalty"] * trap).clip(lower=0.01)

    # ---------------- earnings & catalysts (20%)
    ec = cfg["earnings"]
    chain = df["rev_g"] - theme_median(df, "rev_g", peers)       # supply-chain check: beating its own theme peers
    s_earn, cov_earn, drop_earn = _blend({
        "rev_yoy": rank01(df["rev_g"]), "rev_mom": rank01(df["rev_mom"]), "roe": rank01(df["roe"]),
        "margin": rank01(df["margin"]), "earnings_revision": rank01(df["earn_rev"]), "supply_chain": rank01(chain),
    }, ec, ucov, ccov, neutral)

    # ---------------- valuation guardrail (10%)
    vc = cfg["valuation"]
    rel_pe = df["pe"] / theme_median(df, "pe", peers)
    s_val, cov_val, drop_val = _blend({
        "pe_percentile": rank01(df["pe"], False), "rel_pe": rank01(rel_pe, False),
        "downside_potential": rank01(df["ma60_gap"], False),     # stretched above MA60 = more room to fall
    }, vc, ucov, ccov, neutral)

    # ---------------- macro & regime (10%): market score x stock sensitivity (beta proxy)
    macro = compute_macro(market, macro_frag, df, regime, trade_date, cfg, warnings)
    mcfg = cfg["macro"]
    vr = rank01(df["vol20"]).fillna(0.5)
    beta = mcfg["beta_core_share"] * df["core"].astype(float) + (1 - mcfg["beta_core_share"]) * vr
    s_macro = (neutral + mcfg.get("gain", 1.5) * (macro["score"] - neutral) * (2 * beta - 1)).clip(0, 1)

    comps = {"momentum": s_mom, "risk": s_risk, "earnings": s_earn, "macro": s_macro, "valuation": s_val}
    total = sum(W[k] * comps[k] for k in W)

    # ---------------- data-safety multipliers (not hard limits)
    no_price = df[list(CORE_PRICE_KEYS)].isna().all(axis=1)
    total = total.where(~no_price, total * nz["missing_price_penalty"])
    stretched = (rel_pe > vc["guard_rel_pe"]) & (df["ma60_gap"] > vc["guard_extension_pct"])
    total = total.where(~stretched, total * vc["guard_multiplier"])

    flags = []
    covs = {"momentum": cov_mom, "risk": cov_risk, "earnings": cov_earn, "valuation": cov_val}
    for i in df.index:
        f = list(df.at[i, "_flags"])
        if no_price[i]:
            f.append("no_price_data")
        for k, cv in covs.items():
            if cv[i] < ccov:
                f.append(f"low_{k}_coverage")
        if stretched[i]:
            f.append("valuation_stretched")
        flags.append(",".join(f))

    out = df.drop(columns=["_flags"]).copy()
    out["s_momentum"], out["s_risk"], out["s_earnings"] = s_mom.round(4), s_risk.round(4), s_earn.round(4)
    out["s_macro"], out["s_valuation"] = s_macro.round(4), s_val.round(4)
    out["coverage"] = sum(W[k] * cv for k, cv in covs.items()).add(W["macro"] * (1.0 if macro["status"] == "ok" else 0.0)).round(3)
    out["score"] = total.round(4)
    out["data_flags"] = flags
    # legacy aliases (older code paths read these names)
    out["fund"], out["mom"], out["risk"], out["val"] = out["s_earnings"], out["s_momentum"], out["s_risk"], out["s_valuation"]
    out = out.sort_values(["score", "ticker"], ascending=[False, True], kind="mergesort").reset_index(drop=True)
    dropped = {"momentum": drop_mom, "risk": drop_risk, "earnings": drop_earn, "valuation": drop_val}
    out.attrs["macro"] = macro
    out.attrs["dropped_metrics"] = {k: v for k, v in dropped.items() if v}
    out.attrs["warnings"] = warnings + [f"{k}: metric(s) unavailable today and ignored for all stocks: {', '.join(v)}"
                                        for k, v in dropped.items() if v]
    out.attrs["weights"] = dict(W)
    return out
