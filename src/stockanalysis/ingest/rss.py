"""RSS ingest — DESIGN §3.3's backbone.

Free, unlimited, no key, and the best India coverage available. It has exactly
one limitation and it is a big one: **a feed carries no history.** Every URL in
`settings.news_feeds` returns the most recent 15-50 items and nothing else, so
running this daily builds a forward archive and running it once builds a
snapshot. It cannot backfill, which is why GDELT exists in this phase — the
sentiment factor is not backtestable on RSS at all, and would silently look
"available" if that were not said out loud.

WHY THE PARSER IS HAND-ROLLED
-----------------------------
`feedparser` is the obvious dependency and does more than this needs. The
feeds in question emit RSS 2.0 with occasional CDATA and one Atom variant;
~80 lines of ElementTree covers them, is testable offline against a fixture
string, and does not add a package whose failure mode is a silent parse of
zero items. If a fifth feed format shows up, revisit.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.news.resolve import TickerResolver
from stockanalysis.news.store import Article, StoreStats, store_articles, to_ist

log = logging.getLogger(__name__)

PROVIDER = "RSS"
PUBLISHED_FROM_FEED = "FEED"

# Two of the four outlets have *opposite* bot rules, which is why this is a
# list rather than a constant. Moneycontrol's edge returns 403 to anything with
# a browser User-Agent and 200 to `python-requests/*`; Business Standard does
# the reverse. Measured, not guessed — see PHASE3-FINDINGS. The tool identifies
# itself first and only falls back when an outlet refuses that.
USER_AGENTS = (
    "python-requests/2.34.2 stockanalysis/0.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _localname(tag: str) -> str:
    """`{http://www.w3.org/2005/Atom}entry` -> `entry`."""
    return tag.rsplit("}", 1)[-1].lower()


def strip_html(text: str | None) -> str:
    """RSS descriptions arrive with thumbnails and markup inline.

    Moneycontrol prefixes every description with an `<img>` tag. Feeding that
    to FinBERT means scoring a filename, so the markup comes out before the
    text is ever stored.
    """
    if not text:
        return ""
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", html.unescape(text)))).strip()


def parse_datetime(raw: str | None) -> dt.datetime | None:
    """RFC-822 (RSS) or ISO-8601 (Atom), returned as naive IST."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return to_ist(parsedate_to_datetime(raw))
    except (TypeError, ValueError):
        pass
    try:
        return to_ist(dt.datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_feed(xml_text: str, feed_url: str = "") -> list[Article]:
    """Articles from one feed document. Items missing a date or link are skipped.

    An item without a timestamp cannot be given a knowledge date, and guessing
    "now" would date a week-old story to today — which in a point-in-time
    system is a small act of forgery. Skipped and counted instead.
    """
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as e:
        log.warning("unparseable feed %s: %s", feed_url, e)
        return []

    default_source = urlparse(feed_url).netloc.replace("www.", "") or "unknown"
    channel_title = ""
    for el in root.iter():
        if _localname(el.tag) == "title" and el.text:
            channel_title = el.text.strip()
            break

    out: list[Article] = []
    for node in root.iter():
        name = _localname(node.tag)
        if name not in ("item", "entry"):
            continue

        fields: dict[str, str] = {}
        link = ""
        for child in node:
            tag = _localname(child.tag)
            if tag == "link":
                link = (child.get("href") or child.text or "").strip()
            elif child.text:
                fields.setdefault(tag, child.text.strip())

        headline = strip_html(fields.get("title"))
        published = parse_datetime(
            fields.get("pubdate") or fields.get("published") or fields.get("updated")
        )
        url = link or fields.get("guid", "")
        if not headline or published is None or not url:
            continue

        out.append(
            Article(
                provider=PROVIDER,
                source=default_source or channel_title,
                url=url,
                headline=headline,
                body=strip_html(fields.get("description") or fields.get("summary")),
                published_at=published,
                published_at_source=PUBLISHED_FROM_FEED,
            )
        )
    return out


def fetch_feed(url: str, timeout: float = 20.0) -> str:
    """GET a feed, retrying once with a different User-Agent on a 403."""
    last: Exception | None = None
    for agent in USER_AGENTS:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": agent})
        if resp.status_code == 403:
            last = requests.HTTPError(f"403 for {url} as {agent!r}", response=resp)
            continue
        resp.raise_for_status()
        return resp.text
    raise last  # noqa: RSE102 - always set when the loop falls through


def ingest_rss(
    db: Database,
    feeds: list[str] | None = None,
    since: dt.datetime | None = None,
    delay: float | None = None,
    fetch=fetch_feed,
) -> tuple[StoreStats, dict[str, int]]:
    """Fetch every configured feed and store what it returns.

    A feed that fails is logged and skipped rather than aborting the run — one
    outlet changing its URL should not cost the other eight.
    """
    feeds = feeds if feeds is not None else settings.news_feeds
    delay = delay if delay is not None else settings.request_delay_seconds
    resolver = TickerResolver.from_db(db)

    articles: list[Article] = []
    per_feed: dict[str, int] = {}

    for i, url in enumerate(feeds):
        if i:
            time.sleep(delay)
        try:
            items = parse_feed(fetch(url), url)
        except Exception as e:  # noqa: BLE001 - any outlet, any failure, keep going
            log.warning("feed failed %s: %s", url, e)
            per_feed[url] = -1
            continue

        if since is not None:
            items = [a for a in items if a.published_at >= since]
        per_feed[url] = len(items)
        articles.extend(items)

    stats = store_articles(
        db, articles, resolver, settings.news_min_resolution_confidence
    )
    return stats, per_feed
