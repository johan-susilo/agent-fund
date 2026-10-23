"""Step 2: fetch the previous trading day's closing prices for the 150-stock universe.

Official OpenAPI sources (no key needed):
  TWSE (listed) : https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
  TPEx (OTC)    : https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes

Usage:
    python src/fetch_data.py 2026-10-23 2026-10-22
                             ^trade_date ^as_of_date (settlement date on the DataPack)
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor

from settings import ROOT, UNIVERSE_SIZE
from universe import load_universe
from util import Counter, http_get_json, iso, now_tw

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


def _num(x):
    """'2,585.00' -> 2585.0; blanks, '--' and '---' -> None."""
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return None


def _pick(row, names):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


def _roc_date(s):
    """'1151022' (ROC) or '20261022' -> '2026-10-22'; anything else -> None."""
    s = str(s or "").strip()
    try:
        if len(s) == 7:
            return f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"
        if len(s) == 8:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    except ValueError:
        pass
    return None


def parse_rows(rows, code_keys, close_keys, change_keys, date_keys=("Date",)):
    out, dates = {}, set()
    for r in rows:
        d = _roc_date(_pick(r, date_keys))
        if d:
            dates.add(d)
        code, close = _pick(r, code_keys), _num(_pick(r, close_keys))
        if not code or not close:            # no trade / no close -> skip
            continue
        chg = _num(_pick(r, change_keys))
        prev = close - chg if chg is not None else None
        out[str(code).strip()] = {"close": close,
                                  "change_pct": round(chg / prev * 100, 2) if prev and prev > 0 else None}
    return out, dates


def fetch_prices(counter: Counter):
    """Fetch both exchanges in parallel."""
    with ThreadPoolExecutor(2) as ex:
        f1 = ex.submit(http_get_json, TWSE_URL, None, None, 30, 2, counter)
        f2 = ex.submit(http_get_json, TPEX_URL, None, None, 30, 2, counter)
        rows_twse, rows_tpex = f1.result(), f2.result()
    twse, d1 = parse_rows(rows_twse, ["Code"], ["ClosingPrice"], ["Change"])
    tpex, d2 = parse_rows(rows_tpex, ["SecuritiesCompanyCode"], ["Close"], ["Change"])
    return twse, tpex, d1 | d2


def main(trade_date: str, as_of_date: str) -> dict:
    universe = load_universe()
    counter = Counter()
    twse, tpex, dates = fetch_prices(counter)

    # Stale-data guard: if the API reports a date it must be the settlement date.
    if dates and dates != {as_of_date}:
        raise RuntimeError(f"exchange data is dated {sorted(dates)}, expected {as_of_date}; "
                           "prices are stale or already roll-forwarded")

    now = iso(now_tw())
    prices, missing = {}, []
    for code, name in universe.names.items():
        src = twse if universe.markets[code] == "twse" else tpex
        other = tpex if src is twse else twse
        row = src.get(code) or other.get(code)   # tolerate a wrong market tag
        if row:
            prices[code] = {"name": name, "market": universe.markets[code], **row}
        else:
            missing.append(code)

    content_as_of = f"{as_of_date}T18:30:00+08:00"   # the moment the content describes
    data = {
        "trade_date": trade_date, "as_of_date": as_of_date, "fetched_at": now,
        "tool_calls": counter.n,
        "sources": [
            {"authority": "twse", "name": "TWSE 上市個股日收盤價 (OpenAPI STOCK_DAY_ALL)", "url": TWSE_URL,
             "content_as_of": content_as_of, "fetched_at": now},
            {"authority": "tpex", "name": "TPEx 上櫃個股日收盤價 (OpenAPI)", "url": TPEX_URL,
             "content_as_of": content_as_of, "fetched_at": now},
        ],
        "prices": prices, "missing": missing,
    }
    out_dir = ROOT / "inputs" / trade_date
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "market_data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"Fetched {len(prices)}/{UNIVERSE_SIZE} closes; missing {len(missing)}: {missing}")
    if not dates:
        print("  note: API returned no date field, so date freshness was not verified (check 2330 manually)")
    for c in ("2330", "2454", "2891"):
        if c in prices:
            print(f"  spot check {c} {prices[c]['name']}: close {prices[c]['close']} ({prices[c]['change_pct']}%)")
    if missing:
        print("!!! Do NOT buy stocks without a price today !!!")
    return data


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
