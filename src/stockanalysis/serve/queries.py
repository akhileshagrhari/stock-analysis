"""The read layer behind both serving surfaces — DESIGN §9 phase 4.

The API and the dashboard answer the same questions ("what is the latest signal
for RELIANCE", "which names are flagged today"). Phrasing those questions twice,
in two files, is how the two surfaces end up disagreeing about what the model
said — and a dashboard that contradicts the API is worse than either one alone.
So the SQL lives here once, returns plain Python types, and each surface is left
with only its own rendering.

**Everything returned is JSON-safe.** DuckDB hands pandas a NaN for a NULL
DOUBLE, and NaN is not valid JSON — `json.dumps(float("nan"))` emits a bare
`NaN` token that strict parsers reject. Coercing at this boundary, rather than in
each caller, is why `Optional[float]` in the API models can be trusted.

These are reporting reads over already-computed signals, not backtest decision
reads. They deliberately do not go through `Database.as_of_*`: those enforce a
knowledge date for *scoring*, and a dashboard showing today's stored signals has
no decision to protect. Nothing here may be called from inside the backtest loop.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from stockanalysis.db.database import Database

# ----------------------------------------------------------------------
# Coercion
# ----------------------------------------------------------------------


def _opt_float(value: Any) -> float | None:
    """NaN, NA, None and non-finite floats all become None."""
    if value is None or value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _req_float(value: Any, default: float = 0.0) -> float:
    """For columns a caller treats as always-present. Never returns NaN."""
    number = _opt_float(value)
    return default if number is None else number


def _opt_str(value: Any) -> str | None:
    if value is None or value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return text or None


def _req_str(value: Any, default: str = "") -> str:
    text = _opt_str(value)
    return default if text is None else text


def _opt_date(value: Any) -> dt.date | None:
    if value is None or value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    converted = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(converted) else converted.date()


def _flag_list(value: Any) -> list[str]:
    """`red_flags` is stored as a comma-joined string, empty when nothing tripped."""
    text = _opt_str(value)
    if text is None:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


# ----------------------------------------------------------------------
# Row types
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Instrument:
    isin: str
    nse_symbol: str
    name: str
    sector: str | None = None
    bse_code: str | None = None
    listing_date: dt.date | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FactorScore:
    factor_name: str
    raw_value: float | None
    sector_zscore: float | None


@dataclass(frozen=True)
class Signal:
    isin: str
    nse_symbol: str
    name: str
    as_of: dt.date
    composite_score: float | None
    signal: str | None
    coverage: float | None
    red_flags: list[str] = field(default_factory=list)
    unknown_flags: list[str] = field(default_factory=list)
    narrative: str | None = None
    model_version: str | None = None
    sector: str | None = None

    @property
    def has_red_flag(self) -> bool:
        return bool(self.red_flags)


@dataclass(frozen=True)
class NewsItem:
    published_at: dt.date | None
    headline: str | None
    label: str | None
    score: float | None
    source: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class SentimentCounts:
    """30-day label mix behind the sentiment factor, for one instrument."""

    positive: int = 0
    negative: int = 0
    neutral: int = 0

    @property
    def total(self) -> int:
        return self.positive + self.negative + self.neutral


# ----------------------------------------------------------------------
# Row builders
# ----------------------------------------------------------------------


def _instrument(row: pd.Series) -> Instrument:
    return Instrument(
        isin=_req_str(row.get("isin")),
        nse_symbol=_req_str(row.get("nse_symbol")),
        name=_req_str(row.get("name")),
        sector=_opt_str(row.get("sector")),
        bse_code=_opt_str(row.get("bse_code")),
        listing_date=_opt_date(row.get("listing_date")),
    )


def _signal(row: pd.Series) -> Signal:
    return Signal(
        isin=_req_str(row.get("isin")),
        nse_symbol=_req_str(row.get("nse_symbol")),
        name=_req_str(row.get("name")),
        sector=_opt_str(row.get("sector")),
        as_of=_opt_date(row.get("as_of_date")) or dt.date.min,
        composite_score=_opt_float(row.get("composite_score")),
        signal=_opt_str(row.get("signal")),
        coverage=_opt_float(row.get("coverage")),
        red_flags=_flag_list(row.get("red_flags")),
        unknown_flags=_flag_list(row.get("unknown_flags")),
        narrative=_opt_str(row.get("narrative")),
        model_version=_opt_str(row.get("model_version")),
    )


_SIGNAL_COLUMNS = """
    s.isin, i.nse_symbol, i.name, i.sector, s.as_of_date, s.composite_score,
    s.signal, s.coverage, s.red_flags, s.unknown_flags, s.narrative,
    s.model_version
"""


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------


def latest_as_of(db: Database) -> dt.date | None:
    """Most recent scored date, or None when nothing has been scored yet."""
    df = db.query("SELECT MAX(as_of_date) AS d FROM signals")
    if df.empty:
        return None
    return _opt_date(df.iloc[0]["d"])


def scored_dates(db: Database, limit: int = 90) -> list[dt.date]:
    """Distinct scored dates, newest first — the dashboard's date picker."""
    df = db.query(
        "SELECT DISTINCT as_of_date FROM signals ORDER BY as_of_date DESC LIMIT ?",
        [int(limit)],
    )
    return [d for d in (_opt_date(v) for v in df["as_of_date"]) if d is not None]


def sectors(db: Database) -> list[str]:
    df = db.query(
        "SELECT DISTINCT sector FROM instruments "
        "WHERE sector IS NOT NULL AND sector != '' ORDER BY sector"
    )
    return [s for s in (_opt_str(v) for v in df["sector"]) if s is not None]


def list_instruments(db: Database, sector: str | None = None) -> list[Instrument]:
    sql = (
        "SELECT isin, nse_symbol, bse_code, name, sector, listing_date "
        "FROM instruments"
    )
    params: list[Any] = []
    if sector:
        sql += " WHERE sector = ?"
        params.append(sector)
    sql += " ORDER BY nse_symbol"
    df = db.query(sql, params)
    return [_instrument(row) for _, row in df.iterrows()]


def get_instrument(db: Database, isin: str) -> Instrument | None:
    df = db.query(
        "SELECT isin, nse_symbol, bse_code, name, sector, listing_date "
        "FROM instruments WHERE isin = ?",
        [isin],
    )
    if df.empty:
        return None
    return _instrument(df.iloc[0])


def resolve_symbol(db: Database, symbol: str) -> Instrument | None:
    """Look an instrument up by NSE symbol — what a human types, not an ISIN."""
    df = db.query(
        "SELECT isin, nse_symbol, bse_code, name, sector, listing_date "
        "FROM instruments WHERE upper(nse_symbol) = upper(?)",
        [symbol],
    )
    if df.empty:
        return None
    return _instrument(df.iloc[0])


def signals_on(
    db: Database,
    as_of: dt.date | None = None,
    signal: str | None = None,
    sector: str | None = None,
    flagged_only: bool = False,
    limit: int | None = None,
) -> list[Signal]:
    """Signals for one date, newest scored date when `as_of` is omitted.

    Ordering is score-descending with NULLs last: an unscored company (coverage
    below the floor) sorts to the bottom rather than to the top, which is where
    DuckDB's default NULL ordering would put it on a DESC sort.
    """
    params: list[Any] = []
    if as_of is None:
        date_filter = "s.as_of_date = (SELECT MAX(as_of_date) FROM signals)"
    else:
        date_filter = "s.as_of_date = ?"
        params.append(as_of)

    sql = f"""
        SELECT {_SIGNAL_COLUMNS}
        FROM signals s
        JOIN instruments i ON i.isin = s.isin
        WHERE {date_filter}
    """
    if signal:
        sql += " AND s.signal = ?"
        params.append(signal)
    if sector:
        sql += " AND i.sector = ?"
        params.append(sector)
    if flagged_only:
        sql += " AND s.red_flags IS NOT NULL AND s.red_flags != ''"

    sql += " ORDER BY s.composite_score DESC NULLS LAST, i.nse_symbol"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))

    df = db.query(sql, params)
    return [_signal(row) for _, row in df.iterrows()]


def latest_signal(db: Database, isin: str) -> Signal | None:
    df = db.query(
        f"""
        SELECT {_SIGNAL_COLUMNS}
        FROM signals s
        JOIN instruments i ON i.isin = s.isin
        WHERE s.isin = ?
        ORDER BY s.as_of_date DESC
        LIMIT 1
        """,
        [isin],
    )
    if df.empty:
        return None
    return _signal(df.iloc[0])


def signal_history(db: Database, isin: str, limit: int = 60) -> list[Signal]:
    """Recent signals for one instrument, oldest first so a chart reads left-to-right."""
    df = db.query(
        f"""
        SELECT * FROM (
            SELECT {_SIGNAL_COLUMNS}
            FROM signals s
            JOIN instruments i ON i.isin = s.isin
            WHERE s.isin = ?
            ORDER BY s.as_of_date DESC
            LIMIT ?
        ) ORDER BY as_of_date
        """,
        [isin, int(limit)],
    )
    return [_signal(row) for _, row in df.iterrows()]


def factor_breakdown(db: Database, isin: str, as_of: dt.date) -> list[FactorScore]:
    df = db.query(
        """
        SELECT factor_name, raw_value, sector_zscore
        FROM factor_scores
        WHERE isin = ? AND as_of_date = ?
        ORDER BY factor_name
        """,
        [isin, as_of],
    )
    return [
        FactorScore(
            factor_name=_req_str(row["factor_name"]),
            raw_value=_opt_float(row["raw_value"]),
            sector_zscore=_opt_float(row["sector_zscore"]),
        )
        for _, row in df.iterrows()
    ]


def recent_news(db: Database, isin: str, limit: int = 10) -> list[NewsItem]:
    """Latest scored news for one instrument. Unscored articles still appear."""
    df = db.query(
        """
        SELECT n.published_at, n.headline, n.source, n.url, ns.label, ns.score
        FROM news n
        LEFT JOIN news_sentiment ns ON ns.news_id = n.news_id
        WHERE n.isin = ?
        ORDER BY n.published_at DESC
        LIMIT ?
        """,
        [isin, int(limit)],
    )
    return [
        NewsItem(
            published_at=_opt_date(row["published_at"]),
            headline=_opt_str(row["headline"]),
            label=_opt_str(row["label"]),
            score=_opt_float(row["score"]),
            source=_opt_str(row["source"]),
            url=_opt_str(row["url"]),
        )
        for _, row in df.iterrows()
    ]


def sentiment_counts(
    db: Database,
    isins: list[str],
    as_of: dt.date,
    window_days: int = 30,
) -> dict[str, SentimentCounts]:
    """Label mix per ISIN over the window ending on `as_of`, in one scan.

    Batched deliberately. The narrative pass runs over the whole universe, and
    one round trip per company is a hundred queries for data that fits in a
    single group-by.

    ISINs with no scored news are absent from the result rather than present with
    zeros — "no coverage" and "covered, and the news was neutral" are different
    facts, and the caller needs to be able to tell them apart.
    """
    if not isins:
        return {}

    start = as_of - dt.timedelta(days=int(window_days))
    placeholders = ", ".join("?" for _ in isins)
    df = db.query(
        f"""
        SELECT n.isin, ns.label, COUNT(*) AS n
        FROM news n
        JOIN news_sentiment ns ON ns.news_id = n.news_id
        WHERE n.isin IN ({placeholders})
          AND n.published_at <= ?
          AND n.published_at >= ?
        GROUP BY n.isin, ns.label
        """,
        [*isins, dt.datetime.combine(as_of, dt.time.max), start],
    )

    tally: dict[str, dict[str, int]] = {}
    for _, row in df.iterrows():
        isin = _req_str(row["isin"])
        label = (_opt_str(row["label"]) or "").lower()
        if not isin or label not in ("positive", "negative", "neutral"):
            continue
        tally.setdefault(isin, {})[label] = int(row["n"])

    return {
        isin: SentimentCounts(
            positive=counts.get("positive", 0),
            negative=counts.get("negative", 0),
            neutral=counts.get("neutral", 0),
        )
        for isin, counts in tally.items()
    }


def signal_counts(db: Database, as_of: dt.date) -> dict[str, int]:
    """BUY/HOLD/SELL tally for one date. Unscored companies are excluded."""
    df = db.query(
        "SELECT signal, COUNT(*) AS n FROM signals "
        "WHERE as_of_date = ? AND signal IS NOT NULL GROUP BY signal",
        [as_of],
    )
    return {_req_str(row["signal"]): int(row["n"]) for _, row in df.iterrows()}
