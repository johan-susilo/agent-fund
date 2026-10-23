"""Step 1a: 讀取資料包 (DataPack)，轉成程式好用的 state。

用法:
    python src/datapack.py inputs/2026-10-23/datapack.json
"""
import json
import sys
from pathlib import Path


class DataPackError(Exception):
    pass


def load_datapack(path):
    dp = json.loads(Path(path).read_text(encoding="utf-8"))

    if dp.get("doc_type") != "DataPack":
        raise DataPackError("這不是 DataPack 檔案")

    # 前一日結算還沒產生 -> account 是 null，不能拿來下單
    if "prev_settlement" in dp.get("missing", []) or dp.get("account") is None:
        raise DataPackError("前一日結算尚未產生，請稍後重新下載資料包")

    acct = dp["account"]
    holdings = {}
    for h in dp.get("holdings") or []:
        # 只計 shares（庫存）。receivable_shares 是應收配股，不能賣、不計權重
        if h["shares"] > 0:
            holdings[h["code"]] = int(h["shares"])

    return {
        "team_id": dp["team_id"],
        "trade_date": dp["trade_date"],
        "as_of_date": dp["as_of_date"],
        "nav": float(acct["nav_tradable"]),   # 一律用 nav_tradable，不用 aum
        "cash": float(acct["cash"]),
        "holdings": holdings,                 # {"2330": 73000, ...}
        "last_close": {h["code"]: h["close_price"]
                       for h in (dp.get("holdings") or [])
                       if h.get("close_price")},
        "violations": dp["compliance"]["violations"],
        "prev_outcome": dp["prev_day_orders"]["outcome"],
        "missing": dp.get("missing", []),
    }


def print_summary(s):
    print(f"隊伍 {s['team_id']} | 交易日 {s['trade_date']} | 結算日 {s['as_of_date']}")
    print(f"NAV(可交易淨值) = {s['nav']:,.0f}   現金 = {s['cash']:,.0f}")
    print(f"持股 {len(s['holdings'])} 檔: {s['holdings']}")
    print(f"累計違規天數 = {s['violations']}  (滿 3 次取消資格)")
    if s["violations"] >= 2:
        print("!!! 警告：再違規一次就會被取消資格，今天必須成功繳交且全部合規 !!!")
    if not s["holdings"]:
        print("目前空手：今天必須一次建倉到 20-30 檔，且現金比 <= 25%")


if __name__ == "__main__":
    print_summary(load_datapack(sys.argv[1]))