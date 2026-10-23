"""Single source of truth: paths, official competition rules and internal safety buffers.

Every other module imports its numbers from here, and the LLM system prompt is rendered
from the same `Limits` object, so code and prompt can never drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
PROMPT_DIR = ROOT / "src" / "prompts"
TW = timezone(timedelta(hours=8))

SCHEMA_VERSION = "4.3"
LOT = 1000                      # board lot (shares); odd lots are not allowed
FEE_RATE = 0.001425             # brokerage fee, charged on buys and sells
TAX_RATE = 0.003                # securities transaction tax, sells only
MAX_VIOLATIONS = 3              # warnings at which the team is disqualified
UNIVERSE_SIZE = 150
MAX_OBS, MAX_SOURCES = 100, 50  # D-Plan schema limits (maxItems)
SUBMIT_OPEN = (19, 30)          # HH:MM on the previous trading day
SUBMIT_CLOSE = (8, 55)          # HH:MM on the trade date

# Official hard limits (competition rules, section 6). Used by the final gate.
OFFICIAL = {"min_n": 20, "max_n": 30, "cap_2330": 0.25, "cap_other": 0.10,
            "max_cash": 0.25, "hold_tol": 0.02}


@dataclass(frozen=True)
class Limits:
    """Internal limits: deliberately tighter than the official ones.

    Orders are priced at the previous close, but fills happen at the same-day average price
    and NAV is marked at the close, so every limit needs a buffer.
    """
    min_n: int = 22
    max_n: int = 28
    cap_2330: float = 0.22
    cap_other: float = 0.085
    cash_lo: float = 0.04
    cash_hi: float = 0.10
    hold_tol: float = 0.015      # |net flow| tolerance for net_exposure_intent="hold"
    core_min: float = 0.62       # minimum NAV share of the core (AI / semiconductor) themes
    no_trade_band: float = 0.01  # skip rebalancing a held stock when weight drift <= band
    price_tol: float = 0.001     # datapack close vs fetched close tolerance

    def cap(self, ticker: str) -> float:
        return self.cap_2330 if ticker == "2330" else self.cap_other

    @property
    def sum_lo(self) -> float:   # sum of target weights; margin absorbs lot flooring + fees
        return round(1 - self.cash_hi + 0.01, 4)

    @property
    def sum_hi(self) -> float:
        return round(1 - self.cash_lo - 0.01, 4)


NORMAL = Limits()
# One violation away from disqualification: shrink every margin.
STRICT = Limits(min_n=23, max_n=27, cap_2330=0.20, cap_other=0.08,
                cash_lo=0.05, cash_hi=0.09, hold_tol=0.01)


def limits_for(violations: int) -> Limits:
    return STRICT if violations >= MAX_VIOLATIONS - 1 else NORMAL
