import datetime as dt
from decimal import Decimal

import pytest

from stockanalysis.portfolio.store import StoredHolding
from stockanalysis.portfolio.view import (
    GROUP_ORDER,
    OUTSIDE_UNIVERSE,
    Lookups,
    build_portfolio,
)
from stockanalysis.serve.queries import Instrument, PreviousSignal, Signal

UPLOAD = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)
D = dt.date
RUN = D(2026, 10, 24)


def hold(symbol, isin, qty="10", cost="100", file_price="110", uploaded_at=UPLOAD):
    return StoredHolding(
        symbol,
        isin,
        Decimal(qty),
        Decimal(cost),
        None if file_price is None else Decimal(file_price),
        uploaded_at,
    )


def sig(isin, signal="BUY", as_of=RUN, model="b", previous=None, flags=None):
    return Signal(
        isin=isin,
        nse_symbol=isin,
        name=isin,
        as_of=as_of,
        composite_score=0.5,
        signal=signal,
        coverage=1.0,
        red_flags=flags or [],
        model_version=model,
        previous=previous,
    )


def look(
    isins=(), closes=None, signals=None, split=None, runs=None, reasons=None, as_of=RUN, model="b"
):
    return Lookups(
        instruments={i: Instrument(i, i, i.upper()) for i in isins},
        closes=closes or {},
        signals=signals or {},
        split_bonus=split or {},
        complete_runs=runs if runs is not None else {RUN},
        not_scored_reason=reasons or {},
        ratings_as_of=as_of,
        run_model=model,
    )


def test_site_close_used_when_newer_than_the_upload():
    lk = look(["A"], closes={"A": (D(2026, 10, 24), 120.0)})
    r = build_portfolio([hold("A", "A")], lk).holdings[0]
    assert (r.price, r.price_source, r.price_date) == (120.0, "site", D(2026, 10, 24))
    assert r.value == 1200.0 and r.invested == 1000.0 and r.pnl == 200.0
    assert r.pnl_pct == pytest.approx(0.2)


def test_file_price_used_when_newer_than_the_site_close():
    lk = look(["A"], closes={"A": (D(2026, 10, 19), 120.0)})
    r = build_portfolio([hold("A", "A")], lk).holdings[0]
    assert (r.price, r.price_source) == (110.0, "file")


def test_close_on_upload_day_in_india_counts_as_on_or_after():
    # 00:30 IST on 21 Oct is 19:00 UTC on 20 Oct.
    up = dt.datetime(2026, 10, 20, 19, 0, tzinfo=dt.UTC)
    lk = look(["A"], closes={"A": (D(2026, 10, 21), 120.0)})
    r = build_portfolio([hold("A", "A", uploaded_at=up)], lk).holdings[0]
    assert r.price_source == "site"
    # a close of the 20th is before the IST upload day
    lk = look(["A"], closes={"A": (D(2026, 10, 20), 120.0)})
    r = build_portfolio([hold("A", "A", uploaded_at=up)], lk).holdings[0]
    assert r.price_source == "file"


def test_split_after_upload_uses_file_price_and_flags_it():
    lk = look(["A"], closes={"A": (D(2026, 10, 24), 60.0)}, split={"A": [D(2026, 10, 21)]})
    r = build_portfolio([hold("A", "A")], lk).holdings[0]
    assert r.price_source == "file" and r.price == 110.0 and r.split_since_upload
    lk = look(["A"], closes={"A": (D(2026, 10, 24), 60.0)}, split={"A": [D(2026, 10, 20)]})
    r = build_portfolio([hold("A", "A")], lk).holdings[0]
    assert r.price_source == "site" and not r.split_since_upload


def test_unpriced_holding_is_left_out_of_totals():
    lk = look(["A"], closes={"A": (D(2026, 10, 24), 10.0)})
    p = build_portfolio([hold("A", "A"), hold("ZZ", None, file_price=None)], lk)
    assert p.unpriced == 1 and p.value == 100 and p.invested == 1000 - 0
    assert p.holdings[-1].symbol == "ZZ"
    z = p.holdings[-1]
    assert z.weight is None and z.value is None and z.pnl is None


def test_groups_order_and_shares_sum_to_one():
    lk = look(
        ["A", "B"],
        closes={"A": (RUN, 30.0), "B": (RUN, 10.0)},
        signals={"A": sig("A", "BUY"), "B": sig("B", "SELL")},
    )
    p = build_portfolio([hold("A", "A"), hold("B", "B"), hold("X", None, file_price="10")], lk)
    assert [g.group for g in p.groups] == list(GROUP_ORDER)
    by = {g.group: g for g in p.groups}
    assert by["HOLD"].holdings == 0 and by["HOLD"].share == 0
    assert by["NOT_RATED"].holdings == 1
    assert sum(g.share for g in p.groups) == pytest.approx(1)
    assert [r.group for r in p.holdings] == ["BUY", "SELL", "NOT_RATED"]
    assert p.holdings[0].weight == pytest.approx(300 / 500)


def test_empty_portfolio():
    p = build_portfolio([], look())
    assert p.value == 0 and p.pnl_pct is None and len(p.groups) == 4
    assert all(g.share == 0 for g in p.groups)


def test_rows_sorted_by_value_within_group():
    lk = look(
        ["A", "B", "C", "D"],
        closes={"A": (RUN, 10.0), "B": (RUN, 30.0), "C": (RUN, 30.0)},
        signals={i: sig(i) for i in "ABCD"},
    )
    hs = [hold("A", "A"), hold("D", "D", file_price=None), hold("C", "C"), hold("B", "B")]
    p = build_portfolio(hs, lk)
    assert [r.symbol for r in p.holdings] == ["B", "C", "A", "D"]


def test_older_rating_and_earlier_model_flags():
    lk = look(
        ["A", "B"],
        signals={"A": sig("A", as_of=D(2026, 8, 14), model="a"), "B": sig("B")},
    )
    p = build_portfolio([hold("A", "A"), hold("B", "B")], lk)
    a = next(r for r in p.holdings if r.symbol == "A")
    b = next(r for r in p.holdings if r.symbol == "B")
    assert a.older_rating and a.earlier_model and a.signal_as_of == D(2026, 8, 14)
    assert not b.older_rating and not b.earlier_model
    assert p.older_ratings == 1 and p.ratings_as_of == RUN


def test_newer_partial_run_rating_is_not_older():
    """A partial run after the latest full one rates a stock more recently:
    not an older rating, not an earlier model, not in the count."""
    lk = look(
        ["A", "B"],
        signals={
            "A": sig("A", as_of=D(2026, 10, 28), model="c"),
            "B": sig("B", as_of=D(2026, 8, 14), model="a"),
        },
    )
    p = build_portfolio([hold("A", "A"), hold("B", "B")], lk)
    a = next(r for r in p.holdings if r.symbol == "A")
    b = next(r for r in p.holdings if r.symbol == "B")
    assert not a.older_rating and not a.earlier_model and a.signal_as_of == D(2026, 10, 28)
    assert b.older_rating and b.earlier_model
    assert p.older_ratings == 1


def test_no_ratings_as_of_means_no_older_flags():
    lk = look(
        ["A"],
        signals={"A": sig("A", as_of=D(2026, 8, 14), model="a")},
        as_of=None,
        runs=set(),
        model=None,
    )
    r = build_portfolio([hold("A", "A")], lk).holdings[0]
    assert not r.older_rating and not r.earlier_model


def test_was_signal_only_like_for_like():
    def prev(**kw):
        base = dict(as_of=D(2026, 10, 17), composite_score=0.1, signal="SELL", model_version="b")
        return PreviousSignal(**{**base, **kw})

    runs = {RUN, D(2026, 10, 17)}
    cases = {
        "same": (prev(), "BUY", "SELL"),
        "model": (prev(model_version="a"), "BUY", None),
        "partial": (prev(as_of=D(2026, 10, 10)), "BUY", None),
        "unchanged": (prev(signal="BUY"), "BUY", None),
    }
    for name, (pv, s, want) in cases.items():
        lk = look(["A"], signals={"A": sig("A", s, previous=pv)}, runs=runs)
        assert build_portfolio([hold("A", "A")], lk).holdings[0].was_signal == want, name
    lk = look(["A"], signals={"A": sig("A", "BUY")}, runs=runs)
    assert build_portfolio([hold("A", "A")], lk).holdings[0].was_signal is None


def test_not_rated_reasons():
    lk = look(["B", "C"], signals={"C": sig("C")}, reasons={"B": "Missing filings."})
    p = build_portfolio([hold("U", None), hold("B", "B"), hold("C", "C"), hold("N", "N")], lk)
    by = {r.symbol: r for r in p.holdings}
    assert by["U"].not_rated_reason == OUTSIDE_UNIVERSE
    assert by["B"].not_rated_reason == "Missing filings."
    assert by["C"].not_rated_reason is None and by["C"].group == "BUY"
    assert by["N"].not_rated_reason == OUTSIDE_UNIVERSE  # ISIN unknown to the universe


def test_matched_unscored_defaults_and_unrated_signal():
    lk = look(["B", "C"], signals={"C": sig("C", signal=None)})
    by = {r.symbol: r for r in build_portfolio([hold("B", "B"), hold("C", "C")], lk).holdings}
    assert by["B"].not_rated_reason == "Not scored yet."
    assert by["C"].group == "NOT_RATED" and by["C"].not_rated_reason == "Not scored yet."


def test_prices_range_and_name():
    lk = look(["A", "B"], closes={"A": (D(2026, 10, 22), 1.0), "B": (D(2026, 10, 24), 1.0)})
    p = build_portfolio([hold("A", "A"), hold("B", "B")], lk)
    assert (p.prices_from, p.prices_to) == (D(2026, 10, 22), D(2026, 10, 24))
    assert p.holdings[0].name in {"A", "B"}


def test_future_split_after_the_close_is_not_flagged():
    lk = look(["A"], closes={"A": (D(2026, 10, 24), 120.0)}, split={"A": [D(2026, 10, 27)]})
    r = build_portfolio([hold("A", "A")], lk).holdings[0]
    assert r.price_source == "site" and r.price == 120.0 and not r.split_since_upload


def test_split_between_upload_and_close_is_flagged():
    lk = look(["A"], closes={"A": (D(2026, 10, 24), 60.0)}, split={"A": [D(2026, 10, 23)]})
    r = build_portfolio([hold("A", "A")], lk).holdings[0]
    assert r.price_source == "file" and r.split_since_upload


def test_split_on_the_close_date_is_flagged():
    lk = look(["A"], closes={"A": (D(2026, 10, 24), 60.0)}, split={"A": [D(2026, 10, 24)]})
    r = build_portfolio([hold("A", "A")], lk).holdings[0]
    assert r.price_source == "file" and r.split_since_upload


def test_split_without_a_close_is_not_flagged():
    lk = look(["A"], split={"A": [D(2026, 10, 24)]})
    r = build_portfolio([hold("A", "A")], lk).holdings[0]
    assert r.price_source == "file" and not r.split_since_upload
