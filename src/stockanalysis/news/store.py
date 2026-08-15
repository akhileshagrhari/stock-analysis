"""Turning fetched articles into `news` rows.

Every provider — RSS, Marketaux, GDELT — funnels through `store_articles`, so
the four decisions that determine whether the sentiment factor is backtestable
are made exactly once.

1. **The knowledge date is `published_at`, in IST.** Feeds report RFC-822
   timestamps with a +0530 offset; GDELT reports UTC. Both are converted to
   Asia/Kolkata and stored naive, because every other date in this database is
   an Indian trading date and `as_of_sentiment` compares against
   `datetime.combine(as_of, time.max)`. Store UTC instead and every article
   published after 5:30am IST silently lands on the wrong side of a decision
   date boundary once a month.

2. **One row per (article, company).** See schema.sql.

3. **Syndication is not sentiment.** The same wire story carried by four
   outlets is one piece of news, not four. Rows are deduplicated on
   (isin, content_hash, publication date) keeping the earliest timestamp,
   because a factor that averages sentiment would otherwise quadruple-weight
   whatever PTI put out that morning. Only exact normalised-headline matches
   are caught — a rewritten headline survives as a separate article, and that
   residual is reported rather than assumed away.

4. **Unresolved articles are stored, not dropped.** With isin NULL they are
   invisible to every factor and cheap to keep, and they are the only way to
   state a resolution rate honestly.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

import pandas as pd

from stockanalysis.db.database import Database
from stockanalysis.news.aliases import normalise
from stockanalysis.news.resolve import Mention, TickerResolver

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

UNRESOLVED = "UNRESOLVED"
PROVIDER_ENTITY = "PROVIDER_ENTITY"
ROUNDUP = "ROUNDUP"

# An article naming more than this many companies is a list, not news about a
# company. Found in the first live FinBERT run: "Top Gainers & Losers on 13
# August: Apar Industries, Hindalco, Ather Energy, Force Motors" scored -0.97
# and that number was attributed to Hindalco, which was in the *gainers* half.
# "Reliance Industries, Adani Enterprises among 10 stocks with highest DII
# buying" gave six companies an identical +0.93.
#
# The problem is not the resolver — every one of those mentions is real. It is
# that a document-level sentiment score is only meaningful when the document is
# about one thing, and no amount of better attribution fixes a roundup. Three
# is the cutoff because genuine two-company stories are common ("Tata Motors
# gains as JLR margins beat") and genuine four-company ones are not.
MAX_COMPANIES_PER_ARTICLE = 3

# Demoted rather than dropped: below the 0.7 ingest threshold, so the row is
# stored and visible to `news-status` but never scored or read by the factor.
ROUNDUP_CONFIDENCE = 0.5

NEWS_KEY = ["news_id"]


@dataclass(frozen=True)
class Article:
    """One fetched article, before it is attributed to any company."""

    provider: str                       # RSS | MARKETAUX | GDELT
    source: str                         # the outlet
    url: str
    headline: str
    published_at: dt.datetime
    published_at_source: str            # FEED | MARKETAUX | GDELT_SEENDATE
    body: str | None = None
    # Providers that tag entities themselves (Marketaux) supply ISINs here and
    # skip the text resolver entirely.
    entity_isins: tuple[str, ...] = ()
    # Providers that searched *for* a company (GDELT) supply it here; the
    # resolver must then confirm it in the text before it is believed.
    query_isin: str | None = None


@dataclass
class StoreStats:
    fetched: int = 0
    stored: int = 0
    resolved: int = 0
    unresolved: int = 0
    duplicates: int = 0
    below_threshold: int = 0
    unconfirmed: int = 0
    by_method: dict[str, int] = field(default_factory=dict)

    @property
    def resolution_rate(self) -> float:
        total = self.resolved + self.unresolved
        return self.resolved / total if total else 0.0

    def __str__(self) -> str:
        return (
            f"fetched {self.fetched}  stored {self.stored}  "
            f"resolved {self.resolved}  unresolved {self.unresolved}  "
            f"dupes {self.duplicates}"
        )


def to_ist(when: dt.datetime) -> dt.datetime:
    """Naive IST. A naive input is assumed to already be IST."""
    if when.tzinfo is None:
        return when
    return when.astimezone(IST).replace(tzinfo=None)


def content_hash(headline: str) -> str:
    """Identity of the *story*, for syndication dedupe.

    Headline only, normalised. Bodies differ between outlets carrying the same
    wire copy — including the body would defeat the purpose — and a headline
    that genuinely recurs ("Sensex ends higher") is disambiguated by the
    publication date, which is part of the dedupe key rather than this hash.
    """
    return hashlib.sha1(normalise(headline).encode()).hexdigest()[:16]


def article_id(url: str, headline: str, published_at: dt.datetime) -> str:
    """Stable identity for one article from one outlet.

    Keyed on the URL where there is one, so re-fetching a feed does not create
    duplicates. GDELT sometimes returns the same story under an AMP URL and a
    canonical one; the content-hash dedupe catches that pair afterwards.
    """
    basis = url.strip() or f"{normalise(headline)}|{published_at:%Y-%m-%d}"
    return hashlib.sha1(basis.encode()).hexdigest()[:20]


def news_id(article_id_: str, isin: str | None) -> str:
    return f"{article_id_}:{isin}" if isin else article_id_


def store_articles(
    db: Database,
    articles: list[Article],
    resolver: TickerResolver | None,
    min_confidence: float,
    now: dt.datetime | None = None,
) -> StoreStats:
    """Resolve, deduplicate and persist. Returns what happened to each article."""
    stats = StoreStats(fetched=len(articles))
    if not articles:
        return stats

    ingested_at = now or dt.datetime.now()
    seen = _existing_story_keys(db, articles)
    # The same URL genuinely appears in two of the configured feeds — an
    # Economic Times story is in both the markets and the stocks feed — and an
    # article naming no company skips the story-level dedupe below, because
    # that one is keyed on ISIN. Without this guard the batch carries two rows
    # with one primary key and DuckDB aborts the whole ingest.
    emitted: set[str] = set()
    rows: list[dict] = []

    for art in articles:
        published = to_ist(art.published_at)
        aid = article_id(art.url, art.headline, published)
        chash = content_hash(art.headline)

        mentions = _mentions_for(art, resolver, stats)
        if not mentions:
            nid = news_id(aid, None)
            if nid in emitted:
                stats.duplicates += 1
                continue
            emitted.add(nid)
            stats.unresolved += 1
            rows.append(_row(art, aid, chash, published, ingested_at, None))
            continue

        usable = duplicated = 0
        for m in mentions:
            key = (m.isin, chash, published.date())
            nid = news_id(aid, m.isin)
            if key in seen or nid in emitted:
                stats.duplicates += 1
                duplicated += 1
                continue
            seen.add(key)
            emitted.add(nid)

            if m.confidence < min_confidence:
                # Stored anyway, with its ISIN, and skipped by the scorer —
                # which is where the threshold is enforced. A later threshold
                # change or a better alias table can then reuse the text
                # without re-fetching anything.
                stats.below_threshold += 1
            else:
                usable += 1
                stats.by_method[m.method] = stats.by_method.get(m.method, 0) + 1

            rows.append(_row(art, aid, chash, published, ingested_at, m))

        if usable:
            stats.resolved += 1
        elif duplicated < len(mentions):
            # Named companies, but none at a confidence the scorer will use.
            # A wholly duplicated article is neither resolved nor unresolved —
            # it is already in the table, and is counted under `duplicates`.
            stats.unresolved += 1

    if rows:
        stats.stored = db.upsert_df("news", pd.DataFrame(rows), NEWS_KEY)
    return stats


def _mentions_for(
    art: Article, resolver: TickerResolver | None, stats: StoreStats
) -> list[Mention]:
    """Which companies this article is about, by whichever route the provider allows."""
    if art.entity_isins:
        return [
            Mention(isin, alias="", method=PROVIDER_ENTITY, confidence=1.0,
                    matched_in="HEADLINE")
            for isin in art.entity_isins
        ]

    if resolver is None:
        return []

    # Demotion is decided on the whole article, *before* the provider filter
    # below narrows it to one company. Order matters: a GDELT query for ABB
    # returned "Stocks to Watch Today: Vedanta, Hindustan Zinc, TCS, Tata
    # Power, ABB, LIC, Bajaj Finance, RVNL", and filtering first left a
    # one-mention list that no roundup rule can recognise. It went into the
    # table at 0.90.
    mentions = demote_roundups(resolver.resolve(art.headline, art.body))

    if art.query_isin is not None and not any(
        m.isin == art.query_isin for m in mentions
    ):
        # GDELT searched for this company and the article's own title does not
        # name it. A full-text index matches paraphrases, related coverage and
        # homonyms — 526 of 645 backfilled articles were this — and attributing
        # them to the queried company would make the backfill self-confirming.
        #
        # Counted, not filtered. Whatever the *text* names is still the
        # attribution, exactly as it is for RSS: an article fetched under a
        # query for Asian Paints whose title is about Berger Paints is news
        # about Berger Paints. Attribution has one rule in this system and the
        # provider's search terms are not part of it — which is also what keeps
        # `reresolve` able to reproduce what the ingest did.
        stats.unconfirmed += 1

    return mentions


def demote_roundups(mentions: list[Mention]) -> list[Mention]:
    """Cap the confidence of every mention in a list-style article."""
    if len(mentions) <= MAX_COMPANIES_PER_ARTICLE:
        return mentions
    return [
        Mention(
            isin=m.isin,
            alias=m.alias,
            method=ROUNDUP,
            confidence=min(m.confidence, ROUNDUP_CONFIDENCE),
            matched_in=m.matched_in,
        )
        for m in mentions
    ]


def _row(
    art: Article,
    aid: str,
    chash: str,
    published: dt.datetime,
    ingested_at: dt.datetime,
    m: Mention | None,
) -> dict:
    return {
        "news_id": news_id(aid, m.isin if m else None),
        "article_id": aid,
        "isin": m.isin if m else None,
        "published_at": published,
        "ingested_at": ingested_at,
        "headline": art.headline[:1000],
        "body": (art.body or "")[:4000] or None,
        "source": art.source,
        "url": art.url,
        "provider": art.provider,
        "published_at_source": art.published_at_source,
        "content_hash": chash,
        "resolution_method": m.method if m else UNRESOLVED,
        "resolution_confidence": m.confidence if m else None,
        "matched_alias": (m.alias or None) if m else None,
        "matched_in": m.matched_in if m else None,
    }


def _existing_story_keys(
    db: Database, articles: list[Article]
) -> set[tuple[str, str, dt.date]]:
    """Stories already stored in the date range being ingested.

    Scoped to the incoming window rather than the whole table: a five-year
    GDELT backfill would otherwise pull every row it has already written into
    memory on each batch.
    """
    dates = [to_ist(a.published_at).date() for a in articles]
    if not dates:
        return set()
    df = db.query(
        "SELECT isin, content_hash, CAST(published_at AS DATE) AS d FROM news "
        "WHERE isin IS NOT NULL AND published_at >= ? AND published_at < ?",
        [
            dt.datetime.combine(min(dates), dt.time.min),
            dt.datetime.combine(max(dates) + dt.timedelta(days=1), dt.time.min),
        ],
    )
    return {(r.isin, r.content_hash, as_date(r.d)) for r in df.itertuples(index=False)}


@dataclass
class ReresolveStats:
    articles: int = 0
    changed: int = 0
    newly_resolved: int = 0
    lost: int = 0
    duplicates_removed: int = 0
    rows_before: int = 0
    rows_after: int = 0


def _drop_duplicate_stories(db: Database) -> int:
    """Collapse (isin, story, day) groups down to their earliest article.

    A repair pass, not part of the normal flow: the ingest path prevents these
    and the re-resolution path now does too, but a table written by an earlier
    version can hold them, and a duplicate story double-weights that day's
    sentiment for that company. The survivor is the earliest publication, so
    re-running is stable.
    """
    dupes = db.query(
        """
        SELECT news_id FROM (
            SELECT news_id, ROW_NUMBER() OVER (
                PARTITION BY isin, content_hash, CAST(published_at AS DATE)
                ORDER BY published_at, news_id
            ) AS rn
            FROM news WHERE isin IS NOT NULL
        ) WHERE rn > 1
        """
    )["news_id"].tolist()
    if not dupes:
        return 0
    for chunk in (dupes[i : i + 200] for i in range(0, len(dupes), 200)):
        marks = ", ".join("?" for _ in chunk)
        db.conn.execute(f"DELETE FROM news_sentiment WHERE news_id IN ({marks})", chunk)
        db.conn.execute(f"DELETE FROM news WHERE news_id IN ({marks})", chunk)
    return len(dupes)


def reresolve(db: Database, resolver: TickerResolver, min_confidence: float) -> ReresolveStats:
    """Re-run the resolver over already-stored article text.

    This is the return on storing unresolved articles. Improving the alias
    table is otherwise worth nothing until the next fetch, and for GDELT the
    next fetch is six hours of rate-limited requests — so an alias fix that
    should cost seconds would instead cost an afternoon, and in practice would
    not get made.

    Rows keep their `news_id` (`article_id:isin`) when the attribution is
    unchanged, so existing sentiment scores survive. An article that resolves
    to a *different* company loses its old row and its old score with it,
    which is correct: that score was attached to the wrong company.
    """
    stats = ReresolveStats()
    stats.duplicates_removed = _drop_duplicate_stories(db)

    stored = db.query(
        "SELECT article_id, ANY_VALUE(headline) AS headline, ANY_VALUE(body) AS body "
        "FROM news GROUP BY article_id"
    )
    if stored.empty:
        return stats

    current = db.query(
        "SELECT article_id, isin, resolution_method, resolution_confidence FROM news"
    )
    by_article: dict[str, set[tuple]] = {}
    isins_before: dict[str, set[str | None]] = {}
    for r in current.itertuples(index=False):
        # `text_or_none` matters here: a SQL NULL arrives as float('nan'), and
        # {nan} != {None} makes every unresolved article look changed. The
        # first live run reported "215 changed, 0 newly resolved" for exactly
        # that reason.
        isin = text_or_none(r.isin)
        # Keyed on the whole attribution rather than the ISIN alone. A rule
        # that changes a mention's *confidence* without changing which company
        # it names — the roundup demotion is precisely that — is a change, and
        # comparing ISINs reported "0 changed" while leaving every stale
        # confidence in the table.
        by_article.setdefault(r.article_id, set()).add(
            (isin, text_or_none(r.resolution_method), _round(r.resolution_confidence))
        )
        isins_before.setdefault(r.article_id, set()).add(isin)
    stats.rows_before = len(current)

    resolved_now: dict[str, list[Mention]] = {}
    changed_articles: list[str] = []
    for r in stored.itertuples(index=False):
        mentions = demote_roundups(
            resolver.resolve(text_or_none(r.headline), text_or_none(r.body))
        )
        resolved_now[r.article_id] = mentions
        # Compared over *every* mention, not just the usable ones, because the
        # ingest path stores below-threshold rows too and this must leave the
        # table in the same shape a fresh ingest would.
        wanted = {
            (m.isin, m.method, _round(m.confidence)) for m in mentions
        } or {(None, UNRESOLVED, None)}
        before = by_article.get(r.article_id, set())
        if wanted == before:
            continue

        changed_articles.append(r.article_id)
        stats.changed += 1

        was = isins_before.get(r.article_id, set())
        usable = {m.isin for m in mentions if m.confidence >= min_confidence}
        if was == {None} and usable:
            stats.newly_resolved += 1
        elif was - {None} - {m.isin for m in mentions}:
            stats.lost += 1

    stats.articles = len(stored)
    if not changed_articles:
        stats.rows_after = stats.rows_before
        return stats

    keep = stored[stored["article_id"].isin(changed_articles)]
    originals = db.query(
        f"SELECT * FROM news WHERE article_id IN "
        f"({', '.join('?' for _ in changed_articles)})",
        changed_articles,
    ).groupby("article_id", as_index=False).head(1).set_index("article_id")

    # The syndication dedupe applies here too, and did not at first: an article
    # that only *becomes* resolvable when the alias table improves can land on
    # a story another article already carries. Two Moneycontrol URLs for the
    # same "Tata Consumer Q4 net profit falls 19%" story ended up in the table
    # that way, one of them re-resolved into a duplicate of the other. Stories
    # already claimed by an article this pass is not touching are seeded first.
    claimed = {
        (r.isin, r.content_hash, as_date(r.published_at))
        for r in db.query(
            "SELECT isin, content_hash, published_at, article_id FROM news "
            "WHERE isin IS NOT NULL"
        ).itertuples(index=False)
        if r.article_id not in set(changed_articles)
    }

    rows = []
    for r in sorted(keep.itertuples(index=False), key=lambda x: x.article_id):
        base = originals.loc[r.article_id]
        chash = text_or_none(base["content_hash"])
        published = as_date(base["published_at"])

        emitted_for_article = []
        for m in resolved_now[r.article_id]:
            key = (m.isin, chash, published)
            if key in claimed:
                continue
            claimed.add(key)
            emitted_for_article.append(m)

        for m in emitted_for_article or [None]:
            row = {c: base[c] for c in originals.columns}
            row["article_id"] = r.article_id
            row["news_id"] = news_id(r.article_id, m.isin if m else None)
            row["isin"] = m.isin if m else None
            row["resolution_method"] = m.method if m else UNRESOLVED
            row["resolution_confidence"] = m.confidence if m else None
            row["matched_alias"] = (m.alias or None) if m else None
            row["matched_in"] = m.matched_in if m else None
            rows.append(row)

    # Deleted by `news_id`, the primary key, and only the rows that are
    # actually going away. The obvious version — DELETE ... WHERE article_id IN
    # (110 placeholders), then re-insert — killed the connection with
    # `FATAL Error: Failed to delete all rows from index. Only deleted 35 out
    # of 110 rows`: DuckDB was deleting a wide, non-key column match out of a
    # primary-key index. The transaction rolled back cleanly, which is the only
    # reason this was a bug report rather than a restore.
    keep_ids = {row["news_id"] for row in rows}
    stale = [
        r.news_id
        for r in db.query(
            f"SELECT news_id FROM news WHERE article_id IN "
            f"({', '.join('?' for _ in changed_articles)})",
            changed_articles,
        ).itertuples(index=False)
        if r.news_id not in keep_ids
    ]
    for chunk in (stale[i : i + 200] for i in range(0, len(stale), 200)):
        marks = ", ".join("?" for _ in chunk)
        # The score went with the attribution it was attached to.
        db.conn.execute(f"DELETE FROM news_sentiment WHERE news_id IN ({marks})", chunk)
        db.conn.execute(f"DELETE FROM news WHERE news_id IN ({marks})", chunk)

    db.upsert_df("news", pd.DataFrame(rows), NEWS_KEY)
    stats.rows_after = db.query("SELECT COUNT(*) AS c FROM news")["c"].iloc[0]
    return stats


def _round(value) -> float | None:
    return None if value is None or pd.isna(value) else round(float(value), 4)


def text_or_none(value) -> str | None:
    """A SQL NULL comes back from pandas as `float('nan')`, not as None.

    Shared because every module that reads a nullable text column out of
    DuckDB hits it — three separate `AttributeError: 'float' object has no
    attribute 'strip'` crashes during phase 3 before it was factored out.
    """
    return None if value is None or pd.isna(value) else str(value)


def as_date(value) -> dt.date:
    """A real `datetime.date`, whatever pandas handed back.

    `pd.Timestamp` subclasses `datetime.date`, so an isinstance check passes
    and leaves a Timestamp in place — which then compares unequal to every
    `date` in a set key and silently disables deduplication. Found by the
    re-run test, which is the only place the difference is observable.
    """
    return pd.Timestamp(value).date()
