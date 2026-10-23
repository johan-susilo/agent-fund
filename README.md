# Code structure
moe-agent/
├── .env                       ⬜ you create (project ID, model name)
├── config/
│   ├── ticker_list.json      ⬜ next (the 150 allowed tickers)
│   └── D-Plan.schema.json     ⬜ copy from your project files
├── inputs/2026-10-23/
│   └── datapack.json          ✅ your file
├── src/
│   ├── datapack.py            ✅ done
│   ├── derive_orders.py       ✅ done
│   ├── test_step1.py          ✅ done (fake prices)
│   ├── fetch_data.py          ⬜ real prices and market data
│   ├── llm.py                 ⬜ Google connection (below)
│   ├── agent.py               ⬜ builds the prompt, asks the LLM for weights
│   ├── prompts/system_prompt.md ⬜
│   ├── build_dplan.py         ⬜ assembles the official JSON
│   ├── validate.py            ⬜ schema and rule check
│   └── run_daily.py           ⬜ runs everything in order
├── outputs/                   ⬜ the D-Plan file lands here
└── logs/                      ⬜ raw LLM replies, kept for the code review