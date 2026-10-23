"""Tradable universe (150 stocks) and theme groups, loaded from config/ and validated once."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from settings import CONFIG_DIR, UNIVERSE_SIZE


@dataclass
class Universe:
    names: dict           # ticker -> name
    markets: dict         # ticker -> "twse" | "tpex"
    themes: dict          # theme_id -> {"label", "core", "tickers"}
    ticker_theme: dict    # ticker -> theme_id

    def theme_of(self, t: str) -> str:
        return self.ticker_theme.get(t, "other")

    def is_core(self, t: str) -> bool:
        th = self.ticker_theme.get(t)
        return bool(th and self.themes[th]["core"])

    @property
    def core_ids(self) -> list:
        return [k for k, v in self.themes.items() if v["core"]]

    def ordered(self) -> list:
        """Tickers grouped by theme (so observation chunks stay thematically coherent)."""
        seen, out = set(), []
        for th in self.themes.values():
            for t in th["tickers"]:
                if t in self.names and t not in seen:
                    seen.add(t); out.append(t)
        return out + [t for t in self.names if t not in seen]


def load_universe() -> Universe:
    rows = json.loads((CONFIG_DIR / "ticker_list.json").read_text(encoding="utf-8"))["tickers"]
    names, markets = {}, {}
    for r in rows:
        t = str(r.get("ticker", "")).strip()
        if re.fullmatch(r"\d{4}", t):            # ignores stray header rows pasted from a spreadsheet
            names[t] = str(r.get("name", "")).strip()
            markets[t] = r.get("market", "twse")
    if len(names) != UNIVERSE_SIZE:
        raise ValueError(f"ticker_list.json has {len(names)} valid tickers, expected {UNIVERSE_SIZE}")

    cats = json.loads((CONFIG_DIR / "ticker_category.json").read_text(encoding="utf-8"))["categories"]
    themes, ticker_theme = {}, {}
    for c in cats:
        bad = [t for t in c["tickers"] if t not in names]
        if bad:
            raise ValueError(f"category {c['id']} contains tickers outside the 150-stock list: {bad}")
        themes[c["id"]] = {"label": c["name"], "core": bool(c.get("core")), "tickers": list(c["tickers"])}
        for t in c["tickers"]:
            if t in ticker_theme:
                raise ValueError(f"{t} appears in two categories")
            ticker_theme[t] = c["id"]
    return Universe(names, markets, themes, ticker_theme)
