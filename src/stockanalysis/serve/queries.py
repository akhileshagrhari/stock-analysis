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
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from stockanalysis.config import settings
from stockanalysis.db.database import Database
from stockanalysis.factors.panel import SHARES_STATED, share_count
from stockanalysis.factors.share_basis import ShareBasis, restate

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


def _opt_int(value: Any) -> int | None:
    """For integer columns that can hold NULL — which DuckDB hands over as NaN."""
    number = _opt_float(value)
    return None if number is None else int(number)


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
    industry: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FactorScore:
    factor_name: str
    raw_value: float | None
    sector_zscore: float | None


@dataclass(frozen=True)
class PreviousSignal:
    """The same instrument's most recent signal strictly before a row's date."""

    as_of: dt.date
    composite_score: float | None
    signal: str | None
    model_version: str | None


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
    previous: PreviousSignal | None = None

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
    relevance: str | None = None            # primary | mention | None
    sentiment_status: str = "not_scored"    # scored | pending | not_scored
    counts_toward_sentiment: bool = False


PRIMARY = "primary"
MENTION = "mention"
SCORED = "scored"
PENDING = "pending"
NOT_SCORED = "not_scored"
# Kept in step with news.store.ROUNDUP, which demotes an article naming more
# than three companies; not imported, so serving does not load the resolver.
_ROUNDUP = "ROUNDUP"


def _relevance(matched_in: str | None, method: str | None) -> str | None:
    """Primary when the company is in the headline of an article about it."""
    if matched_in is None:
        return None
    if matched_in == "HEADLINE" and method != _ROUNDUP:
        return PRIMARY
    return MENTION


@dataclass(frozen=True)
class SentimentCounts:
    """30-day label mix behind the sentiment factor, for one instrument."""

    positive: int = 0
    negative: int = 0
    neutral: int = 0

    @property
    def total(self) -> int:
        return self.positive + self.negative + self.neutral


# Annual line items, in statement order, paired with the label to show. Held as
# an ordered tuple rather than derived from the row's columns because a
# statement read in column order is not a statement — revenue belongs above
# expenses whatever order DuckDB hands the fields back in.
ANNUAL_LINE_ITEMS: tuple[tuple[str, str], ...] = (
    ("revenue", "Revenue"),
    ("other_income", "Other income"),
    ("total_income", "Total income"),
    ("total_expenses", "Total expenses"),
    ("ebitda", "EBITDA"),
    ("depreciation", "Depreciation"),
    ("interest_expense", "Interest expense"),
    ("profit_before_tax", "Profit before tax"),
    ("tax_expense", "Tax expense"),
    ("pat", "PAT"),
    ("eps", "EPS (₹)"),
    ("ocf", "Operating cash flow"),
    ("capex", "Capex"),
    ("fcf", "Free cash flow"),
    ("total_assets", "Total assets"),
    ("total_equity", "Total equity"),
    ("total_liabilities", "Total liabilities"),
    ("total_debt", "Total debt"),
    ("cash", "Cash"),
    ("contingent_liabilities", "Contingent liabilities"),
)

# The rows above that are per-share rather than crore. EPS is the only one, but
# naming it here keeps the unit decision next to the line-item list rather than
# hidden in a formatter.
PER_SHARE_ITEMS = frozenset({"eps"})


@dataclass(frozen=True)
class AnnualFinancials:
    """One fiscal year of `fundamentals_annual`, in crore except EPS.

    `values` carries the line items rather than twenty named fields: the page
    renders them as a statement driven by `ANNUAL_LINE_ITEMS`, and a dataclass
    field per line would mean editing two places to add one row.
    """

    fiscal_year: int
    basis: str
    period_end: dt.date | None
    filing_date: dt.date | None
    source: str | None
    auditor_opinion: str | None
    confidence: float | None
    values: dict[str, float | None] = field(default_factory=dict)
    eps_adjusted: float | None = None   # values["eps"] on today's share basis
    source_url: str | None = None       # the XBRL instance or report PDF read


# Results formats the annual reader cannot build statements from, by a word in
# the exchange's own file name (…/xbrl/INTEGRATED_FILING_BANKING_1740913_….xml).
# Every annual filing in these formats has been read and refused: banks tag no
# revenue line or total equity, life insurers no revenue, profit or equity.
_UNSUPPORTED_FORMATS = {"BANKING": "banks", "LI": "insurers", "GI": "insurers"}
_FORMAT = re.compile(r"/([A-Z_]+?)_\d")


def results_format(xbrl_url: str | None) -> str | None:
    """The exchange's name for a filing's results format, from its file name."""
    match = _FORMAT.search(xbrl_url or "")
    return match.group(1) if match else None


def unsupported_kind(xbrl_url: str | None) -> str | None:
    """'banks' or 'insurers' when the filing is in a format the model can't read."""
    words = (results_format(xbrl_url) or "").split("_")
    for word, kind in _UNSUPPORTED_FORMATS.items():
        if word in words:
            return kind
    return None


@dataclass(frozen=True)
class QuarterlyFinancials:
    """One quarter of `fundamentals_quarterly`, in crore except EPS.

    Rows read from the quarter's XBRL carry the P&L down to tax; the few NSE's
    `results_comparison` supplied carry only revenue, net profit and EPS.
    """

    period_end: dt.date
    filing_date: dt.date | None
    relating_to: str | None
    is_consolidated: bool | None
    revenue: float | None
    pat: float | None
    eps: float | None
    eps_adjusted: float | None = None   # eps on today's share basis
    basis: str | None = None            # CONSOLIDATED | STANDALONE
    total_income: float | None = None
    other_income: float | None = None
    total_expenses: float | None = None
    depreciation: float | None = None
    interest_expense: float | None = None
    profit_before_tax: float | None = None
    tax_expense: float | None = None
    ebitda: float | None = None         # derived; see quarterly_financials
    source_url: str | None = None       # the XBRL instance read


@dataclass(frozen=True)
class PriceBar:
    """One trading day from `prices_daily`, in rupees.

    Both closes, because they answer different questions. `close` is the day's
    close restated to today's share basis — split- and bonus-adjusted, not
    dividend-adjusted — so before a bonus it is not the price that traded
    (RELIANCE's 2024 closes are half what it traded at). `adj_close` is also
    dividend-adjusted, the one a total return is computed from.
    """

    date: dt.date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None
    volume: int | None


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
        industry=_opt_str(row.get("industry")),
    )


def _signal(row: pd.Series) -> Signal:
    previous_date = _opt_date(row.get("prev_as_of_date"))
    previous = None if previous_date is None else PreviousSignal(
        as_of=previous_date,
        composite_score=_opt_float(row.get("prev_composite_score")),
        signal=_opt_str(row.get("prev_signal")),
        model_version=_opt_str(row.get("prev_model_version")),
    )
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
        previous=previous,
    )


# Every signal row beside the same instrument's row just before it. A window
# over the whole table, not over whatever a query then filters to: the previous
# row of a 16 Aug signal is its 14 Aug one even when the query asks only for
# 16 Aug, or for one row of history. Aliased because Postgres before 16 rejects
# an unaliased subquery in FROM.
_SIGNALS = """(
    SELECT sig.*,
        LAG(sig.as_of_date) OVER prior AS prev_as_of_date,
        LAG(sig.composite_score) OVER prior AS prev_composite_score,
        LAG(sig.signal) OVER prior AS prev_signal,
        LAG(sig.model_version) OVER prior AS prev_model_version
    FROM signals sig
    WINDOW prior AS (PARTITION BY sig.isin ORDER BY sig.as_of_date)
)"""

_SIGNAL_COLUMNS = """
    s.isin, i.nse_symbol, i.name, i.sector, s.as_of_date, s.composite_score,
    s.signal, s.coverage, s.red_flags, s.unknown_flags, s.narrative,
    s.model_version, s.prev_as_of_date, s.prev_composite_score, s.prev_signal,
    s.prev_model_version
"""


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------


#: A scoring date counts as a complete run when at least this share of the
#: instruments active that day got a signal. The 16 Aug 2026 run scored 4 of
#: 100 and the 15 Aug one 1; both sit in `signals` like any other date and
#: became "latest", so the newest *complete* date is reported beside it.
COMPLETE_RUN_SHARE = 0.5


@dataclass(frozen=True)
class ScoredDateCount:
    as_of: dt.date
    scored: int        # instruments with a signal that day
    instruments: int   # instruments listed and not delisted that day


def scored_date_counts(db: Database, limit: int | None = 90) -> list[ScoredDateCount]:
    """How many companies each scoring date covered, newest first.

    An instrument with no listing date counts as listed: the exchange's master
    list leaves it blank for most of the universe.
    """
    sql = """
        SELECT d.as_of_date, d.scored,
            (SELECT COUNT(*) FROM instruments i
             WHERE (i.listing_date IS NULL OR i.listing_date <= d.as_of_date)
               AND (i.delisting_date IS NULL OR i.delisting_date > d.as_of_date)
            ) AS instruments
        FROM (
            SELECT as_of_date, COUNT(signal) AS scored
            FROM signals GROUP BY as_of_date
        ) AS d
        ORDER BY d.as_of_date DESC
    """
    params: list[Any] = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    df = db.query(sql, params)
    return [
        ScoredDateCount(
            as_of=_opt_date(row["as_of_date"]) or dt.date.min,
            scored=int(row["scored"]),
            instruments=int(row["instruments"]),
        )
        for _, row in df.iterrows()
    ]


def latest_complete_as_of(
    db: Database, share: float = COMPLETE_RUN_SHARE
) -> dt.date | None:
    """Newest date on which at least `share` of active instruments were scored."""
    for count in scored_date_counts(db, limit=None):
        if count.instruments and count.scored >= share * count.instruments:
            return count.as_of
    return None


@dataclass(frozen=True)
class Freshness:
    latest_price_date: dt.date | None
    latest_filing_date: dt.date | None
    latest_news_date: dt.date | None


def freshness(db: Database) -> Freshness:
    """The newest date each kind of input reaches, across every company."""
    row = db.query(
        "SELECT (SELECT MAX(date) FROM prices_daily) AS prices, "
        "(SELECT MAX(filing_date) FROM fundamentals_quarterly) AS quarterly, "
        "(SELECT MAX(filing_date) FROM fundamentals_annual) AS annual, "
        "(SELECT MAX(published_at) FROM news WHERE isin IS NOT NULL) AS news"
    ).iloc[0]
    filings = [d for d in (_opt_date(row["quarterly"]), _opt_date(row["annual"])) if d]
    return Freshness(
        latest_price_date=_opt_date(row["prices"]),
        latest_filing_date=max(filings, default=None),
        latest_news_date=_opt_date(row["news"]),
    )


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
        "SELECT isin, nse_symbol, bse_code, name, sector, industry, listing_date "
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
        "SELECT isin, nse_symbol, bse_code, name, sector, industry, listing_date "
        "FROM instruments WHERE isin = ?",
        [isin],
    )
    if df.empty:
        return None
    return _instrument(df.iloc[0])


def resolve_symbol(db: Database, symbol: str) -> Instrument | None:
    """Look an instrument up by NSE symbol — what a human types, not an ISIN."""
    df = db.query(
        "SELECT isin, nse_symbol, bse_code, name, sector, industry, listing_date "
        "FROM instruments WHERE upper(nse_symbol) = upper(?)",
        [symbol],
    )
    if df.empty:
        return None
    return _instrument(df.iloc[0])


def resolve_symbols(db: Database, symbols: list[str]) -> dict[str, Instrument]:
    """Listed instruments for many NSE symbols at once, keyed by upper-cased symbol.

    Delisted instruments are skipped. If two listed instruments share a symbol,
    the lower ISIN wins, so the answer does not depend on row order.
    """
    wanted = sorted({s.upper() for s in symbols})
    if not wanted:
        return {}
    marks = ", ".join("?" for _ in wanted)
    df = db.query(
        "SELECT i.isin, i.nse_symbol, i.bse_code, i.name, i.sector, i.industry, "
        "i.listing_date FROM instruments AS i "
        f"WHERE UPPER(i.nse_symbol) IN ({marks}) AND i.delisting_date IS NULL "
        "ORDER BY i.isin",
        wanted,
    )
    found: dict[str, Instrument] = {}
    for _, row in df.iterrows():
        inst = _instrument(row)
        found.setdefault(inst.nse_symbol.upper(), inst)
    return found


def search_instruments(db: Database, text: str, limit: int = 10) -> list[Instrument]:
    """Instruments matching what someone is typing, best match first.

    Ranked: the exact symbol, then symbols starting with the text, then names
    starting with it, then anything containing it — ties in symbol order.

    **Matched in Python, not SQL.** The published Postgres copy pins its text
    columns to `COLLATE "C"` (see `db.publish`), under which `upper` and
    `lower` fold ASCII letters only, while DuckDB folds all of Unicode — the
    same SQL would find "Ünïcødé" on one backend and miss it on the other.
    `str.casefold` gives one answer on both, and with no LIKE a typed `%` or
    `_` is a character rather than a wildcard. It reads the whole instrument
    list per call, which is nothing at the size of a listed universe; past
    that, the place to go is a trigram index or the client holding the list.
    """
    needle = text.strip().casefold()
    if not needle:
        return []

    ranked: list[tuple[int, Instrument]] = []
    for instrument in list_instruments(db):   # already in symbol order
        symbol, name = instrument.nse_symbol.casefold(), instrument.name.casefold()
        if symbol == needle:
            rank = 0
        elif symbol.startswith(needle):
            rank = 1
        elif name.startswith(needle):
            rank = 2
        elif needle in symbol or needle in name:
            rank = 3
        else:
            continue
        ranked.append((rank, instrument))

    ranked.sort(key=lambda pair: pair[0])     # stable, so symbol order breaks ties
    return [instrument for _rank, instrument in ranked[:limit]]


def signals_on(
    db: Database,
    as_of: dt.date | None = None,
    signal: str | None = None,
    sector: str | None = None,
    flagged_only: bool = False,
    limit: int | None = None,
    per_instrument: bool = False,
) -> list[Signal]:
    """Signals for one date, newest scored date when `as_of` is omitted.

    With `per_instrument`, each instrument's newest signal instead, whatever its
    date — one ranking across dates, each row carrying its own `as_of`. A
    partial run then cannot hide everyone it left out; `as_of` makes no sense
    with it and is refused.

    Ordering is score-descending with NULLs last: an unscored company (coverage
    below the floor) sorts to the bottom rather than to the top, which is where
    DuckDB's default NULL ordering would put it on a DESC sort.
    """
    params: list[Any] = []
    newest = ""
    if per_instrument:
        if as_of is not None:
            raise ValueError("per_instrument takes each instrument's own date, not as_of")
        newest = """
        JOIN (
            SELECT isin, MAX(as_of_date) AS newest FROM signals GROUP BY isin
        ) AS latest ON latest.isin = s.isin AND latest.newest = s.as_of_date
        """
        date_filter = "1 = 1"
    elif as_of is None:
        date_filter = "s.as_of_date = (SELECT MAX(as_of_date) FROM signals)"
    else:
        date_filter = "s.as_of_date = ?"
        params.append(as_of)

    sql = f"""
        SELECT {_SIGNAL_COLUMNS}
        FROM {_SIGNALS} AS s
        {newest}
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


@dataclass(frozen=True)
class LatestScore:
    as_of: dt.date
    composite_score: float | None
    signal: str | None


def latest_scores(db: Database, isins: list[str]) -> dict[str, LatestScore]:
    """Each instrument's newest signal row, for many at once. Absent: none stored."""
    if not isins:
        return {}
    marks = ", ".join("?" for _ in isins)
    df = db.query(
        f"""
        SELECT s.isin, s.as_of_date, s.composite_score, s.signal
        FROM signals s
        JOIN (
            SELECT isin, MAX(as_of_date) AS newest FROM signals
            WHERE isin IN ({marks}) GROUP BY isin
        ) AS latest ON latest.isin = s.isin AND latest.newest = s.as_of_date
        """,
        list(isins),
    )
    return {
        _req_str(row["isin"]): LatestScore(
            as_of=_opt_date(row["as_of_date"]) or dt.date.min,
            composite_score=_opt_float(row["composite_score"]),
            signal=_opt_str(row["signal"]),
        )
        for _, row in df.iterrows()
    }


def latest_signals(db: Database, isins: list[str]) -> dict[str, Signal]:
    """Each instrument's newest signal, with `previous` filled, for many at once.

    Absent: no signal stored. The window in `_SIGNALS` runs over the whole
    table, so `previous` is right whichever instruments are asked for.
    """
    if not isins:
        return {}
    marks = ", ".join("?" for _ in isins)
    df = db.query(
        f"""
        SELECT {_SIGNAL_COLUMNS}
        FROM {_SIGNALS} AS s
        JOIN (
            SELECT isin, MAX(as_of_date) AS newest FROM signals
            WHERE isin IN ({marks}) GROUP BY isin
        ) AS latest ON latest.isin = s.isin AND latest.newest = s.as_of_date
        JOIN instruments i ON i.isin = s.isin
        """,
        list(isins),
    )
    return {_req_str(row["isin"]): _signal(row) for _, row in df.iterrows()}


def latest_closes(db: Database, isins: list[str]) -> dict[str, tuple[dt.date, float]]:
    """Each instrument's newest non-null close and its date. Absent: no close."""
    if not isins:
        return {}
    marks = ", ".join("?" for _ in isins)
    df = db.query(
        f"""
        SELECT p.isin, p.date, p.close
        FROM prices_daily p
        JOIN (
            SELECT isin, MAX(date) AS newest FROM prices_daily
            WHERE isin IN ({marks}) AND close IS NOT NULL GROUP BY isin
        ) AS latest ON latest.isin = p.isin AND latest.newest = p.date
        """,
        list(isins),
    )
    out: dict[str, tuple[dt.date, float]] = {}
    for _, row in df.iterrows():
        day, close = _opt_date(row["date"]), _opt_float(row["close"])
        if day is not None and close is not None:
            out[_req_str(row["isin"])] = (day, close)
    return out


def split_bonus_dates(db: Database, isins: list[str]) -> dict[str, list[dt.date]]:
    """Ex-dates of each instrument's splits and bonuses, oldest first."""
    if not isins:
        return {}
    marks = ", ".join("?" for _ in isins)
    df = db.query(
        f"""
        SELECT a.isin, a.ex_date
        FROM corporate_actions a
        WHERE a.isin IN ({marks}) AND a.action_type IN ('SPLIT', 'BONUS')
        ORDER BY a.isin, a.ex_date
        """,
        list(isins),
    )
    out: dict[str, list[dt.date]] = {}
    for _, row in df.iterrows():
        day = _opt_date(row["ex_date"])
        if day is not None:
            days = out.setdefault(_req_str(row["isin"]), [])
            if day not in days:
                days.append(day)
    return out


def complete_run_dates(db: Database) -> set[dt.date]:
    """Every scoring date that covered at least `COMPLETE_RUN_SHARE` of the universe."""
    return {
        c.as_of
        for c in scored_date_counts(db, limit=None)
        if c.instruments and c.scored >= COMPLETE_RUN_SHARE * c.instruments
    }


def signal_on(db: Database, isin: str, as_of: dt.date) -> Signal | None:
    """The instrument's signal stored for exactly `as_of`, or None.

    No fall-back to the nearest date: a holder comparing today with the day
    they bought must be told there is no score for that day, not shown the
    week after's as though it were.
    """
    df = db.query(
        f"""
        SELECT {_SIGNAL_COLUMNS}
        FROM {_SIGNALS} AS s
        JOIN instruments i ON i.isin = s.isin
        WHERE s.isin = ? AND s.as_of_date = ?
        """,
        [isin, as_of],
    )
    if df.empty:
        return None
    return _signal(df.iloc[0])


def latest_signal(db: Database, isin: str) -> Signal | None:
    df = db.query(
        f"""
        SELECT {_SIGNAL_COLUMNS}
        FROM {_SIGNALS} AS s
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
    # `AS recent`: Postgres before 16 rejects an unaliased subquery in FROM, and
    # this SQL also runs against the published Postgres copy (`db.publish`).
    df = db.query(
        f"""
        SELECT * FROM (
            SELECT {_SIGNAL_COLUMNS}
            FROM {_SIGNALS} AS s
            JOIN instruments i ON i.isin = s.isin
            WHERE s.isin = ?
            ORDER BY s.as_of_date DESC
            LIMIT ?
        ) AS recent ORDER BY as_of_date
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


def recent_news(
    db: Database, isin: str, limit: int = 10, relevant_only: bool = False
) -> list[NewsItem]:
    """Latest news for one instrument, scored or not, newest first.

    Each item says how the company was found in it and whether the sentiment
    factor reads it. The factor reads an article only when it is scored and
    its company match clears `news_min_resolution_confidence` — the same
    threshold `Database.as_of_sentiment` applies — so a roundup scored before
    it was demoted keeps its label here but no longer counts.
    """
    threshold = settings.news_min_resolution_confidence
    relevant = (
        " AND n.matched_in = 'HEADLINE' AND COALESCE(n.resolution_method, '') <> ?"
        if relevant_only else ""
    )
    df = db.query(
        f"""
        SELECT n.published_at, n.headline, n.source, n.url, ns.label, ns.score,
               n.matched_in, n.resolution_method, n.resolution_confidence,
               ns.news_id AS scored_id
        FROM news n
        LEFT JOIN news_sentiment ns ON ns.news_id = n.news_id
        WHERE n.isin = ?{relevant}
        ORDER BY n.published_at DESC
        LIMIT ?
        """,
        [isin, *([_ROUNDUP] if relevant_only else []), int(limit)],
    )
    out: list[NewsItem] = []
    for _, row in df.iterrows():
        confidence = _opt_float(row["resolution_confidence"])
        attributed = confidence is not None and confidence >= threshold
        scored = _opt_str(row["scored_id"]) is not None
        out.append(
            NewsItem(
                published_at=_opt_date(row["published_at"]),
                headline=_opt_str(row["headline"]),
                label=_opt_str(row["label"]),
                score=_opt_float(row["score"]),
                source=_opt_str(row["source"]),
                url=_opt_str(row["url"]),
                relevance=_relevance(
                    _opt_str(row["matched_in"]), _opt_str(row["resolution_method"])
                ),
                sentiment_status=(
                    SCORED if scored else PENDING if attributed else NOT_SCORED
                ),
                counts_toward_sentiment=scored and attributed,
            )
        )
    return out


def annual_financials(db: Database, isin: str) -> list[AnnualFinancials]:
    """Every stored fiscal year for one company, newest first.

    Deliberately not filtered to one basis. A company that changed basis, or
    that has a consolidated row for one year and a standalone one for another,
    should show that on the page rather than have a year silently vanish — the
    mixed history *is* the finding, and it is what makes a CAGR across those
    years wrong.
    """
    df = db.query(
        """
        SELECT * FROM fundamentals_annual
        WHERE isin = ?
        ORDER BY fiscal_year DESC, basis
        """,
        [isin],
    )
    basis = ShareBasis.load(db, [isin])
    out: list[AnnualFinancials] = []
    for _, row in df.iterrows():
        filed = _opt_date(row.get("filing_date"))
        eps = _opt_float(row.get("eps"))
        out.append(
            AnnualFinancials(
                fiscal_year=int(row["fiscal_year"]),
                basis=_req_str(row.get("basis"), "—"),
                period_end=_opt_date(row.get("period_end_date")),
                filing_date=_opt_date(row.get("filing_date")),
                source=_opt_str(row.get("source")),
                auditor_opinion=_opt_str(row.get("auditor_opinion")),
                confidence=_opt_float(row.get("extraction_confidence")),
                values={
                    key: _opt_float(row.get(key)) for key, _label in ANNUAL_LINE_ITEMS
                },
                eps_adjusted=_adjusted(eps, basis, isin, filed),
                source_url=_opt_str(row.get("source_url")),
            )
        )
    return out


def _adjusted(
    eps: float | None, basis: ShareBasis, isin: str, filed: dt.date | None
) -> float | None:
    """`eps` restated across every split and bonus since it was filed."""
    return None if eps is None else eps / basis.factor(isin, filed)


def quarterly_financials(
    db: Database, isin: str, limit: int = 12
) -> list[QuarterlyFinancials]:
    """Stored quarters for one company, newest first.

    `results_comparison` returns only about five quarters per company, so the
    default limit is generous rather than restrictive — it is there to bound the
    page, not to hide history.
    """
    # `SELECT *` so a database not yet migrated to the P&L columns reads them
    # as missing rather than failing: a read-only connection cannot migrate.
    df = db.query(
        """
        SELECT * FROM fundamentals_quarterly
        WHERE isin = ?
        ORDER BY period_end_date DESC
        LIMIT ?
        """,
        [isin, int(limit)],
    )
    basis = ShareBasis.load(db, [isin])
    out: list[QuarterlyFinancials] = []
    for _, row in df.iterrows():
        period_end = _opt_date(row["period_end_date"])
        if period_end is None:
            continue
        filed = _opt_date(row.get("filing_date"))
        eps = _opt_float(row.get("eps"))
        consolidated = row.get("is_consolidated")
        out.append(
            QuarterlyFinancials(
                period_end=period_end,
                filing_date=_opt_date(row.get("filing_date")),
                relating_to=_opt_str(row.get("relating_to")),
                is_consolidated=(
                    None if consolidated is None or pd.isna(consolidated)
                    else bool(consolidated)
                ),
                revenue=_opt_float(row.get("revenue")),
                pat=_opt_float(row.get("pat")),
                eps=eps,
                eps_adjusted=_adjusted(eps, basis, isin, filed),
                basis=(
                    None if consolidated is None or pd.isna(consolidated)
                    else "CONSOLIDATED" if consolidated else "STANDALONE"
                ),
                **{name: _opt_float(row.get(name)) for name in _QUARTERLY_PNL},
                ebitda=_quarterly_ebitda(row),
                source_url=_opt_str(row.get("xbrl_url")),
            )
        )
    return out


_QUARTERLY_PNL = (
    "total_income", "other_income", "total_expenses", "depreciation",
    "interest_expense", "profit_before_tax", "tax_expense",
)


def _quarterly_ebitda(row: pd.Series) -> float | None:
    """PBT + finance costs + depreciation — the value factor's own fallback.

    None for banks and insurers: a bank's finance cost is the interest it pays
    depositors, its main operating cost, so adding it back describes nothing.
    """
    if unsupported_kind(_opt_str(row.get("xbrl_url"))) is not None:
        return None
    pbt = _opt_float(row.get("profit_before_tax"))
    depreciation = _opt_float(row.get("depreciation"))
    if pbt is None or depreciation is None:
        return None
    return pbt + (_opt_float(row.get("interest_expense")) or 0.0) + depreciation


@dataclass(frozen=True)
class CorporateAction:
    ex_date: dt.date
    action_type: str
    ratio: float | None
    amount: float | None
    record_date: dt.date | None
    details: str | None
    source: str | None


def corporate_actions(db: Database, isin: str) -> list[CorporateAction]:
    """Every stored action for one company, newest first."""
    df = db.query(
        """
        SELECT ex_date, action_type, ratio, amount, record_date, details, source
        FROM corporate_actions
        WHERE isin = ?
        ORDER BY ex_date DESC, action_type
        """,
        [isin],
    )
    return [
        CorporateAction(
            ex_date=_opt_date(row["ex_date"]) or dt.date.min,
            action_type=_req_str(row["action_type"]),
            ratio=_opt_float(row["ratio"]),
            amount=_opt_float(row["amount"]),
            record_date=_opt_date(row["record_date"]),
            details=_opt_str(row["details"]),
            source=_opt_str(row["source"]),
        )
        for _, row in df.iterrows()
    ]


@dataclass(frozen=True)
class ShareholdingQuarter:
    """One quarter's shareholding pattern, as percentages of total shares."""

    quarter_end: dt.date
    disclosed_date: dt.date | None
    disclosed_date_source: str | None
    promoter_pct: float | None
    promoter_pledged_pct: float | None
    fii_pct: float | None
    dii_pct: float | None
    public_pct: float | None
    employee_trust_pct: float | None


def shareholding(db: Database, isin: str, quarters: int = 12) -> list[ShareholdingQuarter]:
    """The newest `quarters` patterns, newest first."""
    df = db.query(
        """
        SELECT quarter_end, disclosed_date, disclosed_date_source, promoter_pct,
               promoter_pledged_pct, fii_pct, dii_pct, public_pct, employee_trust_pct
        FROM shareholding
        WHERE isin = ?
        ORDER BY quarter_end DESC
        LIMIT ?
        """,
        [isin, int(quarters)],
    )
    return [
        ShareholdingQuarter(
            quarter_end=_opt_date(row["quarter_end"]) or dt.date.min,
            disclosed_date=_opt_date(row["disclosed_date"]),
            disclosed_date_source=_opt_str(row["disclosed_date_source"]),
            promoter_pct=_opt_float(row["promoter_pct"]),
            promoter_pledged_pct=_opt_float(row["promoter_pledged_pct"]),
            fii_pct=_opt_float(row["fii_pct"]),
            dii_pct=_opt_float(row["dii_pct"]),
            public_pct=_opt_float(row["public_pct"]),
            employee_trust_pct=_opt_float(row["employee_trust_pct"]),
        )
        for _, row in df.iterrows()
    ]


@dataclass(frozen=True)
class KeyMetrics:
    """Valuation figures on the value factors' own share count and price basis."""

    as_of: dt.date | None              # the close everything is taken at
    price: float | None
    shares_outstanding_cr: float | None
    share_source: str | None           # panel.SHARES_STATED | SHARES_IMPLIED
    shares_filing_date: dt.date | None
    market_cap_cr: float | None
    eps_ttm: float | None
    pe_ttm: float | None
    ttm_through: dt.date | None
    ttm_filing_date: dt.date | None
    eps_annual: float | None
    pe_annual: float | None
    annual_fiscal_year: int | None
    annual_basis: str | None
    annual_filing_date: dt.date | None
    book_value_per_share: float | None
    pb: float | None
    dividends_ttm: float | None
    dividend_yield: float | None
    high_52w: float | None
    low_52w: float | None


TTM_QUARTERS = 4
_YEAR = dt.timedelta(days=365)
_NO_CUTOFF = dt.date(9999, 12, 31)

# Consolidated where the company files it, else its one basis — the rule
# `Database.as_of_fundamentals_history` applies for scoring, in SQL the
# Postgres copy can run.
_ANNUAL_ON_ONE_BASIS = """
    SELECT a.* FROM fundamentals_annual a
    WHERE a.isin = ? AND a.filing_date <= ?
      AND a.basis = (
          SELECT CASE
              WHEN SUM(CASE WHEN b.basis = 'CONSOLIDATED' THEN 1 ELSE 0 END) > 0
              THEN 'CONSOLIDATED' ELSE MIN(b.basis) END
          FROM fundamentals_annual b
          WHERE b.isin = ? AND b.filing_date <= ?
      )
    ORDER BY a.period_end_date DESC
    LIMIT 5
"""


def _first_with(frame: pd.DataFrame, column: str) -> pd.Series | None:
    """The first (newest) row of `frame` with a value in `column`."""
    if frame.empty or column not in frame.columns:
        return None
    rows = frame[pd.to_numeric(frame[column], errors="coerce").notna()]
    return None if rows.empty else rows.iloc[0]


def _consecutive_quarters(ends: list[dt.date]) -> bool:
    """Newest first, each about a quarter before the last."""
    return all(80 <= (a - b).days <= 100 for a, b in zip(ends, ends[1:], strict=False))


def key_metrics(db: Database, isin: str) -> KeyMetrics:
    """Market cap, P/E, P/B and the rest, as the value factors would compute them.

    Taken at the company's newest stored close and read only from what was
    filed by that day. The share count is the one `factors.panel` scores with —
    the newest stated count, quarterly first, else PAT / EPS — and every
    per-share figure is restated onto the price series' share basis by the
    same `share_basis` rule, so `pe_annual` is exactly 1 / earnings_yield and
    `pb` exactly 1 / book_to_price on a scoring date.
    """
    newest = db.query("SELECT MAX(date) AS d FROM prices_daily WHERE isin = ?", [isin])
    last = _opt_date(newest.iloc[0]["d"]) if not newest.empty else None
    bars = pd.DataFrame(columns=["date", "high", "low", "close"])
    if last is not None:
        bars = db.query(
            "SELECT date, high, low, close FROM prices_daily "
            "WHERE isin = ? AND date > ? ORDER BY date",
            [isin, last - _YEAR - dt.timedelta(days=45)],
        )
        for column in ("high", "low", "close"):
            bars[column] = pd.to_numeric(bars[column], errors="coerce")
        bars = bars[bars["close"] > 0]
    as_of = _opt_date(bars["date"].iloc[-1]) if not bars.empty else None
    price = _opt_float(bars["close"].iloc[-1]) if not bars.empty else None

    high_52w = low_52w = None
    if as_of is not None:
        year = bars[pd.to_datetime(bars["date"]).dt.date > as_of - _YEAR]
        high_52w = _opt_float(year["high"].fillna(year["close"]).max())
        low_52w = _opt_float(year["low"].fillna(year["close"]).min())

    cutoff = as_of or _NO_CUTOFF
    basis = ShareBasis.load(db, [isin])
    annual = restate(db.query(_ANNUAL_ON_ONE_BASIS, [isin, cutoff, isin, cutoff]), basis)
    quarterly = restate(
        db.query(
            "SELECT * FROM fundamentals_quarterly WHERE isin = ? AND filing_date <= ? "
            "ORDER BY period_end_date DESC LIMIT 8",
            [isin, cutoff],
        ),
        basis,
    )
    latest = annual.iloc[0] if not annual.empty else None

    def latest_value(column: str) -> float | None:
        return None if latest is None else _opt_float(latest.get(column))

    eps_annual = latest_value("eps")
    shares, source = share_count(
        quarterly,
        annual,
        pd.Series([latest_value("pat")], index=[isin], dtype=float),
        pd.Series([eps_annual], index=[isin], dtype=float),
        [isin],
    )
    n_shares = _opt_float(shares.iloc[0])
    share_source = _opt_str(source.iloc[0])
    stated_from = _first_with(quarterly, "shares_crore")
    if stated_from is None:
        stated_from = _first_with(annual, "shares_crore")
    shares_row = stated_from if share_source == SHARES_STATED else latest

    ttm_rows = quarterly.head(TTM_QUARTERS)
    eps_ttm = ttm_through = ttm_filing = None
    if (
        len(ttm_rows) == TTM_QUARTERS
        and pd.to_numeric(ttm_rows["eps"], errors="coerce").notna().all()
        and _consecutive_quarters([_opt_date(d) for d in ttm_rows["period_end_date"]])
    ):
        eps_ttm = _opt_float(pd.to_numeric(ttm_rows["eps"]).sum())
        ttm_through = _opt_date(ttm_rows["period_end_date"].iloc[0])
        ttm_filing = _opt_date(ttm_rows["filing_date"].iloc[0])

    equity = latest_value("total_equity")
    bvps = equity * 1e7 / n_shares if equity is not None and n_shares else None

    dividends = None
    if as_of is not None:
        actions = db.query(
            "SELECT ex_date, action_type, amount FROM corporate_actions WHERE isin = ?",
            [isin],
        )
        if not actions.empty:      # never fetched is unknown, not zero
            paid = 0.0
            for row in actions.itertuples(index=False):
                ex = _opt_date(row.ex_date)
                amount = _opt_float(row.amount)
                if (
                    row.action_type == "DIVIDEND" and amount is not None
                    and ex is not None and as_of - _YEAR < ex <= as_of
                ):
                    paid += amount / basis.factor(isin, ex)
            dividends = paid

    def over(numerator: float | None, denominator: float | None) -> float | None:
        if numerator is None or denominator is None or denominator <= 0:
            return None
        return numerator / denominator

    market_cap = price * n_shares / 1e7 if price is not None and n_shares else None
    return KeyMetrics(
        as_of=as_of,
        price=price,
        shares_outstanding_cr=n_shares / 1e7 if n_shares else None,
        share_source=share_source,
        shares_filing_date=(
            None if shares_row is None else _opt_date(shares_row.get("filing_date"))
        ),
        market_cap_cr=market_cap,
        eps_ttm=eps_ttm,
        pe_ttm=over(price, eps_ttm),
        ttm_through=ttm_through,
        ttm_filing_date=ttm_filing,
        eps_annual=eps_annual,
        pe_annual=over(price, eps_annual),
        annual_fiscal_year=None if latest is None else _opt_int(latest.get("fiscal_year")),
        annual_basis=None if latest is None else _opt_str(latest.get("basis")),
        annual_filing_date=None if latest is None else _opt_date(latest.get("filing_date")),
        book_value_per_share=bvps,
        pb=over(price, bvps),
        dividends_ttm=dividends,
        dividend_yield=over(dividends, price),
        high_52w=high_52w,
        low_52w=low_52w,
    )


def latest_price_date(db: Database, isin: str) -> dt.date | None:
    """The last day with a stored bar for one company, or None if it has none."""
    df = db.query("SELECT MAX(date) AS d FROM prices_daily WHERE isin = ?", [isin])
    if df.empty:
        return None
    return _opt_date(df.iloc[0]["d"])


def price_history(
    db: Database, isin: str, start: dt.date, end: dt.date
) -> list[PriceBar]:
    """Daily bars from `start` to `end`, both inclusive, oldest first."""
    df = db.query(
        """
        SELECT date, open, high, low, close, adj_close, volume
        FROM prices_daily
        WHERE isin = ? AND date >= ? AND date <= ?
        ORDER BY date
        """,
        [isin, start, end],
    )
    out: list[PriceBar] = []
    for _, row in df.iterrows():
        day = _opt_date(row["date"])
        if day is None:
            continue
        out.append(
            PriceBar(
                date=day,
                open=_opt_float(row["open"]),
                high=_opt_float(row["high"]),
                low=_opt_float(row["low"]),
                close=_opt_float(row["close"]),
                adj_close=_opt_float(row["adj_close"]),
                volume=_opt_int(row["volume"]),
            )
        )
    return out


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
    # The attribution threshold the factor reads through: without it, the mix
    # beside a score counted roundups the score itself ignores.
    df = db.query(
        f"""
        SELECT n.isin, ns.label, COUNT(*) AS n
        FROM news n
        JOIN news_sentiment ns ON ns.news_id = n.news_id
        WHERE n.isin IN ({placeholders})
          AND n.published_at <= ?
          AND n.published_at >= ?
          AND n.resolution_confidence >= ?
        GROUP BY n.isin, ns.label
        """,
        [
            *isins, dt.datetime.combine(as_of, dt.time.max), start,
            settings.news_min_resolution_confidence,
        ],
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
