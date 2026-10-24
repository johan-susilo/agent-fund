# AI Agent Fund Manager

Every trading day it turns the competition portal's DataPack into a validated **D-Plan** JSON
(decision report + orders) for a virtual NT$1 billion Taiwan-equity portfolio.

**Strategy:** 主動AI半導體動能優選 (Active AI-Semiconductor Momentum Select). It invests in the Taiwan AI and
semiconductor supply chain and ranks stocks with a deterministic multi-factor model
(40% fundamentals / 30% momentum / 20% volatility and liquidity / 10% valuation). A set of LLM agents
adds macro, news and risk research, and a Portfolio Manager agent chooses the final weights. Code enforces
every rule. The full text fed to the LLM is in `config/strategy.json`.

> The program never submits anything. Uploading the file and pressing **Submit** in the portal stays manual.

---

## How it works

```
DataPack (portal download, manual)
        │
        ▼
 ┌──────────────┐  ┌────────────────────┐  ┌──────────────────────┐   run in parallel
 │ fetch_data   │  │ macro_agent        │  │ fundamental_agent    │
 │ TWSE/TPEx    │  │ FRED, Yahoo, News- │  │ Yahoo price factors, │
 │ closes       │  │ API, Finnhub       │  │ TWSE/TPEx/MOPS data  │
 │ (no LLM)     │  │ + LLM research loop│  │ + LLM research loop  │
 └──────┬───────┘  └─────────┬──────────┘  └──────────┬───────────┘
        └────────────────────┼────────────────────────┘
                             ▼
              EvidenceBook (S1.., O1..)  +  journal (yesterday's result, non-citable)
                             ▼
        tools/factors.py: factor table (score) + regime (risk_on / neutral / risk_off)
                             ▼
        risk_agent: exchange event scan (處置股, 除權息) + LLM tool loop -> VETO / REDUCE / WARN
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
|---|---|---|
| `fetch_data.py` | No | Previous-day closes for the 150 stocks from the TWSE and TPEx OpenAPIs. |
| `macro_agent` | Baseline: no. Research loop: yes | Baseline facts: FRED (US 10Y, VIX, USD/TWD, Nasdaq), Yahoo Finance (SOX, TSM ADR, NVDA, MU, VIX, TAIEX, USD/TWD, each with 1-day change and distance from the 20-day average), NewsAPI headlines, Finnhub high-impact calendar. Then an LLM loop (up to 8 steps / 10 calls, 5 observations) fills gaps with `get_price_data`, `web_search` (GDELT), `fetch_url`. |
| `fundamental_agent` | Baseline: no. Research loop: yes | Price factors for all 150 stocks (20/60-day momentum, 20-day volatility, 20-day traded value); official P/E, P/B, ROE, monthly-revenue YoY and MoM, net margin, institutional net buying; median monthly-revenue YoY of the core themes. Then an LLM loop (up to 10 steps / 12 calls, 4 observations). |
| `risk_agent` | Scan: no. Review: yes | Scans the TWSE and TPEx disposition and ex-dividend lists for the 150 stocks (disposition stocks are always VETO), then an LLM loop (up to 6 steps / 8 calls, 3 observations) checks export-control news, AI supply-chain shocks and extreme volatility or illiquidity. |
| `portfolio_agent` | Yes | Research phase plus one structured JSON decision call. See below. |
| `check_agent` | No | Pre-trade checks inside the retry loop, and the independent `final_gate`. |

All LLM calls go through `llm.py` (Vertex AI, `google-genai`, retry with model fallback) and tool loops through `agent_loop.py`.
`AGENT_AUTONOMY=0` switches off every LLM research step, the risk agent and the portfolio research phase.

### Factor model (`src/tools/factors.py`)

```
score = 0.4 * fundamentals + 0.3 * momentum + 0.2 * risk + 0.1 * valuation     (all percentile ranks)

fundamentals = mean( rank(ROE), 0.5*rank(monthly revenue YoY) + 0.5*rank(monthly revenue MoM), rank(net margin) )
momentum     = 0.35*rank(20d return) + 0.50*rank(60d return) + 0.15*rank(institutional net buy lots)
risk         = 0.5*rank(low 20d volatility) + 0.5*rank(20d traded value)
valuation    = rank(low P/E)
```

Outliers are clipped first (revenue YoY -80..200%, ROE -50..80%, P/E kept only inside 5..120).
A stock with missing price history has its score shrunk by `0.5 + 0.5 * coverage`.

### Regime (`detect_regime`)

One point for each of: VIX >= 23 (or >= 20.5 with a +12% jump); SOX <= -2% (or Nasdaq <= -1.8% with SOX down);
breadth < 25% advancers with more than 65% of core names negative on 20 days; SOX <= -10% or TAIEX <= -5% versus the 20-day average;
foreign futures net open interest <= -30,000 lots (see Known gaps).
If SOX closed up and the TSM ADR closed up at least 0.5%, two or more points are cut back to one.
Two or more points = `risk_off`; one point, weak breadth, more than half the core names negative on 20 days, or SOX <= -5% versus its average = `neutral`; otherwise `risk_on`.

The regime sets the cash window:

| Regime | Cash window |
|---|---|
| risk_on | 4% to 9% |
| neutral | 7% to 13% |
| risk_off | 12% to 20% |

### Portfolio construction

- **Shortlist:** current holdings plus the top 45 ranked, non-vetoed names (about 80% from core themes). New buys need 20-day average traded value of at least NT$200M. 2330 is always visible.
- **Baseline portfolio:** score-tilted starting weights (score^4, +core bonus, +holding bonus) with 25 names, 8% single-name cap and 20% for 2330. The LLM changes it only for a cited reason.
- **Risk manager enforcement:** VETO means never hold (exit if held). REDUCE caps the weight at 4%. WARN and EXDIV are advice only.
- **Research phase:** the LLM can call `web_search`, `fetch_url`, `get_price_data`, `get_fundamentals` and `validate_plan` before it writes the final JSON.
- **Code does the arithmetic:** the LLM supplies target weights only. `derive_orders.py` applies the official formula
  `floor(weight × prev NAV / prev close / 1000) × 1000`, a 1% no-trade band skips tiny rebalances, and one-way turnover is limited to 15% of NAV after day 1.
- **Auto-fixes that save a retry:** weight normalisation, widening the declared cash range to the projected cash, and correcting `net_exposure_intent` to the real net flow.
- **Fallback:** after `MAX_ATTEMPTS` failed LLM attempts, a deterministic score-based portfolio is built so a submission is never missed (`notes` contains `deterministic fallback used`).

### Design principles

- **Facts and inferences are separate.** Observations are only created by code and by fact tools (`fetch_url`, `get_price_data`, `get_fundamentals`), each with a source. The LLM never writes an observation. It reasons over them and must cite valid ids.
- **The LLM never computes shares.** See above.
- **Two layers of rule checks.** Internal limits are stricter than the official ones, and an independent final gate re-validates the finished file. A failing file is saved with a suffix that breaks the naming rule so it cannot be uploaded by accident.
- **One config object.** `settings.py` holds every limit, and the system prompt is rendered from the same `Limits` object.
- **Auditable.** Raw LLM replies go to `logs/`, and a code snapshot (`logs/code_<date>_<version>.zip`) is written for the post-qualification review. Prompts and `config/*.json` count as code, so editing them changes `code_version`.

---

## Competition rules enforced

| Rule | Official | Internal (normal) | Internal (STRICT*) |
|---|---|---|---|
| Holdings | 20–30 | 22–28 | 23–27 |
| 2330 weight cap | 25% | 22% | 20% |
| Other stock cap | 10% | 8.5% | 8% |
| Cash ratio | 0–25% | regime window (4–9 / 7–13 / 12–20%) | regime window** |
| Core AI/semi themes | strategy rule | >= 62% NAV | >= 62% NAV |
| Single theme | strategy rule | <= 30% NAV (semis <= 50%) | same |
| One-way daily turnover | none | <= 15% NAV after day 1 | same |
| Lot size | 1,000 shares | same | same |
| Fees / tax | 0.1425% / 0.3% (sell) | same | same |
| Active Share vs active ETFs | >= 15% | **not enforced** (see Known gaps) | — |

\* `STRICT` activates automatically when the DataPack shows 2 violations (3 = disqualification).
\*\* In `run_daily.py` the regime window always replaces the cash bounds, including in STRICT mode.

Core themes are `semis`, `semi_equip`, `servers`, `components`, `pcb` and `networking` (see `config/ticker_category.json`).

Other points the code handles: the 150-stock universe, the `net_exposure_intent` vs actual net flow check, the declared cash range vs projected cash,
the submission window (previous trading day 19:30 → trade date 08:55), no look-ahead in source timestamps, and a close-price freshness check against the DataPack.

---

## Repository layout

```
agent-fund/
├── .env                        # you create (never commit; see below)
├── requirements.txt
├── config/
│   ├── ticker_list.json        # the 150 allowed tickers (+ market: twse/tpex)
│   ├── ticker_category.json    # theme groups; `core: true` = AI/semiconductor themes
│   ├── strategy.json           # ETF name, theme, philosophy (fed to the LLM)
│   └── D-Plan.schema.json      # official schema 4.3
├── inputs/<trade_date>/
│   ├── datapack.json           # downloaded from the portal (manual)
│   ├── market_data.json        # created by fetch_data
│   ├── macro_fragment.json     # cached macro evidence (TTL 180 min)
│   ├── fundamental_fragment.json  # cached factor evidence (TTL 360 min)
│   └── raw/                    # raw API and web responses (hashed)
├── src/
│   ├── run_daily.py            # entry point: runs everything in order
│   ├── settings.py             # paths, official rules, internal limits (single source of truth)
│   ├── datapack.py             # DataPack → `state`
│   ├── fetch_data.py           # previous-day closes from TWSE / TPEx OpenAPI
│   ├── observations.py         # EvidenceBook: assigns S#/O# ids, enforces schema limits
│   ├── universe.py             # loads/validates the 150 stocks and themes
│   ├── derive_orders.py        # official order formula + post-trade projection
│   ├── build_dplan.py          # assembles the D-Plan JSON, code version, code snapshot
│   ├── llm.py                  # Vertex AI (google-genai): JSON-schema call, retry, model fallback
│   ├── agent_loop.py           # generic Gemini tool-calling loop used by every LLM agent
│   ├── util.py                 # time helpers, HTTP with retry, JSON cache
│   ├── agents/
│   │   ├── macro_agent.py      # FRED / Yahoo / NewsAPI / Finnhub facts + LLM research loop
│   │   ├── fundamental_agent.py# price factors + official fundamentals + LLM research loop
│   │   ├── risk_agent.py       # exchange event scan + LLM risk review
│   │   ├── portfolio_research.py # research phase with tools and validate_plan
│   │   ├── portfolio_agent.py  # shortlist, baseline, decision call, retry loop, fallback
│   │   └── check_agent.py      # rule engine: pre-trade checks and final_gate()
│   ├── prompts/
│   │   ├── system_prompt.md    # portfolio manager, rendered with {{PLACEHOLDERS}} from settings.Limits
│   │   ├── macro_agent.md      # macro research agent
│   │   ├── fundamental_agent.md# fundamental research agent
│   │   └── risk_agent.md       # risk manager agent
│   └── tools/                  # see "Status of src/tools" below
├── outputs/                    # D-Plan and agent_result files land here
└── logs/                       # raw LLM replies + code snapshots
```

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt`: `requests`, `python-dotenv`, `google-genai`, `yfinance`, `pandas`, `jsonschema`.
`yfinance` is imported at module level by `macro_agent.py`, so it is required. Without `jsonschema` the final gate skips schema validation with only a warning, so install it.
On Windows also install `tzdata`. The unused `tools/macro_news_collector.py` would need `feedparser`.

### `.env`

```ini
# LLM (Vertex AI)
GOOGLE_CLOUD_PROJECT=<your project id>
GOOGLE_CLOUD_LOCATION=global           # Gemini 3.x normally needs "global"
GEMINI_MODEL=<primary model>
GEMINI_FALLBACK_MODELS=<a,b>           # optional; default gemini-3.5-flash-lite,gemini-3.1-flash-lite; tried in order on HTTP 404
LLM_TEMPERATURE=                       # optional override; defaults 0.1 (JSON decision call) and 0.2 (tool loops)
LLM_SEED=42                            # optional

# Data sources (each is optional; a missing key just skips that source)
FRED_API_KEY=
NEWS_API_KEY=
FINNHUB_API_KEY=
ARCHIVE_NEWS=1                         # 0 = skip web.archive.org snapshots

# Agent behaviour (optional)
AGENT_AUTONOMY=1                       # 0 = no LLM research agents, no risk agent (also no 處置股 scan), no research phase
MAX_ATTEMPTS=3                         # LLM decision retries after a failed check
SHORTLIST_N=45                         # non-held names shown to the portfolio LLM
MIN_ADV20_TWD_M=200                    # liquidity floor for new buys (NT$ million per day)
FUNDAMENTAL_MAX=60                     # max stocks that get citable fundamental observations
```

`.env` is git-ignored. Do not commit API keys.

Test the LLM connection: `python src/llm.py`

---

## Daily workflow

1. **Download the DataPack** from the portal (Document Submission page) and save it as `inputs/<trade_date>/datapack.json`.
2. **Run the pipeline** (between 19:30 on the previous trading day and 08:55 on the trade date):

   ```bash
   python src/run_daily.py 2026-10-26                  # full run (calls the LLM)
   python src/run_daily.py 2026-10-26 --skip-fetch     # reuse inputs/<date>/market_data.json
   python src/run_daily.py 2026-10-26 --refresh        # ignore macro / factor caches
   python src/run_daily.py 2026-10-26 --no-external    # exchange data only (no macro / factor agents)
   python src/run_daily.py 2026-10-26 --force          # run outside the submission window
   python src/run_daily.py 2026-10-26 --replay logs/2026-10-26_attempt2.txt   # re-use a saved LLM reply
   ```

   Outside the window the run stops unless you pass `--force` (or `--replay`).
   `--replay` skips the LLM decision call and the research phase, but the macro, factor and risk agents still run unless you also pass `--no-external` and set `AGENT_AUTONOMY=0`.

3. **Check the result.** On success, `outputs/D-Plan_<team_id>_<trade_date>.json` is written.
   - `*.REJECTED.json` means the final gate failed. **Do not submit.**
   - `*.REPLAY.json` means the file came from `--replay`. **Do not submit.**
4. **Manual submission:** upload the file in the portal, confirm every intake check passes, review *Daily Data → Orders*, then press **Submit**.
   An upload without Submit does not count (max 25 submissions per day, and the last one before the deadline is used).

Individual steps can be run on their own:

```bash
python src/datapack.py inputs/2026-10-26/datapack.json              # DataPack summary
python src/fetch_data.py 2026-10-26 2026-10-23                      # trade_date as_of_date
python src/agents/macro_agent.py 2026-10-26                         # macro evidence only
```

---

## Outputs

| File | Meaning |
|---|---|
| `outputs/D-Plan_<team>_<date>.json` | Passed the final gate; ready to upload |
| `outputs/D-Plan_<team>_<date>.REJECTED.json` | Failed the gate; the errors are printed. Do not submit |
| `outputs/D-Plan_<team>_<date>.REPLAY.json` | Produced with `--replay`. Do not submit |
| `outputs/agent_result_<date>.json` | Full agent result: evidence, decisions, orders, projected portfolio, notes. Also read by the next day's journal (a timestamped copy `agent_result_<date>_<HHMM>.json` is kept too) |
| `logs/<date>_attempt<N>.txt` | Raw LLM reply per decision attempt |
| `logs/code_<date>_<version>.zip` | Code snapshot for the post-qualification audit |

---

## Status of `src/tools/`

Several modules are now part of the daily pipeline; the rest is a standalone research / backtest toolkit.

| Module | Status |
|---|---|
| `factors.py` | **Integrated.** Factor table, regime detection, price-factor maths. Used by `run_daily.py`, `fundamental_agent.py`, `web_tools.py`. |
| `web_tools.py` | **Integrated.** The LLM tools (`web_search`, `fetch_url`, `get_price_data`, `get_fundamentals`). `fetch_url` has an SSRF guard, a 600 KB cap and optional web.archive.org snapshots. |
| `macro_tools.py` (GDELT) | **Integrated.** Backs the `web_search` tool. |
| `portfolio.py` | **Integrated.** `build_target_weights` builds the baseline portfolio and the fallback portfolio. |
| `journal.py` | **Integrated.** `run_daily.py` adds yesterday's performance review (portfolio vs equal-weight benchmark of the 150 stocks, top and bottom contributors) to the portfolio prompt as **non-citable** context. It is not an observation. |
| `config.py` | Bridge from `settings.py` (limits, fees) to the standalone toolkit. |
| `backtest.py`, `indicators.py`, `data_collector.py`, `data_validator.py`, `costs.py`, `reporting.py`, `fundamentals.py`, `risk.py` | **Standalone.** Not imported by the pipeline. Several expect `config/universe.csv`, and their function defaults (8% cap, 8% cash) differ from `settings.py`. Live rule checks are done by `agents/check_agent.py` and `derive_orders.py`. |
| `cli.py`, `orchestrator.py` | Listed in earlier versions of this README; not in the snapshot reviewed for this update. Verify they still exist. |
| `macro_news_collector.py` | **Unfinished.** The file is truncated (`_fetch_rss` is defined twice) and unused; the macro agent fetches news itself via NewsAPI. |
| `financial_data_collector.py` | **Empty placeholder.** |

`src/agents/__init__.py` and `src/tools/__init__.py` exist (empty) and are required for the imports.

---

## Known gaps and risks

Review these before relying on the pipeline:

1. **Active Share rule is not enforced.** The official rule (competition rules §6.13) disqualifies a team that stays below 15% Active Share against any active ETF on the 2026-09-11 list for 2 consecutive days.
   `settings.ACTIVE_SHARE_FLOOR` and `check_agent.active_share()` exist, but nothing calls them and there is no ETF holdings data.
   Because the strategy is concentrated in 2330 and AI/semiconductor names, overlap with tech-themed active ETFs is plausible.
2. **Foreign futures open-interest signal is not wired in.** `macro_agent._taifex_noi` is never called and its endpoint is a placeholder, so `foreign_noi` is always 0 and that regime point never fires.
3. **Margin-trading data is fetched but unused.** `fundamental_agent` downloads the TWSE/TPEx margin datasets but has no parsing branch for them, so `margin_inc` stays empty and the "margin trap" penalty in the risk score never applies. Gross and operating margin are parsed but not scored. There is no earnings-estimate-revision factor; revisions can only come from the LLM's web research.
4. **Risk agent failure drops the 處置股 veto.** The disposition scan is part of `risk_agent.review`; if that function raises, or `AGENT_AUTONOMY=0`, `risk_ctx` is empty and no stock is vetoed.
5. **Prompt and code disagree on REDUCE.** `prompts/risk_agent.md` says cap at 3.0%; `check_agent.REDUCE_CAP` enforces 4.0%.
6. **STRICT cash bounds are overridden** by the regime window (see the rules table).
7. **Turnover limit is not in the final gate.** It is checked in the retry loop only; the deterministic fallback and `final_gate` do not enforce it.
8. **Fallback decisions are not LLM decisions.** When the fallback runs, `model_version` can read `fallback`. Competition rules §6.1 require decisions to come from the AI agent, so treat a fallback day as an exception to investigate, not a normal path.
9. **Source budget aborts the run.** More than 50 sources raises `EvidenceLimitError` (extra observations beyond 100 are only dropped). Heavy `fetch_url` use by the research agents can hit this.
10. **Stale exchange data only warns.** `fetch_data` prints a warning if the API date differs from the settlement date; only held stocks are protected by the close-price freshness check.
11. **Hard-coded strategy name.** `system_prompt.md`, `macro_agent.md`, `fundamental_agent.md` and `risk_agent.md` contain the ETF name. Change them together with `strategy.json` and the submitted strategy document.

---

## Safety notes

- Three warnings mean disqualification. Typical causes are a missed submission, cash above 25% or negative, holdings outside 20–30,
  a stock above its cap, or a ticker outside the 150-stock list. Never submit a `REJECTED` or `REPLAY` file.
- Orders are priced at the previous close, but fills happen at the same-day average price and NAV is marked at the close.
  That is why the internal limits carry a buffer.
- A missing price for any stock excludes it for the day.
- The first day builds the whole 20–30 stock portfolio from an empty account, so the declared intent must be `increase`.
- Editing `strategy.json`, prompts or any file under `src/` or `config/` changes `code_version`. Finish edits before the daily run so the code snapshot matches what produced the D-Plan.

---

## License / disclaimer

Competition code for a virtual-portfolio contest. It is not investment advice.