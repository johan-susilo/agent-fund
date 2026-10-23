"""Step 5: 一鍵跑完每日流程：抓價 -> Agent -> 組 D-Plan。

用法:
    python src/run_daily.py 2026-10-23                  # 完整流程（會呼叫 Gemini）
    python src/run_daily.py 2026-10-23 --skip-fetch     # 沿用已抓好的 market_data.json
    python src/run_daily.py 2026-10-23 --replay logs/2026-10-23_attempt2.txt   # 不花 token

前提：你已從後台「文件繳交」下載資料包，放在 inputs/<date>/datapack.json
（資料包要登入後台才能下載，程式無法代抓）。
最後一步「上傳 + 送出繳交」仍須你自己在後台做，程式不會自動送出。
"""
import argparse
import json
import sys
import traceback
from datetime import date as Date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICE_TOL = 0.005        # 資料包收盤價 vs 抓到的收盤價，允許 0.5% 差距


def check_price_freshness(state, market):
    """持股的資料包收盤價應等於剛抓到的收盤價；差太多代表抓到的不是結算日的價格。"""
    bad = []
    for t in state["holdings"]:
        a, b = state["last_close"].get(t), market["prices"].get(t, {}).get("close")
        if a and b and abs(a - b) / a > PRICE_TOL:
            bad.append(f"{t}: 資料包 {a} vs 抓到 {b}")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("trade_date")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--replay")
    a = ap.parse_args()
    d = a.trade_date

    from datapack import load_datapack, DataPackError
    import agent
    import build_dplan

    try:
        dp_path = ROOT / "inputs" / d / "datapack.json"
        if not dp_path.exists():
            sys.exit(f"找不到 {dp_path}\n請先到後台「文件繳交」下載資料包並放到該路徑。")
        state = load_datapack(dp_path)                 # 結算未產生時會丟 DataPackError
        if state["trade_date"] != d:
            sys.exit(f"資料包的 trade_date 是 {state['trade_date']}，與參數 {d} 不符，請重新下載。")

        print(f"=== [1/4] 抓取收盤價 ===")
        if a.skip_fetch:
            print("略過（--skip-fetch）")
        else:
            import fetch_data
            as_of = state["as_of_date"] or (Date.fromisoformat(d) - timedelta(days=1)).isoformat()
            fetch_data.main(d, as_of)
        market = json.loads((ROOT / "inputs" / d / "market_data.json").read_text(encoding="utf-8"))

        print("\n=== [2/4] 價格與資料包一致性檢查 ===")
        bad = check_price_freshness(state, market)
        if bad:
            sys.exit("持股收盤價與資料包不一致，可能抓到非結算日的價格，已中止：\n  " + "\n  ".join(bad))
        print("通過" if state["holdings"] else "空手建倉日，無持股可比對（請自行抽查 2330 收盤價）")

        print("\n=== [3/4] Agent 決策 ===")
        ask = None
        if a.replay:
            raw = Path(a.replay).read_text(encoding="utf-8")
            ask = lambda _s, _u: (json.loads(raw), {"model": "replay", "input_tokens": 0, "output_tokens": 0}, raw)
        agent.print_result(agent.run_agent(d, ask=ask))

        print("\n=== [4/4] 組裝 D-Plan ===")
        out = build_dplan.build(d)

        print(f"\n🎉 完成：{out}")
        print("下一步（手動）：1) 後台上傳此檔，確認「收件層檢核全數通過」"
              " 2) 到「每日數據→當日委託」前先確認委託符合預期 3) 按「送出繳交」。")
        if a.replay:
            print("⚠️ 這是 replay 結果，不是今天真的 LLM 輸出，勿當正式繳交。")
    except DataPackError as e:
        sys.exit(f"資料包無法使用：{e}")
    except SystemExit:
        raise
    except Exception:
        print(traceback.format_exc())
        sys.exit("流程中止，不要繳交任何未完成的檔案。")


if __name__ == "__main__":
    main()