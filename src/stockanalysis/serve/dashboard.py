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
from stockanalysis.serve import explain, queries

PAGES = ["Overview", "Signals", "Instrument", "Red flags", "About"]

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
    """Read-only handle for one script run, or a readable error and a halt."""
    try:
        db = Database(settings.db_path, read_only=True)
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


def _require_signals(db: Database) -> dt.date:
    as_of = queries.latest_as_of(db)
    if as_of is None:
        st.warning(
            "No signals stored yet. Run `stockanalysis score --as-of <date>` "
            "to populate them."
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
                _table(signals_frame(rows, drivers))

    with tabs[3]:
        if not unscored:
            st.success("Every company in the universe cleared the coverage floor.")
        else:
            st.caption(
                "Coverage fell below the model's floor, so no score was produced. "
                "These are not HOLDs — they are companies the model could not see."
            )
            _table(signals_frame(unscored, drivers))


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
    _table(signals_frame(signals, drivers))
    st.caption(
        "‘Main driver’ is the family that moved each score most — ↑ pushed it "
        "up, ↓ pulled it down. Open a name on the Instrument page for the full "
        "reasoning."
    )

    st.download_button(
        "Download CSV",
        data=export_frame(signals).to_csv(index=False),
        file_name=f"signals_{as_of:%Y%m%d}.csv",
        mime="text/csv",
    )


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


def show_instrument(db: Database) -> None:
    st.subheader("Instrument analysis")

    instruments = queries.list_instruments(db)
    if not instruments:
        st.warning("No instruments loaded. Run `stockanalysis seed-universe` first.")
        return

    by_symbol = {i.nse_symbol: i for i in instruments}
    symbol = st.selectbox("Instrument", sorted(by_symbol))
    instrument = by_symbol[symbol]

    st.markdown(f"**{instrument.name}** — {instrument.sector or 'unclassified sector'}")
    st.caption(f"ISIN {instrument.isin}")

    signal = queries.latest_signal(db, instrument.isin)
    if signal is None:
        st.info("No signal stored for this instrument yet.")
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
        page = st.radio("Page", PAGES, label_visibility="collapsed")
        st.divider()
        st.caption(f"Database: `{settings.db_path}`")

    if page == "About":
        show_about()
        return

    with open_db() as db:
        if page == "Overview":
            show_overview(db)
        elif page == "Signals":
            show_signals(db)
        elif page == "Instrument":
            show_instrument(db)
        elif page == "Red flags":
            show_red_flags(db)


if __name__ == "__main__":   # pragma: no cover
    main()
