"""FinMind open-data helper (free tier; register at https://finmindtrade.com for a token).

Used for the two signals no keyless exchange endpoint gives us reliably:
  * foreign investors' net open interest in TAIEX futures (TX)  -> regime point `foreign_noi`
  * per-stock margin-purchase balance change                     -> `margin_inc` (margin-trap penalty)

Without FINMIND_TOKEN every function raises RuntimeError and the callers just skip the signal.
The token travels as a query parameter, so sources must cite the site URL, never the API URL.
"""
from __future__ import annotations

import os
from datetime import date, timedelta

from util import http_get_json

BASE = "https://api.finmindtrade.com/api/v4/data"
SITE = "https://finmindtrade.com/"


def have_token() -> bool:
    return bool(os.environ.get("FINMIND_TOKEN", "").strip())


def fetch(dataset: str, data_id: str | None, start_date: str, end_date: str | None = None, counter=None) -> list:
    tok = os.environ.get("FINMIND_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("FINMIND_TOKEN missing")
    params = {"dataset": dataset, "start_date": start_date, "token": tok}
    if data_id:
        params["data_id"] = data_id
    if end_date:
        params["end_date"] = end_date
    js = http_get_json(BASE, params, timeout=25, retries=1, counter=counter)
    if js.get("status") not in (200, None):
        raise RuntimeError(f"FinMind status {js.get('status')}: {str(js.get('msg'))[:80]}")
    return js.get("data") or []


def foreign_futures_noi(as_of: str, counter=None) -> dict | None:
    """Foreign investors' net open interest (long - short, contracts) in TX on the latest day <= as_of."""
    start = (date.fromisoformat(as_of) - timedelta(days=10)).isoformat()
    rows = fetch("TaiwanFuturesInstitutionalInvestors", "TX", start, as_of, counter)
    by_date: dict = {}
    for r in rows:
        if "外資" not in str(r.get("institutional_investors", "")):
            continue
        lo, sh = r.get("long_open_interest_balance_volume"), r.get("short_open_interest_balance_volume")
        if lo is None or sh is None:
            continue
        by_date.setdefault(str(r.get("date"))[:10], float(lo) - float(sh))    # first match per day wins
    days = sorted(d for d in by_date if d <= as_of)
    if not days:
        return None
    return {"date": days[-1], "noi": by_date[days[-1]], "prev": by_date[days[-2]] if len(days) > 1 else None}


def margin_change(ticker: str, as_of: str, lookback: int = 5, counter=None) -> dict | None:
    """Margin-purchase balance (shares) now vs `lookback` trading days earlier, as a fraction."""
    start = (date.fromisoformat(as_of) - timedelta(days=16)).isoformat()
    rows = fetch("TaiwanStockMarginPurchaseShortSale", ticker, start, as_of, counter)
    bal = sorted((str(r["date"])[:10], float(r["MarginPurchaseTodayBalance"]))
                 for r in rows if r.get("MarginPurchaseTodayBalance") is not None and str(r.get("date"))[:10] <= as_of)
    if len(bal) <= lookback:
        return None
    (d_now, now), (_, then) = bal[-1], bal[-1 - lookback]
    return {"date": d_now, "balance_lots": round(now / 1000.0), "chg_frac": (now / then - 1.0) if then > 0 else None}
