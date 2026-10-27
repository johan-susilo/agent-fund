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
# Date-addressable fallbacks (the OpenAPI "latest" snapshots can lag by days; these take an explicit date).
TWSE_DATED_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_DATED_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"


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


def _signed_change(sign_html, diff):
    """TWSE MI_INDEX gives the sign as HTML ('<p style=color:red>+</p>') and the magnitude separately."""
    d = _num(diff)
    if d is None:
        return None
    return -d if "-" in str(sign_html) else d


def _table(payload, need):
    """First table in a MI_INDEX / afterTrading payload whose fields contain all of `need`."""
    for t in payload.get("tables") or []:
        if all(n in (t.get("fields") or []) for n in need):
            return t
    return None


def fetch_twse_dated(as_of_date: str, counter: Counter):
    """TWSE closes for an explicit date. Returns ({code: row}, {date}) or ({}, set()) if the day has no data."""
    ymd = as_of_date.replace("-", "")
    js = http_get_json(TWSE_DATED_URL, {"date": ymd, "type": "ALLBUT0999", "response": "json"}, None, 30, 2, counter)
    t = _table(js, ["證券代號", "收盤價", "漲跌價差"])
    if js.get("stat") != "OK" or not t:
        return {}, set()
    f = t["fields"]
    i_c, i_p, i_s, i_d = f.index("證券代號"), f.index("收盤價"), f.index("漲跌(+/-)"), f.index("漲跌價差")
    out = {}
    for r in t["data"]:
        close, chg = _num(r[i_p]), _signed_change(r[i_s], r[i_d])
        if not close:
            continue
        prev = close - chg if chg is not None else None
        out[str(r[i_c]).strip()] = {"close": close, "change_pct": round(chg / prev * 100, 2) if prev and prev > 0 else None}
    return out, {_roc_date(js.get("date")) or as_of_date}


def fetch_tpex_dated(as_of_date: str, counter: Counter):
    ymd = as_of_date.replace("-", "/")
    js = http_get_json(TPEX_DATED_URL, {"date": ymd, "type": "EW", "response": "json"}, None, 30, 2, counter)
    t = _table(js, ["代號", "收盤", "漲跌"])
    if not t:
        return {}, set()
    f = t["fields"]
    i_c, i_p, i_d = f.index("代號"), f.index("收盤"), f.index("漲跌")
    out = {}
    for r in t["data"]:
        close, chg = _num(r[i_p]), _num(r[i_d])
        if not close:
            continue
        prev = close - chg if chg is not None else None
        out[str(r[i_c]).strip()] = {"close": close, "change_pct": round(chg / prev * 100, 2) if prev and prev > 0 else None}
    return out, {_roc_date(js.get("date")) or as_of_date}


def fetch_prices(counter: Counter, as_of_date: str):
    """Fetch both exchanges in parallel and verify EACH source's date separately.

    The OpenAPI endpoints are 'latest snapshot' feeds and one exchange can lag the other (this is what produced
    dates {10-22, 10-27} and silently priced half the book on a stale day). A source whose date != as_of_date is
    re-fetched from the date-addressable endpoint; if that also fails the run aborts instead of using stale prices.
    """
    with ThreadPoolExecutor(2) as ex:
        f1 = ex.submit(http_get_json, TWSE_URL, None, None, 30, 2, counter)
        f2 = ex.submit(http_get_json, TPEX_URL, None, None, 30, 2, counter)
        rows_twse, rows_tpex = f1.result(), f2.result()
    twse, d1 = parse_rows(rows_twse, ["Code"], ["ClosingPrice"], ["Change"])
    tpex, d2 = parse_rows(rows_tpex, ["SecuritiesCompanyCode"], ["Close"], ["Change"])

    for label, rows, dates, dated_fn in (("TWSE", twse, d1, fetch_twse_dated), ("TPEx", tpex, d2, fetch_tpex_dated)):
        if dates == {as_of_date}:
            continue
        print(f"  !! {label} OpenAPI is dated {sorted(dates) or 'unknown'}, expected {as_of_date}; "
              f"falling back to the dated endpoint")
        fresh, fd = dated_fn(as_of_date, counter)
        if not fresh or fd != {as_of_date}:
            raise RuntimeError(f"{label}: no closes for {as_of_date} (OpenAPI={sorted(dates)}, dated endpoint={sorted(fd)}). "
                               f"The exchange has not published yet - retry later; refusing to use stale prices.")
        rows.clear()
        rows.update(fresh)
        dates.clear()
        dates.add(as_of_date)
    return twse, tpex, d1 | d2


def main(trade_date: str, as_of_date: str) -> dict:
    universe = load_universe()
    counter = Counter()
    twse, tpex, dates = fetch_prices(counter, as_of_date)

    # Stale-data guard: fetch_prices already enforced per-source dates (it raises on stale data).
    assert not dates or dates == {as_of_date}, f"stale exchange dates {sorted(dates)}"

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