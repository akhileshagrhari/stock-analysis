"""Performance metrics computed from a rebalance-frequency NAV series."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass
class Metrics:
    periods: int
    years: float
    total_return: float
    cagr: float
    volatility: float
    sharpe: float
    sortino: float
    max_drawdown: float
    calmar: float
    hit_rate: float
    best_period: float
    worst_period: float
    avg_turnover: float
    total_costs_pct: float

    def to_dict(self) -> dict:
        return asdict(self)


def compute_metrics(
    nav: pd.Series,
    periods_per_year: int = 12,
    risk_free_rate: float = 0.065,
    turnover: pd.Series | None = None,
    total_costs: float = 0.0,
) -> Metrics:
    """Standard metrics from a NAV series indexed by rebalance date.

    `risk_free_rate` defaults to ~6.5%, roughly the Indian short-term government
    rate. Using a US-style 2% here would flatter every Sharpe on the book.
    """
    nav = nav.dropna().sort_index()
    if len(nav) < 2:
        # Positional construction here was silently wrong (13 args for 14
        # fields) and only reachable on a degenerate run, so no test hit it.
        # Keyword defaults cannot drift out of sync with the field list.
        return Metrics(
            periods=0, years=0.0, total_return=0.0, cagr=0.0, volatility=0.0,
            sharpe=0.0, sortino=0.0, max_drawdown=0.0, calmar=0.0, hit_rate=0.0,
            best_period=0.0, worst_period=0.0, avg_turnover=0.0,
            total_costs_pct=0.0,
        )

    rets = nav.pct_change().dropna()
    periods = len(rets)
    years = periods / periods_per_year
    total_return = float(nav.iloc[-1] / nav.iloc[0] - 1.0)

    cagr = float((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1.0) if years > 0 else 0.0
    volatility = float(rets.std() * np.sqrt(periods_per_year))

    excess = cagr - risk_free_rate
    sharpe = float(excess / volatility) if volatility > 0 else 0.0

    downside = rets[rets < 0]
    downside_vol = float(downside.std() * np.sqrt(periods_per_year)) if len(downside) else 0.0
    sortino = float(excess / downside_vol) if downside_vol > 0 else 0.0

    running_max = nav.cummax()
    drawdown = nav / running_max - 1.0
    max_drawdown = float(drawdown.min())

    calmar = float(cagr / abs(max_drawdown)) if max_drawdown < 0 else 0.0

    return Metrics(
        periods=periods,
        years=round(years, 2),
        total_return=total_return,
        cagr=cagr,
        volatility=volatility,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_drawdown,
        calmar=calmar,
        hit_rate=float((rets > 0).mean()),
        best_period=float(rets.max()),
        worst_period=float(rets.min()),
        avg_turnover=float(turnover.mean()) if turnover is not None and len(turnover) else 0.0,
        total_costs_pct=float(total_costs),
    )


def format_metrics(m: Metrics, title: str = "Backtest") -> str:
    pct = lambda x: f"{x * 100:>8.2f}%"  # noqa: E731
    return f"""
{title}
{"=" * len(title)}
  Periods              {m.periods:>9d}  ({m.years} years)
  Total return         {pct(m.total_return)}
  CAGR                 {pct(m.cagr)}
  Volatility (ann.)    {pct(m.volatility)}
  Sharpe               {m.sharpe:>9.2f}
  Sortino              {m.sortino:>9.2f}
  Max drawdown         {pct(m.max_drawdown)}
  Calmar               {m.calmar:>9.2f}
  Hit rate             {pct(m.hit_rate)}
  Best / worst period  {pct(m.best_period)} / {pct(m.worst_period)}
  Avg turnover         {pct(m.avg_turnover)}
  Cumulative costs     {pct(m.total_costs_pct)}
""".rstrip()
