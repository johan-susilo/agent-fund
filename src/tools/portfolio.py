from __future__ import annotations
import pandas as pd

def build_target_weights(scores: pd.DataFrame, ticker_col: str = "ticker",
                         score_col: str = "composite_score", n_holdings: int = 25,
                         max_weight: float = 0.08, cash_target: float = 0.08,
                         tsmc_cap: float = 0.20, tsmc_ticker: str = "2330") -> dict[str, float]:
    """
    Simple transparent baseline allocator, not a production optimizer.
    Ranks eligible scores, applies per-name caps, and iteratively distributes
    the investable weight. Risk/official rules must be validated separately.
    """
    if not 0 <= cash_target < 1: raise ValueError("cash_target must be in [0,1)")
    if ticker_col not in scores or score_col not in scores: raise ValueError("Missing ticker or score column")
    df = scores[[ticker_col, score_col]].dropna().copy()
    df[ticker_col] = df[ticker_col].astype(str).str.zfill(4)
    df = df.sort_values(score_col, ascending=False).drop_duplicates(ticker_col).head(n_holdings)
    if df.empty: return {}
    invest = 1 - cash_target
    caps = {t: (tsmc_cap if t == tsmc_ticker else max_weight) for t in df[ticker_col]}
    positive = df[score_col].clip(lower=0)
    if positive.sum() == 0:
        raw = {t: 1 / len(df) for t in df[ticker_col]}
    else:
        raw = dict(zip(df[ticker_col], positive / positive.sum()))
    weights = {t: 0.0 for t in df[ticker_col]}
    remaining = invest
    active = set(weights)
    # Iterative capped proportional allocation.
    while active and remaining > 1e-9:
        base = sum(max(raw[t], 1e-9) for t in active)
        capped_now = []
        for t in active:
            proposed = remaining * max(raw[t], 1e-9) / base
            room = caps[t] - weights[t]
            if proposed >= room - 1e-12:
                capped_now.append(t)
        if not capped_now:
            for t in active:
                weights[t] += remaining * max(raw[t], 1e-9) / base
            remaining = 0
            break
        for t in capped_now:
            room = max(0.0, caps[t] - weights[t])
            weights[t] += room
            remaining -= room
            active.remove(t)
    # If the configured caps cannot deploy the desired exposure, leave cash higher.
    return {t: round(w, 8) for t, w in weights.items() if w > 1e-8}
