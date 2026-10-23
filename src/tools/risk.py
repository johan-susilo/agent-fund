from __future__ import annotations
from typing import Any

def validate_target_portfolio(weights: dict[str, float], universe: set[str], rules: dict,
                              cash_pct: float | None = None) -> dict[str, Any]:
    checks = []
    def add(name, ok, detail): checks.append({"check": name, "passed": bool(ok), "detail": detail})
    tickers = {str(k).zfill(4) for k in weights}
    norm = {str(k).zfill(4): float(v) for k, v in weights.items()}
    add("universe", tickers.issubset({str(x).zfill(4) for x in universe}),
        f"Unknown tickers: {sorted(tickers - {str(x).zfill(4) for x in universe})}")
    add("holding_count", rules["min_holdings"] <= len(norm) <= rules["max_holdings"],
        f"Count={len(norm)}; allowed={rules['min_holdings']}-{rules['max_holdings']}")
    add("nonnegative_weights", all(0 <= x <= 1 for x in norm.values()), "Weights must be in [0,1].")
    cap_errors = []
    for t, w in norm.items():
        cap = rules["max_weight_tsmc"] if t == "2330" else rules["max_weight_other"]
        if w > cap + 1e-9: cap_errors.append(f"{t}: {w:.2%} > {cap:.2%}")
    add("single_stock_caps", not cap_errors, "; ".join(cap_errors) or "All single-stock caps pass.")
    invested = sum(norm.values())
    cash = 1 - invested if cash_pct is None else float(cash_pct)
    add("weights_sum", abs(invested + cash - 1.0) < 1e-6,
        f"Invested={invested:.4f}, cash={cash:.4f}, total={invested+cash:.4f}")
    add("cash_range", rules["cash_min"] - 1e-9 <= cash <= rules["cash_max"] + 1e-9,
        f"Cash={cash:.2%}; allowed={rules['cash_min']:.2%}-{rules['cash_max']:.2%}")
    return {"passed": all(c["passed"] for c in checks), "checks": checks}

def risk_metrics(equity_curve, periods_per_year=252) -> dict:
    import numpy as np
    import pandas as pd
    s = pd.Series(equity_curve).dropna().astype(float)
    if len(s) < 2:
        return {"observations": len(s), "total_return": None, "annualized_volatility": None,
                "max_drawdown": None, "sharpe_rf0": None}
    r = s.pct_change().dropna()
    peak = s.cummax()
    dd = s / peak - 1
    vol = r.std(ddof=1) * np.sqrt(periods_per_year) if len(r) > 1 else float("nan")
    sharpe = (r.mean() / r.std(ddof=1) * np.sqrt(periods_per_year)
              if len(r) > 1 and r.std(ddof=1) > 0 else float("nan"))
    years = len(r) / periods_per_year
    cagr = (s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1 if years > 0 and s.iloc[0] > 0 else float("nan")
    return {"observations": len(s), "total_return": float(s.iloc[-1] / s.iloc[0] - 1),
            "cagr": float(cagr), "annualized_volatility": float(vol),
            "max_drawdown": float(dd.min()), "sharpe_rf0": float(sharpe)}
