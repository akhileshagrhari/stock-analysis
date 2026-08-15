"""Walk-forward backtest engine.

Contract, in order, at every rebalance date `t`:

  1. Reconstruct the universe **as it stood on t** — including names that later
     delisted, excluding names not yet listed.
  2. Compute factors using only data with knowledge date <= t.
  3. Rank, select, weight.
  4. Realise returns over (t, t_next] — the only place the future is touched.
  5. Charge transaction costs on the weight change.

Step 4 is deliberately the sole point of contact with future data, and it feeds
only the P&L, never the signal. Everything upstream flows through
`Database.as_of_*`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from stockanalysis.backtest.costs import costs_from_weight_change
from stockanalysis.backtest.metrics import Metrics, compute_metrics
from stockanalysis.config import CostModel
from stockanalysis.db.database import Database
from stockanalysis.factors.base import Factor, sector_zscore

log = logging.getLogger(__name__)


@dataclass
class BacktestConfig:
    index_name: str = "NIFTY100"
    start: dt.date = dt.date(2019, 1, 1)
    end: dt.date = dt.date.today()
    rebalance_freq: str = "ME"  # pandas offset alias: month end
    top_n: int = 20
    initial_capital: float = 1_000_000.0
    min_universe_size: int = 10
    apply_costs: bool = True
    cost_model: CostModel | None = None
    # Test hook. The shuffled-label control run injects a permutation here to
    # verify the harness cannot manufacture alpha from noise.
    return_transform: Callable[[pd.Series, dt.date], pd.Series] | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "index_name": self.index_name,
                "start": str(self.start),
                "end": str(self.end),
                "rebalance_freq": self.rebalance_freq,
                "top_n": self.top_n,
                "initial_capital": self.initial_capital,
                "apply_costs": self.apply_costs,
            }
        )


@dataclass
class BacktestResult:
    run_id: str
    nav: pd.Series
    gross_nav: pd.Series
    positions: pd.DataFrame
    turnover: pd.Series
    costs: pd.Series
    metrics: Metrics
    warnings: list[str] = field(default_factory=list)


def rebalance_dates(start: dt.date, end: dt.date, freq: str = "ME") -> list[dt.date]:
    return [d.date() for d in pd.date_range(start=start, end=end, freq=freq)]


class BacktestEngine:
    def __init__(self, db: Database, factor: Factor, config: BacktestConfig):
        self.db = db
        self.factor = factor
        self.config = config

    def run(self) -> BacktestResult:
        cfg = self.config
        run_id = str(uuid.uuid4())[:8]
        warnings: list[str] = []

        if not self.db.membership_is_survivorship_safe(cfg.index_name, cfg.start, cfg.end):
            msg = (
                f"SURVIVORSHIP UNSAFE: no verified historical membership for "
                f"{cfg.index_name} over {cfg.start}..{cfg.end}. The universe is a "
                f"current-constituents snapshot, so companies that collapsed out of "
                f"the index are missing and returns are biased upward. "
                f"Treat absolute performance as unusable; relative factor "
                f"comparisons remain informative."
            )
            log.warning(msg)
            warnings.append(msg)

        dates = rebalance_dates(cfg.start, cfg.end, cfg.rebalance_freq)
        if len(dates) < 2:
            raise ValueError(f"Need >= 2 rebalance dates, got {len(dates)}")

        sectors = self.db.query("SELECT isin, sector FROM instruments").set_index("isin")[
            "sector"
        ]

        nav, gross_nav = cfg.initial_capital, cfg.initial_capital
        prev_weights = pd.Series(dtype=float)

        nav_rows, pos_rows, turn_rows, cost_rows = [], [], [], []
        scored_fraction: list[float] = []
        skipped = 0

        for i in range(len(dates) - 1):
            t, t_next = dates[i], dates[i + 1]

            universe = self.db.as_of_universe(cfg.index_name, t)
            if len(universe) < cfg.min_universe_size:
                skipped += 1
                continue

            raw = self.factor.compute(self.db, universe, t)
            # A composite arrives already sector-relative and with its red-flag
            # overlay applied. Z-scoring it again would re-centre each sector on
            # zero — discarding the cross-sector comparability the composite
            # exists to establish — and would resurrect names the overlay had
            # removed by turning their NaN back into a rank.
            scores = sector_zscore(raw, sectors) if self.factor.needs_sector_zscore else raw
            scores = scores.dropna()
            scored_fraction.append(len(scores) / len(universe))
            if len(scores) < cfg.min_universe_size:
                skipped += 1
                continue

            # Pick the best `top_n` by the factor's own definition of "best".
            # nlargest/nsmallest state the intent directly; the earlier
            # sort-then-slice version silently selected the *worst* names
            # (descending sort followed by .tail()), which is a sign error that
            # a backtest reports as "the factor doesn't work" rather than as a
            # crash.
            n = min(cfg.top_n, len(scores))
            selected = (
                scores.nlargest(n) if self.factor.higher_is_better else scores.nsmallest(n)
            )

            weights = pd.Series(1.0 / len(selected), index=selected.index)

            # --- the only future-facing call in the loop ---
            fwd = self.db.forward_returns(list(weights.index), t, t_next)
            if cfg.return_transform is not None:
                fwd = cfg.return_transform(fwd, t)

            fwd = fwd.reindex(weights.index)
            # A name with no price data in the window is held flat rather than
            # dropped — dropping it silently removes exactly the failures we care
            # about (suspensions, delistings).
            missing = int(fwd.isna().sum())
            if missing:
                log.debug("%s: %d/%d positions had no price data", t, missing, len(fwd))
            fwd = fwd.fillna(0.0)

            period_return = float((weights * fwd).sum())

            costs_value = 0.0
            if cfg.apply_costs:
                participation = self._participation(list(weights.index), t, nav, weights)
                tc = costs_from_weight_change(
                    prev_weights, weights, nav, participation, cfg.cost_model
                )
                costs_value = tc.total
                turn_rows.append({"date": t, "turnover": tc.turnover / nav if nav else 0.0})

            gross_nav *= 1 + period_return
            nav = (nav - costs_value) * (1 + period_return)

            nav_rows.append({"date": t_next, "nav": nav, "gross_nav": gross_nav,
                             "costs_paid": costs_value})
            cost_rows.append({"date": t, "cost": costs_value})
            for isin, w in weights.items():
                pos_rows.append({"date": t, "isin": isin, "weight": float(w)})

            prev_weights = weights

        if skipped:
            msg = f"{skipped}/{len(dates) - 1} rebalances skipped for insufficient data"
            log.warning(msg)
            warnings.append(msg)

        # A run that scored a third of the universe is not a weaker version of a
        # run that scored all of it — it selected from a different, unstated
        # universe, one filtered by whichever companies happened to have data.
        # That filter is very unlikely to be independent of returns.
        if scored_fraction:
            mean_scored = float(np.mean(scored_fraction))
            if mean_scored < 0.8:
                msg = (
                    f"PARTIAL COVERAGE: only {mean_scored:.0%} of the universe was "
                    f"scorable on an average rebalance. Selection was made from that "
                    f"subset, not from the index, and the subset is chosen by data "
                    f"availability rather than at random."
                )
                log.warning(msg)
                warnings.append(msg)

        if not nav_rows:
            raise ValueError(
                "No rebalance produced a portfolio. Check that prices are ingested "
                "and the universe has members over the backtest window."
            )

        nav_df = pd.DataFrame(nav_rows).set_index("date")
        nav_series = nav_df["nav"]
        gross_series = nav_df["gross_nav"]
        turnover_series = (
            pd.DataFrame(turn_rows).set_index("date")["turnover"]
            if turn_rows else pd.Series(dtype=float)
        )
        cost_series = pd.DataFrame(cost_rows).set_index("date")["cost"]

        ppy = _periods_per_year(cfg.rebalance_freq)
        metrics = compute_metrics(
            nav_series,
            periods_per_year=ppy,
            turnover=turnover_series,
            total_costs=float(cost_series.sum()) / cfg.initial_capital,
        )

        self._persist(run_id, cfg, nav_df, pos_rows, metrics, warnings)

        return BacktestResult(
            run_id=run_id,
            nav=nav_series,
            gross_nav=gross_series,
            positions=pd.DataFrame(pos_rows),
            turnover=turnover_series,
            costs=cost_series,
            metrics=metrics,
            warnings=warnings,
        )

    def _participation(
        self, isins: list[str], t: dt.date, nav: float, weights: pd.Series
    ) -> pd.Series:
        """Trade value as a fraction of each name's median daily traded value."""
        px = self.db.as_of_prices(isins, t, lookback_days=90)
        if px.empty or "traded_value" not in px.columns:
            return pd.Series(dtype=float)
        median_tv = px.groupby("isin")["traded_value"].median()
        trade_value = weights * nav
        return (trade_value / median_tv.reindex(weights.index)).replace(
            [np.inf, -np.inf], np.nan
        )

    def _persist(
        self,
        run_id: str,
        cfg: BacktestConfig,
        nav_df: pd.DataFrame,
        pos_rows: list[dict],
        metrics: Metrics,
        warnings: list[str],
    ) -> None:
        self.db.upsert_df(
            "backtest_runs",
            pd.DataFrame([{
                "run_id": run_id,
                "config_json": cfg.to_json(),
                "started_at": dt.datetime.now(),
                "finished_at": dt.datetime.now(),
                "metrics_json": json.dumps(metrics.to_dict()),
                "warnings": " | ".join(warnings),
            }]),
            ["run_id"],
        )
        nav_out = nav_df.reset_index()
        nav_out["run_id"] = run_id
        self.db.upsert_df(
            "backtest_nav",
            nav_out[["run_id", "date", "nav", "gross_nav", "costs_paid"]],
            ["run_id", "date"],
        )
        if pos_rows:
            pos = pd.DataFrame(pos_rows).rename(columns={"date": "as_of_date"})
            pos["run_id"] = run_id
            self.db.upsert_df(
                "backtest_positions",
                pos[["run_id", "as_of_date", "isin", "weight"]],
                ["run_id", "as_of_date", "isin"],
            )


def _periods_per_year(freq: str) -> int:
    return {"ME": 12, "M": 12, "QE": 4, "Q": 4, "W": 52, "YE": 1, "A": 1}.get(freq, 12)
