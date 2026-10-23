from __future__ import annotations
from datetime import datetime, timezone
from .config import load_rules
from .data_collector import load_universe
from .data_validator import validate_price_files
from .reporting import write_json_report

def preflight(universe_path="config/universe.csv", rules_path=None) -> dict:
    rules = load_rules(rules_path)
    u = load_universe(universe_path)
    approved = u[u["approved"].astype(str).str.lower().isin(["true", "yes", "1", "approved"])]
    report = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "rules_verified": bool(rules.get("rules_verified", False)),
        "approved_universe_count": int(len(approved)),
        "duplicate_tickers": approved.loc[approved["ticker"].duplicated(), "ticker"].tolist(),
        "rules": rules,
        "ready_for_competition_run": bool(rules.get("rules_verified", False) and len(approved) > 0
                                          and not approved["ticker"].duplicated().any()),
        "warning": "This preflight checks configuration completeness only; it does not verify official rules."
    }
    return report

def run_data_checks(universe_path="config/universe.csv") -> dict:
    return {"preflight": preflight(universe_path), "price_quality": validate_price_files()}
