from __future__ import annotations
from datetime import datetime, timezone
import re
import requests

DEFAULT_QUERIES = [
    "Taiwan semiconductor industry exports AI chip demand",
    "Taiwan export orders electronics semiconductor",
    "Federal Reserve interest rates Taiwan dollar",
    "global AI data center capital expenditure semiconductor",
]

def fetch_gdelt_news(queries=None, max_records=10, timeout=15) -> list[dict]:
    """Retrieve indexed news metadata via GDELT. Headlines are unverified signals, not facts."""
    results, seen = [], set()
    for query in (queries or DEFAULT_QUERIES):
        try:
            response = requests.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params={"query": query, "mode": "ArtList", "format": "json",
                        "sort": "DateDesc", "maxrecords": str(max_records)},
                timeout=timeout,
                headers={"User-Agent": "ESun-AICup-Research/1.0"}
            )
            response.raise_for_status()
            for item in response.json().get("articles", []) or []:
                url = str(item.get("url", ""))
                if not url or url in seen:
                    continue
                seen.add(url)
                results.append({
                    "title": re.sub(r"\s+", " ", str(item.get("title", ""))).strip(),
                    "url": url, "domain": item.get("domain"),
                    "published_at": item.get("seendate"),
                    "query": query, "retrieved_at": datetime.now(timezone.utc).isoformat()
                })
        except Exception as exc:
            results.append({"query": query, "error": f"{type(exc).__name__}: {exc}"})
    return results
