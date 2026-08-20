"""Streamlit dashboard — DESIGN §9 phase 4.

Reads the database directly through `serve.queries`, the same functions the API
serves, so the two surfaces cannot drift apart. It deliberately does *not* call
the API over HTTP: this is a single-user local research tool, and requiring a
second process to be running before the UI works buys nothing.

The connection is opened per script run rather than cached. Streamlit re-runs the
script on every interaction and shares `st.cache_resource` objects across
sessions and threads, and a DuckDB connection is not safe to use that way — the
previous version's cached handle was a real hazard the moment two browser tabs
were open. A read-only open of a local file costs a millisecond.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterator
from contextlib import contextmanager

import pandas as pd
import streamlit as st

from stockanalysis.config import settings
from stockanalysis.db.database import (
    Database,
    DatabaseLockedError,
    SchemaOutOfDateError,
)
from stockanalysis.factors import redflags
from stockanalysis.factors.composite import BUY_THRESHOLD, FAMILY_WEIGHTS, SELL_THRESHOLD
from stockanalysis.serve import explain, ops, queries
from stockanalysis.serve import readiness as rd

PAGES = ["Run", "Overview", "Signals", "Instrument", "Red flags", "About"]

# Session-state keys shared between pages. The sidebar radio and the
# Instrument page's selectbox are both keyed, so writing these before a rerun
# is how one page hands the app to another.
PAGE_KEY = "nav_page"
INSTRUMENT_KEY = "nav_instrument_symbol"


def open_instrument(symbol: str) -> None:
    """Send the app to the Instrument page for `symbol`.

    Writes the two widget keys rather than rendering anything, so the caller
    stays a click handler and the navigation stays testable without a browser.
    Streamlit reads widget values from session state at the start of a run, so
    this must be followed by a rerun to take effect.
    """
    st.session_state[PAGE_KEY] = "Instrument"
    st.session_state[INSTRUMENT_KEY] = symbol

SIGNAL_EMOJI = {"BUY": "🟢", "HOLD": "🟡", "SELL": "🔴"}


# ----------------------------------------------------------------------
# Formatting helpers (pure — unit tested)
# ----------------------------------------------------------------------


def signal_color(signal: str | None) -> str:
    """Emoji for a signal. Unscored companies get their own marker, not a HOLD."""
    return SIGNAL_EMOJI.get(signal or "", "⚫")


def format_score(score: float | None) -> str:
    """Score with a band marker, using the model's live thresholds."""
    if score is None or pd.isna(score):
        return "—"
    if score >= BUY_THRESHOLD:
        marker = "🟢"
    elif score >= SELL_THRESHOLD:
        marker = "🟡"
    else:
        marker = "🔴"
    return f"{marker} {score:.1f}"


def format_pct(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value * 100:.0f}%"


def format_number(value: float | None, places: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value:.{places}f}"


def format_driver(driver: tuple[str, float] | None) -> str:
    """The family that moved a score most, as "Growth ↑" / "Momentum ↓"."""
    if driver is None:
        return "—"
    family, contribution = driver
    arrow = "↑" if contribution > 0 else "↓"
    return f"{family.capitalize()} {arrow}"


def signals_frame(
    signals: list[queries.Signal],
    drivers: dict[str, tuple[str, float]] | None = None,
) -> pd.DataFrame:
    """Signal rows as a display table. Empty input gives the right columns back.

    `drivers` adds the leading family per row, so the browser answers "why" at a
    glance rather than only on the detail page.
    """
    columns = [
        "Symbol", "Name", "Sector", "Score", "Signal",
        "Main driver", "Coverage", "Red flags",
    ]
    if not signals:
        return pd.DataFrame(columns=columns)
    drivers = drivers or {}
    return pd.DataFrame(
        [
            {
                "Symbol": s.nse_symbol,
                "Name": s.name,
                "Sector": s.sector or "—",
                "Score": format_score(s.composite_score),
                "Signal": f"{signal_color(s.signal)} {s.signal or 'unscored'}",
                "Main driver": format_driver(drivers.get(s.isin)),
                "Coverage": format_pct(s.coverage),
                "Red flags": ", ".join(s.red_flags) or "—",
            }
            for s in signals
        ],
        columns=columns,
    )


def format_crore(value: float | None) -> str:
    """A crore amount, thousands-separated. Blank where the figure is absent.

    Blank rather than 0 or '—' inside the statement: a zero in a financial
    statement is a claim, and `contingent_liabilities` in particular is NULL for
    every XBRL-sourced row because no element carries it. Printing 0 there would
    read as "this company has none", which is the opposite of what is known.

    NaN is treated as absent alongside None. Building the statement puts the
    values through a DataFrame, which turns every None into NaN on the way — so
    checking only for None formats the missing figures as the string "nan".
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.2f}"


def annual_statement_frame(rows: list[queries.AnnualFinancials]) -> pd.DataFrame:
    """Annual financials as a statement: line items down, years across.

    Transposed relative to how the rows are stored, because that is how a
    statement is read — one metric's trajectory across years is the question,
    and a year-per-row table makes the reader scan sideways to answer it.
    """
    if not rows:
        return pd.DataFrame()

    # A year is labelled by year alone unless the same year is stored under two
    # bases, in which case both columns need the basis to be distinguishable —
    # and seeing them side by side is the point, since mixing the two across
    # years is what turns a CAGR into a measurement of the change of basis.
    years = [r.fiscal_year for r in rows]
    columns = [
        f"FY{r.fiscal_year}"
        if years.count(r.fiscal_year) == 1
        else f"FY{r.fiscal_year} {r.basis.title()}"
        for r in rows
    ]
    frame = pd.DataFrame(
        {
            column: [
                r.values.get(key) for key, _label in queries.ANNUAL_LINE_ITEMS
            ]
            for column, r in zip(columns, rows, strict=True)
        },
        index=[label for _key, label in queries.ANNUAL_LINE_ITEMS],
    )
    return frame.map(format_crore)


def annual_provenance_frame(rows: list[queries.AnnualFinancials]) -> pd.DataFrame:
    """Where each year came from, and when it became knowable.

    Kept beside the statement rather than folded into it. Provenance is what
    tells an operator whether a figure was read from tagged data or from a
    model's reading of a page, and mixing it into the numbers would make the
    statement harder to read for both audiences.
    """
    if not rows:
        return pd.DataFrame(
            columns=["Year", "Basis", "Source", "Period end", "Knowable from",
                     "Auditor", "Confidence"]
        )
    return pd.DataFrame(
        [
            {
                "Year": f"FY{r.fiscal_year}",
                "Basis": r.basis.title(),
                "Source": _SOURCE_LABEL.get(r.source or "", r.source or "—"),
                "Period end": f"{r.period_end:%Y-%m-%d}" if r.period_end else "—",
                "Knowable from": (
                    f"{r.filing_date:%Y-%m-%d}" if r.filing_date else "—"
                ),
                "Auditor": r.auditor_opinion or "—",
                "Confidence": format_number(r.confidence),
            }
            for r in rows
        ]
    )


# "LLM" tells an operator nothing about what a row cost or how far to trust it.
_SOURCE_LABEL = {"XBRL": "XBRL (tagged)", "LLM": "Claude (retired path)"}


def quarterly_frame(rows: list[queries.QuarterlyFinancials]) -> pd.DataFrame:
    """Quarterly results, newest first, with the year-on-year move where it exists."""
    columns = [
        "Quarter", "Relating to", "Basis", "Revenue", "PAT", "EPS (₹)",
        "Rev YoY", "PAT YoY", "Knowable from",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)

    by_period = {r.period_end: r for r in rows}

    def yoy(row: queries.QuarterlyFinancials, field: str) -> str:
        """Against the same quarter a year earlier, matched by date.

        Matched on the date rather than by stepping four rows back: the stored
        quarters are not guaranteed contiguous, and a gap would silently turn a
        three-quarter comparison into a "year-on-year" number.
        """
        current = getattr(row, field)
        prior_end = _same_quarter_last_year(row.period_end, by_period)
        prior = getattr(by_period[prior_end], field) if prior_end else None
        if current is None or prior is None or prior == 0:
            return "—"
        return f"{(current - prior) / abs(prior):+.1%}"

    return pd.DataFrame(
        [
            {
                "Quarter": f"{r.period_end:%Y-%m-%d}",
                "Relating to": r.relating_to or "—",
                "Basis": (
                    "—" if r.is_consolidated is None
                    else ("Consolidated" if r.is_consolidated else "Standalone")
                ),
                "Revenue": format_crore(r.revenue) or "—",
                "PAT": format_crore(r.pat) or "—",
                "EPS (₹)": format_number(r.eps),
                "Rev YoY": yoy(r, "revenue"),
                "PAT YoY": yoy(r, "pat"),
                "Knowable from": (
                    f"{r.filing_date:%Y-%m-%d}" if r.filing_date else "—"
                ),
            }
            for r in rows
        ],
        columns=columns,
    )


def _same_quarter_last_year(
    period_end: dt.date, available: dict[dt.date, object]
) -> dt.date | None:
    """The stored period ending closest to one year before `period_end`.

    A tolerance rather than an exact match because quarter ends move by a day
    or two — 52/53-week retailers, and February. Anything further out than a
    fortnight is a different quarter and returns nothing.
    """
    target = period_end - dt.timedelta(days=365)
    nearest = min(
        (d for d in available if d != period_end),
        key=lambda d: abs((d - target).days),
        default=None,
    )
    if nearest is None or abs((nearest - target).days) > 14:
        return None
    return nearest


def export_frame(signals: list[queries.Signal]) -> pd.DataFrame:
    """Unformatted numbers for CSV export — a spreadsheet wants floats, not emoji."""
    return pd.DataFrame(
        [
            {
                "isin": s.isin,
                "nse_symbol": s.nse_symbol,
                "name": s.name,
                "sector": s.sector,
                "as_of": s.as_of.isoformat(),
                "composite_score": s.composite_score,
                "signal": s.signal,
                "coverage": s.coverage,
                "red_flags": ";".join(s.red_flags),
                "unknown_flags": ";".join(s.unknown_flags),
                "model_version": s.model_version,
            }
            for s in signals
        ]
    )


# ----------------------------------------------------------------------
# Connection
# ----------------------------------------------------------------------


@contextmanager
def open_db() -> Iterator[Database]:
    """Readable handle for one script run, or a readable error and a halt.

    `connect_for_read` rather than a plain read-only open: the Run page starts
    a job on a worker thread in this process, and that job holds a writable
    connection for as long as it runs. DuckDB will not open the same file
    read-only and writable at once, so a read-only open here would fail for the
    whole duration of every job — exactly when the operator most wants to look
    at the data.
    """
    try:
        db = Database.connect_for_read(settings.db_path)
    except FileNotFoundError:
        st.error(
            f"No database at `{settings.db_path}`. "
            "Run `stockanalysis init` and the phase 0-2 commands first."
        )
        st.stop()
    except SchemaOutOfDateError as exc:
        st.error(str(exc))
        st.stop()
    except DatabaseLockedError:
        st.warning(
            "The database is locked by another process — an ingest is probably "
            "running. DuckDB allows one writer at a time; retry when it finishes."
        )
        st.stop()
    try:
        yield db
    finally:
        db.close()


def _table(frame: pd.DataFrame) -> None:
    st.dataframe(frame, width="stretch", hide_index=True)


def _signal_table(
    signals: list[queries.Signal],
    drivers: dict[str, tuple[str, float]] | None,
    key: str,
) -> None:
    """A signal table whose rows open the company on the Instrument page.

    Every list of companies in this app is a list someone will want to drill
    into, so the click-through lives here rather than on one page — a table
    that navigates in one place and not another reads as a bug.
    """
    selection = st.dataframe(
        signals_frame(signals, drivers),
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key=key,
    )
    picked = _selected_row(selection)
    if picked is not None and picked < len(signals):
        open_instrument(signals[picked].nse_symbol)
        st.rerun()


def _require_signals(db: Database) -> dt.date:
    as_of = queries.latest_as_of(db)
    if as_of is None:
        st.warning(
            "No signals stored yet. Open the **Run** page and run a job with "
            "*Score and persist signal* ticked, or `stockanalysis score "
            "--as-of <date> --persist` from a terminal."
        )
        st.stop()
    return as_of


# ----------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------


def show_overview(db: Database) -> None:
    as_of = _require_signals(db)
    st.subheader(f"As of {as_of:%Y-%m-%d}")

    counts = queries.signal_counts(db, as_of)
    signals = queries.signals_on(db, as_of=as_of)
    flagged = [s for s in signals if s.has_red_flag]
    unscored = [s for s in signals if s.composite_score is None]

    columns = st.columns(5)
    columns[0].metric("🟢 BUY", counts.get("BUY", 0))
    columns[1].metric("🟡 HOLD", counts.get("HOLD", 0))
    columns[2].metric("🔴 SELL", counts.get("SELL", 0))
    columns[3].metric("🚩 Red flags", len(flagged))
    # Surfaced next to the signal counts on purpose: how much of the universe
    # the model could not score is part of reading the other three numbers.
    columns[4].metric("⚫ Unscored", len(unscored))

    st.divider()

    drivers = explain.dominant_families(db, as_of)

    tabs = st.tabs(["BUY", "HOLD", "SELL", "Unscored"])
    for name, tab in zip(["BUY", "HOLD", "SELL"], tabs[:3], strict=True):
        with tab:
            rows = [s for s in signals if s.signal == name]
            if not rows:
                st.info(f"No {name} signals on this date.")
            else:
                _signal_table(rows, drivers, key=f"overview_{name}")

    with tabs[3]:
        if not unscored:
            st.success("Every company in the universe cleared the coverage floor.")
        else:
            st.caption(
                "Coverage fell below the model's floor, so no score was produced. "
                "These are not HOLDs — they are companies the model could not see."
            )
            _signal_table(unscored, drivers, key="overview_unscored")

    st.caption("Click any row to open that company on the Instrument page.")


def show_signals(db: Database) -> None:
    _require_signals(db)
    st.subheader("Signal browser")

    dates = queries.scored_dates(db)
    left, middle, right = st.columns(3)
    with left:
        as_of = st.selectbox("Date", dates, format_func=lambda d: f"{d:%Y-%m-%d}")
    with middle:
        chosen = st.selectbox("Signal", ["All", "BUY", "HOLD", "SELL"])
    with right:
        sector = st.selectbox("Sector", ["All", *queries.sectors(db)])

    signals = queries.signals_on(
        db,
        as_of=as_of,
        signal=None if chosen == "All" else chosen,
        sector=None if sector == "All" else sector,
    )
    if not signals:
        st.info("Nothing matches those filters.")
        return

    search = st.text_input("Search symbol or name").strip().lower()
    if search:
        signals = [
            s
            for s in signals
            if search in s.nse_symbol.lower() or search in s.name.lower()
        ]
        if not signals:
            st.info(f"No match for {search!r}.")
            return

    st.caption(f"{len(signals)} companies")
    # One panel computation for the whole date, not one per row.
    drivers = explain.dominant_families(db, as_of)

    _signal_table(signals, drivers, key="signals_table")
    st.caption(
        "‘Main driver’ is the family that moved each score most — ↑ pushed it "
        "up, ↓ pulled it down. **Click a row** to open that company on the "
        "Instrument page."
    )

    st.download_button(
        "Download CSV",
        data=export_frame(signals).to_csv(index=False),
        file_name=f"signals_{as_of:%Y%m%d}.csv",
        mime="text/csv",
    )


def _selected_row(selection) -> int | None:
    """The single selected row index, or None.

    Defensive about the shape because `st.dataframe`'s return value is a
    Streamlit-version-dependent mapping, and a page that raises on an
    unexpected shape would take the whole Signals browser down rather than
    merely failing to navigate.
    """
    try:
        rows = selection["selection"]["rows"]
    except (TypeError, KeyError, AttributeError):
        return None
    return int(rows[0]) if rows else None


def family_frame(rows: list[explain.FamilyContribution]) -> pd.DataFrame:
    """Family attribution as a display table."""
    return pd.DataFrame(
        [
            {
                "Family": row.family.capitalize(),
                "What it measures": explain.FAMILY_MEANING.get(row.family, ""),
                "Weight": f"{row.weight:.0%}",
                "Percentile": (
                    "—" if row.percentile is None else f"{row.percentile:.0f}"
                ),
                "Pushes score": (
                    "—" if row.contribution is None else f"{row.contribution:+.2f}"
                ),
                "Verdict": row.verdict,
                "Data": f"{row.factors_measured}/{row.factors_total} factors",
            }
            for row in rows
        ]
    )


def driver_frame(drivers: list[explain.FactorDriver]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Factor": d.label,
                "Family": d.family,
                "Value": format_number(d.raw_value, 3),
                "vs sector": "—" if d.z is None else f"{d.z:+.2f} SD",
            }
            for d in drivers
        ]
    )


def show_why(db: Database, isin: str, signal: queries.Signal) -> None:
    """The reasoning behind a signal, reconstructed from stored factor values.

    This is deliberately not the LLM narrative. The narrative is optional, costs
    money, and may be absent; this is derived arithmetic and is always available.
    """
    st.markdown("### Why this signal")

    reasoning = explain.explain(db, isin, signal.as_of)
    if reasoning is None:
        st.info("No stored signal to explain.")
        return

    banner = {"BUY": st.success, "SELL": st.error}.get(signal.signal or "", st.warning)
    banner(reasoning.headline)

    if reasoning.stale:
        # Precise about what this does and does not imply. The stored version is
        # a hash over weights, thresholds, coverage floor and the overlay flag,
        # so a mismatch may be entirely benign — only a change in *family
        # weights* actually moves the attribution below.
        st.caption(
            f"⚠️ Stored under scoring config `{reasoning.stored_version}`; this "
            f"breakdown re-derives it under `{reasoning.current_version}`. The "
            "factor z-scores shown are the stored ones either way. If the family "
            "weights changed between the two, the contributions are approximate; "
            "a difference only in the coverage floor or thresholds does not "
            "affect them. Re-run `stockanalysis score` to realign."
        )

    for reason in reasoning.reasons:
        st.markdown(f"- {reason}")

    if reasoning.families:
        st.markdown("#### Where the score came from")
        _table(family_frame(reasoning.families))
        st.caption(
            "‘Pushes score’ is each family's signed contribution to the composite "
            "— they sum to it exactly. Percentile is rank within the universe "
            "scored that day, so it is relative, never absolute."
        )

        contributions = {
            row.family.capitalize(): row.contribution
            for row in reasoning.families
            if row.contribution is not None
        }
        if contributions:
            st.bar_chart(
                pd.Series(contributions, name="contribution"), width="stretch"
            )

    left, right = st.columns(2)
    with left:
        st.markdown("#### Arguing for it")
        if reasoning.strengths:
            _table(driver_frame(reasoning.strengths[:6]))
        else:
            st.info("No factor scores above its sector average.")
    with right:
        st.markdown("#### Arguing against it")
        if reasoning.weaknesses:
            _table(driver_frame(reasoning.weaknesses[:6]))
        else:
            st.info("No factor scores below its sector average.")

    if reasoning.strengths or reasoning.weaknesses:
        st.caption(
            "‘vs sector’ is standard deviations from the sector mean, "
            "sign-adjusted — +1.0 SD is good whether the underlying metric is "
            "return on equity or debt/equity."
        )

    if signal.red_flags:
        st.markdown("#### 🚩 Red flags")
        descriptions = {d.name: d.description for d in redflags.DEFINITIONS}
        for flag in signal.red_flags:
            st.error(f"**{flag}** — {descriptions.get(flag, 'no description')}")
        st.caption("A tripped flag forces SELL regardless of the factor scores.")

    if signal.unknown_flags:
        st.caption(
            "Red flags that could not be evaluated for want of data: "
            + ", ".join(signal.unknown_flags)
            + ". Their absence is not a clean bill of health."
        )

    if signal.narrative:
        st.markdown("#### Written summary")
        st.info(signal.narrative)
        st.caption("Generated by Claude from the same numbers shown above.")


HAVE_ICON = {
    rd.Have.PRESENT: "✅ have",
    rd.Have.PARTIAL: "🟡 partial",
    rd.Have.ABSENT: "❌ missing",
}

FLAG_ICON = {"TRIPPED": "🔴", "CLEAR": "✅", "UNKNOWN": "❔"}


def sources_frame(report: rd.Readiness) -> pd.DataFrame:
    """The data inventory, one row per source.

    "Closed by" names the step and what it costs. The annual row is the reason
    it is worth a column: the same gap is closed for nothing by the exchange's
    tagged filing for most companies and only costs money for the rest, and
    without the step named the operator cannot tell which case they are in.
    """
    return pd.DataFrame(
        [
            {
                "Source": s.label,
                "": HAVE_ICON.get(s.have, s.have.value),
                "What we hold": s.detail,
                "What is missing": s.gap or "—",
                "Closed by": _step_label(s),
                "Factors blocked": len(s.blocks),
            }
            for s in report.sources
        ]
    )


def _step_label(source: rd.SourceStatus) -> str:
    from stockanalysis.run.steps import PAID, STEPS_BY_KEY

    if source.have is rd.Have.PRESENT:
        return "—"
    if not source.step:
        # A gap with no step is the honest end of the line: the free path has
        # read this company's filings and found nothing usable, and there is no
        # paid path behind it any more. Naming a step here would be a button
        # that runs nothing.
        return "nothing further available"
    spec = STEPS_BY_KEY.get(source.step)
    if spec is None:
        return source.step
    return f"{spec.label} (paid)" if spec.cost == PAID else spec.label


def blocked_frame(report: rd.Readiness) -> pd.DataFrame:
    """Uncomputable factors, heaviest first — the order to fix them in."""
    blocked = sorted(
        (f for f in report.factors if not f.computable), key=lambda f: -f.weight
    )
    return pd.DataFrame(
        [
            {
                "Factor": explain.factor_label(f.name),
                "Family": f.family,
                "Weight": format_pct(f.weight),
                "Why": f.reason,
            }
            for f in blocked
        ]
    )


def flags_frame(report: rd.Readiness) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Flag": f.name,
                "": FLAG_ICON.get(f.state, "·"),
                "State": f.state,
                "Needs": (
                    "no source ingests this — it cannot be cleared"
                    if not f.reachable
                    else ", ".join(
                        rd.DATASETS_BY_KEY[k].label.lower() for k in f.blocked_by
                    ) or "—"
                ),
            }
            for f in report.flags
        ]
    )


def show_readiness(report: rd.Readiness) -> None:
    """What we hold for one company, what is missing, and the button that fixes it.

    Rendered whether or not a signal exists. The page used to return early when
    there was none, which meant the one screen that could explain *why* a
    company is unrated went blank in exactly that case.
    """
    columns = st.columns(4)
    columns[0].metric("Model coverage", format_pct(report.coverage))
    columns[1].metric(
        "Scorable today",
        "yes" if report.scorable else "no",
        help=(
            f"Coverage must reach {report.min_coverage:.0%} and the company "
            f"must be in {report.index_name} on the decision date."
        ),
    )
    columns[2].metric("Sources with gaps", f"{len(report.gaps)}/{len(report.sources)}")
    columns[3].metric(
        "Factors measured",
        f"{sum(1 for f in report.factors if f.computable)}/{len(report.factors)}",
    )

    if not report.in_universe:
        st.warning(
            f"Not a member of {report.index_name} on {report.as_of}. Scoring is "
            f"sector-relative within an index universe, so this company is not "
            f"scored at all — its data is still stored."
        )
    elif not report.scorable:
        st.warning(
            f"Coverage {report.coverage:.0%} is below the {report.min_coverage:.0%} "
            f"floor, so a run today would leave this company **unscored**. "
            f"Unscored is not HOLD — the model cannot see enough to have an "
            f"opinion."
        )

    _table(sources_frame(report))

    st.markdown("##### Coverage by family")
    fams = pd.DataFrame(
        [
            {
                "Family": f.family,
                "Model weight": format_pct(f.weight),
                "Factors measured": f"{f.measured}/{f.total}",
                "Family covered": format_pct(f.covered),
            }
            for f in report.families
        ]
    )
    _table(fams)
    st.caption(
        "Model coverage is the family weights times the share of each family "
        "actually measured. A family with no data is subtracted, never scored "
        "as neutral."
    )

    blocked = blocked_frame(report)
    if not blocked.empty:
        with st.expander(f"Not computable ({len(blocked)} factors)", expanded=False):
            _table(blocked)
            st.caption(
                "A reason naming a dataset is a gap an ingest can close. "
                "\"Inputs present — the ratio is undefined\" is not: the data "
                "is there and the company is loss-making or has a negative "
                "base, so re-running would change nothing."
            )

    with st.expander("Red flags — what could and could not be checked", expanded=False):
        _table(flags_frame(report))

    _fill_gaps_control(report)


def _fill_gaps_control(report: rd.Readiness) -> None:
    """Start the run that closes this company's gaps, then re-scores.

    Every step this can now offer is free. The paid annual-report extraction
    that used to sit behind a confirmation checkbox has been retired — NSE tags
    the balance sheet and cash flow in the results filing, so the figures it
    bought were already available for nothing.
    """
    from stockanalysis.run.runner import JobAlreadyRunning, runner
    from stockanalysis.run.steps import STEPS_BY_KEY, RunOptions, company_plan

    st.markdown("##### Fill the gaps and re-evaluate")

    gap_steps = list(report.next_steps())
    if not gap_steps:
        st.success(
            "Every source is complete. Re-score to refresh the signal against "
            "the current data."
        )

    # Scoring always runs last: ingesting without re-scoring leaves the stored
    # signal describing the data as it was before the run.
    steps = [*gap_steps, "score"]

    st.caption(
        "Will run: " + " → ".join(STEPS_BY_KEY[k].label for k in steps)
        + f"  ·  scored as of {report.as_of}  ·  no paid steps"
    )

    unreachable = [s for s in report.gaps if not s.step]
    if unreachable:
        st.warning(
            "No step can close: "
            + ", ".join(s.label for s in unreachable)
            + ". The free path has read this company's tagged filings and found "
            "nothing usable in them — banks are the usual case, since their "
            "results taxonomy carries no revenue line."
        )

    active = runner.is_active()
    if st.button(
        "▶ Fill gaps and re-score",
        type="primary",
        disabled=active,
        key=f"fill_{report.isin}",
    ):
        plan = company_plan(
            report.symbol or report.isin,
            steps,
            RunOptions(
                index_name=report.index_name,
                as_of=report.as_of,
                min_coverage=report.min_coverage,
            ),
        )
        try:
            runner.start(plan)
        except JobAlreadyRunning as e:
            st.error(str(e))
            return
        st.rerun()

    if ops.render_live_job():
        st.caption(
            "The same runner the Run page drives — one job at a time, because "
            "DuckDB allows one writer."
        )


def show_instrument(db: Database) -> None:
    st.subheader("Instrument analysis")

    instruments = queries.list_instruments(db)
    if not instruments:
        st.warning("No instruments loaded. Run `stockanalysis seed-universe` first.")
        return

    by_symbol = {i.nse_symbol: i for i in instruments}
    symbols = sorted(by_symbol)

    # A symbol handed over by a click-through elsewhere. Dropped if it is not in
    # this universe rather than raising: the stored value outlives the run that
    # wrote it, and a re-seeded universe should not break the page.
    if st.session_state.get(INSTRUMENT_KEY) not in symbols:
        st.session_state.pop(INSTRUMENT_KEY, None)

    symbol = st.selectbox("Instrument", symbols, key=INSTRUMENT_KEY)
    instrument = by_symbol[symbol]

    st.markdown(f"**{instrument.name}** — {instrument.sector or 'unclassified sector'}")
    st.caption(f"ISIN {instrument.isin}")

    rating_tab, financials_tab, data_tab = st.tabs(
        ["Rating", "Financials", "Data & gaps"]
    )

    with financials_tab:
        show_financials(db, instrument)

    with data_tab:
        as_of = st.date_input(
            "Decision date",
            value=dt.date.today(),
            key=f"readiness_asof_{instrument.isin}",
            help=(
                "Only data knowable on this date counts. Today answers "
                "'what would a run right now produce'."
            ),
        )
        try:
            report = rd.readiness(db, instrument.isin, as_of)
        except ValueError as e:
            st.error(str(e))
        else:
            show_readiness(report)

    with rating_tab:
        _show_rating(db, instrument)


def show_financials(db: Database, instrument: queries.Instrument) -> None:
    """The reported figures behind the score, annual and quarterly.

    Separate from the Rating tab on purpose. That tab answers "what does the
    model think and why"; this one answers "what did the company actually
    report", which is the question an operator asks when the model's answer
    looks wrong. Keeping them apart means neither has to be read through the
    other.
    """
    annual = queries.annual_financials(db, instrument.isin)
    quarters = queries.quarterly_financials(db, instrument.isin)

    st.markdown("### Annual")
    if not annual:
        st.info(
            "No annual financials stored. These come from NSE's tagged XBRL "
            "results filing — the **Data & gaps** tab can run the steps that "
            "fetch and read it."
        )
    else:
        st.caption(
            f"{len(annual)} year(s) · ₹ crore except EPS · a blank cell is a "
            f"figure the filing does not carry, not a zero"
        )
        _table(annual_statement_frame(annual))

        bases = {r.basis for r in annual}
        if len(bases) > 1:
            # Worth saying out loud: a growth rate computed across a change of
            # basis is measuring the change, not the business.
            st.warning(
                f"These years are not all on the same basis ({', '.join(sorted(bases))}). "
                f"Growth across a basis change measures the change of basis as "
                f"much as the business."
            )

        with st.expander("Where these figures came from", expanded=False):
            _table(annual_provenance_frame(annual))
            st.caption(
                "XBRL rows are read from the exchange's tagged filing with no "
                "model in the loop, which is why they carry full confidence. "
                "`contingent_liabilities` is blank on every one of them — the "
                "taxonomy has no element for it."
            )

    st.divider()
    st.markdown("### Quarterly")
    if not quarters:
        st.info(
            "No quarterly results stored. The **Ingest quarterly results** step "
            "fetches them from NSE for free."
        )
        return

    st.caption(
        f"{len(quarters)} quarter(s) · ₹ crore except EPS · year-on-year is "
        f"against the same quarter a year earlier, matched by date"
    )
    _table(quarterly_frame(quarters))

    plottable = [
        (q.period_end, q.revenue, q.pat)
        for q in reversed(quarters)
        if q.revenue is not None or q.pat is not None
    ]
    if len(plottable) >= 2:
        chart = pd.DataFrame(
            plottable, columns=["Quarter", "Revenue", "PAT"]
        ).set_index("Quarter")
        st.bar_chart(chart, width="stretch")


def _show_rating(db: Database, instrument: queries.Instrument) -> None:
    signal = queries.latest_signal(db, instrument.isin)
    if signal is None:
        st.info(
            "No signal stored for this instrument yet. The **Data & gaps** tab "
            "shows what is missing and can run the pipeline that fixes it."
        )
        return

    columns = st.columns(4)
    columns[0].metric("Score", format_score(signal.composite_score))
    columns[1].metric("Signal", f"{signal_color(signal.signal)} {signal.signal or '—'}")
    columns[2].metric("Coverage", format_pct(signal.coverage))
    columns[3].metric("As of", f"{signal.as_of:%Y-%m-%d}")

    if signal.composite_score is None:
        st.warning(
            "Coverage was below the model's floor on this date, so no score was "
            "produced. The factor values below are still what was measured."
        )

    show_why(db, instrument.isin, signal)

    st.divider()
    left, right = st.columns(2)

    with left:
        st.markdown("### All factors")
        factors = queries.factor_breakdown(db, instrument.isin, signal.as_of)
        if not factors:
            st.info("No factor values stored for this date.")
        else:
            _table(
                pd.DataFrame(
                    [
                        {
                            "Factor": explain.factor_label(f.factor_name),
                            "Raw": format_number(f.raw_value),
                            "Sector z": format_number(f.sector_zscore),
                        }
                        for f in factors
                    ]
                )
            )
            st.caption(
                "Sector z is relative to the company's own sector and "
                "sign-adjusted, so positive is always good."
            )

    with right:
        st.markdown("### Score history")
        history = queries.signal_history(db, instrument.isin, limit=60)
        points = [(s.as_of, s.composite_score) for s in history if s.composite_score]
        if len(points) < 2:
            st.info("Not enough scored dates to plot a trend.")
        else:
            frame = pd.DataFrame(points, columns=["date", "score"]).set_index("date")
            st.line_chart(frame, width="stretch")

    st.divider()
    st.markdown("### Recent news")
    news = queries.recent_news(db, instrument.isin, limit=10)
    if not news:
        st.info("No news resolved to this instrument.")
    else:
        # The sentiment factor only reads the window ending on the scoring date.
        # Showing the latest headlines without marking that boundary made the
        # page contradict itself: six articles listed under a signal whose
        # reasoning said there was no scored news.
        window = settings.narrative_news_window_days
        cutoff = signal.as_of - dt.timedelta(days=window)
        marker = {"positive": "✅", "negative": "⚠️", "neutral": "ℹ️"}
        counted = 0
        for item in news:
            when = f"{item.published_at:%Y-%m-%d}" if item.published_at else "undated"
            emoji = marker.get((item.label or "").lower(), "•")
            inside = item.published_at is not None and cutoff <= item.published_at <= signal.as_of
            counted += inside
            suffix = "" if inside else "  ·  _outside the scoring window_"
            st.markdown(f"{emoji} **{when}** — {item.headline or 'no headline'}{suffix}")
        st.caption(
            f"The sentiment factor reads only the {window} days to "
            f"{signal.as_of:%Y-%m-%d} ({cutoff:%Y-%m-%d} onwards) — "
            f"{counted} of these {len(news)} articles. Older items are shown for "
            "context and did not affect the score."
        )


def show_red_flags(db: Database) -> None:
    as_of = _require_signals(db)
    st.subheader("Red flags")

    flagged = queries.signals_on(db, as_of=as_of, flagged_only=True)
    if not flagged:
        st.success(f"No red flags tripped as of {as_of:%Y-%m-%d}.")
    else:
        st.warning(f"{len(flagged)} companies flagged as of {as_of:%Y-%m-%d}.")
        descriptions = {d.name: d.description for d in redflags.DEFINITIONS}
        for signal in flagged:
            label = f"{signal.nse_symbol} — {signal.name}"
            with st.expander(f"{label}  ({format_score(signal.composite_score)})"):
                for flag in signal.red_flags:
                    st.error(f"**{flag}** — {descriptions.get(flag, 'no description')}")
                if signal.narrative:
                    st.info(signal.narrative)
                # A flag is the most likely reason to want the reported figures
                # in front of you, so the jump to them is offered right here.
                if st.button(
                    f"Open {signal.nse_symbol}",
                    key=f"open_flagged_{signal.isin}",
                ):
                    open_instrument(signal.nse_symbol)
                    st.rerun()

    unreachable = redflags.unreachable_flags()
    if unreachable:
        st.divider()
        st.caption(
            "No ingest path currently supplies data for these flags, so they can "
            "never trip and their absence means nothing: "
            + ", ".join(unreachable)
        )


def show_about() -> None:
    st.markdown(
        """
        ## About

        A factor-based research system for Indian equities. Companies are scored
        against a transparent model, and every number on these pages traces back
        to stored factor values.

        **The score is relative.** It is a percentile within the universe scored
        on that date — 80 means "near the top of this universe today", not
        "cheap". A universe of uniformly overvalued companies still produces
        BUYs. That is a property of every cross-sectional factor model.
        """
    )

    st.markdown("### Factor weights")
    _table(
        pd.DataFrame(
            [
                {"Family": family.capitalize(), "Weight": f"{weight:.0%}"}
                for family, weight in FAMILY_WEIGHTS.items()
            ]
        )
    )

    st.markdown("### Thresholds")
    st.markdown(
        f"- **BUY** at {BUY_THRESHOLD:.0f} and above\n"
        f"- **HOLD** between {SELL_THRESHOLD:.0f} and {BUY_THRESHOLD:.0f}\n"
        f"- **SELL** below {SELL_THRESHOLD:.0f}\n"
        "- **Unscored** where coverage fell below the model's floor"
    )

    st.markdown("### Red flag overlay")
    st.caption("Any tripped flag forces SELL regardless of the factor scores.")
    _table(
        pd.DataFrame(
            [
                {
                    "Flag": d.name,
                    "Rule": d.description,
                    "Data available": "yes" if d.reachable else "no source ingested",
                }
                for d in redflags.DEFINITIONS
            ]
        )
    )

    st.caption(
        "Weights, thresholds and flag rules on this page are read from the "
        "running model, not transcribed — they cannot drift out of date."
    )


# ----------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title="StockAnalysis — factor research",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.title("📊 Factor-based equity research")
    st.caption("Indian equities (NSE/BSE) — scoring, red flags and factor attribution")

    with st.sidebar:
        # Keyed so `open_instrument` can move the app to another page. Streamlit
        # reads a widget's value from session state on the next run, so setting
        # the key before `st.rerun()` is what makes the sidebar follow a
        # click-through instead of snapping back to where it was.
        page = st.radio(
            "Page", PAGES, label_visibility="collapsed", key=PAGE_KEY
        )
        st.divider()
        st.caption(f"Database: `{settings.db_path}`")

    if page == "About":
        show_about()
        return

    with open_db() as db:
        if page == "Run":
            ops.show_run(db)
        elif page == "Overview":
            show_overview(db)
        elif page == "Signals":
            show_signals(db)
        elif page == "Instrument":
            show_instrument(db)
        elif page == "Red flags":
            show_red_flags(db)


if __name__ == "__main__":   # pragma: no cover
    main()
