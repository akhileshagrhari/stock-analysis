"""Sentiment scoring and the sentiment factor, end to end.

The load-bearing test in this file is `test_finbert_label_order_is_read_from_the
_model`. FinBERT's `id2label` is {0: positive, 1: negative, 2: neutral}, which
is neither alphabetical nor the negative-first order most classifiers use. Code
that assumes an order inverts the entire factor, and nothing downstream would
notice: the backtest still runs, the score still moves, and every conclusion is
backwards.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from stockanalysis.factors.panel import PanelCache
from stockanalysis.factors.sentiment import MIN_ARTICLES, NewsSentiment30d
from stockanalysis.news.finbert import (
    ScorerUnavailableError,
    SentimentScore,
    _label_index,
    signed_score,
)
from stockanalysis.news.scoring import (
    coverage_report,
    pending_news,
    score_news,
    scoring_text,
)

FINBERT_CACHE = (
    Path.home() / ".cache/huggingface/hub/models--ProsusAI--finbert"
)


@dataclass
class FakeScorer:
    """Scores by keyword. Lets the pipeline be tested without a 440MB download."""

    name: str = "fake-scorer"
    calls: list[list[str]] = None

    def __post_init__(self):
        self.calls = []

    def score(self, texts: list[str]) -> list[SentimentScore]:
        self.calls.append(list(texts))
        out = []
        for t in texts:
            low = t.lower()
            p_pos = 0.8 if any(w in low for w in ("jumps", "beats", "wins")) else 0.1
            p_neg = 0.8 if any(w in low for w in ("falls", "probe", "cuts")) else 0.1
            p_neu = max(0.0, 1.0 - p_pos - p_neg)
            label = ("positive" if p_pos > max(p_neg, p_neu)
                     else "negative" if p_neg > p_neu else "neutral")
            out.append(
                SentimentScore(label, signed_score(p_pos, p_neg), p_pos, p_neg, p_neu)
            )
        return out


# ----------------------------------------------------------------------
# The sign convention
# ----------------------------------------------------------------------


def test_the_score_is_signed_and_neutral_is_zero():
    # factors/sentiment.py documents that it expects a signed score. This is
    # the only place that contract is implemented.
    assert signed_score(0.9, 0.05) > 0
    assert signed_score(0.05, 0.9) < 0
    assert signed_score(0.05, 0.05) == 0.0
    # A confidently neutral article and a genuinely mixed one both mean "no
    # directional information", and both land on zero.
    assert signed_score(0.45, 0.45) == 0.0


def test_finbert_label_order_is_read_from_the_model():
    # FinBERT's actual ordering — positive first.
    assert _label_index({0: "positive", 1: "negative", 2: "neutral"}) == (0, 1, 2)
    # A model with a different order must still be read correctly rather than
    # silently inverting the factor.
    assert _label_index({0: "negative", 1: "neutral", 2: "positive"}) == (2, 0, 1)
    assert _label_index({"0": "Positive", "1": "Negative", "2": "Neutral"}) == (0, 1, 2)


def test_a_model_without_explicit_polarity_labels_is_refused():
    with pytest.raises(ScorerUnavailableError, match="positive"):
        _label_index({0: "LABEL_0", 1: "LABEL_1"})


@pytest.mark.skipif(
    not FINBERT_CACHE.exists(), reason="FinBERT not in the local HF cache"
)
def test_the_real_model_agrees_with_the_sign_convention():
    """The end-to-end guard against a silently inverted factor."""
    from stockanalysis.news.finbert import FinBertScorer

    scorer = FinBertScorer()
    good, bad, flat = scorer.score(
        [
            "Infosys raises FY26 revenue guidance after record deal wins",
            "SEBI opens fraud probe into the company; shares crash 20%",
            "The company will hold its annual general meeting on 30 June",
        ]
    )
    assert good.score > 0.3 and good.label == "positive"
    assert bad.score < -0.3 and bad.label == "negative"
    assert abs(flat.score) < 0.3


# ----------------------------------------------------------------------
# The scoring pipeline
# ----------------------------------------------------------------------


def test_only_resolved_above_threshold_rows_are_scored(db):
    _news(db, [
        ("n1", "INE_A", "Infosys beats estimates", 0.95),
        ("n2", None, "Sensex ends higher", None),
        ("n3", "INE_B", "Nestle flat", 0.6),
    ])
    stats = score_news(db, FakeScorer(), min_confidence=0.7)

    assert stats.scored == 1
    assert db.query("SELECT news_id FROM news_sentiment")["news_id"].tolist() == ["n1"]


def test_identical_text_is_scored_once_not_once_per_company(db):
    # A wire story naming three companies is three rows and one inference.
    _news(db, [(f"a:{i}", f"INE_{i}", "Infosys and Wipro jumps on deal wins", 0.95)
               for i in "ABC"])
    scorer = FakeScorer()
    stats = score_news(db, scorer)

    assert stats.scored == 3
    assert stats.reused == 2
    assert len(scorer.calls[0]) == 1
    assert db.query("SELECT COUNT(DISTINCT score) c FROM news_sentiment")["c"].iloc[0] == 1


def test_rescoring_is_a_no_op_but_a_second_model_is_not(db):
    _news(db, [("n1", "INE_A", "Infosys beats estimates", 0.95)])
    score_news(db, FakeScorer())
    assert score_news(db, FakeScorer()).scored == 0

    other = FakeScorer(name="other-model")
    assert score_news(db, other).scored == 1
    # Two models, two rows, one article — the comparison DESIGN §5.4 asks for
    # in extraction, applied to sentiment.
    assert db.query("SELECT COUNT(*) c FROM news_sentiment")["c"].iloc[0] == 2


def test_the_scored_text_is_headline_plus_body():
    assert scoring_text("Infosys wins deal", "Multi-year contract.") == (
        "Infosys wins deal. Multi-year contract."
    )
    # Feeds that repeat the headline as the description must not double it.
    assert scoring_text("Infosys wins deal", "infosys wins deal") == "Infosys wins deal"
    assert scoring_text("Infosys wins deal", None) == "Infosys wins deal"


def test_pending_news_excludes_what_is_already_scored(db):
    _news(db, [("n1", "INE_A", "Infosys beats estimates", 0.95),
               ("n2", "INE_A", "Infosys falls on probe", 0.95)])
    score_news(db, FakeScorer(), limit=1)
    assert len(pending_news(db, "fake-scorer")) == 1


def test_coverage_report_counts_companies_per_month(db):
    _news(db, [("n1", "INE_A", "Infosys beats estimates", 0.95),
               ("n2", "INE_B", "Wipro wins deal", 0.95)],
          published=dt.datetime(2024, 5, 10, 9, 0))
    score_news(db, FakeScorer())

    report = coverage_report(db, "fake-scorer")
    assert report["companies"].iloc[0] == 2
    assert report["scored"].iloc[0] == 2


# ----------------------------------------------------------------------
# The factor
# ----------------------------------------------------------------------


def test_the_factor_reads_the_sign_the_scorer_wrote(db):
    _news(db, [(f"good{i}", "INE_A", f"Infosys beats estimates {i}", 0.95)
               for i in range(3)]
          + [(f"bad{i}", "INE_B", f"Wipro falls on probe {i}", 0.95)
             for i in range(3)])
    score_news(db, FakeScorer())

    values = _factor_values(db, ["INE_A", "INE_B"], dt.date(2024, 5, 20))
    assert values["INE_A"] > 0 > values["INE_B"]


def test_a_company_with_no_news_is_nan_not_neutral(db):
    # The phase-2 regression, in its phase-3 form. "We could not measure this"
    # and "this was balanced" are different claims and only one of them is
    # true; scoring the second dilutes every factor that was measured.
    _news(db, [(f"n{i}", "INE_A", f"Infosys beats estimates {i}", 0.95)
               for i in range(3)])
    score_news(db, FakeScorer())

    values = _factor_values(db, ["INE_A", "INE_QUIET"], dt.date(2024, 5, 20))
    assert values["INE_A"] > 0
    assert np.isnan(values["INE_QUIET"])


def test_thin_coverage_is_not_a_measurement(db):
    _news(db, [(f"n{i}", "INE_A", f"Infosys beats estimates {i}", 0.95)
               for i in range(MIN_ARTICLES - 1)])
    score_news(db, FakeScorer())
    assert np.isnan(_factor_values(db, ["INE_A"], dt.date(2024, 5, 20))["INE_A"])


def test_recent_news_outweighs_stale_news(db):
    # Two weeks is one half-life: an old positive story cannot outvote a fresh
    # negative one at equal count.
    old = dt.datetime(2024, 5, 1, 9, 0)
    new = dt.datetime(2024, 5, 20, 9, 0)
    _news(db, [(f"old{i}", "INE_A", f"Infosys beats estimates {i}", 0.95)
               for i in range(3)], published=old)
    _news(db, [(f"new{i}", "INE_A", f"Infosys falls on probe {i}", 0.95)
               for i in range(3)], published=new)
    score_news(db, FakeScorer())

    assert _factor_values(db, ["INE_A"], dt.date(2024, 5, 20))["INE_A"] < 0


def test_the_factor_cannot_see_news_published_after_the_decision(db):
    _news(db, [(f"n{i}", "INE_A", f"Infosys beats estimates {i}", 0.95)
               for i in range(3)], published=dt.datetime(2024, 5, 20, 9, 0))
    score_news(db, FakeScorer())

    assert np.isnan(_factor_values(db, ["INE_A"], dt.date(2024, 5, 19))["INE_A"])
    assert _factor_values(db, ["INE_A"], dt.date(2024, 5, 20))["INE_A"] > 0


# ----------------------------------------------------------------------


def _news(db, rows, published: dt.datetime = dt.datetime(2024, 5, 15, 9, 0)) -> None:
    db.upsert_df(
        "news",
        pd.DataFrame(
            [
                {
                    "news_id": nid,
                    "article_id": nid.split(":")[0],
                    "isin": isin,
                    "published_at": published,
                    "ingested_at": dt.datetime(2026, 1, 1),
                    "headline": headline,
                    "body": None,
                    "source": "example.com",
                    "url": f"https://example.com/{nid}",
                    "provider": "RSS",
                    "content_hash": nid,
                    "resolution_method": "CURATED" if isin else "UNRESOLVED",
                    "resolution_confidence": conf,
                    "matched_in": "HEADLINE",
                }
                for nid, isin, headline, conf in rows
            ]
        ),
        ["news_id"],
    )


def _factor_values(db, isins: list[str], as_of: dt.date) -> pd.Series:
    # A fresh cache per call: the module-level one is keyed on (db, date,
    # universe) and these tests reuse all three across mutations.
    return NewsSentiment30d(cache=PanelCache()).compute(db, isins, as_of)
