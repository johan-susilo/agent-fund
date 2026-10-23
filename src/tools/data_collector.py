from __future__ import annotations
import time
from pathlib import Path
import pandas as pd

def load_universe(path: str | Path) -> pd.DataFrame:
    u = pd.read_csv(path, dtype={"ticker": str})
    required = {"ticker", "market", "sector", "approved"}
    if not required.issubset(u.columns):
        raise ValueError(f"Universe CSV needs columns {sorted(required)}")
    u["ticker"] = u["ticker"].astype(str).str.strip().str.zfill(4)
    u["market"] = u["market"].astype(str).str.upper().str.strip()
    return u

def yahoo_symbol(ticker: str, market: str) -> str:
    suffix = ".TW" if str(market).upper() == "TWSE" else ".TWO"
    return f"{str(ticker).zfill(4)}{suffix}"

def collect_prices(universe_path: str | Path, start: str, end: str | None = None,
                   output_dir: str | Path = "data/prices", pause: float = 0.15) -> dict:
    """Download adjusted daily OHLCV. The official universe must be provided by the user."""
    try:
        import yfinance as yf
    except ImportError as e:
        raise RuntimeError("Install yfinance: pip install yfinance") from e
    u = load_universe(universe_path)
    approved = u[u["approved"].astype(str).str.lower().isin(["true", "yes", "1", "approved"])]
    out = Path(output_dir); out.mkdir(parents=True, exist_ok=True)
    results, errors = [], []
    for row in approved.to_dict("records"):
        ticker, market = row["ticker"], row["market"]
        if market not in {"TWSE", "TPEX", "TPEx".upper()}:
            errors.append({"ticker": ticker, "error": f"Unknown market value: {market}"}); continue
        symbol = yahoo_symbol(ticker, "TPEx" if market == "TPEX" else market)
        try:
            frame = yf.download(symbol, start=start, end=end, auto_adjust=False,
                                progress=False, threads=False)
            if frame is None or frame.empty:
                errors.append({"ticker": ticker, "symbol": symbol, "error": "No price rows"})
            else:
                if isinstance(frame.columns, pd.MultiIndex):
                    frame.columns = frame.columns.get_level_values(0)
                frame = frame.rename(columns={c: str(c).lower().replace(" ", "_") for c in frame.columns})
                frame.index.name = "date"
                frame = frame.reset_index()
                frame["ticker"] = ticker
                frame["symbol"] = symbol
                frame["market"] = market
                frame.to_csv(out / f"{ticker}.csv", index=False)
                results.append({"ticker": ticker, "symbol": symbol, "rows": len(frame),
                                "first_date": str(frame["date"].min()), "last_date": str(frame["date"].max())})
        except Exception as e:
            errors.append({"ticker": ticker, "symbol": symbol, "error": f"{type(e).__name__}: {e}"})
        time.sleep(max(0.0, pause))
    report = {"requested": len(approved), "downloaded": len(results), "failed": len(errors),
              "results": results, "errors": errors,
              "warning": "Provider data may be incomplete; validate before research use."}
    return report

def load_price_panel(price_dir: str | Path = "data/prices", field: str = "adj_close") -> pd.DataFrame:
    """Return a date x ticker price panel from per-ticker CSV files."""
    price_dir = Path(price_dir)
    frames = []
    for p in sorted(price_dir.glob("*.csv")):
        df = pd.read_csv(p, parse_dates=["date"], dtype={"ticker": str})
        col = field
        if col not in df.columns and field == "adj_close" and "close" in df.columns:
            col = "close"
        if col not in df.columns:
            continue
        s = df.set_index("date")[col].rename(p.stem)
        frames.append(s)
    if not frames:
        raise FileNotFoundError(f"No usable price CSV files in {price_dir}")
    panel = pd.concat(frames, axis=1).sort_index()
    return panel[~panel.index.duplicated(keep="last")]
