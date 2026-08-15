"""Indian equity transaction costs (delivery segment).

Charges differ by side — STT hits both legs, stamp duty only the buy — so buys
and sells are priced separately rather than through a single blended rate.

Slippage scales with participation: taking 5% of a stock's median daily traded
value costs more than taking 0.1% of it. A backtest that equal-weights 20
mid-caps and assumes 5bps slippage on all of them is quietly assuming infinite
liquidity in the smallest names.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from stockanalysis.config import CostModel, cost_model


@dataclass
class TradeCosts:
    buy_value: float
    sell_value: float
    statutory: float
    brokerage_and_gst: float
    slippage: float

    @property
    def total(self) -> float:
        return self.statutory + self.brokerage_and_gst + self.slippage

    @property
    def turnover(self) -> float:
        return self.buy_value + self.sell_value


def compute_costs(
    buy_value: float,
    sell_value: float,
    n_buy_orders: int = 0,
    n_sell_orders: int = 0,
    participation: pd.Series | None = None,
    model: CostModel | None = None,
) -> TradeCosts:
    """Cost of a rebalance.

    `participation` is per-trade traded-value / median-daily-traded-value; its
    mean drives the slippage multiplier.
    """
    m = model or cost_model

    # Statutory: STT, stamp duty. Not subject to GST.
    statutory = buy_value * (m.stt_buy + m.stamp_duty_buy) + sell_value * m.stt_sell

    # Exchange + SEBI fees, brokerage — these attract GST.
    turnover = buy_value + sell_value
    exch_and_sebi = turnover * (m.exchange_txn + m.sebi_turnover)
    brokerage = (
        turnover * m.brokerage_pct
        + (n_buy_orders + n_sell_orders) * m.brokerage_flat_per_order
    )
    brokerage_and_gst = (exch_and_sebi + brokerage) * (1 + m.gst)

    slippage_bps = m.base_slippage_bps
    if participation is not None and len(participation.dropna()):
        avg_participation = float(participation.dropna().mean())
        if avg_participation > m.participation_penalty_threshold:
            multiplier = 1.0 + (avg_participation / m.participation_penalty_threshold)
            slippage_bps *= min(multiplier, 10.0)  # cap the pathological case
    slippage = turnover * slippage_bps / 10_000.0

    return TradeCosts(
        buy_value=buy_value,
        sell_value=sell_value,
        statutory=statutory,
        brokerage_and_gst=brokerage_and_gst,
        slippage=slippage,
    )


def costs_from_weight_change(
    old_weights: pd.Series,
    new_weights: pd.Series,
    portfolio_value: float,
    participation: pd.Series | None = None,
    model: CostModel | None = None,
) -> TradeCosts:
    """Translate a weight delta into a costed trade list."""
    all_isins = old_weights.index.union(new_weights.index)
    old = old_weights.reindex(all_isins).fillna(0.0)
    new = new_weights.reindex(all_isins).fillna(0.0)
    delta = new - old

    buys = delta[delta > 0]
    sells = delta[delta < 0]

    return compute_costs(
        buy_value=float(buys.sum()) * portfolio_value,
        sell_value=float(np.abs(sells.sum())) * portfolio_value,
        n_buy_orders=int(len(buys)),
        n_sell_orders=int(len(sells)),
        participation=participation,
        model=model,
    )
