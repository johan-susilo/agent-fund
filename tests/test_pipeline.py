# tests/test_pipeline.py
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agents import check_agent
from datapack import load_datapack
from derive_orders import derive_orders, project_portfolio, target_shares
from settings import limits_for
from tools.journal import review
from universe import load_universe


def test_target_shares_official_formula():
    # floor(0.1197 * 1_000_000_000 / 2385.0 / 1000) * 1000 == 50,000
    assert target_shares(0.1197, 1_000_000_000.0, 2385.0) == 50_000


def test_active_share_check():
    etf = {f"{2300+i}": 1 / 15 for i in range(15)}
    assert check_agent.active_share(etf, etf, top=15) == 0.0
    distinct = {f"{3000+i}": 1 / 15 for i in range(15)}
    assert check_agent.active_share(distinct, etf, top=15) >= 0.99


def test_journal_review_recovers_previous_close():
    state = {"nav": 1_000_000_000.0, "holdings": {"2330": 50_000}, "last_close": {"2330": 2385.0}, "violations": 2}
    market = {"prices": {"2330": {"close": 2385.0, "change_pct": -1.45}}}
    res = review("2026-10-23", state, market)
    assert res is not None
    assert res["port_ret_pct"] != 0.0


def test_final_gate_on_2026_10_23_fixture():
    state = load_datapack(ROOT / "inputs/2026-10-23/datapack.json")
    market = json.loads((ROOT / "inputs/2026-10-23/market_data.json").read_text(encoding="utf-8"))
    dplan = json.loads((ROOT / "outputs/D-Plan_TEAM_11837_2026-10-23.json").read_text(encoding="utf-8"))
    universe = load_universe()
    limits = limits_for(state["violations"])
    closes = {t: p["close"] for t, p in market["prices"].items() if t in universe.names}

    errs, _ = check_agent.final_gate(dplan, state, closes, universe, limits, "D-Plan_TEAM_11837_2026-10-23.json")
    assert errs == []

    # Mutate D-Plan to trigger failure
    bad_dplan = json.loads(json.dumps(dplan))
    bad_dplan["orders"][0]["shares"] = 50_500  # non-multiple of 1000
    bad_errs, _ = check_agent.final_gate(bad_dplan, state, closes, universe, limits)
    assert any("multiple of 1000" in e for e in bad_errs)