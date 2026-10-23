"""Step 2: 抓「前一交易日收盤價」（150 檔）並存成 market_data.json。

資料來源（官方 OpenAPI，免金鑰）：
  上市 twse : https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL
  上櫃 tpex : https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes

用法:
    python src/fetch_data.py 2026-10-23 2026-10-22
                             ^trade_date ^as_of_date（資料包上的結算日）
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

TW = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


def _num(x):
    """'2,585.00' -> 2585.0；空白、'--'、'---' -> None"""
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return None


def _pick(row, names):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return None


def parse_rows(rows, code_keys, close_keys, change_keys):
    out = {}
    for r in rows:
        code = _pick(r, code_keys)
        close = _num(_pick(r, close_keys))
        if not code or not close:      # 沒成交 / 沒收盤價就跳過
            continue
        chg = _num(_pick(r, change_keys))
        prev = close - chg if chg is not None else None
        out[str(code).strip()] = {
            "close": close,
            "change_pct": round(chg / prev * 100, 2) if prev else None,
        }
    return out


def get_json(url):
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def fetch_prices():
    twse = parse_rows(get_json(TWSE_URL), ["Code"], ["ClosingPrice"], ["Change"])
    tpex = parse_rows(get_json(TPEX_URL), ["SecuritiesCompanyCode"], ["Close"], ["Change"])
    return twse, tpex


def main(trade_date, as_of_date):
    # Updated to read the AI agent config format
    agent_config = json.loads((ROOT / "config/ticker_list.json").read_text(encoding="utf-8"))
    ticker_list = agent_config.get("tickers", [])
    
    twse, tpex = fetch_prices()
    now = datetime.now(TW).isoformat(timespec="seconds")

    prices, missing = {}, []
    
    # Loop through the new array structure
    for item in ticker_list:
        code = str(item.get("Ticker")).strip()
        name = str(item.get("Name")).strip()
        
        if code == "證券代號":  # Skip accidental headers from excel
            continue
            
        # Dynamically determine the market since it's no longer in the JSON
        if code in twse:
            prices[code] = {"name": name, "market": "twse", **twse[code]}
        elif code in tpex:
            prices[code] = {"name": name, "market": "tpex", **tpex[code]}
        else:
            missing.append(code)

    content_as_of = f"{as_of_date}T18:30:00+08:00"   # 這份資料「描述的時點」
    data = {
        "trade_date": trade_date,
        "as_of_date": as_of_date,
        "fetched_at": now,
        "sources": [
            {"authority": "twse", "name": "TWSE 上市個股日收盤價 (OpenAPI STOCK_DAY_ALL)",
             "url": TWSE_URL, "content_as_of": content_as_of, "fetched_at": now},
            {"authority": "tpex", "name": "TPEx 上櫃個股日收盤價 (OpenAPI)",
             "url": TPEX_URL, "content_as_of": content_as_of, "fetched_at": now},
        ],
        "prices": prices,
        "missing": missing,
    }
    out_dir = ROOT / "inputs" / trade_date
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "market_data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"抓到 {len(prices)}/150 檔收盤價；缺 {len(missing)} 檔: {missing}")
    for c in ("2330", "2454", "2891"):
        if c in prices:
            print(f"  抽查 {c} {prices[c]['name']}: 收盤 {prices[c]['close']}  ({prices[c]['change_pct']}%)")
    print("請拿 2330 的收盤價跟 Yahoo/證交所網站對一下，確認是 as_of_date 當天。")
    if missing:
        print("!!! 缺價的股票今天不要買 !!!")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])