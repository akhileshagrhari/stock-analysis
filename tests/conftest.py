"""Synthetic fixtures.

Phase-0 tests run entirely on generated data. That is deliberate: correctness
properties (no lookahead, no survivorship bias, no alpha from noise) must hold
for *any* input, and generated data lets us construct the exact adversarial
cases — a company that delists mid-backtest, a filing published months after the
period it describes — that real data only supplies by accident.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from stockanalysis.db.database import Database

SECTORS = ["Financials", "IT", "Energy", "FMCG", "Pharma", "Auto"]


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    yield database
    database.close()


def make_instruments(n: int = 30, delisted: dict[int, dt.date] | None = None) -> pd.DataFrame:
    delisted = delisted or {}
    return pd.DataFrame(
        [
            {
                "isin": f"INE{i:09d}",
                "nse_symbol": f"TEST{i:03d}",
                "bse_code": None,
                "name": f"Test Company {i}",
                "sector": SECTORS[i % len(SECTORS)],
                "industry": SECTORS[i % len(SECTORS)],
                "listing_date": dt.date(2015, 1, 1),
                "delisting_date": delisted.get(i),
                "is_active": i not in delisted,
            }
            for i in range(n)
        ]
    )


def make_prices(
    isins: list[str],
    start: dt.date,
    end: dt.date,
    seed: int = 42,
    momentum_strength: float = 0.0,
    delisting: dict[str, dt.date] | None = None,
) -> pd.DataFrame:
    """Generate daily price series.

    `momentum_strength` > 0 embeds genuine, persistent cross-sectional drift
    differences — the thing a momentum factor is supposed to find. That lets the
    control tests show a *difference* rather than two indistinguishable nulls.

    VOLATILITY IS DELIBERATELY UNIFORM ACROSS INSTRUMENTS. An earlier version
    drew per-stock vol from U(1.2%, 2.8%) and produced data with *anti*-momentum:
    selecting on top realized return disproportionately picks high-vol names,
    and those carry a larger geometric drag (-sigma^2/2), so the selection lost
    money regardless of drift. That is a real effect worth knowing about, but it
    made this fixture useless for testing whether the harness can detect signal.
    Vol dispersion belongs in a dedicated low-volatility-factor fixture instead.
    """
    rng = np.random.default_rng(seed)
    delisting = delisting or {}
    dates = pd.bdate_range(start, end)
    rows = []

    vol = 0.015  # ~24% annualised, uniform — see docstring

    for k, isin in enumerate(isins):
        # Persistent per-instrument drift, centred on zero so this is a pure
        # cross-sectional bet rather than a rising-tide effect.
        drift = rng.normal(0.0, 0.0008) * momentum_strength
        px = 100.0 * rng.uniform(0.5, 3.0)
        stop = delisting.get(isin)

        for d in dates:
            day = d.date()
            if stop is not None and day >= stop:
                break
            shock = rng.normal(drift, vol)
            px = max(px * (1 + shock), 1.0)
            volume = int(rng.uniform(50_000, 5_000_000))
            rows.append(
                {
                    "isin": isin,
                    "date": day,
                    "open": px * 0.995,
                    "high": px * 1.01,
                    "low": px * 0.99,
                    "close": px,
                    "adj_close": px,
                    "volume": volume,
                    "traded_value": px * volume,
                }
            )
        _ = k
    return pd.DataFrame(rows)


def make_fundamentals(
    isins: list[str],
    fiscal_years: list[int],
    overrides: dict[tuple[str, int], dict] | None = None,
    filing_lag_months: int = 6,
    **defaults: float,
) -> pd.DataFrame:
    """Annual fundamentals that satisfy the arithmetic identities by construction.

    Every row is internally consistent — assets equal equity plus liabilities,
    PBT less tax equals PAT — so a test that fails is failing on the factor
    logic rather than on fixture data the validators would have rejected.

    `filing_lag_months` is not decoration. Fundamentals become knowable months
    after the period they describe, and a fixture that files on the period end
    would let a lookahead test pass while the bug it exists to catch is present.
    """
    overrides = overrides or {}
    base = {
        "revenue": 1000.0,
        "other_income": 20.0,
        "total_expenses": 800.0,
        "ebitda": 250.0,
        "depreciation": 50.0,
        "interest_expense": 30.0,
        "profit_before_tax": 200.0,
        "tax_expense": 50.0,
        "pat": 150.0,
        "eps": 15.0,
        "ocf": 180.0,
        "capex": 60.0,
        "fcf": None,
        "total_assets": 2000.0,
        "total_equity": 1200.0,
        "total_liabilities": 800.0,
        "total_debt": 500.0,
        "cash": 100.0,
        "contingent_liabilities": 100.0,
        "auditor_opinion": "UNMODIFIED",
        **defaults,
    }

    rows = []
    for isin in isins:
        for fy in fiscal_years:
            period_end = dt.date(fy, 3, 31)
            row = {
                "isin": isin,
                "fiscal_year": fy,
                "period_end_date": period_end,
                "filing_date": period_end + dt.timedelta(days=30 * filing_lag_months),
                "basis": "CONSOLIDATED",
                "extraction_confidence": 1.0,
                "source_filing_id": f"{isin}-{fy}",
                **base,
                **overrides.get((isin, fy), {}),
            }
            rows.append(row)
    return pd.DataFrame(rows)


def make_shareholding(
    isin: str, quarter_ends: list[dt.date], promoter_pct: list[float]
) -> pd.DataFrame:
    """Shareholding rows with the LODR-deadline knowledge date applied."""
    return pd.DataFrame(
        [
            {
                "isin": isin,
                "quarter_end": q,
                "disclosed_date": q + dt.timedelta(days=21),
                "disclosed_date_source": "ASSUMED_LODR_DEADLINE",
                "promoter_pct": p,
                "promoter_pledged_pct": None,   # NSE supplies no pledge figure
                "fii_pct": None,
                "dii_pct": None,
                "public_pct": 100.0 - p,
                "employee_trust_pct": None,
            }
            for q, p in zip(quarter_ends, promoter_pct, strict=True)
        ]
    )


def make_membership(
    isins: list[str], index_name: str, from_date: dt.date
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "index_name": index_name,
                "isin": isin,
                "from_date": from_date,
                "to_date": None,
            }
            for isin in isins
        ]
    )


# Cross-sectional drift dispersion of ~20%/yr against ~24%/yr idiosyncratic vol.
# Calibrated so 12-1 momentum earns a Sharpe around 0.5 — modest, and in the
# range the published literature reports. An earlier value of 3.0 gave a drift
# dispersion 2.5x the vol and produced a Sharpe of 6.5 with no losing month in
# 46, which made the "implausible Sharpe" guard meaningless.
DEFAULT_MOMENTUM_STRENGTH = 1.0


@pytest.fixture
def seeded_db(db: Database) -> Database:
    """30 instruments, 5 years of prices with real momentum, verified membership."""
    start, end = dt.date(2019, 1, 1), dt.date(2024, 1, 1)

    instruments = make_instruments(30)
    db.upsert_df("instruments", instruments, ["isin"])

    isins = instruments["isin"].tolist()
    prices = make_prices(
        isins, start, end, momentum_strength=DEFAULT_MOMENTUM_STRENGTH
    )
    db.upsert_df("prices_daily", prices, ["isin", "date"])

    db.upsert_df(
        "index_membership",
        make_membership(isins, "TESTIDX", start),
        ["index_name", "isin", "from_date"],
    )
    db.upsert_df(
        "index_membership_coverage",
        pd.DataFrame(
            [
                {
                    "index_name": "TESTIDX",
                    "verified_from": start,
                    "verified_to": end,
                    "source": "SYNTHETIC",
                    "loaded_at": dt.datetime.now(),
                }
            ]
        ),
        ["index_name", "verified_from"],
    )
    return db
