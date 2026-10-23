"""Step 3: Agent — 建立 observations、請 LLM 決定目標權重、用官方公式算委託、自我檢核。

LLM 只做三件事：研判市場、寫推論、給目標權重。
observations 由程式從真實行情產生（LLM 不能自己編事實）；
股數、BUY/SELL/ADD/TRIM、no_trade 都由程式推導。

用法:
    python src/agent.py 2026-10-23
輸出:
    outputs/agent_result_<date>.json   （下一步 build_dplan.py 會讀它）
    logs/<date>_attempt<N>.txt         （LLM 原始回覆）
"""
import json
import os
import re
import statistics
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from datapack import load_datapack
from derive_orders import derive_orders, preview_portfolio, target_shares

ROOT = Path(__file__).resolve().parent.parent
TW = timezone(timedelta(hours=8))
MAX_ATTEMPTS = 3

# ---- 給 LLM 的軟上限（比官方規定留緩衝，因為成交價是當日均價、不是前日收盤）----
MIN_N, MAX_N = 22, 28
CAP_2330, CAP_OTHER = 0.22, 0.085
HOLD_TOL = 0.015      # hold 的淨流向容差（官方 2%，留緩衝）

# ---- 動態載入主題分群 (Dynamic Theme Loading) ----
_cat_data = json.loads((ROOT / "config/ticker_category.json").read_text(encoding="utf-8"))

# 建立代號 (e.g., "cat_0", "cat_1") 讓程式好處理
THEMES = {f"cat_{i}": cat["tickers"] for i, cat in enumerate(_cat_data["categories"])}
THEME_LABEL = {f"cat_{i}": cat["name"] for i, cat in enumerate(_cat_data["categories"])}
TICKER_THEME = {t: th for th, ts in THEMES.items() for t in ts}

# 定義你的「核心主題」 (Core Themes) 
# 對應你的 JSON: cat_0(半導體), cat_1(設備), cat_2(伺服器), cat_3(零組件), cat_4(PCB)
CORE_THEMES = ("cat_0", "cat_1", "cat_2", "cat_3", "cat_4")
CORE_MIN = 0.62 # 你的核心主題權重加總必須大於 62%


# =============================================================== 載入
def load_allowed():
    cfg = json.loads((ROOT / "config/ticker_list.json").read_text(encoding="utf-8"))
    # ticker_list.json 裡混進了表頭 {"Ticker":"證券代號"}，只收 4 位數字
    return {str(x["Ticker"]).strip(): x["Name"] for x in cfg["tickers"]
            if re.fullmatch(r"\d{4}", str(x["Ticker"]).strip())}


def load_strategy():
    p = ROOT / "config/strategy.json"
    return json.loads(p.read_text(encoding="utf-8"))


# =============================================================== L0/L1：sources + observations
def build_sources(market):
    srcs = []
    for i, s in enumerate(market["sources"], 1):
        srcs.append({
            "source_id": f"S{i}",
            "authority": s["authority"],
            "name": s["name"],
            "url": s["url"],
            "content_as_of": s["content_as_of"],
            "fetched_at": s["fetched_at"],
        })
    return srcs


def _fmt_pct(x):
    return "n/a" if x is None else f"{x:+.2f}%"


def build_observations(market, state, allowed):
    """回傳 (observations, ticker->obs_id, universe)。全部由真實行情機械產生。"""
    prices = market["prices"]
    src_ids = ["S1", "S2"]                     # twse + tpex
    obs, tmap = [], {}

    def add(statement, values, topic):
        oid = f"O{len(obs) + 1}"
        obs.append({"obs_id": oid, "source_ref": src_ids, "topic": topic,
                    "statement": statement, "values": values})
        return oid

    # 1) 市場廣度（150 檔）
    chg = [p["change_pct"] for p in prices.values() if p.get("change_pct") is not None]
    up = sum(c > 0 for c in chg); dn = sum(c < 0 for c in chg); flat = len(chg) - up - dn
    add(f"150 檔標的中 {up} 檔上漲、{dn} 檔下跌、{flat} 檔平盤；漲跌幅中位數 {statistics.median(chg):+.2f}%、平均 {statistics.mean(chg):+.2f}%",
        {"advancers": up, "decliners": dn, "unchanged": flat,
         "median_chg_pct": round(statistics.median(chg), 2), "mean_chg_pct": round(statistics.mean(chg), 2)},
        "tw_breadth")

    # 2) 各主題等權平均
    for th, ts in THEMES.items():
        cs = [prices[t]["change_pct"] for t in ts
              if t in prices and prices[t].get("change_pct") is not None]
        if not cs:
            continue
        add(f"{THEME_LABEL[th]}籃子（{len(cs)} 檔等權）平均漲跌 {statistics.mean(cs):+.2f}%，{sum(c > 0 for c in cs)}/{len(cs)} 檔上漲",
            {"avg_chg_pct": round(statistics.mean(cs), 2), "n": len(cs),
             "up_count": sum(c > 0 for c in cs)}, f"theme_{th}")

    # 3) 個股：策略宇宙 + 目前持股
    universe = [t for t in TICKER_THEME if t in prices] + \
               [t for t in state["holdings"] if t not in TICKER_THEME and t in prices]
    for t in universe:
        p = prices[t]
        vals = {"close": p["close"]}
        if p.get("change_pct") is not None:
            vals["chg_pct"] = p["change_pct"]
        tmap[t] = add(f"{t} {p['name']} 收盤 {p['close']:g}，單日 {_fmt_pct(p.get('change_pct'))}",
                      vals, "stock_close")
    assert len(obs) <= 100, "observations 超過 schema 上限 100"
    return obs, tmap, universe


# =============================================================== LLM 輸入
def build_user_prompt(strategy, state, market, obs, tmap, universe, feedback=None):
    prices = market["prices"]
    nav = state["nav"]
    cur = []
    for t, sh in sorted(state["holdings"].items()):
        px = prices.get(t, {}).get("close") or state["last_close"].get(t)
        cur.append({"ticker": t, "shares": sh, "weight": round(sh * px / nav, 4) if px else None})
    payload = {
        "trade_date": state["trade_date"],
        "etf_strategy": strategy,
        "account": {"nav": nav, "cash": state["cash"], "cash_ratio": round(state["cash"] / nav, 4),
                    "holdings_count": len(state["holdings"]), "current_holdings": cur},
        "candidate_universe": [{"ticker": t, "name": prices[t]["name"],
                                "theme": TICKER_THEME.get(t, "other"), "obs_id": tmap[t]}
                               for t in universe],
        "observations": obs,
    }
    text = json.dumps(payload, ensure_ascii=False)
    if feedback:
        text += ("\n\n[上一次輸出未通過程式檢核，請修正後重新輸出完整 JSON]\n"
                 + "\n".join(f"- {m}" for m in feedback))
    return text


# =============================================================== LLM 輸出檢核 + 推導
def _norm_llm(out):
    """只留 schema 允許的欄位，補必填的 counter_evidence。"""
    mv = out.get("market_view") or {}
    infs = []
    for i in out.get("inferences") or []:
        infs.append({"inf_id": i.get("inf_id"), "premise_refs": i.get("premise_refs") or [],
                     "logic": i.get("logic", ""), "conclusion": (i.get("conclusion") or "")[:300],
                     "counter_evidence": i.get("counter_evidence"),
                     "confidence": i.get("confidence")})
    return mv, infs, out.get("portfolio") or [], out.get("exits") or []


def check_structure(mv, infs, portfolio, exits, obs_ids, state, universe, prices):
    errs = []
    inf_ids = {i["inf_id"] for i in infs}
    if [i["inf_id"] for i in infs] != [f"I{k}" for k in range(1, len(infs) + 1)]:
        errs.append("inf_id 必須是 I1、I2… 連續編號")
    if not infs:
        errs.append("inferences 不能為空")
    for i in infs:
        if len(i["logic"]) < 20:
            errs.append(f"{i['inf_id']} logic 少於 20 字")
        bad = [r for r in i["premise_refs"] if r not in obs_ids]
        if bad or not i["premise_refs"]:
            errs.append(f"{i['inf_id']} premise_refs 不存在或為空: {bad}")
    if not mv.get("basis_refs") or any(r not in obs_ids for r in mv["basis_refs"]):
        errs.append("market_view.basis_refs 為空或引用不存在的 obs")
    if len(mv.get("logic", "")) < 20:
        errs.append("market_view.logic 少於 20 字")
    if mv.get("regime") not in ("risk_on", "neutral", "risk_off"):
        errs.append("regime 值不合法")
    if mv.get("stance") not in ("aggressive", "neutral", "defensive"):
        errs.append("stance 值不合法")
    if mv.get("net_exposure_intent") not in ("increase", "hold", "reduce"):
        errs.append("net_exposure_intent 值不合法")
    r = mv.get("target_cash_pct_range")
    if not (isinstance(r, list) and len(r) == 2 and 0 <= r[0] <= r[1] <= 0.25):
        errs.append("target_cash_pct_range 必須是 [low, high]，0 ≤ low ≤ high ≤ 0.25")

    seen = set()
    for p in portfolio:
        t = p.get("ticker")
        if t in seen:
            errs.append(f"{t} 重複出現在 portfolio")
        seen.add(t)
        if t not in universe:
            errs.append(f"{t} 不在候選宇宙")
        w = p.get("target_weight")
        cap = CAP_2330 if t == "2330" else CAP_OTHER
        if not isinstance(w, (int, float)) or not (0 < w <= cap):
            errs.append(f"{t} target_weight={w} 不在 (0, {cap}]")
        if not p.get("inference_refs") or any(r_ not in inf_ids for r_ in p["inference_refs"]):
            errs.append(f"{t} inference_refs 為空或不存在")
    for e in exits:
        t = e.get("ticker")
        if t not in state["holdings"]:
            errs.append(f"exits 裡的 {t} 並非持股")
        if t in seen:
            errs.append(f"{t} 同時在 portfolio 與 exits")
        if not e.get("inference_refs") or any(r_ not in inf_ids for r_ in e["inference_refs"]):
            errs.append(f"exits {t} inference_refs 為空或不存在")
    missing = [t for t in state["holdings"] if t not in seen and t not in {e.get("ticker") for e in exits}]
    if missing:
        errs.append(f"持股 {missing} 沒有被交代（要放 portfolio 或 exits）")
    if not (MIN_N <= len(portfolio) <= MAX_N):
        errs.append(f"portfolio 共 {len(portfolio)} 檔，必須 {MIN_N}–{MAX_N} 檔")
    s = sum(p.get("target_weight", 0) for p in portfolio if isinstance(p.get("target_weight"), (int, float)))
    if not (0.90 <= s <= 0.96):
        errs.append(f"權重總和 {s:.3f}，必須在 0.90–0.96（現金 4–10%）")
    return errs


def derive_decisions(portfolio, exits, state, prices_close):
    """把目標權重換成 decisions / no_trade_decisions（action 由程式判斷）。"""
    decisions, no_trade = [], []
    n = 0
    for p in portfolio:
        t, w = p["ticker"], p["target_weight"]
        cur = state["holdings"].get(t, 0)
        tgt = target_shares(w, state["nav"], prices_close[t])
        if tgt == cur:
            no_trade.append({"ticker": t, "reason_refs": p["inference_refs"],
                             "reason": f"目標股數 {tgt:,} 與現持相同，維持不動"})
            continue
        n += 1
        action = "BUY" if cur == 0 else ("ADD" if tgt > cur else "TRIM")
        cap = CAP_2330 if t == "2330" else CAP_OTHER
        decisions.append({"decision_id": f"D{n}", "ticker": t, "action": action,
                          "target_weight": w, "inference_refs": p["inference_refs"],
                          "risk_check": f"目標權重 {w:.2%} ≤ 內部上限 {cap:.1%}（官方 {'25%' if t == '2330' else '10%'}）"})
    for e in exits:
        n += 1
        decisions.append({"decision_id": f"D{n}", "ticker": e["ticker"], "action": "SELL_ALL",
                          "target_weight": 0.0, "inference_refs": e["inference_refs"],
                          "risk_check": "出清，釋出資金"})
    return decisions, no_trade


def check_outcome(mv, report, problems):
    errs = list(problems)               # derive_orders.preview_portfolio 的官方規則檢查
    flow, cash = report["net_flow_pct_nav"], report["cash_ratio"]
    intent = mv["net_exposure_intent"]
    if intent == "increase" and not flow > 0:
        errs.append(f"宣告 increase 但淨流向 {flow:+.2%}（需淨買超）")
    if intent == "reduce" and not flow < 0:
        errs.append(f"宣告 reduce 但淨流向 {flow:+.2%}（需淨賣超）")
    if intent == "hold" and abs(flow) > HOLD_TOL:
        errs.append(f"宣告 hold 但淨流向 {flow:+.2%} 超過 ±{HOLD_TOL:.1%}")
    lo, hi = mv["target_cash_pct_range"]
    if not lo <= cash <= hi:
        errs.append(f"預估現金比 {cash:.2%} 不在宣告區間 [{lo:.2%}, {hi:.2%}]")
    return errs


def check_theme(report):
    core = sum(w for t, w in report["weights"].items() if TICKER_THEME.get(t) in CORE_THEMES)
    if core < CORE_MIN:
        return [f"AI/半導體/伺服器/PCB 主題合計 {core:.1%}，低於 {CORE_MIN:.0%}，與 ETF 宣告主題不符"]
    return []


# =============================================================== 主流程
def run_agent(trade_date, ask=None):
    started_at = datetime.now(TW).isoformat(timespec="seconds")
    in_dir = ROOT / "inputs" / trade_date
    state = load_datapack(in_dir / "datapack.json")
    market = json.loads((in_dir / "market_data.json").read_text(encoding="utf-8"))
    if market["missing"]:
        print(f"注意：缺價 {market['missing']}，這些股票會被排除")
    allowed = load_allowed()
    for t in TICKER_THEME:
        assert t in allowed, f"主題清單的 {t} 不在 150 檔名單"
    prices = {t: p for t, p in market["prices"].items() if t in allowed}
    prices_close = {t: p["close"] for t, p in prices.items()}
    for t in state["holdings"]:                       # 持股若查無新價，退回資料包的收盤價
        prices_close.setdefault(t, state["last_close"].get(t))
    market = {**market, "prices": prices}

    strategy = load_strategy()
    system_prompt = (ROOT / "src/prompts/system_prompt.md").read_text(encoding="utf-8")
    sources = build_sources(market)
    obs, tmap, universe = build_observations(market, state, allowed)
    obs_ids = {o["obs_id"] for o in obs}

    if ask is None:
        from llm import ask_json_meta as ask         # 延遲載入，方便離線測試
    (ROOT / "logs").mkdir(exist_ok=True)

    feedback, total_in, total_out = None, 0, 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        user = build_user_prompt(strategy, state, market, obs, tmap, universe, feedback)
        try:
            res = ask(system_prompt, user)
            if isinstance(res, dict):                 # 相容只回傳 dict 的舊版 llm.py
                out, raw = res, json.dumps(res, ensure_ascii=False)
                meta = {"model": os.environ.get("GEMINI_MODEL", "unknown"),
                        "input_tokens": 0, "output_tokens": 0}
            else:
                out, meta, raw = res
        except Exception as e:                        # JSON 壞掉 / API 錯誤
            feedback = [f"輸出無法解析或呼叫失敗：{type(e).__name__}: {str(e)[:200]}"]
            print(f"[attempt {attempt}] {feedback[0]}")
            print(traceback.format_exc())             # 顯示真正出錯的位置
            continue
        total_in += meta["input_tokens"]; total_out += meta["output_tokens"]
        (ROOT / "logs" / f"{trade_date}_attempt{attempt}.txt").write_text(raw, encoding="utf-8")

        mv, infs, portfolio, exits = _norm_llm(out)
        errs = check_structure(mv, infs, portfolio, exits, obs_ids, state, set(universe), prices)
        if not errs:
            decisions, no_trade = derive_decisions(portfolio, exits, state, prices_close)
            orders = derive_orders(decisions, state, prices_close)
            report, problems = preview_portfolio(orders, state, prices_close)
            errs = check_outcome(mv, report, problems) + check_theme(report)
        if errs:
            feedback = errs
            print(f"[attempt {attempt}] 未通過 {len(errs)} 項：")
            for m in errs:
                print("   -", m)
            continue

        result = {
            "trade_date": trade_date,
            "sources": sources,
            "observations": obs,
            "market_view": {
                "basis_refs": mv["basis_refs"], "logic": mv["logic"],
                "regime": mv["regime"], "stance": mv["stance"],
                "posture": {"net_exposure_intent": mv["net_exposure_intent"],
                            "target_cash_pct_range": mv["target_cash_pct_range"]},
                "counter_evidence": mv.get("counter_evidence"),
                **({"confidence": mv["confidence"]} if isinstance(mv.get("confidence"), (int, float)) else {}),
            },
            "inferences": [{k: v for k, v in i.items() if not (k == "confidence" and v is None)}
                           for i in infs],
            "decisions": decisions,
            "no_trade_decisions": no_trade,
            "orders": orders,
            "preview": report,
            "llm_meta": {"model": meta["model"], "input_tokens": total_in,
                         "output_tokens": total_out, "attempts": attempt},
            "run_started_at": started_at,
            "generated_at": datetime.now(TW).isoformat(timespec="seconds"),
        }
        out_path = ROOT / "outputs" / f"agent_result_{trade_date}.json"
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        return result

    raise RuntimeError(f"{MAX_ATTEMPTS} 次嘗試都未通過檢核；最後的問題：{feedback}。今天不要繳交未檢核的檔案")


def print_result(r):
    p = r["preview"]
    print(f"\n✅ 通過 | 嘗試 {r['llm_meta']['attempts']} 次 | 委託 {len(r['orders'])} 筆 "
          f"| 持股 {p['holdings_count']} 檔 | 現金比 {p['cash_ratio']:.2%} | 淨流向 {p['net_flow_pct_nav']:+.2%} NAV")
    mv = r["market_view"]
    print(f"市場研判 {mv['regime']} / {mv['stance']} / {mv['posture']['net_exposure_intent']} "
          f"現金區間 {mv['posture']['target_cash_pct_range']}")
    print("權重(前 8):", {t: f"{w:.1%}" for t, w in list(p["weights"].items())[:8]})
    core = sum(w for t, w in p["weights"].items() if TICKER_THEME.get(t) in CORE_THEMES)
    print(f"AI 主題合計 {core:.1%}")


if __name__ == "__main__":
    # python src/agent.py 2026-10-23                       -> 真的呼叫 Gemini
    # python src/agent.py 2026-10-23 --replay logs/xxx.txt -> 重播舊回覆，不花 token
    if "--replay" in sys.argv:
        raw = Path(sys.argv[sys.argv.index("--replay") + 1]).read_text(encoding="utf-8")

        def _replay(_sys, _user):
            return json.loads(raw), {"model": "replay", "input_tokens": 0, "output_tokens": 0}, raw
        print_result(run_agent(sys.argv[1], ask=_replay))
    else:
        print_result(run_agent(sys.argv[1]))