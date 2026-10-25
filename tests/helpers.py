import numpy as np

CORE = ["semis", "semi_equip", "servers", "components", "pcb", "networking"]
OTHER = ["financials", "telecom", "others"]


class FakeUniverse:
    def __init__(self, n=150):
        self.names, self._theme = {}, {}
        tickers = ["2330"] + [str(2000 + i) for i in range(1, n)]
        for i, t in enumerate(tickers):
            self.names[t] = f"N{t}"
            self._theme[t] = (CORE + OTHER)[i % 9]
        self.core_ids = CORE
        self.themes = {th: {"tickers": [t for t in tickers if self._theme[t] == th]} for th in CORE + OTHER}

    def theme_of(self, t):
        return self._theme[t]

    def is_core(self, t):
        return self._theme[t] in CORE


def make_frag(u, as_of="2026-10-23", seed=1, price=True, fund=True):
    rng = np.random.default_rng(seed)
    pf, fu = {}, {}
    for t in u.names:
        if price:
            pf[t] = {"mom20": float(rng.normal(3, 10)), "mom60": float(rng.normal(8, 20)), "vol20": float(rng.uniform(20, 70)),
                     "adv20": float(rng.uniform(100, 5000)), "ma20_gap": float(rng.normal(2, 5)),
                     "ma60_gap": float(rng.normal(5, 10)), "ma20_vs_ma60": float(rng.normal(2, 4)),
                     "ud_vol20": float(rng.normal(0, 0.5)), "mdd60": float(rng.uniform(5, 25)),
                     "dd_dev20": float(rng.uniform(10, 40)), "hl20": float(rng.uniform(1, 4)), "last_date": as_of}
        if fund:
            fu[t] = {"roe": float(rng.uniform(2, 30)), "pe": float(rng.uniform(8, 40)), "rev_g": float(rng.normal(0.15, 0.2)),
                     "rev_mom": float(rng.normal(0.03, 0.1)), "margin": float(rng.uniform(0.02, 0.3)),
                     "inst_lots": float(rng.normal(0, 500))}
    return {"price_factors": pf, "fundamentals": fu, "observations": []}


def macro_frag(vals, ts="2026-10-23T05:00:00+08:00"):
    return {"sources": [{"id": "S1", "content_as_of": ts}], "observations": [{"source_ids": ["S1"], "values": vals}]}


BULL = {"vix_yf": 14.0, "vix_yf_vs_ma20_pct": -15, "sox_vs_ma20_pct": 9, "sox_chg_pct": 2, "tsm_adr_chg_pct": 2.5,
        "us10y_chg_bp": -10, "taiex_vs_ma20_pct": 5}
BEAR = {"vix_yf": 29.0, "vix_yf_vs_ma20_pct": 18, "sox_vs_ma20_pct": -9, "sox_chg_pct": -2.5, "tsm_adr_chg_pct": -2.5,
        "us10y_chg_bp": 12, "taiex_vs_ma20_pct": -5}
