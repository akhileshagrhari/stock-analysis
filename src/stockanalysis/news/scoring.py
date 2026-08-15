"""Running a scorer over stored news and persisting the result.

Separated from `finbert.py` so the pipeline can be tested without loading a
440MB model, and so a second scorer (Marketaux's, or a FinGPT LoRA later) drops
in behind the same `score(texts) -> [SentimentScore]` interface.

WHERE THE RESOLUTION THRESHOLD IS ENFORCED
------------------------------------------
Here, not at ingest. `store_articles` writes every mention it finds, including
the low-confidence single-token ones; `pending_news` then declines to score
anything below `news_min_resolution_confidence`. Two consequences worth being
deliberate about: an unscored row is invisible to the factor (the factor reads
through a join on `news_sentiment`), and raising the threshold later costs
nothing but a re-run of this step, with no re-fetching.

The scored text is `headline. body`, truncated by the tokenizer. Bodies are
RSS descriptions — two or three sentences — so this is closer to "headline plus
standfirst" than to a full article, which is what FinBERT was fine-tuned on
anyway.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import pandas as pd

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.news.store import text_or_none

log = logging.getLogger(__name__)


@dataclass
class ScoreStats:
    scored: int = 0
    reused: int = 0            # identical text already scored in this run
    skipped_unresolved: int = 0
    by_label: dict[str, int] = field(default_factory=dict)

    def __str__(self) -> str:
        labels = "  ".join(f"{k} {v}" for k, v in sorted(self.by_label.items()))
        return f"scored {self.scored}  deduped {self.reused}  [{labels}]"


def scoring_text(headline: str, body: str | None) -> str:
    """What the model actually sees."""
    head = (text_or_none(headline) or "").strip()
    tail = (text_or_none(body) or "").strip()
    if tail and tail.lower() != head.lower():
        return f"{head}. {tail}"
    return head


def pending_news(
    db: Database,
    model: str,
    limit: int | None = None,
    min_confidence: float | None = None,
) -> pd.DataFrame:
    """Resolved, above-threshold news rows with no score from `model` yet."""
    threshold = (
        min_confidence
        if min_confidence is not None
        else settings.news_min_resolution_confidence
    )
    sql = """
        SELECT n.news_id, n.isin, n.headline, n.body, n.published_at
        FROM news n
        LEFT JOIN news_sentiment s
               ON s.news_id = n.news_id AND s.model = ?
        WHERE n.isin IS NOT NULL
          AND n.resolution_confidence >= ?
          AND s.news_id IS NULL
        ORDER BY n.published_at DESC
    """
    params: list = [model, threshold]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return db.query(sql, params)


def score_news(
    db: Database,
    scorer,
    limit: int | None = None,
    min_confidence: float | None = None,
    progress=None,
) -> ScoreStats:
    """Score everything outstanding and write `news_sentiment` rows.

    Identical text is scored once. A wire story that survived the ingest
    deduplication because it names three companies is one inference, not
    three — the model's output is a function of the text alone.
    """
    stats = ScoreStats()
    df = pending_news(db, scorer.name, limit=limit, min_confidence=min_confidence)
    if df.empty:
        return stats

    texts = [scoring_text(r.headline, r.body) for r in df.itertuples(index=False)]

    unique: dict[str, int] = {}
    for t in texts:
        unique.setdefault(t, len(unique))
    stats.reused = len(texts) - len(unique)

    ordered = sorted(unique, key=unique.get)
    results = scorer.score(ordered)
    if len(results) != len(ordered):
        raise RuntimeError(
            f"scorer returned {len(results)} scores for {len(ordered)} texts"
        )
    by_text = dict(zip(ordered, results, strict=True))

    now = dt.datetime.now()
    rows = []
    for news_id, text in zip(df["news_id"], texts, strict=True):
        s = by_text[text]
        stats.by_label[s.label] = stats.by_label.get(s.label, 0) + 1
        rows.append(
            {
                "news_id": news_id,
                "model": scorer.name,
                "label": s.label,
                "score": s.score,
                "computed_at": now,
            }
        )
        if progress and len(rows) % 200 == 0:
            progress(len(rows), len(texts))

    stats.scored = db.upsert_df(
        "news_sentiment", pd.DataFrame(rows), ["news_id", "model"]
    )
    return stats


def coverage_report(db: Database, model: str | None = None) -> pd.DataFrame:
    """Per-month article and scored-article counts. The backtestability check.

    A sentiment factor needs ~3 articles per company per 30-day window to
    compute at all (`factors/sentiment.py::MIN_ARTICLES`), so the question this
    answers is not "do we have news" but "on how many rebalance dates would the
    factor have had anything to say".
    """
    model_filter = "AND s.model = ?" if model else ""
    params = [model] if model else []
    return db.query(
        f"""
        SELECT date_trunc('month', n.published_at)::DATE AS month,
               COUNT(*)                                  AS articles,
               COUNT(DISTINCT n.isin)                    AS companies,
               COUNT(s.news_id)                          AS scored
        FROM news n
        LEFT JOIN news_sentiment s ON s.news_id = n.news_id {model_filter}
        WHERE n.isin IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """,
        params,
    )


def resolution_report(db: Database) -> pd.DataFrame:
    """How articles were attributed, by provider and method."""
    return db.query(
        """
        SELECT provider,
               COALESCE(resolution_method, 'UNRESOLVED') AS method,
               COUNT(*)                                  AS rows,
               ROUND(AVG(resolution_confidence), 3)      AS avg_conf
        FROM news
        GROUP BY 1, 2 ORDER BY 1, 3 DESC
        """
    )
