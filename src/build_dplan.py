"""Step 4: 把 agent_result 組成官方 D-Plan JSON（schema 4.3）。

用法:
    python src/build_dplan.py 2026-10-23
輸入:  outputs/agent_result_<date>.json、inputs/<date>/datapack.json
輸出:  outputs/D-Plan_<team_id>_<date>.json   （檔名規約：同日重繳用同一檔名）
       logs/code_<date>_<hash>.zip            （當日程式碼快照，晉級後資格審查用）
"""
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from datapack import load_datapack

ROOT = Path(__file__).resolve().parent.parent
TW = timezone(timedelta(hours=8))


def _cut(s, n):
    """schema 對字串有 maxLength，超過會整份退回，所以先截斷。"""
    if s is None:
        return None
    return s if len(s) <= n else s[: n - 1] + "…"


def code_files():
    files = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "src/prompts").glob("*.md"))
    files.append(ROOT / "config/strategy.json")      # 提示詞與參數也算程式碼的一部分
    return [f for f in files if f.exists()]


def code_version():
    """git 乾淨 -> git:<hash>；否則用程式碼+提示詞+策略檔的 sha256 前 12 碼。"""
    try:
        run = lambda *a: subprocess.run(a, cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        if not run("git", "status", "--porcelain"):
            return "git:" + run("git", "rev-parse", "--short", "HEAD")
    except Exception:
        pass
    h = hashlib.sha256()
    for f in code_files():
        h.update(f.name.encode()); h.update(f.read_bytes())
    return "sha256:" + h.hexdigest()[:12]


def snapshot_code(trade_date, version):
    out = ROOT / "logs" / f"code_{trade_date}_{version.split(':')[-1]}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in code_files():
            z.write(f, f.relative_to(ROOT))
    return out


def build(trade_date):
    state = load_datapack(ROOT / "inputs" / trade_date / "datapack.json")
    r = json.loads((ROOT / "outputs" / f"agent_result_{trade_date}.json").read_text(encoding="utf-8"))
    if r["trade_date"] != trade_date or state["trade_date"] != trade_date:
        raise ValueError("trade_date 不一致（agent_result / datapack / 參數）")

    version = code_version()
    mv = dict(r["market_view"])
    mv["logic"] = _cut(mv["logic"], 1000)
    mv["counter_evidence"] = _cut(mv.get("counter_evidence"), 500)

    inferences = []
    for i in r["inferences"]:
        i = dict(i)
        i["logic"] = _cut(i["logic"], 1000)
        i["counter_evidence"] = _cut(i.get("counter_evidence"), 500)   # 必填欄位，null 合法
        if "conclusion" in i:
            i["conclusion"] = _cut(i["conclusion"], 300)
        inferences.append(i)

    meta = r["llm_meta"]
    dplan = {
        "schema_version": "4.3",
        "doc_type": "D-Plan",
        "team_id": state["team_id"],
        "trade_date": trade_date,
        "sources": r["sources"],
        "observations": r["observations"],
        "market_view": mv,
        "inferences": inferences,
        "decisions": r["decisions"],
        "no_trade_decisions": r["no_trade_decisions"],
        "orders": [{**o, "derivation_detail": _cut(o.get("derivation_detail"), 300)} for o in r["orders"]],
        "agent_metadata": {
            "model_provider": "google",
            "model_version": _cut(os.environ.get("GEMINI_MODEL") or meta["model"], 60),
            "run_started_at": r["run_started_at"],
            "run_completed_at": r["generated_at"],      # 必須早於送出時間
            "input_tokens": meta["input_tokens"],
            "output_tokens": meta["output_tokens"],
            "tool_use_count": 0,
            "code_version": version,
        },
    }

    # 若已安裝 jsonschema 且 config/D-Plan.schema.json 有內容，就順便驗一次
    schema_path = ROOT / "config/D-Plan.schema.json"
    if schema_path.exists() and schema_path.stat().st_size > 0:
        try:
            import jsonschema
            jsonschema.validate(dplan, json.loads(schema_path.read_text(encoding="utf-8")))
            print("schema 驗證通過")
        except ImportError:
            print("（未安裝 jsonschema，略過；pip install jsonschema）")
    else:
        print("（config/D-Plan.schema.json 是空的，略過 schema 驗證）")

    out = ROOT / "outputs" / f"D-Plan_{state['team_id']}_{trade_date}.json"
    out.write_text(json.dumps(dplan, ensure_ascii=False, indent=2), encoding="utf-8")
    zp = snapshot_code(trade_date, version)
    print(f"已產生 {out.name}  | 委託 {len(dplan['orders'])} 筆 | code_version={version}")
    print(f"程式碼快照 {zp.name}")
    return out


if __name__ == "__main__":
    build(sys.argv[1])