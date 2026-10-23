"""Macro RSS news collector compatible with the project's EvidenceBook schema.

Place at: src/tools/macro_news_collector.py
Dependencies: python -m pip install feedparser requests

The EvidenceBook accepts numeric observation values only. Therefore article titles,
summaries and links are stored in the observation statement and source registry;
numeric timestamps are stored in values.
"""
from __future__ import annotations

import hashlib
import html
import os
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from observations import chunks, new_fragment, frag_add_source
from settings import ROOT
from util import Counter, iso, now_tw, read_cache, write_cache

CACHE_TTL_MIN = int(os.environ.get("MACRO_NEWS_CACHE_TTL_MIN", "60"))
MAX_ARTICLES = int(os.environ.get("MACRO_NEWS_MAX_ARTICLES", "30"))
GROUP_SIZE = 2

DEFAULT_FEEDS = [
    {"name": "CNA Finance RSS", "url": "https://feeds.feedburner.com/rsscna/finance", "category": "finance"},
    {"name": "CNA Technology RSS", "url": "https://feeds.feedburner.com/rsscna/technology", "category": "technology"},
]
DEFAULT_KEYWORDS = [
    "Taiwan economy", "Taiwan exports", "Taiwan semiconductor", "AI chip demand",
    "semiconductor supply chain", "US interest rates", "Federal Reserve",
    "Taiwan dollar", "China trade",
]


def _clean(value, limit=700):
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _fetch_rss(feed, counter, fetched_at):
    try:
        import feedparser
    except ImportError:
        return [], "feedparser missing; install with: python -m pip install feedparser"
    counter.inc()
    parsed = feedparser.parse(feed["url"])
    out = []
    for entry in getattr(parsed, "entries", [])[:MAX_ARTICLES]:
        title = _clean(getattr(entry, "title", ""), 200)
        link = str(getattr(entry, "link", "")).strip()
        pub = _parse_dt(getattr(entry, "published", None) or getattr(entry, "updated", None))
        if not title or not link:
            continue
        out.append({
            "title": title,
            "url": link,
            "published_at": _iso(pub) or fetched_at,
            "feed_name": feed["name"],
        })
    return out, None


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = parsedate_to_datetime(str(value))
            except (TypeError, ValueError, OverflowError):
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _epoch(dt):
    return int(dt.timestamp()) if dt else None


def _article_id(url, title, published):
    raw = (url.strip() or f"{title}|{published}").lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _feed_configs():
    """Optional override: MACRO_RSS_FEEDS='Name|URL|category;Name|URL|category'."""
    raw = os.environ.get("MACRO_RSS_FEEDS", "").strip()
    if not raw:
        return DEFAULT_FEEDS
    result = []
    for item in raw.split(";"):
        parts = [x.strip() for x in item.split("|", 2)]
        if len(parts) >= 2 and parts[0] and parts[1].startswith(("http://", "https://")):
            result.append({"name": parts[0], "url": parts[1],
                           "category": parts[2] if len(parts) > 2 and parts[2] else "general"})
    return result or DEFAULT_FEEDS


def _fetch_rss(feed, counter, fetched_at):
    try:
        import feedparser
    except ImportError:
        return [], "feedparser missing; install with: python -m pip install feedparser"
    counter.inc()