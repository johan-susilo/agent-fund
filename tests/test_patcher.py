"""The patcher is tested against stubs rebuilt from the repo excerpts it targets (anchors must match real code)."""
import re
import textwrap

import apply_upgrade as up

FUND = '''
import os
def _factors(universe, as_of, counter):
    out = {}
    f = _price_factors(sub)
    if f:
        out[code] = f
    return out, 0

def collect(trade_date, as_of_date, universe, holdings, refresh=False):
    frag, counter, factors = {}, None, {}
    infos = {}
    frag["fundamentals"] = infos          # full-universe stash for the factor table (not an observation)
    if os.environ.get("AGENT_AUTONOMY", "1") != "0":
        try:
            research(trade_date, as_of_date, universe, holdings, frag, counter)
        except Exception as e:
            frag["warnings"].append(f"fundamental research agent failed: {type(e).__name__}: {str(e)[:120]}")
    return frag
'''

MACRO = '''
import os
def collect(trade_date, refresh=False):
    frag, counter = {}, None
    if os.environ.get("AGENT_AUTONOMY", "1") != "0":
        try:
            research(trade_date, frag, counter)
        except Exception as e:                       # baseline evidence is still valid
            pass
    return frag
'''

CHECK = '''
import re
REDUCE_CAP = 0.04   # weight cap for tickers the risk manager marks REDUCE (2330 is exempt: it is the anchor)


# ===================================================== risk-manager overrides (shared helpers)
def risk_maps(risk_ctx) -> dict:
    """Normalise the risk manager output into ticker sets: VETO / REDUCE / WARN / EXDIV."""
    out = {"VETO": set(), "REDUCE": set(), "WARN": set(), "EXDIV": set()}
    if not risk_ctx:
        return out
    for o in (risk_ctx.get("structured_risk") or {}).get("overrides", []) or []:
        t, a = str(o.get("ticker", "")).strip(), o.get("action")
        if a in ("VETO", "REDUCE", "WARN") and re.fullmatch(r"\\d{4}", t):
            out[a].add(t)
    flags = risk_ctx.get("event_flags") or {}
    out["VETO"] |= set(flags.get("disposition_tickers", []))      # 處置股 are always vetoed
    out["EXDIV"] = set(flags.get("ex_dividend_tickers", []))
    out["REDUCE"].discard("2330")
    return out


def check_risk_overrides(weights: dict, held: dict, rm: dict) -> list:
    """weights: {ticker: target_weight} of the proposed portfolio."""
    errs = []
    for t in sorted(weights):
        if t in rm["VETO"]:
            errs.append(f"{t} is VETO'd by the risk manager (disposition / severe risk): "
                        + ("exit it" if t in held else "do not buy it"))
    return errs


# ===================================================== pre-trade checks (internal, buffered limits)
def active_share(port_w, etf_w):
    return 0
'''

PORT = '''
from agents import check_agent
class Limits: ...
# ===================================================================== risk caps / shortlist / baseline
def weight_caps(limits: Limits, rm: dict) -> dict:
    """Per-ticker caps tighter than the normal cap (risk manager REDUCE)."""
    return {t: min(limits.cap(t), check_agent.REDUCE_CAP) for t in rm["REDUCE"] if t != "2330"}


def build_shortlist(candidates):
    row = {
            "rank": rank, "score": _num(f.get("score"), 3),
            "mom20": 1,
    }
    logic = f"依據量化多因子模型（40%基本面、30%動能、20%波動與流動性、10%估值）與台股收盤數據，{t}"
'''

RUN = '''
def main():
    with ctx:
        factor_df = factors.build_factor_table(universe, market, frags.get("factor"))
        regime = factors.detect_regime(market, frags.get("macro"), factor_df, universe)
        limits = limits_for(state["violations"], regime=regime)
        print(f"  regime: {regime} -> cash window [{limits.cash_lo:.0%}, {limits.cash_hi:.0%}]")

        risk_ctx = None
        if os.environ.get("AGENT_AUTONOMY", "1") != "0":
            try:
                pass
            except Exception as e:
                print(f"  !! risk agent failed: {type(e).__name__}: {e}")

        if book.dropped:
            print(f"  !! evidence budget full: {book.dropped} observation(s) dropped")

        result = {}
        if risk_ctx:
            result["llm_meta"]["input_tokens"] += risk_ctx["usage"]["input_tokens"]
            result["llm_meta"]["output_tokens"] += risk_ctx["usage"]["output_tokens"]
'''

PROMPT = ("- `candidates`: a ranked SHORTLIST (rank 1 = best composite score: 40% fundamentals, 30% momentum, 20% risk/liquidity, "
          "10% valuation). Fields: `score`, `mom20`/`mom60` (%).\n")

README = ("# x\nscores with a deterministic multi-factor model\n(40% fundamentals / 30% momentum / 20% volatility and liquidity / 10% valuation).\n\n"
          "### Factor model (`src/tools/factors.py`)\n\nOLD TEXT\n\n### Regime (`detect_regime`)\n\nkeep\n")

STUBS = {"src/agents/fundamental_agent.py": FUND, "src/agents/macro_agent.py": MACRO, "src/agents/check_agent.py": CHECK,
         "src/agents/portfolio_agent.py": PORT, "src/run_daily.py": RUN, "src/prompts/system_prompt.md": PROMPT, "README.md": README}


def make_repo(tmp_path):
    for rel, txt in STUBS.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")
    return tmp_path


def test_apply_patches_everything_and_compiles(tmp_path):
    repo = make_repo(tmp_path)
    assert up.main(["--repo", str(repo)]) == 0
    for rel in STUBS:
        if rel.endswith(".py"):
            compile((repo / rel).read_text(encoding="utf-8"), rel, "exec")        # still valid Python
    fund = (repo / "src/agents/fundamental_agent.py").read_text(encoding="utf-8")
    assert "_price_factors(sub, as_of)" in fund and 'frag["price_factors"] = factors' in fund and 'EVIDENCE_MODE' in fund
    assert "EVIDENCE_MODE" in (repo / "src/agents/macro_agent.py").read_text(encoding="utf-8")
    run = (repo / "src/run_daily.py").read_text(encoding="utf-8")
    assert run.count("evidence_agent.research") == 1 and "risk_gate.ensure_risk_ctx" in run and "regime=regime" in run
    assert run.index("risk_gate.ensure_risk_ctx") < run.index("if book.dropped:")
    assert run.index('if ev:') < run.index("if risk_ctx:")
    pa = (repo / "src/agents/portfolio_agent.py").read_text(encoding="utf-8")
    assert '"comp":' in pa and "35%動能與趨勢" in pa and "40%基本面" not in pa
    pr = (repo / "src/prompts/system_prompt.md").read_text(encoding="utf-8")
    assert "35% momentum/trend" in pr and "`comp`" in pr
    rd = (repo / "README.md").read_text(encoding="utf-8")
    assert "scoring.py" in rd and "OLD TEXT" not in rd and "### Regime" in rd and "35% momentum / 25%" in rd
    assert (repo / "src/run_daily.py.pre-upgrade").exists()


def test_idempotent(tmp_path):
    repo = make_repo(tmp_path)
    up.main(["--repo", str(repo)])
    snap = {rel: (repo / rel).read_text(encoding="utf-8") for rel in STUBS}
    assert up.main(["--repo", str(repo)]) == 0
    assert snap == {rel: (repo / rel).read_text(encoding="utf-8") for rel in STUBS}


def test_dry_run_changes_nothing(tmp_path):
    repo = make_repo(tmp_path)
    up.main(["--repo", str(repo), "--dry-run"])
    assert all((repo / rel).read_text(encoding="utf-8") == txt for rel, txt in STUBS.items())


def test_reports_missing_anchor(tmp_path, capsys):
    repo = make_repo(tmp_path)
    (repo / "src/run_daily.py").write_text("def main():\n    pass\n", encoding="utf-8")
    assert up.main(["--repo", str(repo)]) == 1
    assert "MISSING" in capsys.readouterr().out


def test_patched_check_agent_enforces_caps_and_portfolio_limits(tmp_path):
    import sys, types
    from tools import risk_gate
    repo = make_repo(tmp_path)
    up.main(["--repo", str(repo)])
    ns = {}
    exec(compile((repo / "src/agents/check_agent.py").read_text(encoding="utf-8"), "check_agent", "exec"), ns)
    ctx = {"structured_risk": {"overrides": [{"ticker": "2001", "action": "REDUCE"}]},
           "event_flags": {"disposition_tickers": ["2002"], "ex_dividend_tickers": ["2003"]},
           "quant_gate": {"caps": {"2004": 0.05, "2330": 0.01}, "vol": {"2004": 80, "2005": 80},
                          "portfolio": {"max_weighted_vol20": 50.0, "unknown_vol": 60.0, "max_top5": 0.9}}}
    rm = ns["risk_maps"](ctx)
    assert rm["VETO"] == {"2002"} and rm["EXDIV"] == {"2003"} and rm["CAPS"] == {"2004": 0.05}      # 2330 never capped
    assert ns["risk_maps"](None)["CAPS"] == {}
    errs = ns["check_risk_overrides"]({"2004": 0.07, "2001": 0.06, "2330": 0.20, "2002": 0.03}, {"2002": 1}, rm)
    joined = "\n".join(errs)
    assert "2004 weight 7.00% exceeds" in joined and "2001 weight 6.00% exceeds" in joined
    assert "2002 is VETO" in joined and "2330 weight" not in joined
    assert "weighted 20d volatility" in joined
    assert ns["check_risk_overrides"]({"2004": 0.05, "2001": 0.04}, {}, {**rm, "PORT": None}) == []


def test_patched_weight_caps(tmp_path):
    repo = make_repo(tmp_path)
    up.main(["--repo", str(repo)])
    ca = types_module = __import__("types").SimpleNamespace(REDUCE_CAP=0.04)
    pkg = __import__("types").ModuleType("agents")
    pkg.check_agent = ca
    import sys
    sys.modules["agents"] = pkg
    ns = {"check_agent": ca}
    src = (repo / "src/agents/portfolio_agent.py").read_text(encoding="utf-8")
    fn = re.search(r"^def weight_caps.*?(?=^def build_shortlist)", src, re.M | re.S).group(0)
    exec("class Limits: ...\n" + fn, ns)

    class L:
        def cap(self, t):
            return 0.08
    caps = ns["weight_caps"](L(), {"REDUCE": {"2001", "2330"}, "CAPS": {"2001": 0.03, "2004": 0.05, "2330": 0.01}})
    assert caps == {"2001": 0.03, "2004": 0.05}
    assert ns["weight_caps"](L(), {"REDUCE": set()}) == {}          # old-style rm without CAPS still works
    sys.modules.pop("agents", None)
