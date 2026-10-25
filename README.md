# AI Agent Fund Manager

Every trading day it turns the competition portal's DataPack into a validated **D-Plan** JSON
(decision report + orders) for a virtual NT$1 billion Taiwan-equity portfolio.

**Strategy:** 主動AI半導體動能優選 (Active AI-Semiconductor Momentum Select). It invests in the Taiwan AI and
semiconductor supply chain and ranks stocks with a deterministic multi-factor model
(35% momentum/trend, 25% risk/liquidity, 20% earnings/catalysts, 10% macro/regime, 10% valuation). LLM agents add macro, news,
**technical** and **risk** research, and a Portfolio Manager agent chooses the final weights. Code enforces every rule.
The full text fed to the LLM is in `config/strategy.json`.

> 

---

## What changed in this update (technical + risk upgrade)

| Area                           | Change                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **New `technical_agent`**      | RSI14, MACD, Bollinger %B, ATR, MA20/60 trend, 52-week drawdown, volume ratio, breakout/breakdown flags and a 0-100 timing `tech_score` for all 150 stocks (Yahoo bars, deterministic), market breadth, then an LLM research loop that checks flagged names.                                                                                                                                                                                                                      |
| **New free data: FinMind**     | Foreign net open interest in TAIEX futures (finally feeds the `foreign_noi` regime point) and 5-day margin-balance change (finally feeds the margin-trap penalty). Needs `FINMIND_TOKEN`; optional.                                                                                                                                                                                                                                                                               |
| **`risk_agent` rewritten**     | Four layers that each survive the failure of the next: exchange scan (處置股 VETO, **注意股 WARN**, 除權息), **rule overrides** (volatility/ATR, liquidity limit, breakdown, overbought, margin trap), **portfolio risk facts** (VaR95, beta, correlation, effective N, max drawdown), **stress facts** (WTI, DXY, US10Y, HY credit spread, 10Y-2Y), then the LLM, which may add or escalate but never downgrade. Dead exchange endpoints are reported, not silently read as "empty list". |
| **Portfolio agent knows more** | Each candidate row now carries `tech`, `beta`, `liq_w`; the payload carries `portfolio_risk` and `desk_notes_non_citable` (free text from the macro, factor, technical and risk agents). The system prompt explains how to use them (timing/sizing overlay, no chasing RSI>75, respect `liq_w`).                                                                                                                                                                                  |
| **Regime**                     | `detect_regime` now also sees technical-agent facts, so the foreign-futures point can fire.                                                                                                                                                                                                                                                                                                                                                                                       |
| **LLM client**                 | One genai client per thread (`llm.py`). The old shared client caused the `client has been closed` / SSL errors seen in the macro and fundamental research agents.                                                                                                                                                                                                                                                                                                                 |
| **Fixed known gaps**           | #2 foreign futures OI, #3 margin data, #4 veto dropped when the risk agent failed, #5 REDUCE 3% vs 4% mismatch (all now 4.0%).                                                                                                                                                                                                                                                                                                                                                    |

The technical score is a **timing overlay**, not a fifth factor: the 40/30/20/10 score and the submitted strategy text are unchanged.

---

## How it works

```
DataPack (portal download, manual)
        │
        ▼
 ┌────────────┐ ┌──────────────┐ ┌───────────────────┐ ┌─────────────────────────┐  run in parallel
 │ fetch_data │ │ macro_agent  │ │ fundamental_agent │ │ technical_agent  (NEW)  │
 │ TWSE/TPEx  │ │ FRED, Yahoo, │ │ price factors,    │ │ Yahoo OHLCV indicators, │
 │ closes     │ │ NewsAPI,     │ │ TWSE/TPEx/MOPS    │ │ breadth, FinMind OI &   │
 │ (no LLM)   │ │ Finnhub + LLM│ │ + LLM loop        │ │ margin + LLM loop       │
 └─────┬──────┘ └──────┬───────┘ └─────────┬─────────┘ └────────────┬────────────┘
       └───────────────┴───────────────────┴────────────────────────┘
                                   ▼
        EvidenceBook (S1.., O1..)  +  journal (yesterday's result, non-citable)
                                   ▼
        tools/factors.py: factor table (score) + regime (risk_on / neutral / risk_off)
        (margin_inc from FinMind merged in; regime also reads technical facts)
                                   ▼
        risk_agent (REWRITTEN): exchange scan -> rule overrides -> portfolio risk facts
                    -> stress facts -> LLM add/escalate  => VETO / REDUCE / WARN + stock_risk
                                   ▼
        portfolio_agent: shortlist + baseline + tech/beta/liq_w per row + portfolio_risk + desk notes
          research phase (tools + validate_plan) -> decision call -> code derives orders -> checks / retries
          -> deterministic fallback if every attempt fails
                                   ▼
                    build_dplan (schema 4.3) -> check_agent.final_gate
                                   ▼
      outputs/D-Plan_<team>_<date>.json   (or .REJECTED.json / .REPLAY.json)
```

### The agents

| Agent                 | LLM?                             | What it does                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| --------------------- | -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fetch_data.py`       | No                               | Previous-day closes for the 150 stocks from the TWSE and TPEx OpenAPIs.                                                                                                                                                                                                                                                                                                                                                                                              |
| `macro_agent`         | Baseline: no. Research loop: yes | FRED (US 10Y, VIX, USD/TWD, Nasdaq), Yahoo (SOX, TSM ADR, NVDA, MU, VIX, TAIEX, USD/TWD), NewsAPI headlines, Finnhub calendar; LLM loop (8 steps / 10 calls, 5 obs) fills gaps.                                                                                                                                                                                                                                                                                      |
| `fundamental_agent`   | Baseline: no. Research loop: yes | 20/60-day momentum, volatility, traded value; official P/E, P/B, ROE, revenue YoY/MoM, margins, institutional net buying; core-theme revenue median; LLM loop (10 steps / 12 calls, 4 obs).                                                                                                                                                                                                                                                                          |
| **`technical_agent`** | Baseline: no. Research loop: yes | One bulk Yahoo download (1 year). Per stock: `tech_score`, RSI14, MACD histogram and cross, Bollinger %B, ATR%, distance to MA20/MA60, trend, 52-week drawdown, volume ratio, 20-day breakout/breakdown, gap. Market breadth observation. FinMind: foreign TX net OI and 5-day margin change (focus names). Stashes `technicals`, `returns60` (for risk maths) and `margin_inc`. LLM loop (8 steps / 10 calls, 3 obs) checks flagged names; its note is non-citable. |
| **`risk_agent`**      | Layers 1-4: no. Layer 5: yes     | See below.                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `portfolio_agent`     | Yes                              | Research phase plus one structured JSON decision call.                                                                                                                                                                                                                                                                                                                                                                                                               |
| `check_agent`         | No                               | Pre-trade checks inside the retry loop and the independent `final_gate`.                                                                                                                                                                                                                                                                                                                                                                                             |

All LLM calls go through `llm.py` (Vertex AI, `google-genai`, retry with model fallback, **one client per thread**) and tool loops through `agent_loop.py`.
`AGENT_AUTONOMY=0` switches off every LLM step (research loops, risk LLM, portfolio research phase). The exchange scan and rule-based risk overrides **still run**.

### Risk agent layers

1. **Exchange events** (TWSE/TPEx OpenAPI, keyless): 處置股 -> VETO, 注意股 -> WARN, 除權息 -> `EXDIV`. Endpoints that fail are listed in `failed_sources` and printed.
2. **Rule overrides** (from the technical stash): 20d volatility > `RISK_VOL_REDUCE` (80%) or ATR > `RISK_ATR_REDUCE` (7%) -> REDUCE (cap 4%); a held stock above its liquidity limit `liq_w` -> REDUCE; held stock with breakdown / below-MA60 downtrend, RSI > 75 (overbought) or margin balance +10% while under MA20 -> WARN. `liq_w = 10% x ADV20 x 3 days / NAV` (largest weight exitable in 3 days at 10% participation). 2330 is never REDUCE-capped.
3. **Portfolio risk facts** (market-level observations, topic `risk_portfolio`): 60-day historical-simulation VaR95 (% NAV), annualised volatility, beta vs TAIEX, average pairwise correlation, effective number of holdings, 60-day max drawdown, top-3 risk contributors. On day 1 (empty book) it is a hypothetical equal-weight book of the 20 best core candidates.
4. **Stress facts** (topics `risk_stress`, `risk_credit`): WTI crude, DXY, US10Y (Yahoo, keyless); HY credit spread and 10Y-2Y curve (FRED, needs `FRED_API_KEY`).
5. **LLM loop**: up to 6 steps / 8 calls, 3 observations. It can add or escalate overrides, never downgrade. Result is merged per ticker, most severe action wins.

### Factor model (`src/tools/factors.py`)

```
score = 0.4 * fundamentals + 0.3 * momentum + 0.2 * risk + 0.1 * valuation     (all percentile ranks)

fundamentals = mean( rank(ROE), 0.5*rank(monthly revenue YoY) + 0.5*rank(monthly revenue MoM), rank(net margin) )
momentum     = 0.35*rank(20d return) + 0.50*rank(60d return) + 0.15*rank(institutional net buy lots)
risk         = 0.5*rank(low 20d volatility) + 0.5*rank(20d traded value)   (- 0.3 margin trap)
valuation    = rank(low P/E)
```

Outliers are clipped first. A stock with missing price history has its score shrunk by `0.5 + 0.5 * coverage`.
**Margin trap** now works: `margin_inc` comes from the technical agent (FinMind), set only when the 5-day margin balance growth is at least 5%, and applies when 20-day momentum is negative.

### Technical score (`src/tools/technicals.py`, timing overlay, not part of the factor score)

`tech_score` (0-100) = trend 30 (close > MA20 > MA60) + RSI zone 20 (50-70 best) + MACD 20 (positive and rising best) + volume 10 + Bollinger %B 10 + proximity to 52-week high 10.
Flags: `overbought` (RSI>75), `oversold`, `breakout`/`breakdown` (vs prior 20-day high/low), `below_ma60`, `gap_up`/`gap_down` (>=3%), `high_atr` (>6%), `macd_golden_cross`/`macd_dead_cross`.

### Regime (`detect_regime`)

One point for each of: VIX >= 23 (or >= 20.5 with a +12% jump); SOX <= -2% (or Nasdaq <= -1.8% with SOX down);
breadth < 25% advancers with more than 65% of core names negative on 20 days; SOX <= -10% or TAIEX <= -5% versus the 20-day average;
foreign futures net open interest <= -30,000 lots (**now fed by FinMind through the technical agent**).
If SOX closed up and the TSM ADR closed up at least 0.5%, two or more points are cut back to one.
Two or more points = `risk_off`; one point, weak breadth, more than half the core names negative on 20 days, or SOX <= -5% versus its average = `neutral`; otherwise `risk_on`.

| Regime   | Cash window |
| -------- | ----------- |
| risk_on  | 4% to 9%    |
| neutral  | 7% to 13%   |
| risk_off | 12% to 20%  |

### Portfolio construction

- **Shortlist:** holdings plus the top 45 ranked, non-vetoed names (about 80% core). New buys need 20-day average traded value >= NT$200M. 2330 is always visible.
- **Candidate row** (what the PM sees): factor fields, `cur_w`, `base_w`, `max_w`, `lot_w`, **`tech{score,rsi,d20,d60,atr,vr,ddh,trend,flags}`, `beta`, `liq_w`**, up to 7 observation ids, risk flags.
- **Baseline portfolio:** score-tilted weights (score^4, +core bonus, +holding bonus), 25 names, 8% cap, 20% for 2330. The LLM changes it only for a cited reason.
- **Risk manager enforcement:** VETO never hold (exit if held); REDUCE caps at 4.0%; WARN and EXDIV are advice.
- **Desk notes:** the free-text notes of the macro, factor, technical and risk agents are passed as `desk_notes_non_citable`. They are inferences, never citable.
- **Code does the arithmetic:** the LLM supplies target weights only. `derive_orders.py` applies `floor(weight × prev NAV / prev close / 1000) × 1000`, a 1% no-trade band, and a 15% one-way turnover limit after day 1.
- **Fallback:** after `MAX_ATTEMPTS` failed LLM attempts a deterministic score-based portfolio is built (`notes` contains `deterministic fallback used`).

### Design principles

- **Facts and inferences are separate.** Observations are only created by code and fact tools, each with a source. The LLM never writes an observation. Technical, VaR, beta and stress numbers are arithmetic on cited Yahoo/FRED/FinMind data.
- **The LLM never computes shares.**
- **Two layers of rule checks.** Internal limits are stricter than official ones; an independent final gate re-validates the file.
- **One config object.** `settings.py` holds every limit; the system prompt is rendered from it.
- **Auditable.** Raw LLM replies go to `logs/`, a code snapshot to `logs/code_<date>_<version>.zip`. Prompts and `config/*.json` count as code.

---

## Competition rules enforced

| Rule                        | Official              | Internal (normal)                   | Internal (STRICT*) |
| --------------------------- | --------------------- | ----------------------------------- | ------------------ |
| Holdings                    | 20–30                 | 22–28                               | 23–27              |
| 2330 weight cap             | 25%                   | 22%                                 | 20%                |
| Other stock cap             | 10%                   | 8.5%                                | 8%                 |
| Cash ratio                  | 0–25%                 | regime window (4–9 / 7–13 / 12–20%) | regime window**    |
| Core AI/semi themes         | strategy rule         | >= 62% NAV                          | >= 62% NAV         |
| Single theme                | strategy rule         | <= 30% NAV (semis <= 50%)           | same               |
| One-way daily turnover      | none                  | <= 15% NAV after day 1              | same               |
| Lot size                    | 1,000 shares          | same                                | same               |
| Fees / tax                  | 0.1425% / 0.3% (sell) | same                                | same               |
| Active Share vs active ETFs | >= 15%                | **not enforced** (see Known gaps)   | —                  |

\* `STRICT` activates automatically when the DataPack shows 2 violations (3 = disqualification).
\*\* In `run_daily.py` the regime window always replaces the cash bounds, including in STRICT mode.

Core themes: `semis`, `semi_equip`, `servers`, `components`, `pcb`, `networking`, `optoelectronics` (see `config/ticker_category.json`).

---

## Repository layout

```
agent-fund/
├── .env                        # you create (never commit)
├── .env.example                # template, includes the new variables
├── requirements.txt            # unchanged: no new packages
├── config/                     # ticker_list.json, ticker_category.json, strategy.json, D-Plan.schema.json
├── inputs/<trade_date>/
│   ├── datapack.json  market_data.json
│   ├── macro_fragment.json  fundamental_fragment.json
│   ├── technical_fragment.json   # NEW cache (TTL 360 min)
│   └── raw/
├── src/
│   ├── run_daily.py  settings.py  datapack.py  fetch_data.py  observations.py  universe.py
│   ├── derive_orders.py  build_dplan.py  llm.py  agent_loop.py  util.py
│   ├── agents/
│   │   ├── macro_agent.py  fundamental_agent.py
│   │   ├── technical_agent.py   # NEW
│   │   ├── risk_agent.py        # REWRITTEN
│   │   ├── portfolio_research.py  portfolio_agent.py  check_agent.py
│   ├── prompts/
│   │   ├── system_prompt.md  macro_agent.md  fundamental_agent.md
│   │   ├── technical_agent.md   # NEW
│   │   └── risk_agent.md        # REWRITTEN
│   └── tools/
│       ├── technicals.py        # NEW: indicator maths
│       ├── finmind.py           # NEW: FinMind helper
│       └── (factors, web_tools, macro_tools, portfolio, journal, ... see below)
├── outputs/   logs/
```

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` is unchanged (`requests`, `python-dotenv`, `google-genai`, `yfinance`, `pandas`, `jsonschema`; numpy comes with pandas). On Windows also install `tzdata`.

### `.env`

```ini
# LLM (Vertex AI)
GOOGLE_CLOUD_PROJECT=<your project id>
GOOGLE_CLOUD_LOCATION=global
GEMINI_MODEL=<primary model>
GEMINI_FALLBACK_MODELS=<a,b>
LLM_TEMPERATURE=
LLM_SEED=42

# Data sources (each optional; a missing key just skips that source)
FRED_API_KEY=            # US rates/VIX/Nasdaq/FX; now also HY credit spread and 10Y-2Y
NEWS_API_KEY=
FINNHUB_API_KEY=
FINMIND_TOKEN=           # NEW. Free, https://finmindtrade.com. Foreign futures OI + margin balances
ARCHIVE_NEWS=1

# Agent behaviour (optional)
AGENT_AUTONOMY=1         # 0 = no LLM research steps; exchange scan + rule-based risk still run
MAX_ATTEMPTS=3
SHORTLIST_N=45
MIN_ADV20_TWD_M=200
FUNDAMENTAL_MAX=60
TECH_FOCUS=40            # NEW: stocks with citable technical observations
TECH_MAX_STALE_DAYS=0    # NEW: 0 = last Yahoo bar must equal the settlement day
RISK_VOL_REDUCE=80       # NEW: volatility % above which REDUCE
RISK_ATR_REDUCE=7        # NEW: ATR % above which REDUCE
```

Free sources used and their limits: TWSE/TPEx OpenAPI (keyless), Yahoo via `yfinance` (keyless, unofficial), FRED (free key), FinMind (free token, roughly 600 requests/hour; the agent uses about 45 per run), GDELT (keyless).

Test the LLM connection: `python src/llm.py`. Smoke-test the technical agent: `python src/agents/technical_agent.py <trade_date> <as_of_date>`.

---

## Daily workflow

1. **Download the DataPack** and save it as `inputs/<trade_date>/datapack.json`.

2. **Run the pipeline** (between 19:30 on the previous trading day and 08:55 on the trade date):
   
   ```bash
   python src/run_daily.py 2026-10-26                  # full run
   python src/run_daily.py 2026-10-26 --skip-fetch
   python src/run_daily.py 2026-10-26 --refresh        # ignore macro / factor / technical caches
   python src/run_daily.py 2026-10-26 --no-external    # exchange data only (no macro / factor / technical agents)
   python src/run_daily.py 2026-10-26 --force          # run outside the submission window
   python src/run_daily.py 2026-10-26 --replay logs/2026-10-26_attempt2.txt
   ```

3. **Check the result.** `outputs/D-Plan_<team_id>_<trade_date>.json` on success; `*.REJECTED.json` = gate failed, **do not submit**; `*.REPLAY.json` = replay, **do not submit**.

4. **Manual submission:** upload in the portal, confirm every intake check, review *Daily Data → Orders*, press **Submit**.

Check the console for `!! risk: exchange endpoints unreadable` and for `technical:` warnings before submitting.

---

## Outputs

| File                                         | Meaning                                                 |
| -------------------------------------------- | ------------------------------------------------------- |
| `outputs/D-Plan_<team>_<date>.json`          | Passed the final gate; ready to upload                  |
| `outputs/D-Plan_<team>_<date>.REJECTED.json` | Failed the gate. Do not submit                          |
| `outputs/D-Plan_<team>_<date>.REPLAY.json`   | Produced with `--replay`. Do not submit                 |
| `outputs/agent_result_<date>.json`           | Full agent result (also read by the next day's journal) |
| `logs/<date>_attempt<N>.txt`                 | Raw LLM reply per decision attempt                      |
| `logs/code_<date>_<version>.zip`             | Code snapshot for the audit                             |

---

## Status of `src/tools/`

| Module                                                                                                                             | Status                                                                                                        |
| ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `factors.py`, `web_tools.py`, `macro_tools.py` (GDELT), `portfolio.py`, `journal.py`                                               | **Integrated.**                                                                                               |
| `technicals.py`, `finmind.py`                                                                                                      | **Integrated (new).**                                                                                         |
| `config.py`                                                                                                                        | Bridge from `settings.py` to the standalone toolkit.                                                          |
| `backtest.py`, `indicators.py`, `data_collector.py`, `data_validator.py`, `costs.py`, `reporting.py`, `fundamentals.py`, `risk.py` | **Standalone**, not imported by the pipeline. `indicators.py` is an older, simpler cousin of `technicals.py`. |
| `macro_news_collector.py`                                                                                                          | **Unfinished** (truncated, `_fetch_rss` defined twice). Unused.                                               |
| `financial_data_collector.py`                                                                                                      | Empty placeholder.                                                                                            |
| `macro_agent._taifex_noi`                                                                                                          | **Dead code**, superseded by `finmind.foreign_futures_noi`. Safe to delete.                                   |

---

## Known gaps and risks

1. **Active Share rule is not enforced.** Official rule §6.13 disqualifies a team below 15% Active Share vs any active ETF (2026-09-11 list) for 2 consecutive days. `check_agent.active_share()` exists but nothing calls it and there is no ETF holdings data. A concentrated 2330/AI-semis book can overlap with tech-themed active ETFs.
2. ~~Foreign futures OI not wired~~ **Fixed** via FinMind, but only when `FINMIND_TOKEN` is set. Without it that regime point is off.
3. ~~Margin data unused~~ **Fixed** for the ~40 holdings/focus names (FinMind). The TWSE/TPEx margin downloads in `fundamental_agent` are still fetched and unused.
4. ~~Risk failure drops the veto~~ **Fixed**: exchange scan and rule overrides always run, and an LLM failure keeps them. Residual risk: if the exchange endpoints are dead the lists are UNKNOWN (reported in `failed_sources`).
5. ~~REDUCE 3% vs 4%~~ **Fixed**: prompt and code both say 4.0%.
6. **STRICT cash bounds are overridden** by the regime window.
7. **Turnover limit is not in the final gate.** Retry loop only.
8. **Fallback decisions are not LLM decisions.** Rules §6.1 require agent decisions; treat a fallback day as an incident.
9. **Source budget aborts the run.** More than 50 sources raises `EvidenceLimitError`; observations beyond 100 are dropped (last added first, so risk observations go first). The update adds about 8-10 observations and 2-3 sources; watch the `evidence budget full` message.
10. **Stale exchange data only warns** in `fetch_data`. The technical agent is stricter: with `TECH_MAX_STALE_DAYS=0` a stock whose last Yahoo bar is not the settlement day gets no indicators. If Yahoo is late, expect `no technical indicators available` and the pipeline continues without them.
11. **Hard-coded strategy name** in four prompts plus the new `technical_agent.md`; change them together with `strategy.json` and the submitted strategy document.
12. **Unverified endpoint names.** The 注意股 endpoints (`announcement/notice`, `tpex_trading_warning_information`) and the FinMind field names (`long_open_interest_balance_volume`, `MarginPurchaseTodayBalance`) were written from documentation memory. Check them on the first run: failures show up as `failed_sources` / warnings, never as a crash.
13. **Rule overrides are opinionated.** With the default 80% volatility threshold several high-beta AI names are capped at 4%. Tune `RISK_VOL_REDUCE` and watch whether it starves the core-theme minimum (62%).
14. **Yahoo is unofficial** and can rate-limit; four agents now download in parallel. Caches (`inputs/<date>/*_fragment.json`) absorb reruns.

---

## Safety notes

- Three warnings mean disqualification: missed submission, cash above 25% or negative, holdings outside 20–30, a stock above its cap, or a ticker outside the 150 list. Never submit a `REJECTED` or `REPLAY` file.
- Orders are priced at the previous close, fills happen at the same-day average price and NAV is marked at the close; internal limits carry a buffer.
- A missing price for any stock excludes it for the day.
- The first day builds the whole 20–30 stock portfolio from an empty account; declared intent must be `increase`.
- Editing prompts or any file under `src/` or `config/` changes `code_version`. Finish edits before the daily run so the snapshot matches what produced the D-Plan.

---

## License / disclaimer

Competition code for a virtual-portfolio contest. It is not investment advice.
