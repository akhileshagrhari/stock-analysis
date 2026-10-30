"""Portfolio maths: stored holdings plus looked-up stock data in, page data out.

Pure: no database access. Arithmetic is in `Decimal`; floats appear only in the
output dataclasses. Closes, signal dates and ex-dates are Indian trading dates,
so an upload's date is its calendar date in India time.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from stockanalysis.portfolio.store import StoredHolding
from stockanalysis.serve.queries import Instrument, Signal

Group = Literal["BUY", "HOLD", "SELL", "NOT_RATED"]
GROUP_ORDER: tuple[Group, ...] = ("BUY", "HOLD", "SELL", "NOT_RATED")
OUTSIDE_UNIVERSE = (
    "Outside the NIFTY 100 companies the model covers. ETFs, gold bonds and REITs are never rated."
)
NOT_SCORED_YET = "Not scored yet."

_IST = ZoneInfo("Asia/Kolkata")
_ZERO = Decimal(0)


@dataclass(frozen=True)
class Lookups:
    instruments: dict[str, Instrument]  # by ISIN
    closes: dict[str, tuple[dt.date, float]]
    signals: dict[str, Signal]
    split_bonus: dict[str, list[dt.date]]
    complete_runs: set[dt.date]
    not_scored_reason: dict[str, str]  # ISIN -> explain.score_status(...).detail
    ratings_as_of: dt.date | None  # max(complete_runs), or None
    run_model: str | None  # model_version of the signals on ratings_as_of


@dataclass(frozen=True)
class HoldingRow:
    symbol: str
    isin: str | None
    name: str | None
    quantity: float
    avg_cost: float
    price: float | None
    price_date: dt.date | None
    price_source: Literal["site", "file"] | None
    value: float | None
    invested: float
    pnl: float | None
    pnl_pct: float | None
    weight: float | None
    group: Group
    score: float | None
    signal_as_of: dt.date | None
    older_rating: bool
    earlier_model: bool
    was_signal: str | None
    red_flags: list[str]
    not_rated_reason: str | None
    split_since_upload: bool


@dataclass(frozen=True)
class GroupShare:
    group: Group
    holdings: int
    value: float
    share: float


@dataclass(frozen=True)
class Portfolio:
    holdings: list[HoldingRow]  # grouped in GROUP_ORDER, value desc within, unpriced last
    groups: list[GroupShare]  # all four, in GROUP_ORDER, zeros included
    value: float
    invested: float
    pnl: float
    pnl_pct: float | None
    unpriced: int
    ratings_as_of: dt.date | None  # latest complete run
    older_ratings: int
    prices_from: dt.date | None
    prices_to: dt.date | None


@dataclass
class _Row:
    holding: StoredHolding
    group: Group
    price: Decimal | None
    price_date: dt.date | None
    source: Literal["site", "file"] | None
    split: bool
    signal: Signal | None
    value: Decimal | None
    invested: Decimal


def _price(
    h: StoredHolding, look: Lookups, matched: bool
) -> tuple[Decimal | None, dt.date | None, Literal["site", "file"] | None, bool]:
    upload = h.uploaded_at.astimezone(_IST).date()
    file_price = h.file_price
    close = look.closes.get(h.isin) if matched and h.isin else None
    split = bool(matched and h.isin and any(d > upload for d in look.split_bonus.get(h.isin, ())))
    if split:
        return (
            file_price,
            upload if file_price is not None else None,
            ("file" if file_price is not None else None),
            True,
        )
    if close is not None and (close[0] >= upload or file_price is None):
        return Decimal(str(close[1])), close[0], "site", False
    if file_price is not None:
        return file_price, upload, "file", False
    return None, None, None, False


def _f(x: Decimal | None) -> float | None:
    return None if x is None else float(x)


def build_portfolio(holdings: list[StoredHolding], look: Lookups) -> Portfolio:
    rows: list[_Row] = []
    for h in holdings:
        matched = h.isin is not None and h.isin in look.instruments
        sig = look.signals.get(h.isin) if matched and h.isin else None
        group: Group = (
            sig.signal if sig is not None and sig.signal in ("BUY", "HOLD", "SELL") else "NOT_RATED"  # type: ignore[assignment]
        )
        price, pdate, source, split = _price(h, look, matched)
        value = None if price is None else price * h.quantity
        rows.append(
            _Row(
                h,
                group,
                price,
                pdate,
                source,
                split,
                sig if group != "NOT_RATED" else None,
                value,
                h.avg_cost * h.quantity,
            )
        )

    priced = [r for r in rows if r.value is not None]
    total = sum((r.value for r in priced if r.value is not None), _ZERO)
    invested = sum((r.invested for r in priced), _ZERO)
    pnl = total - invested

    out: list[HoldingRow] = []
    older = 0
    for r in rows:
        h = r.holding
        matched = h.isin is not None and h.isin in look.instruments
        sig = r.signal
        older_rating = bool(
            sig is not None and look.ratings_as_of is not None and sig.as_of != look.ratings_as_of
        )
        earlier = older_rating and sig is not None and sig.model_version != look.run_model
        older += older_rating
        was = None
        if sig is not None and (p := sig.previous) is not None:
            if (
                p.model_version == sig.model_version
                and p.as_of in look.complete_runs
                and p.signal != sig.signal
            ):
                was = p.signal
        if sig is not None:
            reason = None
        elif matched:
            reason = look.not_scored_reason.get(h.isin or "", NOT_SCORED_YET)
        else:
            reason = OUTSIDE_UNIVERSE
        row_pnl = None if r.value is None else r.value - r.invested
        out.append(
            HoldingRow(
                symbol=h.symbol,
                isin=h.isin,
                name=look.instruments[h.isin].name if matched and h.isin else None,
                quantity=float(h.quantity),
                avg_cost=float(h.avg_cost),
                price=_f(r.price),
                price_date=r.price_date,
                price_source=r.source,
                value=_f(r.value),
                invested=float(r.invested),
                pnl=_f(row_pnl),
                pnl_pct=_f(row_pnl / r.invested) if row_pnl is not None and r.invested else None,
                weight=_f(r.value / total) if r.value is not None and total else None,
                group=r.group,
                score=sig.composite_score if sig is not None else None,
                signal_as_of=sig.as_of if sig is not None else None,
                older_rating=older_rating,
                earlier_model=earlier,
                was_signal=was,
                red_flags=list(sig.red_flags) if sig is not None else [],
                not_rated_reason=reason,
                split_since_upload=r.split,
            )
        )

    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    keyed = sorted(
        zip(rows, out, strict=True),
        key=lambda p: (
            order[p[1].group],
            p[0].value is None,
            -(p[0].value or _ZERO),
            p[1].symbol,
        ),
    )
    shares: list[GroupShare] = []
    for g in GROUP_ORDER:
        members = [r for r, _ in keyed if r.group == g]
        gv = sum((r.value for r in members if r.value is not None), _ZERO)
        shares.append(GroupShare(g, len(members), float(gv), float(gv / total) if total else 0.0))

    dates = [r.price_date for r in rows if r.price_date is not None]
    return Portfolio(
        holdings=[o for _, o in keyed],
        groups=shares,
        value=float(total),
        invested=float(invested),
        pnl=float(pnl),
        pnl_pct=float(pnl / invested) if invested else None,
        unpriced=len(rows) - len(priced),
        ratings_as_of=look.ratings_as_of,
        older_ratings=older,
        prices_from=min(dates) if dates else None,
        prices_to=max(dates) if dates else None,
    )
