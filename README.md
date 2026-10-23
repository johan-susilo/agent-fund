# AI Agent Fund Manager

Every trading day it turns the competition portal's DataPack into a validated **D-Plan** JSON
(decision report + orders) for a virtual NT$1 billion Taiwan-equity portfolio.

**Strategy:** 主動AI半導體動能優選 (Active AI-Semiconductor Momentum Select). It focuses on the Taiwan AI and
semiconductor supply chain and combines profit quality, 20/60-day price momentum, volatility,
liquidity and macro risk control. The full text is in `config/strategy.json`.

> The program never submits anything. Uploading the file and pressing **Submit** in the portal stays manual.

---

## How it works

```
DataPack (portal download)
        │
        ▼
 ┌──────────────┐   ┌───────────────┐   ┌──────────────────┐
 │ fetch_data   │   │ macro_agent   │   │ fundamental_agent│   run in parallel, no LLM
 │ TWSE/TPEx    │   │ FRED, NewsAPI,│   │ Yahoo Finance:   │
 │ closes       │   │ Finnhub       │   │ momentum, ROE... │
 └──────┬───────┘   └──────┬────────┘   └────────┬─────────┘
        └──────────────────┼─────────────────────┘
                           ▼
                  EvidenceBook (S1.., O1..)  + journal (yesterday's performance)
                           ▼
              portfolio_agent  ◄── the only LLM step (Gemini on Vertex AI)
              LLM returns: market view, inferences, target weights
              code does:   normalise weights, classify BUY/ADD/TRIM/SELL_ALL,
                           derive share counts, run pre-trade checks (retry loop)
                           ▼
                    build_dplan  (schema 4.3)
                           ▼
                check_agent.final_gate  (schema + chain + orders + official limits)
                           ▼
        outputs/D-Plan_<team>_<date>.json   (or .REJECTED.json / .REPLAY.json)
```

### Design principles

- **Facts and inferences are separate.** Agents produce sourced observations (facts) with no LLM.
  The LLM only reasons over them and must cite valid observation IDs.
- **The LLM never computes shares.** It supplies target weights; `derive_orders.py` applies the official formula
  `floor(weight × prev NAV / prev close / 1000) × 1000`.
- **Two layers of rule checks.** Internal limits are deliberately stricter than the official ones, and an independent final gate
  re-validates the finished file. A failing file is saved with a suffix that breaks the naming rule so it cannot be uploaded by accident.
- **One config object.** `settings.py` holds every limit, and the system prompt is rendered from the same `Limits` object, so code and prompt cannot drift apart.
- **Auditable.** Raw LLM replies go to `logs/`, and a code snapshot (`logs/code_<date>_<version>.zip`) is written for the post-qualification review.

---

## Competition rules enforced

| Rule | Official | Internal (normal) | Internal (STRICT*) |
|---|---|---|---|
| Holdings | 20–30 | 22–28 | 23–27 |
| 2330 weight cap | 25% | 22% | 20% |
| Other stock cap | 10% | 8.5% | 8% |
| Cash ratio | ≤ 25% | 4–10% | 5–9% |
| Core AI/semi themes | strategy rule | ≥ 62% NAV | ≥ 62% NAV |
| Lot size | 1,000 shares | same | same |
| Fees / tax | 0.1425% / 0.3% (sell) | same | same |

\* `STRICT` activates automatically when the DataPack shows 2 violations (3 = disqualification).

Other points the code handles: the 150-stock universe, the `net_exposure_intent` vs actual net flow check, the declared cash range vs projected cash,
the submission window (previous trading day 19:30 → trade date 08:55), and no look-ahead in source timestamps.

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
│   ├── macro_fragment.json     # cached macro evidence
│   └── fundamental_fragment.json  # cached factor evidence
├── src/
│   ├── run_daily.py            # entry point: runs everything in order
│   ├── settings.py             # paths, official rules, internal limits (single source of truth)
│   ├── datapack.py             # DataPack → `state`
│   ├── fetch_data.py           # previous-day closes from TWSE / TPEx OpenAPI
│   ├── observations.py         # EvidenceBook: assigns S#/O# ids, enforces schema limits
│   ├── universe.py             # loads/validates the 150 stocks and themes
│   ├── derive_orders.py        # official order formula + post-trade projection
│   ├── build_dplan.py          # assembles the D-Plan JSON, code version, code snapshot
│   ├── llm.py                  # Vertex AI (google-genai): retry, model fallback
│   ├── util.py                 # time helpers, HTTP with retry, JSON cache
│   ├── agents/
│   │   ├── macro_agent.py      # FRED / NewsAPI / Finnhub facts (no LLM)
│   │   ├── fundamental_agent.py# Yahoo Finance price factors + key stats (no LLM)
│   │   ├── portfolio_agent.py  # the LLM step + retry loop + order derivation
│   │   └── check_agent.py      # rule engine: pre-trade checks and final_gate()
│   ├── prompts/
│   │   └── system_prompt.md    # rendered with {{PLACEHOLDERS}} from settings.Limits
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
The optional `tools/macro_news_collector.py` also needs `feedparser`.

### `.env`

```ini
# LLM (Vertex AI)
GOOGLE_CLOUD_PROJECT=<your project id>
GOOGLE_CLOUD_LOCATION=global           # Gemini 3.x normally needs "global"
GEMINI_MODEL=<primary model>
GEMINI_FALLBACK_MODELS=<a,b>           # optional; tried in order on HTTP 404
LLM_TEMPERATURE=0.2                    # optional

# Data sources (each is optional; a missing key just skips that source)
FRED_API_KEY=
NEWS_API_KEY=
FINNHUB_API_KEY=
ARCHIVE_NEWS=1                         # 0 = skip web.archive.org snapshots

# Tuning (optional)
MAX_ATTEMPTS=3                         # LLM retries after a failed check
FUNDAMENTAL_MAX=90                     # max stocks with Yahoo key statistics
```

`.env` is git-ignored. Do not commit API keys.

Test the LLM connection: `python src/llm.py`

---

## Daily workflow

1. **Download the DataPack** from the portal (Document Submission page) and save it as `inputs/<trade_date>/datapack.json`.
2. **Run the pipeline** (between 19:30 on the previous trading day and 08:55 on the trade date):

   ```bash
   python src/run_daily.py 2026-10-23                  # full run (calls the LLM)
   python src/run_daily.py 2026-10-23 --skip-fetch     # reuse inputs/<date>/market_data.json
   python src/run_daily.py 2026-10-23 --refresh        # ignore macro / factor caches
   python src/run_daily.py 2026-10-23 --no-external    # exchange data only (no macro / factor agents)
   python src/run_daily.py 2026-10-23 --replay logs/2026-10-23_attempt2.txt   # re-use a saved LLM reply, spends no tokens
   ```

3. **Check the result.** On success, `outputs/D-Plan_<team_id>_<trade_date>.json` is written.
   - `*.REJECTED.json` means the final gate failed. **Do not submit.**
   - `*.REPLAY.json` means the file came from `--replay`. **Do not submit.**
4. **Manual submission:** upload the file in the portal, confirm every intake check passes, review *Daily Data → Orders*, then press **Submit**.
   An upload without Submit does not count (max 25 submissions per day, and the last one before the deadline is used).

Individual steps can be run on their own:

```bash
python src/datapack.py inputs/2026-10-23/datapack.json              # DataPack summary
python src/fetch_data.py 2026-10-23 2026-10-22                      # trade_date as_of_date
python src/agents/macro_agent.py 2026-10-23                         # macro evidence only
```

---

## Outputs

| File | Meaning |
|---|---|
| `outputs/D-Plan_<team>_<date>.json` | Passed the final gate; ready to upload |
| `outputs/D-Plan_<team>_<date>.REJECTED.json` | Failed the gate; the errors are printed. Do not submit |
| `outputs/D-Plan_<team>_<date>.REPLAY.json` | Produced with `--replay`. Do not submit |
| `outputs/agent_result_<date>.json` | Full agent result: evidence, decisions, orders, projected portfolio, notes. Also read by the next day's journal |
| `logs/<date>_attempt<N>.txt` | Raw LLM reply per attempt |
| `logs/code_<date>_<version>.zip` | Code snapshot for the post-qualification audit |

---

## Status of `src/tools/`

`src/tools/` is mostly a **standalone research / backtest toolkit** and is **not** part of the daily pipeline.

| Module | Status |
|---|---|
| `journal.py` | **Integrated.** `run_daily.py` calls it to add yesterday's performance review (portfolio vs equal-weight benchmark, top and bottom contributors) as an observation. |
| `cli.py`, `orchestrator.py`, `backtest.py`, `portfolio.py`, `indicators.py`, `data_collector.py`, `data_validator.py`, `costs.py`, `reporting.py`, `fundamentals.py`, `risk.py` | **Standalone.** Not imported by the pipeline. They expect `tools/config.py` (`load_rules`, `DATA_DIR`, ...) and `config/universe.csv`, and use their own default parameters (8% cap, 8% cash) that differ from `settings.py`. Live rule checks are done by `agents/check_agent.py` and `derive_orders.py`. |
| `macro_news_collector.py` | **Unfinished.** The file is truncated and unused; the macro agent fetches news itself via NewsAPI. |
| `macro_tools.py` (GDELT) | **Unused.** |
| `financial_data_collector.py` | **Empty placeholder.** |

Also empty placeholders: `agents/risk_agent.py` and `prompts/{fundamental,macro,risk}_agent.md`.
The relative imports inside the standalone tools require `src/tools/__init__.py`.

---

## Safety notes

- Three warnings mean disqualification. Typical causes are a missed submission, cash above 25% or negative, holdings outside 20–30,
  a stock above its cap, or a ticker outside the 150-stock list. Never submit a `REJECTED` or `REPLAY` file.
- Orders are priced at the previous close, but fills happen at the same-day average price and NAV is marked at the close.
  That is why the internal limits carry a buffer.
- A missing price for any stock excludes it for the day.
- The first day builds the whole 20–30 stock portfolio from an empty account, so the declared intent must be `increase`.

---

## License / disclaimer

Competition code for a virtual-portfolio contest. It is not investment advice.
