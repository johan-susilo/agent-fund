"""Single source of truth: paths, official competition rules and internal safety buffers.

Every other module imports its numbers from here, and the LLM system prompt is rendered
from the same `Limits` object, so code and prompt can never drift apart.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
OFFICIAL = {
    "min_n": 20, "max_n": 30, "cap_2330": 0.25, "cap_other": 0.10,
    "max_cash": 0.25, "hold_tol": 0.02, "min_active_share": 0.15,
}

CASH_BY_REGIME = {
    "risk_on":  (0.04, 0.09),
    "neutral":  (0.07, 0.13),
    "risk_off": (0.12, 0.20),
}

ACTIVE_SHARE_FLOOR = 0.20       # internal pre-trade floor (official rule §6.13 is 0.15)
MAX_DAILY_TURNOVER = 0.15       # max 15% NAV one-way turnover after Day 1


@dataclass(frozen=True)
class Limits:
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
    active_share_floor: float = ACTIVE_SHARE_FLOOR
    max_turnover: float = MAX_DAILY_TURNOVER
    theme_cap: float = 0.30       # max NAV share of any single theme (strategy: 主題集中度上限)
    theme_cap_semis: float = 0.50 # the broad semiconductor theme gets a wider cap

    def theme_limit(self, theme_id: str) -> float:
        return self.theme_cap_semis if theme_id == "semis" else self.theme_cap

    def cap(self, ticker: str) -> float:
        return self.cap_2330 if ticker == "2330" else self.cap_other

    @property
    def sum_lo(self) -> float:   # sum of target weights; margin absorbs lot flooring + fees
        return round(1 - self.cash_hi + 0.01, 4)

    @property
    def sum_hi(self) -> float:
        return round(1 - self.cash_lo - 0.01, 4)


NORMAL = Limits()
STRICT = Limits(
    min_n=23, max_n=27, cap_2330=0.20, cap_other=0.08,
    cash_lo=0.05, cash_hi=0.09, hold_tol=0.01,
)


def limits_for(violations: int, regime: str | None = None) -> Limits:
    base = STRICT if violations >= MAX_VIOLATIONS - 1 else NORMAL
    if regime and regime in CASH_BY_REGIME:
        lo, hi = CASH_BY_REGIME[regime]
        return replace(base, cash_lo=lo, cash_hi=hi)
    return base
