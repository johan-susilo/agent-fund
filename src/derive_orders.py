"""Step 1b: 官方公式 — 目標權重 -> 委託股數，並預先檢查合規。

LLM 只負責給 target_weight；股數一律由這支程式算，不准手調。
"""
import math

MAX_WEIGHT_2330 = 0.25
MAX_WEIGHT_OTHER = 0.10
MIN_HOLDINGS, MAX_HOLDINGS = 20, 30
MAX_CASH_RATIO = 0.25


def target_shares(weight, nav, price):
    """目標股數 = floor(權重 × 前日淨值 ÷ 前日收盤價 ÷ 1000) × 1000"""
    return math.floor(weight * nav / price / 1000) * 1000


def derive_orders(decisions, state, prices):
    """decisions: [{"decision_id","ticker","action","target_weight"}, ...]
    prices: {"2330": 收盤價, ...}（交易所公布的前日收盤價）
    回傳 orders 清單（格式同 D-Plan 的 orders）
    """
    orders = []
    for d in decisions:
        t = d["ticker"]
        if t not in prices:
            raise ValueError(f"{t} 缺少前日收盤價")
        tgt = target_shares(d["target_weight"], state["nav"], prices[t])
        cur = state["holdings"].get(t, 0)
        diff = tgt - cur
        if diff == 0:
            raise ValueError(f"{d['decision_id']} {t}: 算出來不用交易，請改放 no_trade_decisions")
        side = "BUY" if diff > 0 else "SELL"
        if side == "SELL" and -diff > cur:
            raise ValueError(f"{t}: 賣出 {-diff} 超過持股 {cur}（超賣）")
        orders.append({
            "ticker": t,
            "side": side,
            "shares": abs(diff),
            "decision_ref": d["decision_id"],
            "derivation_detail": (
                f"target_shares = floor({d['target_weight']}×{state['nav']:,.0f} / "
                f"{prices[t]} / 1000)×1000 = {tgt:,}；現持 {cur:,} → {side} {abs(diff):,}"
            ),
        })
    return orders


def preview_portfolio(orders, state, prices):
    """用前日收盤價估算成交後的組合，回傳 (報告, 問題清單)。問題清單為空才可繳交。"""
    shares = dict(state["holdings"])
    cash = state["cash"]
    buy_amt = sell_amt = 0.0
    for o in orders:
        amt = o["shares"] * prices[o["ticker"]]
        if o["side"] == "BUY":
            shares[o["ticker"]] = shares.get(o["ticker"], 0) + o["shares"]
            cash -= amt * (1 + 0.001425)
            buy_amt += amt
        else:
            shares[o["ticker"]] -= o["shares"]
            cash += amt * (1 - 0.001425 - 0.003)
            sell_amt += amt
    shares = {k: v for k, v in shares.items() if v > 0}

    values = {t: s * prices[t] for t, s in shares.items()}
    nav = cash + sum(values.values())
    weights = {t: v / nav for t, v in values.items()}
    cash_ratio = cash / nav

    problems = []
    if not (MIN_HOLDINGS <= len(shares) <= MAX_HOLDINGS):
        problems.append(f"持股 {len(shares)} 檔，不在 20-30")
    if cash < 0:
        problems.append(f"現金為負 {cash:,.0f}")
    if cash_ratio > MAX_CASH_RATIO:
        problems.append(f"現金比 {cash_ratio:.1%} 超過 25%")
    for t, w in weights.items():
        cap = MAX_WEIGHT_2330 if t == "2330" else MAX_WEIGHT_OTHER
        if w > cap:
            problems.append(f"{t} 權重 {w:.2%} 超過上限 {cap:.0%}")

    report = {
        "holdings_count": len(shares),
        "cash": cash,
        "cash_ratio": cash_ratio,
        "buy_amt": buy_amt,
        "sell_amt": sell_amt,
        "net_flow_pct_nav": (buy_amt - sell_amt) / state["nav"],
        "weights": dict(sorted(weights.items(), key=lambda x: -x[1])),
    }
    return report, problems