"""Equal-weight buy-and-hold benchmark.

A factor backtest without a benchmark is uninterpretable. An 18% CAGR is
excellent if the universe returned 8% and worthless if it returned 22% — and
Indian equities have had long stretches of both. Every headline number this
system produces should be reported against the alternative of simply holding
the universe.

This is the *equal-weight* universe, deliberately, because the strategy it is
being compared against is also equal-weight. Benchmarking an equal-weight
strategy against a cap-weighted index conflates the factor's contribution with
the well-documented small-cap tilt that equal weighting introduces on its own.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from stockanalysis.backtest.metrics import Metrics, compute_metrics
from stockanalysis.db.database import Database


def equal_weight_benchmark(
    db: Database,
    index_name: str,
    dates: list[dt.date],
    initial_capital: float = 1_000_000.0,
    periods_per_year: int = 12,
) -> tuple[pd.Series, Metrics]:
    """Rebalanced equal-weight portfolio of the whole as-of universe.

    Rebalances on the same dates as the strategy so the two are compared on
    identical windows. Costs are not charged: the point is to measure what the
    market gave you, and a buy-and-hold investor's costs are near zero at this
    turnover.
    """
    nav = initial_capital
    rows = []

    for i in range(len(dates) - 1):
        t, t_next = dates[i], dates[i + 1]
        universe = db.as_of_universe(index_name, t)
        if not universe:
            continue

        fwd = db.forward_returns(universe, t, t_next).dropna()
        if fwd.empty:
            continue

        nav *= 1 + float(fwd.mean())
        rows.append({"date": t_next, "nav": nav})

    if not rows:
        return pd.Series(dtype=float), compute_metrics(pd.Series(dtype=float))

    series = pd.DataFrame(rows).set_index("date")["nav"]
    return series, compute_metrics(series, periods_per_year=periods_per_year)


def format_comparison(
    strategy: Metrics, benchmark: Metrics, strategy_name: str = "Strategy"
) -> str:
    """Side-by-side comparison. Excess return is the number that matters."""
    excess = strategy.cagr - benchmark.cagr

    def row(label: str, a: float, b: float, pct: bool = True) -> str:
        fa = f"{a * 100:>9.2f}%" if pct else f"{a:>10.2f}"
        fb = f"{b * 100:>9.2f}%" if pct else f"{b:>10.2f}"
        return f"  {label:<22}{fa}{fb}"

    verdict = (
        f"[+] {strategy_name} beat equal-weight hold by {excess * 100:.2f}%/yr"
        if excess > 0
        else f"[-] {strategy_name} LAGGED equal-weight hold by {abs(excess) * 100:.2f}%/yr"
    )

    return "\n".join([
        "",
        f"  {'':<22}{strategy_name:>10}{'EW hold':>10}",
        f"  {'-' * 42}",
        row("CAGR", strategy.cagr, benchmark.cagr),
        row("Volatility", strategy.volatility, benchmark.volatility),
        row("Sharpe", strategy.sharpe, benchmark.sharpe, pct=False),
        row("Max drawdown", strategy.max_drawdown, benchmark.max_drawdown),
        f"  {'-' * 42}",
        f"  {verdict}",
        "",
    ])
