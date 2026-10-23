"""Small shared helpers: time, string cutting, HTTP with retry, JSON caches."""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
import hashlib


import requests

from settings import TW


def now_tw() -> datetime:
    return datetime.now(TW)


def iso(dt: datetime) -> str:
    return dt.astimezone(TW).isoformat(timespec="seconds")


def parse_ts(s: str) -> datetime:
    """Parse an ISO-8601 string; a trailing 'Z' means UTC, naive values are treated as Taipei time."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=TW)


def cut(s, n: int):
    """Truncate to n chars (the D-Plan schema rejects the whole file when maxLength is exceeded)."""
    if s is None:
        return None
    return s if len(s) <= n else s[: n - 1] + "…"


def prev_weekday(d: str) -> str:
    x = date.fromisoformat(d) - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x.isoformat()


class Counter:
    """Thread-safe counter used to count data-tool calls (reported as tool_use_count)."""

    def __init__(self):
        self.n = 0
        self._lock = threading.Lock()

    def inc(self, k: int = 1):
        with self._lock:
            self.n += k


def http_get_json(url, params=None, headers=None, timeout=20, retries=2, counter: Counter | None = None):
    """GET + JSON with exponential backoff on network errors, 429 and 5xx. Raises on final failure."""
    last = None
    for attempt in range(retries + 1):
        if counter:
            counter.inc()
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json", **(headers or {})})
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            if attempt < retries:
                time.sleep(2 ** attempt)
    raise last


def read_cache(path: Path, ttl_minutes: float):
    """Return cached JSON if it is younger than ttl_minutes, else None."""
    try:
        if path.exists() and (time.time() - path.stat().st_mtime) / 60 <= ttl_minutes:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return None


def write_cache(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def save_raw_bytes(trade_date: str | None, label: str, content: bytes) -> str:
    from settings import ROOT
    sha = hashlib.sha256(content).hexdigest()
    folder = (ROOT / "inputs" / trade_date / "raw") if trade_date else (ROOT / "inputs" / "raw")
    folder.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
    (folder / f"{safe_label}_{sha[:12]}.json").write_bytes(content)
    return sha
