"""Step 4: assemble the official D-Plan JSON (schema 4.3) from the agent result.

Everything here is mechanical: trimming strings to schema limits, filling agent_metadata,
computing a code version and snapshotting the code for the post-qualification audit.
"""
from __future__ import annotations

import hashlib
import subprocess
import zipfile

from settings import ROOT, SCHEMA_VERSION
from util import cut, iso, now_tw


def code_files() -> list:
    """Prompts, parameters and config count as code: changing them is a new version."""
    files = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "src/prompts").glob("*.md")) \
        + sorted((ROOT / "config").glob("*.json"))
    return [f for f in files if f.is_file()]


def code_version() -> str:
    """Always use the Git commit hash for GitHub traceability (schema compliant)."""
    try:
        def run(*a):
            return subprocess.run(a, cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    
        git_hash = run("git", "rev-parse", "--short=8", "HEAD")
        if git_hash:
            return f"git:{git_hash}"
            
    except Exception:
        pass
    
    # Fallback ONLY if the environment has no .git folder at all
    h = hashlib.sha256()
    for f in code_files():
        h.update(str(f.relative_to(ROOT)).encode())
        h.update(f.read_bytes())
    return "sha256:" + h.hexdigest()[:12]

def snapshot_code(trade_date: str, version: str):
    out = ROOT / "logs" / f"code_{trade_date}_{version.split(':')[-1]}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in code_files():
            z.write(f, f.relative_to(ROOT))
    return out


def build(result: dict, state: dict, tool_calls: int) -> dict:
    """Return the D-Plan dict (not yet written to disk)."""
    if result["trade_date"] != state["trade_date"]:
        raise ValueError("trade_date mismatch between agent result and DataPack")

    mv = dict(result["market_view"])
    mv["logic"], mv["counter_evidence"] = cut(mv["logic"], 1000), cut(mv.get("counter_evidence"), 500)
    mv["basis_refs"] = mv.get("basis_refs", [])[:10]

    inferences = []
    for i in result["inferences"]:
        i = dict(i)
        i["logic"], i["counter_evidence"] = cut(i["logic"], 1000), cut(i.get("counter_evidence"), 500)
        if "conclusion" in i:
            i["conclusion"] = cut(i["conclusion"], 300)
        inferences.append(i)

    meta = result["llm_meta"]
    return {
        "schema_version": SCHEMA_VERSION,
        "doc_type": "D-Plan",
        "team_id": state["team_id"],
        "trade_date": state["trade_date"],
        "sources": result["evidence"]["sources"],
        "observations": result["evidence"]["observations"],
        "market_view": mv,
        "inferences": inferences,
        "decisions": result["decisions"],
        "no_trade_decisions": result["no_trade_decisions"],
        "orders": [{**o, "derivation_detail": cut(o.get("derivation_detail"), 300)} for o in result["orders"]],
        "agent_metadata": {
            "model_provider": "google",
            # The model actually used (after any fallback), so a sampled re-run uses the same version.
            "model_version": cut("+".join(meta["models"]), 60),
            "run_started_at": result["run_started_at"],
            "run_completed_at": iso(now_tw()),       # must be <= the server's receipt time
            "input_tokens": meta["input_tokens"],
            "output_tokens": meta["output_tokens"],
            "tool_use_count": tool_calls,
            "code_version": code_version(),
        },
    }
