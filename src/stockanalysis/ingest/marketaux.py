"""Marketaux ingest — entity-tagged news.

DESIGN §3.3's reason for including it is that it "knows which ticker an article
is about, which saves building a resolver". We built the resolver anyway
(RSS and GDELT need one), so what Marketaux is actually worth here is a
**second opinion on attribution**: its entity tags and our alias matcher can be
compared on the same articles, and disagreement is the cheapest available
measurement of resolver precision.

Its own per-entity `sentiment_score` is stored as a separate row in
`news_sentiment` under model `marketaux`, alongside FinBERT's. It is not used
by the factor — one scorer decides, and DESIGN §5.5 names FinBERT — but having
two independent scores on the same text turns "is FinBERT sane?" into a query
rather than an opinion.

FREE TIER IS SMALL. 100 requests/day, and each returns 3 articles. That is a
supplement to RSS, not a source in its own right, and nowhere near enough to
backfill history.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

import pandas as pd
import requests

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.news.store import (
    Article,
    StoreStats,
    article_id,
    news_id,
    store_articles,
    to_ist,
)

log = logging.getLogger(__name__)

PROVIDER = "MARKETAUX"
PUBLISHED_FROM_API = "MARKETAUX"
MODEL_NAME = "marketaux"

# Marketaux suffixes NSE tickers with .NS and BSE with .BO, the yfinance
# convention.
NSE_SUFFIX = ".NS"
BSE_SUFFIX = ".BO"

SYMBOLS_PER_REQUEST = 10


class MarketauxUnavailableError(RuntimeError):
    """No API key configured."""


def _symbol_to_isin(db: Database) -> dict[str, str]:
    df = db.query(
        "SELECT isin, nse_symbol FROM instruments WHERE nse_symbol IS NOT NULL"
    )
    return {r.nse_symbol.upper(): r.isin for r in df.itertuples(index=False)}


def parse_response(
    payload: dict, symbol_to_isin: dict[str, str]
) -> tuple[list[Article], list[tuple[str, str, float]]]:
    """(articles, provider sentiment rows).

    An entity we do not hold in `instruments` is ignored rather than stored
    unattributed: Marketaux tags US tickers on the same article, and a Reliance
    story tagged with an unrelated ADR should not enter the table twice.
    """
    articles: list[Article] = []
    sentiments: list[tuple[str, str, float]] = []

    for item in payload.get("data") or []:
        url = (item.get("url") or "").strip()
        headline = (item.get("title") or "").strip()
        published = _parse_ts(item.get("published_at"))
        if not headline or published is None:
            continue

        isins, per_entity = [], []
        for ent in item.get("entities") or []:
            symbol = (ent.get("symbol") or "").upper()
            base = symbol.replace(NSE_SUFFIX, "").replace(BSE_SUFFIX, "")
            isin = symbol_to_isin.get(base)
            if isin is None or isin in isins:
                continue
            isins.append(isin)
            score = ent.get("sentiment_score")
            if score is not None:
                per_entity.append((isin, float(score)))

        if not isins:
            continue

        art = Article(
            provider=PROVIDER,
            source=(item.get("source") or "marketaux"),
            url=url,
            headline=headline,
            body=(item.get("description") or item.get("snippet") or "").strip() or None,
            published_at=published,
            published_at_source=PUBLISHED_FROM_API,
            entity_isins=tuple(isins),
        )
        articles.append(art)

        aid = article_id(art.url, art.headline, to_ist(art.published_at))
        for isin, score in per_entity:
            sentiments.append((news_id(aid, isin), _label(score), score))

    return articles, sentiments


def _label(score: float) -> str:
    if score > 0.15:
        return "positive"
    if score < -0.15:
        return "negative"
    return "neutral"


def _parse_ts(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return to_ist(dt.datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def ingest_marketaux(
    db: Database,
    isins: list[str] | None = None,
    published_after: dt.datetime | None = None,
    max_requests: int = 20,
    delay: float | None = None,
    fetch=None,
) -> StoreStats:
    """Pull entity-tagged news for the given instruments."""
    if not settings.marketaux_api_key and fetch is None:
        raise MarketauxUnavailableError(
            "SA_MARKETAUX_API_KEY is not set. Marketaux is optional — RSS and "
            "GDELT need no key — so this command is skippable rather than "
            "blocking."
        )

    delay = delay if delay is not None else settings.request_delay_seconds
    mapping = _symbol_to_isin(db)
    wanted = set(isins) if isins else None
    symbols = sorted(
        sym for sym, isin in mapping.items() if wanted is None or isin in wanted
    )

    total = StoreStats()
    all_sentiments: list[tuple[str, str, float]] = []
    requests_made = 0

    for i in range(0, len(symbols), SYMBOLS_PER_REQUEST):
        if requests_made >= max_requests:
            log.info("marketaux: stopping at the %d-request budget", max_requests)
            break
        batch = symbols[i : i + SYMBOLS_PER_REQUEST]
        params = {
            "api_token": settings.marketaux_api_key,
            "symbols": ",".join(f"{s}{NSE_SUFFIX}" for s in batch),
            "filter_entities": "true",
            "language": "en",
            "limit": 3,
        }
        if published_after:
            params["published_after"] = published_after.strftime("%Y-%m-%dT%H:%M")

        try:
            if requests_made:
                time.sleep(delay)
            payload = (fetch or _fetch)(params)
            requests_made += 1
        except Exception as e:  # noqa: BLE001 - third-party API, many failure modes
            log.warning("marketaux batch failed: %s", e)
            continue

        articles, sentiments = parse_response(payload, mapping)
        all_sentiments.extend(sentiments)
        stats = store_articles(
            db, articles, resolver=None,
            min_confidence=settings.news_min_resolution_confidence,
        )
        _merge(total, stats)

    if all_sentiments:
        _store_provider_sentiment(db, all_sentiments)
    return total


def _fetch(params: dict) -> dict:
    resp = requests.get(settings.marketaux_base_url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _merge(total: StoreStats, part: StoreStats) -> None:
    total.fetched += part.fetched
    total.stored += part.stored
    total.resolved += part.resolved
    total.unresolved += part.unresolved
    total.duplicates += part.duplicates
    total.below_threshold += part.below_threshold
    for k, v in part.by_method.items():
        total.by_method[k] = total.by_method.get(k, 0) + v


def _store_provider_sentiment(
    db: Database, rows: list[tuple[str, str, float]]
) -> int:
    """Write Marketaux's own scores under their own model name.

    Only for news_ids that actually made it into `news` — a row dropped as a
    duplicate has no article to attach a score to, and a foreign score with no
    article is exactly the kind of orphan that later reads as coverage.
    """
    present = set(
        db.query("SELECT news_id FROM news")["news_id"].tolist()
    )
    keep = [r for r in rows if r[0] in present]
    if not keep:
        return 0
    df = pd.DataFrame(
        [
            {
                "news_id": nid,
                "model": MODEL_NAME,
                "label": label,
                "score": score,
                "computed_at": dt.datetime.now(),
            }
            for nid, label, score in keep
        ]
    )
    return db.upsert_df("news_sentiment", df, ["news_id", "model"])
