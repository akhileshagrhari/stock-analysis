"""GDELT historical backfill — the only source in this phase with a past.

DESIGN §3.3: "Needed to backtest the sentiment factor, which RSS cannot do (no
history)." That is the entire justification for this module, and it sets the
standard it has to meet: whatever comes out of here has to be usable as of a
date in 2022, or the sentiment factor stays an assumption.

THREE THINGS THAT DECIDE WHETHER IT IS
--------------------------------------
**`seendate` is a crawl time, not a publication time.** The DOC 2.0 artlist
response has no publication timestamp — `seendate` is when GDELT's crawler
first saw the article, which is at or after publication, usually by minutes and
occasionally by a day. Using it as the knowledge date is therefore
*conservative* in exactly the direction that matters: the backtest learns about
a story no earlier than it could have, and never earlier. It is recorded as
`published_at_source = 'GDELT_SEENDATE'` so no one later mistakes it for the
byline time.

**Full-text search is recall, not attribution.** A query for "Titan Company"
returns articles about titanium, about Titan Cement, and about companies
mentioned in passing next to Titan. Every returned article is therefore put
back through the alias resolver and kept only if it names the queried company
in its own title — see `store._mentions_for`. The alternative, trusting the
query, produces a dataset that agrees with itself by construction.

**One request every five seconds, and they mean it.** GDELT answers a faster
caller with HTTP 429 and a plain-text scolding rather than JSON. At the
documented rate a Nifty-100 x 3-year backfill is 100 x 36 = 3,600 requests and
about six hours, so the work is checkpointed per (company, month) in
`news_backfill_log` and a re-run resumes instead of restarting.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import dataclass

import pandas as pd
import requests

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.news.aliases import normalise, strip_legal_suffix
from stockanalysis.news.resolve import TickerResolver
from stockanalysis.news.store import Article, StoreStats, as_date, store_articles

log = logging.getLogger(__name__)

PROVIDER = "GDELT"
PUBLISHED_FROM_SEENDATE = "GDELT_SEENDATE"

# GDELT's DOC 2.0 index starts here. Asking for earlier windows returns nothing
# and still costs a request, so the backfill clamps rather than trying.
GDELT_EPOCH = dt.date(2017, 1, 1)

# Attempts per window before it is left for the next run. Four covers the
# observed throttling; more would spend the whole budget on one month.
GDELT_ATTEMPTS = 4


class GdeltThrottledError(RuntimeError):
    """GDELT returned its rate-limit message instead of JSON."""


@dataclass(frozen=True)
class Window:
    isin: str
    query: str
    start: dt.date
    end: dt.date


def month_windows(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Calendar months covering [start, end].

    Monthly rather than daily because the request budget is the binding
    constraint, and monthly rather than yearly because `maxrecords` caps a
    response at 250 articles — a year of coverage for a large-cap would be
    truncated, and truncation biased towards whatever GDELT ranks first is
    worse than a smaller sample.
    """
    start = max(start, GDELT_EPOCH)
    out: list[tuple[dt.date, dt.date]] = []
    cur = dt.date(start.year, start.month, 1)
    while cur <= end:
        nxt = dt.date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
        out.append((max(cur, start), min(nxt - dt.timedelta(days=1), end)))
        cur = nxt
    return out


def build_query(name: str) -> str:
    """The search string for a company.

    Quoted phrase — an unquoted "Tata Motors" matches any article containing
    both words anywhere — plus an English filter, since the factor's scorer is
    an English-language model and a Hindi article would be scored as noise.
    """
    cleaned = strip_legal_suffix(normalise(name))
    return f'"{cleaned}" sourcelang:english'


def parse_artlist(payload: str, isin: str) -> list[Article]:
    """Parse a DOC 2.0 artlist response.

    GDELT answers rate limiting and malformed queries with plain text and a
    200 status, so a JSON decode failure here is a throttle until proven
    otherwise — treating it as "no articles" would checkpoint an empty window
    and permanently lose that month.
    """
    text = payload.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise GdeltThrottledError(text[:200]) from e

    out: list[Article] = []
    for rec in data.get("articles") or []:
        headline = (rec.get("title") or "").strip()
        seen = _parse_seendate(rec.get("seendate"))
        url = (rec.get("url") or "").strip()
        if not headline or seen is None or not url:
            continue
        out.append(
            Article(
                provider=PROVIDER,
                source=rec.get("domain") or "unknown",
                url=url,
                headline=headline,
                body=None,          # artlist returns titles only
                published_at=seen,
                published_at_source=PUBLISHED_FROM_SEENDATE,
                query_isin=isin,
            )
        )
    return out


def _parse_seendate(raw: str | None) -> dt.datetime | None:
    """`20260813T043000Z` -> aware UTC. `store.to_ist` converts it."""
    if not raw:
        return None
    try:
        return dt.datetime.strptime(raw.strip(), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=dt.UTC
        )
    except ValueError:
        return None


def _fetch_once(query: str, start: dt.date, end: dt.date, max_records: int) -> str:
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": max_records,
        "sort": "datedesc",
        "startdatetime": f"{start:%Y%m%d}000000",
        "enddatetime": f"{end:%Y%m%d}235959",
    }
    resp = requests.get(
        settings.gdelt_base_url,
        params=params,
        timeout=60,
        headers={"User-Agent": "stockanalysis/0.1 (research)"},
    )
    if resp.status_code == 429:
        # Same condition as the plain-text scolding, different dress. GDELT
        # returns either depending on which layer refuses.
        raise GdeltThrottledError(resp.text[:120])
    resp.raise_for_status()
    return resp.text


def _fetch(query: str, start: dt.date, end: dt.date, max_records: int) -> str:
    """One window, retried through throttling.

    **Measured, not assumed:** at 6s spacing 3 of 4 requests came back 429; at
    12s and 20s spacing, 4 of 4. The documented "one request every 5 seconds"
    describes GDELT's policy, not what the service currently grants — responses
    themselves take 11-17s, which is a loaded backend rather than our pacing.
    So a window is retried with a widening gap rather than surrendered, and
    whatever is still failing after that is left for the next run to pick up
    from the checkpoint.
    """
    delay = settings.gdelt_delay_seconds
    last: Exception | None = None
    for attempt in range(GDELT_ATTEMPTS):
        if attempt:
            time.sleep(delay * (2**attempt))
        try:
            return _fetch_once(query, start, end, max_records)
        except GdeltThrottledError as e:
            last = e
    raise last  # noqa: RSE102 - always set after a failing loop


def pending_windows(
    db: Database, isins: list[str], start: dt.date, end: dt.date
) -> list[Window]:
    """(company, month) pairs not yet successfully fetched."""
    placeholders = ", ".join("?" for _ in isins) if isins else "''"
    names = db.query(
        f"SELECT isin, name FROM instruments WHERE isin IN ({placeholders})",
        list(isins),
    )
    done = db.query(
        "SELECT isin, window_start FROM news_backfill_log "
        "WHERE provider = ? AND status = 'OK'",
        [PROVIDER],
    )
    done_keys = {
        (r.isin, as_date(r.window_start)) for r in done.itertuples(index=False)
    }

    out: list[Window] = []
    for row in names.itertuples(index=False):
        query = build_query(row.name)
        for w_start, w_end in month_windows(start, end):
            if (row.isin, w_start) in done_keys:
                continue
            out.append(Window(row.isin, query, w_start, w_end))
    return out


def backfill_gdelt(
    db: Database,
    isins: list[str],
    start: dt.date,
    end: dt.date,
    max_requests: int | None = None,
    delay: float | None = None,
    fetch=_fetch,
    progress=None,
) -> tuple[StoreStats, int]:
    """Fetch and store historical coverage. Returns (stats, windows completed).

    Interruptible by design: every window is checkpointed as it lands, so a
    six-hour job that dies after two hours resumes from where it stopped.
    """
    delay = delay if delay is not None else settings.gdelt_delay_seconds
    resolver = TickerResolver.from_db(db)
    windows = pending_windows(db, isins, start, end)
    if max_requests:
        windows = windows[:max_requests]

    total = StoreStats()
    completed = 0

    for i, w in enumerate(windows, start=1):
        if i > 1:
            time.sleep(delay)
        try:
            articles = parse_artlist(
                fetch(w.query, w.start, w.end, settings.gdelt_max_records), w.isin
            )
        except GdeltThrottledError as e:
            # Not checkpointed: this window has no result yet, and recording
            # one would turn a transient throttle into a permanent hole.
            log.warning("gdelt throttled on %s %s: %s", w.isin, w.start, e)
            _log_window(db, w, 0, "ERROR", "throttled")
            time.sleep(delay * 3)
            continue
        except Exception as e:  # noqa: BLE001 - network, DNS, 5xx
            log.warning("gdelt failed %s %s: %s", w.isin, w.start, e)
            _log_window(db, w, 0, "ERROR", str(e)[:200])
            continue

        stats = store_articles(
            db, articles, resolver, settings.news_min_resolution_confidence
        )
        _merge(total, stats)
        _log_window(db, w, len(articles), "OK", None)
        completed += 1

        if progress:
            progress(i, len(windows), w, stats)

    return total, completed


def _log_window(
    db: Database, w: Window, articles: int, status: str, detail: str | None
) -> None:
    db.upsert_df(
        "news_backfill_log",
        pd.DataFrame(
            [
                {
                    "provider": PROVIDER,
                    "isin": w.isin,
                    "window_start": w.start,
                    "window_end": w.end,
                    "articles": articles,
                    "status": status,
                    "detail": detail,
                    "fetched_at": dt.datetime.now(),
                }
            ]
        ),
        ["provider", "isin", "window_start"],
    )


def _merge(total: StoreStats, part: StoreStats) -> None:
    total.fetched += part.fetched
    total.stored += part.stored
    total.resolved += part.resolved
    total.unresolved += part.unresolved
    total.duplicates += part.duplicates
    total.below_threshold += part.below_threshold
    total.unconfirmed += part.unconfirmed
    for k, v in part.by_method.items():
        total.by_method[k] = total.by_method.get(k, 0) + v
