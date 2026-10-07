"""News gate (best effort): before an entry fires, check recent headlines for
high-impact market-moving events. RSS feeds are parsed with stdlib only; if a
feed is unreachable the gate degrades to NEWS=MISSING and never blocks on its
own failure - but a CONFIRMED high-impact headline inside the block window
suppresses new entries ("confirm all things, then execute").

Configure via settings: NEWS_SOURCES, NEWS_BLOCK_KEYWORDS, NEWS_BLOCK_MINUTES.
"""
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import settings
from utils import ist_now

log = logging.getLogger(__name__)

_DEFAULT_KEYWORDS = (
    "rbi", "repo", "cpi", "inflation", "gdp", "fed", "fomc", "rate cut",
    "rate hike", "budget", "election", "war", "strike", "crude ban",
    "emergency", "crash", "surge", "ban ", "sanction",
)


def _parse_pubdate(text: str) -> datetime | None:
    formats = ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
               "%a, %d %b %Y %H:%M:%S %Z (%z)")
    for fmt in formats:
        try:
            parsed = datetime.strptime(text.strip(), fmt)
            return parsed.astimezone(settings.IST)
        except (ValueError, TypeError):
            continue
    return None


def fetch_headlines(session) -> list:
    """Best-effort RSS fetch from settings.NEWS_SOURCES. Returns
    [{title, published, source}] - newest first, empty on failure."""
    headlines = []
    for url in settings.NEWS_SOURCES:
        try:
            text = session.get_text(url)
            if not text:
                continue
            root = ET.fromstring(text)
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                if title:
                    headlines.append({"title": title,
                                      "published": _parse_pubdate(pub),
                                      "source": url.split("/")[2]})
        except (ET.ParseError, KeyError, TypeError) as exc:
            log.warning("news feed %s parse failed: %s", url, exc)
    headlines.sort(key=lambda h: h["published"] or datetime.min.replace(
        tzinfo=settings.IST), reverse=True)
    return headlines


def high_impact_block(headlines, now: datetime | None = None):
    """Returns (blocked, headline) when a high-impact headline is newer than
    NEWS_BLOCK_MINUTES. Missing/failed feeds -> (False, None): the news gate
    never blocks on its own failure (graceful degradation, like every source)."""
    now = now or ist_now()
    cutoff = now - timedelta(minutes=settings.NEWS_BLOCK_MINUTES)
    keywords = tuple(settings.NEWS_BLOCK_KEYWORDS)
    for headline in headlines:
        published = headline.get("published")
        if published is None or published < cutoff:
            continue
        title = headline.get("title", "").lower()
        if any(keyword in title for keyword in keywords):
            return True, headline["title"]
    return False, None
