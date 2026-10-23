from __future__ import annotations
import argparse, json
from pathlib import Path
import pandas as pd
from .config import ROOT, DATA_DIR, OUTPUT_DIR, load_rules
from .data_collector import collect_prices, load_price_panel
from .data_validator import validate_price_files
from .indicators import technical_features, cross_sectional_rank
from .backtest import weekly_momentum_weights, backtest_weights
from .reporting import write_backtest_outputs, write_json_report
from .orchestrator import preflight

def main():
    p = argparse.ArgumentParser(description="E.SUN AI CUP research tools")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect"); c.add_argument("--start", default="2021-01-01"); c.add_argument("--end", default=None)
    sub.add_parser("validate"); sub.add_parser("features"); sub.add_parser("backtest")
    sub.add_parser("preflight")
    args = p.parse_args()
    universe = ROOT / "config" / "universe.csv"
    if args.command == "collect":
        if not universe.exists(): raise SystemExit("Create config/universe.csv from the verified official universe first.")
        report = collect_prices(universe, args.start, args.end, DATA_DIR / "prices")
        write_json_report(report, DATA_DIR / "collection_report.json"); print(json.dumps(report, indent=2))
    elif args.command == "validate":
        print(json.dumps(validate_price_files(DATA_DIR / "prices"), indent=2))
    elif args.command == "features":
        prices = load_price_panel(DATA_DIR / "prices")
        f = cross_sectional_rank(technical_features(prices))
        f.to_csv(DATA_DIR / "technical_features.csv", index=False)
        print(f"Saved {len(f):,} feature rows to data/technical_features.csv")
    elif args.command == "backtest":
        rules = load_rules()
        prices = load_price_panel(DATA_DIR / "prices")
        weights = weekly_momentum_weights(prices, n_holdings=min(25, rules["max_holdings"]),
            cash_target=max(rules["cash_min"], min(0.08, rules["cash_max"])),
            max_weight=min(0.08, rules["max_weight_other"]),
            tsmc_cap=min(rules.get("internal_tsmc_cap", rules["max_weight_tsmc"]), rules["max_weight_tsmc"]))
        result = backtest_weights(prices, weights, initial_capital=rules["initial_capital_twd"],
            commission_rate=rules["commission_rate_per_side"], sell_tax_rate=rules["sell_tax_rate"],
            slippage_bps=rules.get("slippage_bps_per_side", 0), execution_lag=1)
        print(json.dumps(write_backtest_outputs(result, OUTPUT_DIR), indent=2))
    elif args.command == "preflight":
        print(json.dumps(preflight(universe), indent=2))

if __name__ == "__main__":
    main()
