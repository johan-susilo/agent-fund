"""Step 5: run the whole daily pipeline.

    fetch closes ┐
    macro agent  ├─ (parallel) ─> evidence book ─> portfolio agent (LLM) ─> build D-Plan ─> risk gate
    factor agent ┘

Usage:
    python src/run_daily.py 2026-10-23                 # full run (calls the LLM)
    python src/run_daily.py 2026-10-23 --skip-fetch    # reuse inputs/<date>/market_data.json
    python src/run_daily.py 2026-10-23 --refresh       # ignore macro / factor caches
    python src/run_daily.py 2026-10-23 --no-external   # exchange data only (no macro / factor agents)
    python src/run_daily.py 2026-10-23 --replay logs/2026-10-23_attempt2.txt   # no tokens spent

Prerequisite: download the DataPack from the portal and save it as inputs/<date>/datapack.json.
The final "upload + submit" step stays manual: the program never submits anything itself.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))    # make `agents.*` importable from any cwd

import build_dplan
import fetch_data
from agents import fundamental_agent, macro_agent, portfolio_agent, check_agent
from datapack import DataPackError, load_datapack
from observations import EvidenceBook, EvidenceLimitError, market_fragment
from settings import CONFIG_DIR, ROOT, limits_for
from universe import load_universe
from util import iso, now_tw, prev_weekday


def check_price_freshness(state: dict, market: dict, tol: float) -> list:
    """Held stocks: the DataPack close must equal the freshly fetched close, else we fetched the wrong day."""
    bad = []
    for t in state["holdings"]:
        a, b = state["last_close"].get(t), market["prices"].get(t, {}).get("close")
        if a and b and abs(a - b) / a > tol:
            bad.append(f"{t}: DataPack {a} vs fetched {b}")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trade_date")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--no-external", action="store_true")
    ap.add_argument("--replay")
    ap.add_argument("--force", action="store_true", help="Bypass submission window hard stop")
    a = ap.parse_args()
    d, started = a.trade_date, iso(now_tw())

    try:
        dp_path = ROOT / "inputs" / d / "datapack.json"
        if not dp_path.exists():
            sys.exit(f"{dp_path} not found.\nDownload the DataPack from the portal (File Submission page) and save it there.")
        state = load_datapack(dp_path)
        if state["trade_date"] != d:
            sys.exit(f"DataPack trade_date is {state['trade_date']}, not {d}; download it again.")
        limits = limits_for(state["violations"])
        universe = load_universe()
        strategy = json.loads((CONFIG_DIR / "strategy.json").read_text(encoding="utf-8"))
        as_of = state["as_of_date"] or prev_weekday(d)

        print(f"=== [1/5] DataPack === team {state['team_id']} | NAV {state['nav']:,.0f} | "
              f"holdings {len(state['holdings'])} | violations {state['violations']} "
              f"| limits: {'STRICT' if limits.cap_other < 0.085 else 'normal'}")
        for w in state["warnings"]:
            print("  !!", w)
        win_errs = check_agent.check_submission_window(now_tw(), d, state["as_of_date"])
        if win_errs:
            for w in win_errs:
                print("  !! [WINDOW]", w)
            if not a.force and not a.replay:
                sys.exit("Submission window check failed. Pass --force to run outside the window.")

        print("\n=== [2/5] Collect evidence (parallel) ===")
        with ThreadPoolExecutor(3) as ex:
            f_mkt = None if a.skip_fetch else ex.submit(fetch_data.main, d, as_of)
            f_mac = None if a.no_external else ex.submit(macro_agent.collect, d, a.refresh)
            f_fun = None if a.no_external else ex.submit(fundamental_agent.collect, d, as_of, universe,
                                                         list(state["holdings"]), a.refresh)
            if f_mkt:
                f_mkt.result()                                    # a price failure must stop the run
            market = json.loads((ROOT / "inputs" / d / "market_data.json").read_text(encoding="utf-8"))
            frags = {}
            for name, f in (("macro", f_mac), ("factor", f_fun)):
                try:
                    frags[name] = f.result() if f else None
                except Exception as e:                            # external evidence is optional
                    print(f"  !! {name} agent failed: {type(e).__name__}: {e}")
                    frags[name] = None
        if market["as_of_date"] != as_of or market["trade_date"] != d:
            sys.exit("market_data.json belongs to a different date; rerun without --skip-fetch")
        bad = check_price_freshness(state, market, limits.price_tol)
        if bad:
            sys.exit("Held-stock closes disagree with the DataPack (wrong day fetched?). Aborting:\n  " + "\n  ".join(bad))
        if market["missing"]:
            print(f"  !! no price for {market['missing']}: these stocks are excluded today")

        book, tool_calls = EvidenceBook(), market.get("tool_calls", 0)
        book.absorb(market_fragment(market, state, universe))
        for name in ("macro", "factor"):
            fr = frags[name]
            if fr:
                n = book.absorb(fr)
                tool_calls += fr.get("tool_calls", 0)
                print(f"  {name}: {n} observations" + "".join(f"\n    - {w}" for w in fr.get("warnings", [])))
            else:
                print(f"  {name}: not used")
        print(f"  evidence book: {len(book.sources)} sources, {len(book.observations)} observations")


        from tools import journal, factors
        j_data = journal.review(as_of, state, market)
        if j_data:
            print(f"  journal: Realised return {j_data['port_ret_pct']:+.2f}% vs bench {j_data['bench_ret_pct']:+.2f}% (non-citable context)")
        else:
            print("  journal: No previous day record found (Day 1).")

        # --- DETERMINISTIC FACTOR SCORING & REGIME DETECTION ---
        factor_df = factors.build_factor_table(universe, market, frags.get("factor"))
        regime = factors.detect_regime(market, frags.get("macro"), factor_df, universe)
        limits = limits_for(state["violations"], regime=regime)
        print(f"  regime: {regime} -> cash window [{limits.cash_lo:.0%}, {limits.cash_hi:.0%}]")

        print("\n=== [3/5] Portfolio agent ===")
        ask = None
        if a.replay:
            raw = Path(a.replay).read_text(encoding="utf-8")
            ask = lambda _s, _u: (json.loads(raw), {"model": "replay", "input_tokens": 0, "output_tokens": 0}, raw)
        result = portfolio_agent.run_agent(
            d, state, market, universe, book, strategy, limits, started,
            ask=ask, factor_df=factor_df, regime=regime, journal_ctx=j_data,
        )

        print("\n=== [4/5] Build D-Plan ===")
        dplan = build_dplan.build(result, state, tool_calls)
        name = f"D-Plan_{state['team_id']}_{d}.json"

        print("\n=== [5/5] Final check gate ===")
        errs, warns = check_agent.final_gate(dplan, state, result["_closes"], universe, limits, name)
        for w in warns:
            print("  warn:", w)
        out_dir = ROOT / "outputs"
        out_dir.mkdir(exist_ok=True)
        if errs or a.replay:
            # A suffix that breaks the naming rule makes accidental upload impossible.
            out = out_dir / (name.replace(".json", ".REJECTED.json") if errs else name.replace(".json", ".REPLAY.json"))
            out.write_text(json.dumps(dplan, ensure_ascii=False, indent=2), encoding="utf-8")
            for e in errs:
                print("  FAIL:", e)
            sys.exit(f"\n⛔ {'Gate failed' if errs else 'Replay output'}: saved as {out.name}. DO NOT SUBMIT.")
        out = out_dir / name
        out.write_text(json.dumps(dplan, ensure_ascii=False, indent=2), encoding="utf-8")
        zp = build_dplan.snapshot_code(d, dplan["agent_metadata"]["code_version"])
        print(f"  all checks passed\n\n🎉 {out}\n   code snapshot: {zp.name}")
        print("Next (manual): 1) upload the file in the portal and confirm every intake check passes\n"
              "               2) review Daily Data > Orders  3) press 'Submit' (upload alone does not count)")
    except DataPackError as e:
        sys.exit(f"DataPack unusable: {e}")
    except EvidenceLimitError as e:
        sys.exit(f"Evidence exceeds D-Plan schema limits: {e}")
    except SystemExit:
        raise
    except Exception:
        print(traceback.format_exc())
        sys.exit("Pipeline aborted. Do not submit any unfinished file.")


if __name__ == "__main__":
    main()
