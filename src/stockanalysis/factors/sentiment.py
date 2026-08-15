"""Sentiment factor — DESIGN §6.1, 10% of the composite.

**This computes to NaN today, by design.** The `news` and `news_sentiment`
tables are filled in phase 3. It is written now rather than then because the
composite needs to demonstrate that it handles an entirely absent family
correctly, and the only way to demonstrate that is to have one.

The failure mode being guarded against is the tempting one: scoring a company
with no news as neutral. Neutral is a claim — it says the news flow was balanced.
Absent is a different claim, and conflating them hands every thinly covered
small-cap an average sentiment score it did nothing to earn, then lets that
score dilute the factors that were actually measured. So no-news is NaN, and the
composite subtracts the missing weight instead.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stockanalysis.factors.base import PanelFactor
from stockanalysis.factors.panel import Panel

FAMILY = "sentiment"

# Articles are weighted by exp(-age / HALF_LIFE_DAYS * ln2) across the 30-day
# window: a two-week-old headline counts about half as much as this morning's.
HALF_LIFE_DAYS = 7.0

# Below this, the average is one or two articles and is noise. Coverage lost
# here is reported, not filled in.
MIN_ARTICLES = 3


class NewsSentiment30d(PanelFactor):
    """Recency-weighted mean sentiment over the 30 days to `as_of`.

    Expects `news_sentiment.score` to be signed — positive for bullish, negative
    for bearish. FinBERT emits a label plus a confidence, so phase 3's scorer is
    responsible for turning that into a signed number; doing it here would put
    the model's output convention in two places.
    """

    name = "news_sentiment_30d"
    family = FAMILY

    def from_panel(self, panel: Panel) -> pd.Series:
        news = panel.sentiment
        if news.empty:
            return pd.Series(np.nan, index=panel.isins, dtype=float)

        df = news.copy()
        df["score"] = pd.to_numeric(df["score"], errors="coerce")
        df = df.dropna(subset=["score"])
        if df.empty:
            return pd.Series(np.nan, index=panel.isins, dtype=float)

        age_days = (
            pd.Timestamp(panel.as_of) - pd.to_datetime(df["published_at"])
        ).dt.total_seconds() / 86400.0
        df["weight"] = np.exp(-np.log(2) * age_days.clip(lower=0) / HALF_LIFE_DAYS)

        out = {}
        for isin, grp in df.groupby("isin"):
            if len(grp) < MIN_ARTICLES:
                continue
            total = grp["weight"].sum()
            if total <= 0:
                continue
            out[isin] = float((grp["score"] * grp["weight"]).sum() / total)

        return pd.Series(out, dtype=float).reindex(panel.isins)


ALL: list[PanelFactor] = [NewsSentiment30d()]
