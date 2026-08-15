"""Database access layer.

The `as_of_*` methods are the **only sanctioned read path for backtest decisions**.
Each one filters by knowledge date, so a caller physically cannot see a row that
was not yet public on the decision date. Reaching past them into raw SQL
reintroduces lookahead bias, and it does so silently — the backtest will simply
get better and stay wrong.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import duckdb
import pandas as pd

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class DatabaseLockedError(RuntimeError):
    """Raised when another process holds the write lock.

    DuckDB allows a single writer. A long ingest therefore blocks every other
    command, which is easy to mistake for corruption if the raw IOException
    surfaces unexplained.
    """


class SchemaOutOfDateError(RuntimeError):
    """Raised when a read-only open finds tables the current schema declares.

    Only a read-only open can hit this. A writable open runs the idempotent
    schema on the way in and repairs itself, which is why the gap stays
    invisible until a reporting command trips over it.
    """


class Database:
    def __init__(self, path: str | Path = ":memory:", read_only: bool = False) -> None:
        self.path = str(path)
        self.read_only = read_only

        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        # read_only requires the file to exist; DuckDB will not create one.
        if read_only and not Path(self.path).exists():
            raise FileNotFoundError(
                f"{self.path} does not exist. Run `stockanalysis init` first."
            )

        try:
            self.conn = duckdb.connect(self.path, read_only=read_only)
        except duckdb.IOException as e:
            if "lock" in str(e).lower():
                raise DatabaseLockedError(
                    f"{self.path} is locked by another process — most likely an "
                    f"ingest still running. DuckDB permits one writer at a time. "
                    f"Wait for it to finish, or use a read-only command."
                ) from e
            raise

        if read_only:
            self._require_current_schema()
        else:
            self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute(SCHEMA_PATH.read_text())

    def _require_current_schema(self) -> None:
        """Fail readably when the file predates a table the schema now declares.

        `_init_schema` is idempotent but needs write access, so a read-only
        command against a database created by an earlier version cannot repair
        itself — it hits whichever missing table it queries first and surfaces a
        raw CatalogException. Phase 1 added `extraction_attempts` and
        `extraction_review`, which is exactly how this was found: `status` and
        `review` broke on a phase-0 database while every write command worked.
        """
        declared = set(re.findall(
            r"CREATE TABLE IF NOT EXISTS\s+(\w+)", SCHEMA_PATH.read_text(), re.IGNORECASE
        ))
        present = {
            r[0] for r in self.conn.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        missing = sorted(declared - present)
        if missing:
            raise SchemaOutOfDateError(
                f"{self.path} is missing {len(missing)} table(s) the current "
                f"schema declares ({', '.join(missing)}). It was created by an "
                f"earlier version. Run `stockanalysis init` to add them — it "
                f"creates only what is absent and touches no existing rows."
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert_df(self, table: str, df: pd.DataFrame, key_cols: list[str]) -> int:
        """Insert-or-replace a dataframe into `table`, keyed on `key_cols`."""
        if df.empty:
            return 0
        self.conn.register("_incoming", df)
        cols = ", ".join(df.columns)
        key_match = " AND ".join(f"t.{k} = s.{k}" for k in key_cols)
        self.conn.execute(
            f"DELETE FROM {table} AS t WHERE EXISTS "
            f"(SELECT 1 FROM _incoming AS s WHERE {key_match})"
        )
        self.conn.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _incoming")
        self.conn.unregister("_incoming")
        return len(df)

    def query(self, sql: str, params: list | None = None) -> pd.DataFrame:
        """Escape hatch for reporting and tests. Never use inside the backtest loop."""
        return self.conn.execute(sql, params or []).df()

    # ------------------------------------------------------------------
    # Point-in-time reads
    # ------------------------------------------------------------------

    def as_of_universe(self, index_name: str, as_of: dt.date) -> list[str]:
        """ISINs that were index members on `as_of`, and were listed and not yet
        delisted on that date.

        Membership intervals are half-open [from_date, to_date). A company that
        left the index or delisted after `as_of` is still included, because on
        that date it was there — that is the whole point.
        """
        sql = """
            SELECT DISTINCT m.isin
            FROM index_membership m
            JOIN instruments i ON i.isin = m.isin
            WHERE m.index_name = ?
              AND m.from_date <= ?
              AND (m.to_date IS NULL OR m.to_date > ?)
              AND (i.listing_date IS NULL OR i.listing_date <= ?)
              AND (i.delisting_date IS NULL OR i.delisting_date > ?)
            ORDER BY m.isin
        """
        rows = self.conn.execute(sql, [index_name, as_of, as_of, as_of, as_of]).fetchall()
        return [r[0] for r in rows]

    def as_of_prices(
        self,
        isins: list[str],
        as_of: dt.date,
        lookback_days: int = 400,
    ) -> pd.DataFrame:
        """Adjusted price history up to and including `as_of`. Never beyond."""
        if not isins:
            return pd.DataFrame(columns=["isin", "date", "adj_close", "traded_value"])
        start = as_of - dt.timedelta(days=lookback_days)
        placeholders = ", ".join("?" for _ in isins)
        sql = f"""
            SELECT isin, date, close, adj_close, volume, traded_value
            FROM prices_daily
            WHERE isin IN ({placeholders})
              AND date <= ?
              AND date >= ?
            ORDER BY isin, date
        """
        return self.conn.execute(sql, [*isins, as_of, start]).df()

    def as_of_fundamentals(
        self, isins: list[str], as_of: dt.date, basis: str = "CONSOLIDATED"
    ) -> pd.DataFrame:
        """Latest annual fundamentals *filed on or before* `as_of`.

        Filters on filing_date, not period_end_date. FY2024 figures were not
        knowable in April 2024 — the report was published in July.
        """
        if not isins:
            return pd.DataFrame()
        placeholders = ", ".join("?" for _ in isins)
        sql = f"""
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY isin ORDER BY period_end_date DESC, filing_date DESC
                ) AS rn
                FROM fundamentals_annual
                WHERE isin IN ({placeholders})
                  AND filing_date <= ?
                  AND basis = ?
            ) WHERE rn = 1
        """
        return self.conn.execute(sql, [*isins, as_of, basis]).df()

    def as_of_fundamentals_history(
        self,
        isins: list[str],
        as_of: dt.date,
        years: int = 5,
        prefer_basis: str = "CONSOLIDATED",
    ) -> pd.DataFrame:
        """Up to `years` of annual rows per ISIN, newest first, filed on or before `as_of`.

        Growth factors need a history, not a snapshot: a 3-year CAGR is four
        observations, and the "CFO/PAT below 0.5 for three consecutive years"
        red flag is three. Both would be silently uncomputable off
        `as_of_fundamentals`, which returns one row.

        **Basis fallback.** DESIGN §11.2 settles on consolidated where available
        with an explicit flag on the row. A company reporting only standalone
        (no subsidiaries) would otherwise vanish from every fundamental factor,
        which reads as missing data rather than as the accounting fact it is. So
        the basis is chosen *per ISIN* — consolidated if any consolidated row is
        visible, standalone otherwise — and never mixed within one company,
        because a CAGR computed across a basis change measures the change of
        basis.
        """
        if not isins:
            return pd.DataFrame()
        placeholders = ", ".join("?" for _ in isins)
        sql = f"""
            WITH visible AS (
                SELECT * FROM fundamentals_annual
                WHERE isin IN ({placeholders}) AND filing_date <= ?
            ),
            chosen AS (
                SELECT isin,
                       CASE WHEN BOOL_OR(basis = ?) THEN ? ELSE MIN(basis) END AS basis
                FROM visible GROUP BY isin
            )
            SELECT v.* FROM visible v
            JOIN chosen c ON c.isin = v.isin AND c.basis = v.basis
            ORDER BY v.isin, v.period_end_date DESC
        """
        df = self.conn.execute(
            sql, [*isins, as_of, prefer_basis, prefer_basis]
        ).df()
        if df.empty:
            return df
        return df.groupby("isin", group_keys=False).head(years)

    def as_of_quarterly(
        self, isins: list[str], as_of: dt.date, quarters: int = 8
    ) -> pd.DataFrame:
        """Quarterly results filed on or before `as_of`, newest first per ISIN.

        Filters on filing_date. NSE's `results_comparison` is free and needs no
        model in the loop, which makes it the only fundamental source with real
        coverage before the annual-report backfill runs.
        """
        if not isins:
            return pd.DataFrame()
        placeholders = ", ".join("?" for _ in isins)
        sql = f"""
            SELECT * FROM fundamentals_quarterly
            WHERE isin IN ({placeholders}) AND filing_date <= ?
            ORDER BY isin, period_end_date DESC
        """
        df = self.conn.execute(sql, [*isins, as_of]).df()
        if df.empty:
            return df
        return df.groupby("isin", group_keys=False).head(quarters)

    def as_of_sentiment(
        self,
        isins: list[str],
        as_of: dt.date,
        window_days: int = 30,
        min_confidence: float | None = None,
    ) -> pd.DataFrame:
        """Scored news published in the `window_days` before `as_of`.

        The knowledge date is `published_at`, not `ingested_at` or `computed_at`
        — an article scored today tells a backtest nothing about a decision made
        last year unless it was public then.

        **The attribution threshold is enforced here, in the read path.** Rows
        below it are stored (a better alias table can reuse the text) and a
        demoted row may still carry a score from before it was demoted, so
        filtering only at scoring time would let a retired attribution keep
        feeding the factor. One filter, on the way out, and the factor cannot
        see what it should not.
        """
        if not isins:
            return pd.DataFrame()
        if min_confidence is None:
            from stockanalysis.config import settings

            min_confidence = settings.news_min_resolution_confidence

        start = as_of - dt.timedelta(days=window_days)
        placeholders = ", ".join("?" for _ in isins)
        sql = f"""
            SELECT n.isin, n.news_id, n.published_at, s.model, s.label, s.score
            FROM news n
            JOIN news_sentiment s ON s.news_id = n.news_id
            WHERE n.isin IN ({placeholders})
              AND n.published_at <= ?
              AND n.published_at >= ?
              AND n.resolution_confidence >= ?
        """
        return self.conn.execute(
            sql,
            [*isins, dt.datetime.combine(as_of, dt.time.max), start, min_confidence],
        ).df()

    def as_of_shareholding(self, isins: list[str], as_of: dt.date) -> pd.DataFrame:
        """Latest shareholding pattern disclosed on or before `as_of`."""
        if not isins:
            return pd.DataFrame()
        placeholders = ", ".join("?" for _ in isins)
        sql = f"""
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY isin ORDER BY quarter_end DESC
                ) AS rn
                FROM shareholding
                WHERE isin IN ({placeholders}) AND disclosed_date <= ?
            ) WHERE rn = 1
        """
        return self.conn.execute(sql, [*isins, as_of]).df()

    def as_of_shareholding_history(
        self, isins: list[str], as_of: dt.date, quarters: int = 6
    ) -> pd.DataFrame:
        """Recent shareholding quarters per ISIN, newest first.

        The batch form of `shareholding.promoter_holding_trend`. The
        "promoter holding falling three consecutive quarters" red flag needs
        four observations per company, and running that one company at a time
        across a full backtest is thousands of round trips for data that fits in
        one scan.
        """
        if not isins:
            return pd.DataFrame()
        placeholders = ", ".join("?" for _ in isins)
        sql = f"""
            SELECT * FROM shareholding
            WHERE isin IN ({placeholders}) AND disclosed_date <= ?
            ORDER BY isin, quarter_end DESC
        """
        df = self.conn.execute(sql, [*isins, as_of]).df()
        if df.empty:
            return df
        return df.groupby("isin", group_keys=False).head(quarters)

    def forward_returns(
        self, isins: list[str], start: dt.date, end: dt.date
    ) -> pd.Series:
        """Realised return per ISIN over (start, end].

        This intentionally looks into the future — it is how the backtest scores a
        decision *after* the fact. Never feed its output into signal generation.

        A name that stops trading inside the window (delisting, suspension) is
        marked to its last available price rather than dropped, so failures are
        counted rather than quietly vanishing.
        """
        if not isins:
            return pd.Series(dtype=float)
        placeholders = ", ".join("?" for _ in isins)
        sql = f"""
            WITH bounds AS (
                SELECT
                    isin,
                    FIRST(adj_close ORDER BY date)  AS px_start,
                    LAST(adj_close ORDER BY date)   AS px_end
                FROM prices_daily
                WHERE isin IN ({placeholders}) AND date > ? AND date <= ?
                GROUP BY isin
            )
            SELECT isin, (px_end / px_start) - 1.0 AS ret
            FROM bounds WHERE px_start IS NOT NULL AND px_start > 0
        """
        df = self.conn.execute(sql, [*isins, start, end]).df()
        return df.set_index("isin")["ret"] if not df.empty else pd.Series(dtype=float)

    # ------------------------------------------------------------------
    # Data-quality introspection
    # ------------------------------------------------------------------

    def membership_is_survivorship_safe(
        self, index_name: str, start: dt.date, end: dt.date
    ) -> bool:
        """True only if verified historical membership covers the whole window."""
        sql = """
            SELECT 1 FROM index_membership_coverage
            WHERE index_name = ? AND verified_from <= ? AND verified_to >= ?
            LIMIT 1
        """
        return self.conn.execute(sql, [index_name, start, end]).fetchone() is not None
