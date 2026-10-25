# AI Agent Fund Manager

Every trading day it turns the competition portal's DataPack into a validated **D-Plan** JSON
(decision report + orders) for a virtual NT$1 billion Taiwan-equity portfolio.

**Strategy:** 主動AI半導體動能優選 (Active AI-Semiconductor Momentum Select). It holds the Taiwan AI and
semiconductor supply chain as the core (at least 62% of NAV) and adds lower-correlation defensive names
(mainly financials and telecom) to reduce maximum drawdown. A deterministic five-component score ranks the
150-stock universe, a set of LLM agents adds news, macro, technical and risk research, and a Portfolio Manager
agent chooses the final weights. Code derives every share count and enforces every rule.
The text fed to the LLM is in `config/strategy.json` (it must match the strategy document submitted to the portal).

> The program never submits anything. Uploading the file and pressing **Submit** in the portal stays manual.

---

## How it works

```
DataPack (portal download, manual)
        │
        ▼
 ┌────────────┐ ┌──────────────┐ ┌───────────────────┐ ┌────────────────────┐   run in parallel
 │ fetch_data │ │ macro_agent  │ │ fundamental_agent │ │ technical_agent    │
 │ TWSE/TPEx  │ │ FRED, Yahoo, │ │ Yahoo price       │ │ Yahoo OHLCV        │
 │ closes     │ │ NewsAPI,     │ │ factors, TWSE/    │ │ indicators, breadth│
 │ (no LLM)   │ │ Finnhub      │ │ TPEx/MOPS data    │ │ FinMind OI/margin  │
 └─────┬──────┘ └──────┬───────┘ └─────────┬─────────┘ │ + LLM research loop│
       │               │                   │           └─────────┬──────────┘
       └───────────────┴───────────────────┴─────────────────────┘
                                   ▼
        EvidenceBook (S1.., O1..)  +  journal (yesterday's result, non-citable)
                                   ▼
        scoring.py: 5-component score (config/scoring.json)  ->  detect_regime  ->  score again with macro + regime
                                   ▼
        evidence_agent: ONE LLM tool loop aimed at data gaps, event risk and missing macro items
                                   ▼
        risk_agent: exchange scan -> rule overrides -> portfolio risk facts -> stress facts -> LLM review
        risk_gate : quantitative VETO / REDUCE, volatility caps, portfolio limits (always runs)
                                   ▼
        portfolio_agent
          1. shortlist (~45 names + holdings) and a deterministic baseline portfolio
          2. research phase: LLM with tools and validate_plan (dry-run against the rule engine)
          3. decision call: LLM returns market view, inferences, target weights (JSON schema)
          4. code: normalise weights, classify BUY/ADD/TRIM/SELL_ALL/no-trade, derive share counts,
             run pre-trade checks; failures go back to the LLM (up to MAX_ATTEMPTS)
          5. if every attempt fails: deterministic score-based fallback portfolio
                                   ▼
                    build_dplan  (schema 4.3)
                                   ▼
              check_agent.final_gate  (schema + chain + orders + official limits)
                                   ▼
      outputs/D-Plan_<team>_<date>.json   (or .REJECTED.json / .REPLAY.json)
```

### The agents

| Agent | LLM? | What it does |
| --- | --- | --- |
| `fetch_data.py` | No  | Previous-day closes for the 150 stocks from the TWSE and TPEx OpenAPIs. |
| `macro_agent` | No (default) | FRED (US 10Y, VIX, USD/TWD, Nasdaq), Yahoo Finance (SOX, TSM ADR, NVDA, MU, VIX, TAIEX, USD/TWD, each with 1-day change and distance from the 20-day average), NewsAPI headlines, Finnhub high-impact calendar. Its old LLM research loop only runs with `EVIDENCE_MODE=legacy`. |
| `fundamental_agent` | No (default) | Price factors for all 150 stocks (20/60-day momentum, volatility, traded value, trend, drawdown, spread proxy); official TWSE/TPEx/MOPS P/E, P/B, ROE, monthly-revenue YoY and MoM, margins, institutional net buying; median monthly-revenue YoY of the core themes. Its old LLM loop only runs with `EVIDENCE_MODE=legacy`. |
| `technical_agent` | Baseline: no. Research loop: yes | One bulk Yahoo download (1 year) for all 150 stocks and TAIEX: RSI14, MACD, Bollinger %B, ATR, MA20/MA60 trend, 52-week drawdown, volume ratio, breakout/breakdown flags, a 0-100 timing `tech_score`, and market breadth. FinMind (needs `FINMIND_TOKEN`): foreign net open interest in TAIEX futures and 5-day margin-balance change for about 40 focus names. LLM loop (8 steps / 10 calls, 3 observations) checks flagged names; its note is non-citable. |
| `evidence_agent` | Yes | One LLM tool loop (10 steps / 12 calls, 6 observations) that replaces the former macro and fundamental research loops. It runs after the score exists, so it targets what the numbers cannot see: event risk on holdings and top names, estimate revisions, monthly-revenue surprises, missing macro items. Its text is a non-citable note. |
| `risk_agent` | Layers 1-4: no. Layer 5: yes | See "Risk layers" below. |
| `portfolio_agent` | Yes | Research phase plus one structured JSON decision call. See "Portfolio construction". |
| `check_agent` | No  | Pre-trade checks inside the retry loop, and the independent `final_gate`. |

All LLM calls go through `llm.py` (Vertex AI, `google-genai`, retry with model fallback, one client per thread) and tool loops through `agent_loop.py`.
LLM agents can call `web_search` (GDELT), `fetch_url` (SSRF guard, 600 KB cap, optional web.archive.org snapshot), `get_price_data`, `get_fundamentals`; the portfolio research phase also gets `validate_plan`.

### Scoring (`src/tools/scoring.py`, weights in `config/scoring.json`)

```
score = 0.35 momentum/trend + 0.25 risk/liquidity + 0.20 earnings/catalysts + 0.10 macro/regime + 0.10 valuation
```

| Component | Inputs (weight inside the component) |
| --- | --- |
| Momentum / trend (35%) | 20d return .20, 60d return .20, MA trend .20, strength vs own theme median .15, up/down volume .15, institutional net buying .10 |
| Risk / liquidity (25%) | low 20d volatility .25, low downside deviation .20, low 60d max drawdown .20, 20d traded value .20, low daily range (spread proxy) .15; margin-trap penalty 0.15 |
| Earnings / catalysts (20%) | monthly revenue YoY .30, MoM .15, ROE .15, net margin .10, earnings revision .15, revenue growth vs theme peers .15 |
| Macro / regime (10%) | market macro score (VIX level and trend, SOX trend, TSM ADR, US10Y move, TWD move, breadth, TAIEX trend, institutional flow) times a per-stock beta proxy, clamped by regime |
| Valuation guardrail (10%) | low P/E percentile .35, P/E relative to theme .35, room to fall from MA60 .30; x0.9 if P/E is above 2x theme median and price is more than 20% above MA60 |

Rules built into the scorer: every metric is a cross-sectional rank in [0, 1]; missing data is NaN, never 0;
a metric available for fewer than 30% of the universe is dropped for everyone; a component with under 50% coverage is neutral and flagged;
stale or future-dated price, fundamental and macro data is treated as missing (macro observations must be dated before 08:55 on the trade date and younger than 96 hours);
a stock with no price data is multiplied by 0.85.

### Regime (`detect_regime`)

One point for each of: VIX >= 23 (or >= 20.5 with a +12% jump); SOX <= -2% (or Nasdaq <= -1.8% with SOX down);
breadth < 25% advancers with more than 65% of core names negative on 20 days; SOX <= -10% or TAIEX <= -5% versus the 20-day average;
foreign futures net open interest <= -30,000 lots (from FinMind through the technical agent).
If SOX closed up and the TSM ADR closed up at least 0.5%, two or more points are cut back to one.
Two or more points = `risk_off`; one point, weak breadth, more than half the core names negative on 20 days, or SOX <= -5% versus its average = `neutral`; otherwise `risk_on`.

| Regime | Cash window |
| --- | --- |
| risk_on | 4% to 9% |
| neutral | 7% to 13% |
| risk_off | 12% to 20% |

### Risk layers

1. **Exchange events** (`risk_agent`, TWSE/TPEx OpenAPI): 處置股 -> VETO, 注意股 -> WARN, upcoming 除權息 -> EXDIV. Unreadable endpoints are reported in `failed_sources`, not read as empty lists.
2. **Rule overrides** (`risk_agent`, from the technical stash): 20d volatility above `RISK_VOL_REDUCE` (80%) or ATR above `RISK_ATR_REDUCE` (7%) -> REDUCE; a held stock above its liquidity limit `liq_w` -> REDUCE; breakdown, overbought (RSI > 75) or margin trap -> WARN. `liq_w = 10% x ADV20 x 3 days / NAV`.
3. **Portfolio risk facts** (market-level observations): 60-day VaR95, volatility, beta vs TAIEX, average correlation, effective number of holdings, 60-day max drawdown, top risk contributors. On day 1 it uses a hypothetical equal-weight book of the 20 best core candidates.
4. **Stress facts**: WTI, DXY, US10Y (Yahoo); HY credit spread and 10Y-2Y (FRED, needs `FRED_API_KEY`).
5. **LLM review**: up to 6 steps / 8 calls, 3 observations. It can add or escalate overrides, never downgrade.
6. **Quantitative gate** (`tools/risk_gate.py`, thresholds in `config/scoring.json`), independent of any score and run even when the LLM fails:
  - VETO if 20d average traded value is below NT$30M.
  - REDUCE if volatility > 70%, 60d max drawdown > 30%, daily range > 6%, traded value below NT$200M, or price data is missing.
  - Per-name cap = 8.5% x 45 / volatility (floor 4%), x0.75 if 60d drawdown > 25%.
  - Portfolio limits: weighted 20d volatility <= 62 / 58 / 52% (risk_on / neutral / risk_off), top-5 weight <= 62%.
  - 2330 is exempt from caps and REDUCE; only an exchange VETO can touch it.
  - If the risk agent produced no exchange flags, the gate re-runs the exchange scan.

Overrides are merged per ticker and the most severe action wins (VETO > REDUCE > WARN). REDUCE caps a weight at 4.0%.

### Portfolio construction

- **Shortlist:** current holdings plus the top 45 ranked, non-vetoed names (about 80% from core themes). New buys need 20-day average traded value of at least NT$200M. 2330 is always visible.
- **Baseline portfolio:** score-tilted starting weights (score^4, +core bonus, +holding bonus) with 25 names, 8% single-name cap and 20% for 2330. The LLM changes it only for a cited reason.
- **Candidate rows** carry factor fields, `cur_w`, `base_w`, `max_w`, `lot_w`, technical fields, `beta`, `liq_w`, observation ids and risk flags. Bulk per-stock observation text is hidden because its numbers are already in the rows.
- **Prompt guidance:** factor rank decides what to own, the technical overlay decides when and how much (no chasing RSI > 75 or d20 > +15), keep a few lower-correlation holdings (financials, telecom) for drawdown control.
- **Code does the arithmetic:** the LLM supplies target weights only. `derive_orders.py` applies the official formula
  `floor(weight x prev NAV / prev close / 1000) x 1000`, a 1% no-trade band skips tiny rebalances, and one-way turnover is limited to 15% of NAV after day 1.
- **Auto-fixes that save a retry:** weight normalisation, widening the declared cash range to the projected cash, correcting `net_exposure_intent` to the real net flow.
- **Fallback:** after `MAX_ATTEMPTS` failed LLM attempts a deterministic score-based portfolio is built (`notes` contains `deterministic fallback used`).

### Design principles

- **Facts and inferences are separate.** Observations are created only by code and by fact tools, each with a source. The LLM never writes an observation; it reasons over them and must cite valid ids. Free-text agent notes reach the portfolio manager as `desk_notes_non_citable`.
- **The LLM never computes shares.**
- **Two layers of rule checks.** Internal limits are stricter than the official ones, and an independent final gate re-validates the finished file. A failing file is saved with a suffix that breaks the naming rule so it cannot be uploaded by accident.
- **One config object.** `settings.py` holds the limits and the system prompt is rendered from the same `Limits` object; `config/scoring.json` holds the score and risk-gate parameters.
- **Auditable.** Raw LLM replies go to `logs/`, and a code snapshot (`logs/code_<date>_<version>.zip`) is written for the post-qualification review. Prompts and `config/*.json` count as code, so editing them changes `code_version`.

---

## Competition rules enforced

| Rule | Official | Internal (normal) | Internal (STRICT*) |
| --- | --- | --- | --- |
| Holdings | 20-30 | 22-28 | 23-27 |
| 2330 weight cap | 25% | 22% | 20% |
| Other stock cap | 10% | 8.5% | 8%  |
| Cash ratio | 0-25% | regime window (4-9 / 7-13 / 12-20%) | regime window** |
| Core AI/semi themes | strategy rule | >= 62% NAV | >= 62% NAV |
| Single theme | strategy rule | <= 30% NAV (semis <= 50%) | same |
| One-way daily turnover | none | <= 15% NAV after day 1 | same |
| Lot size | 1,000 shares | same | same |
| Fees / tax | 0.1425% / 0.3% (sell) | same | same |
| Active Share vs active ETFs | >= 15% | **not enforced** (see Known gaps) | -   |

\* `STRICT` activates automatically when the DataPack shows 2 violations (3 = disqualification).
\*\* In `run_daily.py` the regime window always replaces the cash bounds, including in STRICT mode.

Core themes: `semis`, `semi_equip`, `servers`, `components`, `pcb`, `networking`, `optoelectronics` (see `config/ticker_category.json`; note that `core: true` includes optoelectronics).
Also handled: the 150-stock universe, declared `net_exposure_intent` vs actual net flow, declared cash range vs projected cash, the submission window (previous trading day 19:30 to trade date 08:55), source timestamps (no look-ahead), and a close-price freshness check against the DataPack.

---

## Repository layout

```
agent-fund/
├── .env                          # you create (never commit; see below)
├── requirements.txt
├── config/
│   ├── ticker_list.json          # the 150 allowed tickers (+ market: twse/tpex)
│   ├── ticker_category.json      # theme groups; `core: true` = AI/semiconductor themes
│   ├── strategy.json             # ETF name, theme, philosophy (fed to the LLM)
│   ├── scoring.json              # score weights, validity ranges, hard risk gate thresholds
│   └── D-Plan.schema.json        # official schema 4.3
├── inputs/<trade_date>/          # datapack.json (manual), market_data.json, cached *_fragment.json, raw/
├── outputs/                      # D-Plan and agent_result files
├── logs/                         # raw LLM replies + code snapshots
├── src/
│   ├── run_daily.py              # entry point
│   ├── settings.py               # paths, official rules, internal limits (single source of truth)
│   ├── datapack.py  fetch_data.py  universe.py  observations.py
│   ├── derive_orders.py          # official order formula + post-trade projection
│   ├── build_dplan.py            # assembles the D-Plan, code version, code snapshot
│   ├── llm.py                    # Vertex AI JSON-schema call, retry, model fallback, one client per thread
│   ├── agent_loop.py             # generic Gemini tool-calling loop
│   ├── util.py
│   ├── agents/
│   │   ├── macro_agent.py  fundamental_agent.py  technical_agent.py
│   │   ├── evidence_agent.py     # merged research loop
│   │   ├── risk_agent.py
│   │   ├── portfolio_research.py  portfolio_agent.py
│   │   └── check_agent.py        # rule engine: pre-trade checks and final_gate()
│   ├── prompts/
│   │   ├── system_prompt.md      # portfolio manager ({{PLACEHOLDERS}} filled from settings.Limits)
│   │   ├── evidence_agent.md  technical_agent.md  risk_agent.md
│   │   └── macro_agent.md  fundamental_agent.md   # used only with EVIDENCE_MODE=legacy
│   └── tools/                    # see "Status of src/tools"
└── tests/                        # conftest, helpers, test_patcher, test_pipeline, test_risk_gate, test_scoring
```

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt`: `requests`, `python-dotenv`, `google-genai`, `yfinance`, `pandas`, `jsonschema`.
`yfinance` is imported at module level by `macro_agent.py`, so it is required. Without `jsonschema` the final gate skips schema validation with only a warning, so install it.
On Windows also install `tzdata`. To run `tests/`, install `pytest` (not in `requirements.txt`).

### `.env`

```ini
# LLM (Vertex AI)
GOOGLE_CLOUD_PROJECT=<your project id>
GOOGLE_CLOUD_LOCATION=global           # Gemini 3.x normally needs "global"
GEMINI_MODEL=<primary model>
GEMINI_FALLBACK_MODELS=<a,b>           # default gemini-3.5-flash-lite,gemini-3.1-flash-lite; tried in order on HTTP 404
LLM_TEMPERATURE=                       # optional; defaults 0.1 (JSON decision call) and 0.2 (tool loops)
LLM_SEED=42

# Data sources (each optional; a missing key just skips that source)
FRED_API_KEY=                          # US rates/VIX/Nasdaq/FX and the HY spread / 10Y-2Y stress facts
NEWS_API_KEY=
FINNHUB_API_KEY=
FINMIND_TOKEN=                         # free, https://finmindtrade.com; foreign futures OI + margin balances
ARCHIVE_NEWS=1                         # 0 = skip web.archive.org snapshots

# Agent behaviour (optional)
AGENT_AUTONOMY=1                       # 0 = no LLM research steps (evidence, technical, risk LLM, portfolio research phase)
EVIDENCE_MODE=merged                   # "legacy" restores the separate macro and fundamental research loops
EVIDENCE_MAX_STEPS=10
EVIDENCE_MAX_CALLS=12
EVIDENCE_OBS_CAP=6
MAX_ATTEMPTS=3                         # LLM decision retries after a failed check
SHORTLIST_N=45                         # non-held names shown to the portfolio LLM
MIN_ADV20_TWD_M=200                    # liquidity floor for new buys (NT$ million per day)
FUNDAMENTAL_MAX=60                     # stocks with citable fundamental observations
TECH_FOCUS=40                          # stocks with citable technical observations
TECH_MAX_STALE_DAYS=0                  # 0 = last Yahoo bar must be the settlement day
RISK_VOL_REDUCE=80                     # 20d volatility (%) above which -> REDUCE
RISK_ATR_REDUCE=7                      # ATR (% of price) above which -> REDUCE
```

`AGENT_AUTONOMY=0` does not stop the exchange scan, the rule-based overrides or the quantitative gate; only the LLM steps. The decision call itself still uses the LLM.
Test the LLM connection with `python src/llm.py`. Smoke-test the technical agent with `python src/agents/technical_agent.py <trade_date> <as_of_date>`.

---

## Daily workflow

1. **Download the DataPack** from the portal (Document Submission page) and save it as `inputs/<trade_date>/datapack.json`.
  
2. **Run the pipeline** (between 19:30 on the previous trading day and 08:55 on the trade date):
  
  ```bash
  python src/run_daily.py 2026-10-27                  # full run
  python src/run_daily.py 2026-10-27 --skip-fetch     # reuse inputs/<date>/market_data.json
  python src/run_daily.py 2026-10-27 --refresh        # ignore macro / factor / technical caches
  python src/run_daily.py 2026-10-27 --no-external    # exchange data only (no macro / factor / technical agents)
  python src/run_daily.py 2026-10-27 --force          # run outside the submission window
  python src/run_daily.py 2026-10-27 --replay logs/2026-10-27_attempt2.txt
  ```
  
  Outside the window the run stops unless you pass `--force` (or `--replay`).
  `--replay` reuses a saved decision reply and skips the portfolio research phase, but the evidence, technical and risk agents still call the LLM unless you also pass `--no-external` and set `AGENT_AUTONOMY=0`.
  Cache lifetimes: macro 180 min, factor 360 min, technical 360 min.
  
3. **Check the result.** On success `outputs/D-Plan_<team_id>_<trade_date>.json` is written.
  
  - `*.REJECTED.json` means the final gate failed. **Do not submit.**
  - `*.REPLAY.json` means the file came from `--replay`. **Do not submit.**
    Also read the console for `!! risk: exchange endpoints unreadable`, `technical:` warnings and `evidence budget full`.
4. **Manual submission:** upload in the portal, confirm every intake check passes, review *Daily Data > Orders*, then press **Submit**.
  An upload without Submit does not count (max 25 submissions per day; the last one before the deadline is used).
  

---

## Outputs

| File | Meaning |
| --- | --- |
| `outputs/D-Plan_<team>_<date>.json` | Passed the final gate; ready to upload |
| `outputs/D-Plan_<team>_<date>.REJECTED.json` | Failed the gate; errors are printed. Do not submit |
| `outputs/D-Plan_<team>_<date>.REPLAY.json` | Produced with `--replay`. Do not submit |
| `outputs/agent_result_<date>.json` | Full agent result (also read by the next day's journal); a timestamped copy `_<HHMM>` is kept too |
| `logs/<date>_attempt<N>.txt` | Raw LLM reply per decision attempt |
| `logs/code_<date>_<version>.zip` | Code snapshot for the audit |

### Reference run (2026-10-23 dry run, empty account)

Used to sanity-check the pipeline; numbers will differ in the competition.

- 28 sources and 75 observations (limits 50 and 100), 97 tool uses, about 244k input and 26k output tokens, model `gemini-3.8-flash`.
- Regime `neutral` and stance `defensive`, cash range 7-13%, intent `increase`.
  The regime stayed `neutral` through the SOX/TSM ADR override even though breadth was weak (31 of 150 up), foreign TX net OI was about -87k and SOX was about 11% below its 20-day average.
- 25 BUY orders, 25 holdings, target weights summing to 90% (cash about 10%), 2330 at 15%.
- Core themes about 71.5% of NAV; the rest was financials (7%), electronic distribution, telecom and shipping.
- The risk layer found 4979 on the TPEx disposition list (not held) and five names on the attention list.

---

## Status of `src/tools/`

| Module | Status |
| --- | --- |
| `scoring.py`, `factors.py` | **Integrated.** Five-component score, regime detection, price-factor maths. |
| `risk_gate.py` | **Integrated.** Quantitative VETO / REDUCE, volatility caps, portfolio limits. |
| `technicals.py`, `finmind.py` | **Integrated.** Indicator maths; FinMind helper (needs `FINMIND_TOKEN`). |
| `web_tools.py`, `macro_tools.py` (GDELT) | **Integrated.** LLM tools and the news search behind `web_search`. |
| `portfolio.py`, `journal.py` | **Integrated.** `build_target_weights` builds the baseline and fallback; the journal adds yesterday's realised result vs an equal-weight benchmark as **non-citable** context. |
| `config.py` | Bridge from `settings.py` to the standalone toolkit. |
| `backtest.py`, `indicators.py`, `data_collector.py`, `data_validator.py`, `costs.py`, `reporting.py`, `fundamentals.py`, `risk.py` | **Standalone**, not imported by the pipeline. `indicators.py` is an older cousin of `technicals.py`. Several expect `config/universe.csv`, and their function defaults differ from `settings.py`. |
| `macro_news_collector.py` | **Unfinished** (truncated, `_fetch_rss` defined twice). Unused. |
| `financial_data_collector.py` | **Empty placeholder.** |
| `macro_agent._taifex_noi` | **Dead code**, superseded by `finmind.foreign_futures_noi`. Safe to delete. |

---

## Known gaps and risks

1. **Active Share rule is not enforced.** Official rule section 6.13 disqualifies a team below 15% Active Share against any active ETF on the 2026-09-11 list for 2 consecutive days. `settings.ACTIVE_SHARE_FLOOR` and `check_agent.active_share()` exist but nothing calls them, and there is no ETF holdings data. A 2330-anchored AI/semiconductor book can overlap with tech-themed active ETFs.
2. **FinMind signals need `FINMIND_TOKEN`.** Without it the foreign-futures regime point and the margin-trap penalty switch off.
3. **Some score inputs have no data source.** `earnings_revision` is never filled, so its weight is dropped for every stock (revisions can only come from the evidence agent's web research). The TWSE/TPEx margin downloads in `fundamental_agent` are fetched but unused (margin data comes from FinMind for about 40 names). Gross and operating margin are parsed but not scored.
4. **The overnight-lead override can hide stress.** If SOX and TSM ADR close up, two or more bear points are cut to one, so the regime stays `neutral` even on weak breadth (see the reference run). That limits how often the cash window moves to 12-20%.
5. **STRICT cash bounds are overridden** by the regime window.
6. **Turnover limit is not in the final gate.** It is checked in the retry loop only; the deterministic fallback does not enforce it.
7. **Fallback decisions are not LLM decisions.** Competition rules section 6.1 require decisions to come from the AI agent. Treat a fallback day as an incident; `model_version` can read `fallback`.
8. **Source budget aborts the run.** More than 50 sources raises `EvidenceLimitError`; observations beyond 100 are dropped (last added first, so risk observations go first). The reference run used 28 and 75.
9. **Stale data only warns in some places.** `fetch_data` only warns when the exchange date differs from the settlement date; some FRED series lag (the USD/TWD series was dated 10-16 in the reference run). The scorer ignores macro data older than 96 hours.
10. **Hard-coded strategy name** in six prompts (`system_prompt.md`, `evidence_agent.md`, `technical_agent.md`, `risk_agent.md`, `macro_agent.md`, `fundamental_agent.md`). Change them together with `strategy.json` and the submitted strategy document.
11. **Unverified endpoint names.** The 注意股 endpoints and some FinMind field names were written from documentation. In the reference run the TPEx disposition and attention lists and FinMind worked; the TWSE lists produced no source, so check `failed_sources` on the first live day.
12. **Risk thresholds are opinionated.** The risk agent (80% volatility) and the quantitative gate (70%) cap many high-beta AI names at 4% or less. Watch that this does not starve the 62% core minimum.
13. **Yahoo is unofficial** and can rate-limit; several agents download in parallel. Caches in `inputs/<date>/` absorb reruns.
14. **`tests/` was not reviewed** for this update; run it before relying on the scoring or risk-gate changes.

---

## Safety notes

- Three warnings mean disqualification. Typical causes: a missed submission, cash above 25% or negative, holdings outside 20-30, a stock above its cap, or a ticker outside the 150-stock list. Never submit a `REJECTED` or `REPLAY` file.
- Orders are priced at the previous close, but fills happen at the same-day average price and NAV is marked at the close, so the internal limits carry a buffer.
- A missing price for any stock excludes it for the day.
- The first day builds the whole 20-30 stock portfolio from an empty account, so the declared intent must be `increase`.
- Editing `strategy.json`, `scoring.json`, prompts or any file under `src/` or `config/` changes `code_version`. Finish edits before the daily run so the snapshot matches what produced the D-Plan.

---

## License / disclaimer

Competition code for a virtual-portfolio contest. It is not investment advice.