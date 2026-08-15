"""Phase 2 — the factor families, the overlay, and the composite.

The tests worth having here are not "does ROE divide correctly". They are the
ones covering the places where a wrong answer looks like a right one: a missing
value scored as average, a lower-is-better factor added with the wrong sign, a
family weight that is not the weight that was applied, a red flag that reads as
clear because nothing supplies the data. Each of those produces a plausible
number and no exception.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from stockanalysis.db.database import Database
from stockanalysis.factors import redflags
from stockanalysis.factors.base import Factor, sector_zscore
from stockanalysis.factors.composite import (
    CompositeModel,
    ScoringConfig,
    default_factors,
    intra_family_weights,
    persist,
)
from stockanalysis.factors.growth import QuarterlyRevenueYoY, RevenueCagr
from stockanalysis.factors.panel import PanelCache, load_panel
from stockanalysis.factors.quality import (
    Accruals,
    CfoToPat,
    DebtToEquity,
    InterestCoverage,
    Roe,
)
from stockanalysis.factors.redflags import FlagState
from stockanalysis.factors.value import BookToPrice, EarningsYield, FcfYield
from tests.conftest import (
    make_fundamentals,
    make_instruments,
    make_membership,
    make_prices,
    make_shareholding,
)

AS_OF = dt.date(2024, 6, 28)


@pytest.fixture
def universe(db: Database) -> list[str]:
    """20 instruments across sectors, 3 years of prices, membership seeded."""
    instruments = make_instruments(20)
    db.upsert_df("instruments", instruments, ["isin"])
    isins = instruments["isin"].tolist()

    db.upsert_df(
        "prices_daily",
        make_prices(isins, dt.date(2021, 1, 1), AS_OF, seed=7),
        ["isin", "date"],
    )
    db.upsert_df(
        "index_membership",
        make_membership(isins, "TESTIDX", dt.date(2021, 1, 1)),
        ["index_name", "isin", "from_date"],
    )
    return isins


def load_fundamentals(
    db: Database, isins: list[str], overrides: dict | None = None
) -> None:
    """Three years of fundamentals with cross-sectional and time dispersion.

    Both kinds of spread are needed. Without dispersion *across* companies every
    z-score is zero and a ranking test cannot fail; without dispersion *across
    years* every growth rate is exactly zero, which makes PEG undefined and the
    growth family degenerate — a fixture that quietly tests less than it appears
    to.
    """
    dispersed: dict[tuple[str, int], dict] = {}
    for k, isin in enumerate(isins):
        scale = 0.6 + 0.05 * k          # profitability spread across the universe
        for offset, fy in enumerate([2021, 2022, 2023]):
            growth = 1.08 ** offset     # 8% a year, so CAGRs are positive
            dispersed[(isin, fy)] = {
                "revenue": 1000.0 * scale * growth,
                "pat": 150.0 * scale * growth,
                "eps": 15.0 * growth,
                "ocf": 180.0 * scale * growth,
                "total_equity": 1200.0 * scale,
                "total_assets": 2000.0 * scale,
                "total_debt": 500.0 * scale,
            }
    for key, values in (overrides or {}).items():
        dispersed.setdefault(key, {}).update(values)

    db.upsert_df(
        "fundamentals_annual",
        make_fundamentals(isins, [2021, 2022, 2023], overrides=dispersed),
        ["isin", "fiscal_year", "basis"],
    )


def panel_for(db: Database, isins: list[str], as_of: dt.date = AS_OF):
    return load_panel(db, isins, as_of)


# ======================================================================
# The bug that made everything else look fine
# ======================================================================


def test_a_factor_with_no_data_does_not_score_as_average():
    """An absent value must stay absent through sector z-scoring.

    This is the failure that hid an empty fundamentals table behind a reported
    92% data coverage. When a sector has no usable spread, the fallback score of
    0.0 belongs only to companies that actually have a value; handing it to the
    ones that do not turns "we could not measure this" into "this is average",
    which is a claim, and the composite then weights it as though it were
    measured.
    """
    values = pd.Series({f"INE{i:09d}": np.nan for i in range(10)})
    sectors = pd.Series({f"INE{i:09d}": "IT" for i in range(10)})

    z = sector_zscore(values, sectors)

    assert z.isna().all(), "a factor with no data anywhere scored as neutral"


def test_present_values_still_score_zero_in_a_degenerate_sector():
    """The other half of the same rule: identical values are average, not missing.

    Guards the fix from over-correcting. A sector where every company reports the
    same number has no spread, and each of those companies genuinely is at its
    sector's middle — dropping them to NaN would delete a whole sector from the
    model rather than score it.
    """
    values = pd.Series({f"INE{i:09d}": 5.0 for i in range(10)})
    values["INE000000009"] = np.nan
    sectors = pd.Series({f"INE{i:09d}": "IT" for i in range(10)})

    z = sector_zscore(values, sectors)

    assert (z.dropna() == 0.0).all()
    assert pd.isna(z["INE000000009"])
    assert z.notna().sum() == 9


# ======================================================================
# Composite mechanics
# ======================================================================


def test_lower_is_better_factors_are_flipped_before_aggregation(
    db: Database, universe: list[str]
):
    """A composite sums z-scores, so direction cannot be deferred to selection.

    The single-factor engine handles direction with nlargest/nsmallest. A
    weighted sum has no such opportunity: if debt/equity is not negated here,
    the quality family adds leverage to profitability and the arithmetic gives
    no sign that anything is wrong.
    """
    heavy, light = universe[0], universe[1]
    load_fundamentals(
        db, universe,
        overrides={
            (heavy, fy): {"total_debt": 5000.0} for fy in (2021, 2022, 2023)
        } | {
            (light, fy): {"total_debt": 10.0} for fy in (2021, 2022, 2023)
        },
    )

    model = CompositeModel(factors=[DebtToEquity()], cache=PanelCache())
    result = model.score(db, universe, AS_OF)

    assert result.factor_z.loc[light, "debt_to_equity"] > (
        result.factor_z.loc[heavy, "debt_to_equity"]
    ), "less indebted company did not score higher on a lower-is-better factor"


def test_declared_family_weights_are_the_weights_applied(
    db: Database, universe: list[str]
):
    """Family scores are re-standardised so 30% quality means 30%.

    Averaging six correlated z-scores yields something whose variance depends on
    how correlated they happen to be, not on the weight assigned to it. Without
    re-standardisation the quality family — six intercorrelated accounting
    ratios — contributes with a different spread than a single-factor family,
    and the declared split is not the applied split.
    """
    load_fundamentals(db, universe)
    result = CompositeModel(cache=PanelCache()).score(db, universe, AS_OF)

    for family in result.family_z.columns:
        column = result.family_z[family].dropna()
        if len(column) < 5:
            continue
        assert abs(column.std() - 1.0) < 0.01, (
            f"{family} entered the weighted sum with std {column.std():.3f}, "
            f"so its effective weight is not the declared one"
        )


def test_a_missing_family_renormalises_rather_than_penalising(
    db: Database, universe: list[str]
):
    """No news must not be bad news.

    Sentiment is absent for the whole universe until phase 3. If the missing
    family dragged the weighted sum toward zero, every company would be marked
    down for a factor none of them was measured on — and the ranking would still
    look sensible, because the penalty is uniform.
    """
    load_fundamentals(db, universe)
    cache = PanelCache()

    full = CompositeModel(cache=cache).score(db, universe, AS_OF)
    without_sentiment = CompositeModel(
        factors=[f for f in default_factors() if f.family != "sentiment"],
        cache=cache,
    ).score(db, universe, AS_OF)

    common = full.composite_z.dropna().index.intersection(
        without_sentiment.composite_z.dropna().index
    )
    assert len(common) > 5
    pd.testing.assert_series_equal(
        full.composite_z[common], without_sentiment.composite_z[common],
        check_names=False, atol=1e-9,
    )


def test_insufficient_coverage_withdraws_the_score(db: Database, universe: list[str]):
    """A company scored on 15% of the model gets no score, not a weak one.

    Renormalisation over available families is right up to a point; past it the
    composite silently becomes whichever factor happened to have data. With no
    fundamentals loaded, only momentum is computable — 15% of the model — and
    the default threshold must refuse to publish a number for that.
    """
    momentum_only = CompositeModel(cache=PanelCache())        # no fundamentals loaded
    strict = momentum_only.score(db, universe, AS_OF)
    assert strict.score.isna().all()
    assert strict.signal.isna().all(), "an unscored company was assigned a signal"
    assert (strict.coverage <= 0.16).all()

    relaxed = CompositeModel(
        config=ScoringConfig(min_coverage=0.10), cache=PanelCache()
    ).score(db, universe, AS_OF)
    assert relaxed.score.notna().sum() > 5, (
        "lowering the threshold deliberately should produce scores"
    )


def test_coverage_reports_the_fraction_of_the_model_actually_measured(
    db: Database, universe: list[str]
):
    """Coverage is weight-weighted, not a count of factors.

    The fixture loads annual reports but no quarterly results and no news, so
    the expected figure is not "four families out of five" — it is the value,
    quality and momentum families in full, three fifths of growth, and none of
    sentiment. A coverage number that counted families would report 80% and hide
    the two missing growth factors entirely.
    """
    load_fundamentals(db, universe)
    result = CompositeModel(cache=PanelCache()).score(db, universe, AS_OF)

    expected = 0.25 + 0.30 + 0.20 * (3 / 5) + 0.15 + 0.0
    assert result.coverage.median() == pytest.approx(expected)
    assert result.coverage.max() <= 1.0


def test_scores_span_the_signal_thresholds(db: Database, universe: list[str]):
    """The 0-100 mapping must actually reach DESIGN §6.3's 75 and 45.

    A weighted sum of unit-variance series has a standard deviation well below
    1, so passing it to a normal CDF without re-standardising compresses the
    universe into roughly 30-70 and no company is ever eligible for BUY. The
    thresholds would then be unreachable rather than strict, which is not a
    failure any assertion on the mean would catch.
    """
    load_fundamentals(db, universe)
    result = CompositeModel(cache=PanelCache()).score(db, universe, AS_OF)

    scores = result.score.dropna()
    assert scores.max() > 75.0
    assert scores.min() < 45.0
    assert set(result.signal.dropna()) <= {"BUY", "HOLD", "SELL"}


def test_intra_family_weights_favour_the_india_specific_checks():
    """DESIGN §6.1 gives CFO/PAT and accruals disproportionate weight."""
    weights = intra_family_weights(default_factors())
    quality = {"roe", "roce", "debt_to_equity", "interest_coverage",
               "cfo_to_pat", "accruals"}

    assert sum(weights[k] for k in quality) == pytest.approx(1.0)
    assert weights["cfo_to_pat"] > weights["roe"]
    assert weights["cfo_to_pat"] + weights["accruals"] > 0.35


# ======================================================================
# Red-flag overlay
# ======================================================================


def test_an_unavailable_flag_is_unknown_not_clear(db: Database, universe: list[str]):
    """Promoter pledge and credit ratings have no source. Neither may read CLEAR.

    This is the flag DESIGN calls the most informative in the Indian mid-cap
    universe, and `NSE.shareholding()` carries no pledged-shares figure. A
    boolean overlay would report False — indistinguishable from a company with a
    verified zero pledge — and hand a clean bill of health to exactly the
    companies the flag exists to catch.
    """
    load_fundamentals(db, universe)
    db.upsert_df(
        "shareholding",
        make_shareholding(
            universe[0],
            [dt.date(2023, 12, 31), dt.date(2023, 9, 30)],
            [55.0, 55.0],
        ),
        ["isin", "quarter_end"],
    )

    flags = redflags.evaluate(panel_for(db, universe))

    assert (flags["promoter_pledge"] == FlagState.UNKNOWN.value).all()
    assert (flags["rating_downgrade"] == FlagState.UNKNOWN.value).all()
    assert set(redflags.unreachable_flags()) == {"promoter_pledge", "rating_downgrade"}


def test_unknown_flags_travel_with_the_signal(db: Database, universe: list[str]):
    """A BUY must carry the list of flags that could not be checked."""
    load_fundamentals(db, universe)
    result = CompositeModel(cache=PanelCache()).score(db, universe, AS_OF)

    buys = result.table().query("signal == 'BUY'")
    assert not buys.empty
    assert buys["unknown_flags"].str.contains("promoter_pledge").all()


def test_a_red_flag_removes_a_top_ranked_name_from_selection(
    db: Database, universe: list[str]
):
    """DESIGN §6.2: the overlay caps, it does not adjust.

    A reduced score still wins if everything else is worse, so a capped name has
    to become unselectable rather than merely worse. The company here is
    engineered to rank first on every factor.
    """
    star = universe[0]
    load_fundamentals(
        db, universe,
        overrides={
            (star, fy): {
                "pat": 900.0, "eps": 90.0, "ocf": 1100.0, "revenue": 3000.0,
                "total_debt": 5.0, "auditor_opinion": "QUALIFIED",
            }
            for fy in (2021, 2022, 2023)
        },
    )

    cache = PanelCache()
    unguarded = CompositeModel(
        config=ScoringConfig(apply_red_flags=False), cache=cache
    ).score(db, universe, AS_OF)
    guarded = CompositeModel(
        config=ScoringConfig(apply_red_flags=True), cache=cache
    ).score(db, universe, AS_OF)

    assert unguarded.score.idxmax() == star, "fixture did not rank the flagged name top"
    assert unguarded.signal[star] == "BUY"

    assert pd.isna(guarded.score[star]), "a flagged company was still selectable"
    assert guarded.signal[star] == "SELL"
    assert "auditor_qualification" in guarded.flag_summary.loc[star, "tripped"]

    # The evidence survives: the factors liked it, and the overlay is why it is
    # a SELL. Reporting a bare NaN would delete the more interesting half.
    assert guarded.factor_score[star] == pytest.approx(unguarded.factor_score[star])
    assert guarded.table().loc[star, "score"] > 75.0


def test_promoter_selling_needs_four_quarters_to_see_three_declines(
    db: Database, universe: list[str]
):
    """Three observations show two declines. That is not the flag's condition."""
    isin = universe[0]
    quarters = [dt.date(2023, 12, 31), dt.date(2023, 9, 30), dt.date(2023, 6, 30)]

    db.upsert_df(
        "shareholding", make_shareholding(isin, quarters, [50.0, 52.0, 54.0]),
        ["isin", "quarter_end"],
    )
    flags = redflags.evaluate(panel_for(db, universe))
    assert flags.loc[isin, "promoter_selling"] == FlagState.UNKNOWN.value

    db.upsert_df(
        "shareholding",
        make_shareholding(isin, [*quarters, dt.date(2023, 3, 31)],
                          [50.0, 52.0, 54.0, 56.0]),
        ["isin", "quarter_end"],
    )
    flags = redflags.evaluate(panel_for(db, universe))
    assert flags.loc[isin, "promoter_selling"] == FlagState.TRIPPED.value


def test_flat_promoter_holding_is_not_selling(db: Database, universe: list[str]):
    """Strict inequality — a holding unchanged to two decimals is not a sale."""
    isin = universe[0]
    quarters = [
        dt.date(2023, 12, 31), dt.date(2023, 9, 30),
        dt.date(2023, 6, 30), dt.date(2023, 3, 31),
    ]
    db.upsert_df(
        "shareholding", make_shareholding(isin, quarters, [50.0, 50.0, 50.0, 50.0]),
        ["isin", "quarter_end"],
    )

    flags = redflags.evaluate(panel_for(db, universe))
    assert flags.loc[isin, "promoter_selling"] == FlagState.CLEAR.value


def test_weak_cash_conversion_needs_three_consecutive_computable_years(
    db: Database, universe: list[str]
):
    """A missing cash flow statement is not evidence of a healthy pattern."""
    weak, gappy = universe[0], universe[1]
    load_fundamentals(
        db, universe,
        overrides={(weak, fy): {"ocf": 40.0} for fy in (2021, 2022, 2023)}
        | {(gappy, 2022): {"ocf": None}, (gappy, 2021): {"ocf": 40.0},
           (gappy, 2023): {"ocf": 40.0}},
    )

    flags = redflags.evaluate(panel_for(db, universe))

    assert flags.loc[weak, "weak_cash_conversion"] == FlagState.TRIPPED.value
    assert flags.loc[gappy, "weak_cash_conversion"] == FlagState.UNKNOWN.value
    assert flags.loc[universe[2], "weak_cash_conversion"] == FlagState.CLEAR.value


def test_contingent_liabilities_trip_against_negative_net_worth(
    db: Database, universe: list[str]
):
    """Negative equity plus any contingent liability is a trip, not a NaN.

    The ratio test alone gets this backwards: contingent > 0.5 x a negative
    number is false for every positive contingent liability, so a company with
    no equity to absorb its exposures would pass.
    """
    insolvent = universe[0]
    load_fundamentals(
        db, universe,
        overrides={
            (insolvent, fy): {"total_equity": -200.0, "contingent_liabilities": 50.0}
            for fy in (2021, 2022, 2023)
        },
    )

    flags = redflags.evaluate(panel_for(db, universe))
    assert flags.loc[insolvent, "contingent_liabilities"] == FlagState.TRIPPED.value


# ======================================================================
# Individual factor semantics
# ======================================================================


def test_market_cap_is_implied_from_the_filing_not_from_today(
    db: Database, universe: list[str]
):
    """Shares outstanding come from PAT/EPS, so they carry the filing's own date.

    Any external share count is a current figure applied to a historical balance
    sheet — anachronistic, and wrong on the arithmetic wherever a buyback or
    issuance happened in between.
    """
    isin = universe[0]
    load_fundamentals(
        db, universe,
        overrides={(isin, 2023): {"pat": 150.0, "eps": 15.0}},
    )
    panel = panel_for(db, universe)

    # PAT 150 crore / EPS 15 per share = 10 crore shares.
    assert panel.shares[isin] == pytest.approx(1e8, rel=1e-6)
    expected_cap = panel.price[isin] * 1e8 / 1e7
    assert panel.market_cap[isin] == pytest.approx(expected_cap, rel=1e-6)


def test_loss_making_companies_leave_the_value_factors_rather_than_rank_cheap(
    db: Database, universe: list[str]
):
    """A P/E of -8 is not cheap, and a negative book-to-price is not a bargain."""
    loss_maker = universe[0]
    load_fundamentals(
        db, universe,
        overrides={
            (loss_maker, fy): {"pat": -200.0, "eps": -20.0, "profit_before_tax": -150.0}
            for fy in (2021, 2022, 2023)
        },
    )
    panel = panel_for(db, universe)

    assert pd.isna(panel.market_cap[loss_maker])
    assert pd.isna(BookToPrice().from_panel(panel)[loss_maker])
    # Earnings yield needs no share count, so it survives — and correctly
    # reports the company as the worst in the universe rather than dropping it.
    ey = EarningsYield().from_panel(panel)
    assert ey[loss_maker] < 0
    assert ey[loss_maker] == ey.min()


def test_roe_refuses_a_negative_denominator(db: Database, universe: list[str]):
    """Loss plus negative net worth divides to a *positive* ROE.

    Left alone, the most distressed company in the universe ranks at the top of
    the quality family on the strength of being nearly insolvent.
    """
    distressed = universe[0]
    load_fundamentals(
        db, universe,
        overrides={
            (distressed, fy): {"pat": -300.0, "eps": -30.0, "total_equity": -100.0}
            for fy in (2021, 2022, 2023)
        },
    )

    roe = Roe().from_panel(panel_for(db, universe))
    assert pd.isna(roe[distressed])


def test_a_debt_free_company_tops_interest_coverage_rather_than_dropping_out(
    db: Database, universe: list[str]
):
    """Zero finance costs is the best possible coverage, not an undefined one."""
    debt_free = universe[0]
    load_fundamentals(
        db, universe,
        overrides={
            (debt_free, fy): {"interest_expense": 0.0, "total_debt": 0.0}
            for fy in (2021, 2022, 2023)
        },
    )

    coverage = InterestCoverage().from_panel(panel_for(db, universe))
    assert coverage[debt_free] == coverage.max()
    assert np.isfinite(coverage[debt_free])


def test_cfo_to_pat_does_not_reward_a_loss_making_company(
    db: Database, universe: list[str]
):
    """Negative OCF over negative PAT comes out positive and large."""
    burning = universe[0]
    load_fundamentals(
        db, universe,
        overrides={
            (burning, fy): {"pat": -100.0, "eps": -10.0, "ocf": -200.0}
            for fy in (2021, 2022, 2023)
        },
    )
    panel = panel_for(db, universe)

    assert pd.isna(CfoToPat().from_panel(panel)[burning])
    # Accruals is scaled by assets rather than by PAT, so it still reads —
    # which is why both are in the family.
    assert np.isfinite(Accruals().from_panel(panel)[burning])


def test_fcf_falls_back_to_ocf_minus_capex_with_the_right_sign(
    db: Database, universe: list[str]
):
    """Capex is extracted as a positive magnitude, so it must be subtracted.

    Adding it would rank the most capital-hungry companies as the strongest cash
    generators.
    """
    isin = universe[0]
    load_fundamentals(
        db, universe,
        overrides={(isin, 2023): {"ocf": 180.0, "capex": 60.0, "fcf": None}},
    )
    panel = panel_for(db, universe)

    expected = (180.0 - 60.0) / panel.market_cap[isin]
    assert FcfYield().from_panel(panel)[isin] == pytest.approx(expected)


def test_cagr_refuses_a_negative_base(db: Database, universe: list[str]):
    """Growing from -100 to -50 is not -50% growth."""
    recovering = universe[0]
    load_fundamentals(
        db, universe,
        overrides={
            (recovering, 2021): {"revenue": -100.0},
            (recovering, 2023): {"revenue": 500.0},
        },
    )

    cagr = RevenueCagr().from_panel(panel_for(db, universe))
    assert pd.isna(cagr[recovering])
    # The rest of the universe grows 8% a year in the fixture, and the CAGR is
    # annualised off elapsed days rather than a row count.
    assert cagr[universe[1]] == pytest.approx(0.08, abs=1e-3)


def test_quarterly_growth_compares_the_same_quarter_a_year_earlier(
    db: Database, universe: list[str]
):
    """Sequential comparison measures Indian seasonality, not the company."""
    isin = universe[0]
    rows = [
        # period end, revenue — a strong March quarter and a weak December one
        (dt.date(2024, 3, 31), 130.0),
        (dt.date(2023, 12, 31), 80.0),
        (dt.date(2023, 3, 31), 100.0),
    ]
    db.upsert_df(
        "fundamentals_quarterly",
        pd.DataFrame([
            {
                "isin": isin,
                "period_end_date": p,
                "filing_date": p + dt.timedelta(days=45),
                "revenue": r,
                "pat": r * 0.1,
                "eps": 1.0,
                "source": "TEST",
            }
            for p, r in rows
        ]),
        ["isin", "period_end_date"],
    )

    yoy = QuarterlyRevenueYoY().from_panel(panel_for(db, universe))
    # 130 vs the prior March quarter's 100, not vs December's 80.
    assert yoy[isin] == pytest.approx(0.30)


# ======================================================================
# Point-in-time — the phase-2 version of the phase-0 contract
# ======================================================================


def test_the_composite_cannot_see_a_filing_before_it_was_published(
    db: Database, universe: list[str]
):
    """FY2024 figures describe a year ending 31 March and are filed in September.

    Every fundamental factor routes through one panel loader, so this test
    covers all of them at once — which is the reason the panel exists.
    """
    load_fundamentals(db, universe)
    db.upsert_df(
        "fundamentals_annual",
        make_fundamentals(
            universe, [2024],
            overrides={(i, 2024): {"pat": 9999.0, "revenue": 99999.0} for i in universe},
        ),
        ["isin", "fiscal_year", "basis"],
    )

    # 30 June 2024: the year has ended, the report has not been filed.
    before = load_panel(db, universe, dt.date(2024, 6, 30))
    assert (before.latest["fiscal_year"] == 2023).all(), (
        "an unfiled annual report reached the factor layer"
    )

    after = load_panel(db, universe, dt.date(2024, 11, 30))
    assert (after.latest["fiscal_year"] == 2024).all()


def test_injecting_future_fundamentals_does_not_move_todays_score(
    db: Database, universe: list[str]
):
    """The phase-0 leak test, applied to the fundamental families.

    Compute, inject data that becomes knowable later, recompute. Any movement is
    a leak, and this catches it without assuming *how* the leak occurs.
    """
    load_fundamentals(db, universe)
    cache_a, cache_b = PanelCache(), PanelCache()

    before = CompositeModel(cache=cache_a).score(db, universe, AS_OF).score

    db.upsert_df(
        "fundamentals_annual",
        make_fundamentals(
            universe, [2024],
            overrides={
                (i, 2024): {"pat": float(1000 + 100 * k), "eps": float(50 + k)}
                for k, i in enumerate(universe)
            },
        ),
        ["isin", "fiscal_year", "basis"],
    )
    after = CompositeModel(cache=cache_b).score(db, universe, AS_OF).score

    pd.testing.assert_series_equal(before, after, check_names=False)


def test_the_engine_does_not_z_score_the_composite_twice():
    """A pre-scored factor must bypass the engine's sector z-scoring.

    Re-scoring would re-centre every sector on zero, discarding the cross-sector
    comparability the composite establishes, and would resurrect names the red-
    flag overlay removed by turning their NaN back into a rank.
    """
    assert CompositeModel().needs_sector_zscore is False
    assert all(f.needs_sector_zscore for f in default_factors())


# ======================================================================
# Persistence
# ======================================================================


def test_persisted_signals_record_coverage_and_the_model_version(
    db: Database, universe: list[str]
):
    """A stored score that cannot say which model produced it audits nothing."""
    load_fundamentals(db, universe)
    result = CompositeModel(cache=PanelCache()).score(db, universe, AS_OF)
    n_factors, n_signals = persist(db, result)

    assert n_factors > 0 and n_signals > 0

    stored = db.query("SELECT * FROM signals")
    assert stored["model_version"].nunique() == 1
    assert stored["model_version"].iloc[0].startswith("phase2-composite-")
    assert stored["coverage"].notna().all()
    assert stored["unknown_flags"].str.contains("promoter_pledge").all()

    # A different weighting is a different model and must be distinguishable.
    other = CompositeModel(
        config=ScoringConfig(family_weights={"value": 1.0}), cache=PanelCache()
    ).score(db, universe, AS_OF)
    assert other.version != result.version


def test_unscored_companies_are_not_persisted_as_holds(
    db: Database, universe: list[str]
):
    """No signal is not a neutral signal."""
    result = CompositeModel(cache=PanelCache()).score(db, universe, AS_OF)
    _, n_signals = persist(db, result)

    assert n_signals == 0, "companies below the coverage threshold were given signals"


# ======================================================================
# Attribution
# ======================================================================


def test_attribution_reports_a_working_lower_is_better_factor_as_positive(
    seeded_db: Database,
):
    """IC signs are normalised, so positive always means "the factor worked".

    Without this, debt/equity would report a negative IC while doing exactly
    what it is supposed to, and would read as a failing factor.
    """
    from stockanalysis.backtest.attribution import run_attribution
    from stockanalysis.backtest.engine import rebalance_dates
    from stockanalysis.factors.momentum import Momentum12_1

    class InvertedMomentum(Factor):
        """Momentum with the sign flipped and declared lower-is-better."""

        name = "inverted_momentum"
        family = "momentum"
        higher_is_better = False

        def compute(self, db, isins, as_of):
            return -Momentum12_1().compute(db, isins, as_of)

    dates = rebalance_dates(dt.date(2020, 6, 30), dt.date(2023, 12, 31), "ME")
    df = run_attribution(
        seeded_db, "TESTIDX", dates, [Momentum12_1(), InvertedMomentum()]
    )

    plain = df.set_index("factor").loc["momentum_12_1", "mean_ic"]
    inverted = df.set_index("factor").loc["inverted_momentum", "mean_ic"]

    assert plain > 0, "the fixture embeds momentum; IC should be positive"
    assert inverted == pytest.approx(plain, abs=1e-9), (
        "a sign-declared factor was not normalised, so it reads as failing"
    )


def test_attribution_finds_no_signal_where_none_was_embedded(db: Database):
    """Pure random walks. A meaningful IC t-statistic here is a leak."""
    from stockanalysis.backtest.attribution import run_attribution
    from stockanalysis.backtest.engine import rebalance_dates
    from stockanalysis.factors.momentum import Momentum12_1

    instruments = make_instruments(30)
    db.upsert_df("instruments", instruments, ["isin"])
    isins = instruments["isin"].tolist()
    db.upsert_df(
        "prices_daily",
        make_prices(isins, dt.date(2019, 1, 1), dt.date(2024, 1, 1),
                    seed=5, momentum_strength=0.0),
        ["isin", "date"],
    )
    db.upsert_df(
        "index_membership",
        make_membership(isins, "TESTIDX", dt.date(2019, 1, 1)),
        ["index_name", "isin", "from_date"],
    )

    dates = rebalance_dates(dt.date(2020, 6, 30), dt.date(2023, 12, 31), "ME")
    df = run_attribution(db, "TESTIDX", dates, [Momentum12_1()])

    t_stat = float(df["ic_t_stat"].iloc[0])
    assert abs(t_stat) < 2.5, (
        f"IC t-statistic of {t_stat:.2f} on signalless data indicates a leak"
    )


def test_attribution_reports_coverage_per_factor(db: Database, universe: list[str]):
    """A factor with no data must report zero periods, not a spurious IC."""
    from stockanalysis.backtest.attribution import run_attribution
    from stockanalysis.backtest.engine import rebalance_dates
    from stockanalysis.factors.sentiment import NewsSentiment30d

    dates = rebalance_dates(dt.date(2022, 1, 31), AS_OF, "ME")
    df = run_attribution(db, "TESTIDX", dates, [NewsSentiment30d()]).set_index("factor")

    assert df.loc["news_sentiment_30d", "periods"] == 0
    assert df.loc["news_sentiment_30d", "coverage"] == 0.0
