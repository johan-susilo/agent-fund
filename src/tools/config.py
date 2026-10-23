"""Bridge settings.py into the standalone research/backtest toolkit."""
from __future__ import annotations

from settings import FEE_RATE, NORMAL, OFFICIAL, ROOT, TAX_RATE

DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "outputs"


def load_rules(_path=None) -> dict:
    return {
        "rules_verified": True,
        "initial_capital_twd": 1_000_000_000,
        "min_holdings": NORMAL.min_n,
        "max_holdings": NORMAL.max_n,
        "max_weight_tsmc": OFFICIAL["cap_2330"],
        "internal_tsmc_cap": NORMAL.cap_2330,
        "max_weight_other": NORMAL.cap_other,
        "cash_min": NORMAL.cash_lo,
        "cash_max": NORMAL.cash_hi,
        "commission_rate_per_side": FEE_RATE,
        "sell_tax_rate": TAX_RATE,
        "slippage_bps_per_side": 5.0,
    }