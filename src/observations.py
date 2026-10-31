"""Evidence layer (D-Plan L0/L1): one registry for sources and observations.

Agents never talk to the D-Plan directly. Each returns a *fragment*
    {"sources": [...], "observations": [...], "tool_calls": int, "warnings": [...]}
(observations point to sources by index) and the EvidenceBook assigns the final S1.. / O1..
ids in a fixed order. This keeps ids consecutive (required by the guide), deduplicates
sources, and enforces the schema's size limits in one place.

Robustness: when the observation budget (schema maxItems) is full, extra observations are
DROPPED and counted in `book.dropped` instead of aborting the whole daily run.
"""
from __future__ import annotations

import math
import re
import statistics

from settings import MAX_OBS, MAX_SOURCES
from util import cut

SOURCE_KEYS = ("authority", "name", "url", "archive_url", "published_at", "content_as_of", "fetched_at")
BIG_MOVE_PCT = 3.0              # |1-day move| worth surfacing for non-held stocks
MAX_MOVERS = 12


class EvidenceLimitError(RuntimeError):
    pass


def new_fragment() -> dict:
    return {"sources": [], "observations": [], "tool_calls": 0, "warnings": []}


def frag_add(frag: dict, source: dict, statement: str, values: dict, topic: str,
             level: str = "market", tickers=()) -> None:
    """Append one (source, observation) pair; identical sources inside a fragment are reused."""
    key = (source["authority"], source["url"], source["content_as_of"])
    idx = next((i for i, s in enumerate(frag["sources"])
                if (s["authority"], s["url"], s["content_as_of"]) == key), None)
    if idx is None:
        frag["sources"].append(source)
        idx = len(frag["sources"]) - 1
    frag["observations"].append({"src": [idx], "statement": statement, "values": values,
                                 "topic": topic, "level": level, "tickers": list(tickers)})


def frag_add_obs(frag: dict, src_idx: int, statement: str, values: dict, topic: str,
                 level: str = "market", tickers=()) -> None:
    frag["observations"].append({"src": [src_idx], "statement": statement, "values": values,
                                 "topic": topic, "level": level, "tickers": list(tickers)})


def frag_add_source(frag: dict, source: dict) -> int:
    frag["sources"].append(source)
    return len(frag["sources"]) - 1


def _clean_values(values: dict) -> dict:
    """Schema: only finite numbers allowed (no None / strings / bools)."""
    return {k: round(float(v), 6) for k, v in values.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}


class EvidenceBook:
    def __init__(self):
        self.sources: list = []
        self.observations: list = []
        self.meta: dict = {}                 # obs_id -> {"level", "tickers"}
        self._src_key: dict = {}
        self._obs_key: dict = {}             # (statement, sources) -> obs_id
        self.dropped: int = 0                # observations refused because the budget was full

    # ---- registration -------------------------------------------------
    def add_source(self, s: dict) -> str:
        key = (s["authority"], s["url"], s["content_as_of"])
        if key in self._src_key:
            return self._src_key[key]
        if len(self.sources) >= MAX_SOURCES:
            raise EvidenceLimitError(f"more than {MAX_SOURCES} sources")
        sid = f"S{len(self.sources) + 1}"
        row = {"source_id": sid, **{k: s[k] for k in SOURCE_KEYS if s.get(k)}}
        row["name"] = cut(row.get("name", ""), 300)
        self.sources.append(row)
        self._src_key[key] = sid
        return sid

    def add_obs(self, statement: str, values: dict, source_ids: list, topic: str,
                level: str = "market", tickers=()) -> str | None:
        values = _clean_values(values)
        if not values:                       # schema requires at least one numeric value
            return None
        dkey = (statement, tuple(source_ids))
        if dkey in self._obs_key:            # same fact from the same source: reuse, keep the 100-obs budget
            return self._obs_key[dkey]
        if len(self.observations) >= MAX_OBS:
            self.dropped += 1                # never abort the run for a full budget
            return None
        oid = f"O{len(self.observations) + 1}"
        topic = re.sub(r"[^a-z0-9_]", "_", topic.lower())[:40]
        if not re.match(r"^[a-z]", topic):
            topic = "x" + topic
        self.observations.append({"obs_id": oid, "source_ref": source_ids, "topic": topic,
                                  "statement": cut(statement, 500), "values": values})
        self.meta[oid] = {"level": level, "tickers": list(tickers)}
        self._obs_key[dkey] = oid
        return oid

    def absorb(self, frag: dict) -> int:
        """Merge an agent fragment; returns the number of observations added."""
        sids = [self.add_source(s) for s in frag["sources"]]
        n = 0
        for o in frag["observations"]:
            if self.add_obs(o["statement"], o["values"], [sids[i] for i in o["src"]], o["topic"],
                            o.get("level", "market"), o.get("tickers", ())):
                n += 1
        return n

    # ---- queries ------------------------------------------------------
    def market_level_ids(self) -> list:
        return [o for o, m in self.meta.items() if m["level"] == "market"]

    def ticker_obs(self) -> dict:
        out: dict = {}
        for oid, m in self.meta.items():
            for t in m["tickers"]:
                out.setdefault(t, []).append(oid)
        return out


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def market_fragment(market: dict, state: dict, universe) -> dict:
    """Facts from the official TWSE/TPEx close data: breadth, theme averages, and the closes
    that matter today (current holdings + unusually large movers). The full 150-stock table is
    NOT emitted as evidence: it would burn the observation budget without helping decisions."""
    frag = new_fragment()
    idx = {s["authority"]: frag_add_source(frag, dict(s)) for s in market["sources"]}
    all_src = list(idx.values())
    prices = market["prices"]

    chg = [p["change_pct"] for p in prices.values() if p.get("change_pct") is not None]
    if chg:
        up, dn = sum(c > 0 for c in chg), sum(c < 0 for c in chg)
        frag["observations"].append({
            "src": all_src, "topic": "tw_breadth", "level": "market", "tickers": [],
            "statement": (f"{len(chg)} 檔標的中 {up} 檔上漲、{dn} 檔下跌、{len(chg) - up - dn} 檔平盤；"
                          f"漲跌幅中位數 {statistics.median(chg):+.2f}%、平均 {statistics.mean(chg):+.2f}%"),
            "values": {"advancers": up, "decliners": dn, "unchanged": len(chg) - up - dn,
                       "median_chg_pct": round(statistics.median(chg), 2),
                       "mean_chg_pct": round(statistics.mean(chg), 2)}})

    for th_id, th in universe.themes.items():
        cs = [prices[t]["change_pct"] for t in th["tickers"]
              if t in prices and prices[t].get("change_pct") is not None]
        if cs:
            frag["observations"].append({
                "src": all_src, "topic": f"theme_{th_id}", "level": "market", "tickers": [],
                "statement": (f"{th['label']} 籃子（{len(cs)} 檔等權）平均漲跌 {statistics.mean(cs):+.2f}%，"
                              f"{sum(c > 0 for c in cs)}/{len(cs)} 檔上漲"),
                "values": {"avg_chg_pct": round(statistics.mean(cs), 2), "n": len(cs),
                           "up_count": sum(c > 0 for c in cs)}})

    held = [t for t in state["holdings"] if t in prices]
    movers = sorted((t for t in universe.ordered()
                     if t in prices and t not in held and prices[t].get("change_pct") is not None
                     and abs(prices[t]["change_pct"]) >= BIG_MOVE_PCT),
                    key=lambda t: -abs(prices[t]["change_pct"]))[:MAX_MOVERS]
    for group in chunks(held + movers, 10):
        parts, vals = [], {}
        for t in group:
            p = prices[t]
            c = p.get("change_pct")
            parts.append(f"{t} {p['name']} {p['close']:g}" + (f"({c:+.2f}%)" if c is not None else ""))
            vals[f"{t}_close"] = p["close"]
            if c is not None:
                vals[f"{t}_chg_pct"] = c
        frag["observations"].append({"src": all_src, "topic": "stock_close", "level": "stock",
                                     "tickers": list(group), "values": vals,
                                     "statement": "持股與大幅波動個股前一交易日收盤與漲跌：" + "；".join(parts)})
    return frag
