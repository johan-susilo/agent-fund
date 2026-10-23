from __future__ import annotations
from pathlib import Path
import json
import pandas as pd

REQUIRED = {"date", "open", "high", "low", "close", "volume"}
def validate_price_files(price_dir: str | Path = "data/prices",
                         report_path: str | Path = "data/quality_report.json") -> dict:
    root = Path(price_dir)
    files = sorted(root.glob("*.csv"))
    checks, errors = [], []
    for p in files:
        try:
            df = pd.read_csv(p, parse_dates=["date"])
            missing_cols = sorted(REQUIRED - set(df.columns))
            if missing_cols:
                errors.append({"file": p.name, "error": f"Missing columns: {missing_cols}"}); continue
            df = df.sort_values("date")
            dupes = int(df["date"].duplicated().sum())
            bad_ohlc = int(((df["high"] < df[["open","close","low"]].max(axis=1)) |
                            (df["low"] > df[["open","close","high"]].min(axis=1))).sum())
            nonpositive = int((df[["open","high","low","close"]] <= 0).any(axis=1).sum())
            missing_prices = int(df[["open","high","low","close"]].isna().any(axis=1).sum())
            missing_vol = int(df["volume"].isna().sum())
            checks.append({"ticker": p.stem, "rows": len(df), "duplicate_dates": dupes,
                           "bad_ohlc_rows": bad_ohlc, "nonpositive_price_rows": nonpositive,
                           "missing_price_rows": missing_prices, "missing_volume_rows": missing_vol,
                           "first_date": str(df["date"].min()), "last_date": str(df["date"].max())})
        except Exception as e:
            errors.append({"file": p.name, "error": f"{type(e).__name__}: {e}"})
    report = {"files_found": len(files), "files_checked": len(checks), "errors": errors,
              "checks": checks, "passed": not errors and all(
                  x["duplicate_dates"] == 0 and x["bad_ohlc_rows"] == 0 and
                  x["nonpositive_price_rows"] == 0 and x["missing_price_rows"] == 0
                  for x in checks)}
    dest = Path(report_path); dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
