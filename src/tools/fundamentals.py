from __future__ import annotations
import pandas as pd

def normalize_fundamentals(raw: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize a manually/API supplied fundamental table.
    Expected optional columns include ticker, as_of_date, revenue_growth_yoy,
    gross_margin, operating_margin, roe, pe_ttm, earnings_revision.
    No values are imputed. Missing fields remain missing.
    """
    out = raw.copy()
    if "ticker" not in out:
        raise ValueError("Fundamental table must include ticker")
    out["ticker"] = out["ticker"].astype(str).str.strip().str.zfill(4)
    if "as_of_date" in out:
        out["as_of_date"] = pd.to_datetime(out["as_of_date"], errors="coerce")
    numeric = ["revenue_growth_yoy", "gross_margin", "operating_margin", "roe",
               "pe_ttm", "earnings_revision"]
    for c in numeric:
        if c in out:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out

def score_fundamentals(raw: pd.DataFrame) -> pd.DataFrame:
    """Simple configurable research baseline; percentile ranks are not investment guarantees."""
    df = normalize_fundamentals(raw)
    metrics = {
        "revenue_growth_yoy": True, "gross_margin": True, "operating_margin": True,
        "roe": True, "earnings_revision": True
    }
    score_cols = []
    for col, higher_better in metrics.items():
        if col in df:
            score_col = col + "_pct"
            df[score_col] = df[col].rank(pct=True, ascending=higher_better)
            score_cols.append(score_col)
    if score_cols:
        df["fundamental_score"] = df[score_cols].mean(axis=1, skipna=True)
        df["fundamental_coverage"] = df[score_cols].notna().mean(axis=1)
    else:
        df["fundamental_score"] = float("nan")
        df["fundamental_coverage"] = 0.0
    return df
