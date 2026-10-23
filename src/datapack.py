"""Step 1: read the DataPack downloaded from the contestant portal and turn it into `state`.

Usage:
    python src/datapack.py inputs/2026-10-23/datapack.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from settings import MAX_VIOLATIONS


class DataPackError(Exception):
    pass


def load_datapack(path) -> dict:
    dp = json.loads(Path(path).read_text(encoding="utf-8"))
    if dp.get("doc_type") != "DataPack":
        raise DataPackError("not a DataPack file")

    missing = dp.get("missing", [])
    # Previous settlement not produced yet -> `account` is null and cannot be used for ordering.
    if "prev_settlement" in missing or dp.get("account") is None:
        raise DataPackError("previous-day settlement is not available yet; download the DataPack again later")

    acct, rows = dp["account"], dp.get("holdings") or []
    warnings = []

    holdings, receivable = {}, {}
    for h in rows:
        if h["shares"] > 0:                       # only inventory shares can be sold / count toward weights
            holdings[h["code"]] = int(h["shares"])
        if h.get("receivable_shares"):            # pending stock dividends: not sellable, not in weights
            receivable[h["code"]] = int(h["receivable_shares"])
    no_px = [h["code"] for h in rows if h["shares"] > 0 and not h.get("close_price")]

    if "prev_close" in missing or no_px:
        warnings.append(f"DataPack has no close price for {no_px or 'some stocks'} (missing={missing}); "
                        "nav_tradable may be understated and order sizing unreliable")
    outcome = dp["prev_day_orders"]["outcome"]
    reasons = dp["prev_day_orders"].get("void_reasons", [])
    if outcome == "voided":
        warnings.append(f"yesterday's orders were VOIDED: {reasons}")
    if dp["prev_day_orders"].get("excluded_codes"):
        warnings.append(f"excluded from yesterday's fills: {dp['prev_day_orders']['excluded_codes']}")

    violations = dp["compliance"]["violations"]
    if violations >= MAX_VIOLATIONS - 1:
        warnings.append(f"{violations} violations on record: ONE more means disqualification")

    return {
        "team_id": dp["team_id"],
        "trade_date": dp["trade_date"],
        "as_of_date": dp["as_of_date"],
        "nav": float(acct["nav_tradable"]),       # always nav_tradable (never aum) for ratios and order sizing
        "cash": float(acct["cash"]),
        "holdings": holdings,                     # {"2330": 73000, ...}
        "receivable": receivable,
        # The server's own previous close; authoritative for held stocks when sizing orders.
        "last_close": {h["code"]: float(h["close_price"]) for h in rows if h.get("close_price")},
        "violations": violations,
        "incidents": dp["compliance"].get("incidents", []),
        "prev_outcome": outcome,
        "missing": missing,
        "warnings": warnings,
    }


def print_summary(s: dict) -> None:
    print(f"Team {s['team_id']} | trade date {s['trade_date']} | settled as of {s['as_of_date']}")
    print(f"NAV(tradable) = {s['nav']:,.0f}   cash = {s['cash']:,.0f}")
    print(f"Holdings ({len(s['holdings'])}): {s['holdings']}")
    print(f"Violations = {s['violations']} (disqualified at {MAX_VIOLATIONS})")
    for w in s["warnings"]:
        print("  !!", w)
    if not s["holdings"]:
        print("Empty account: today must build 20-30 positions in one go with cash <= 25%")


if __name__ == "__main__":
    print_summary(load_datapack(sys.argv[1]))
