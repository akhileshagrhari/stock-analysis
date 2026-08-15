"""Phase 4 tests — the read layer, the API, narrative generation, the dashboard.

The `scored_db` fixture below exists because the previous version of this file
asked `seeded_db` for the `NIFTY100` universe while the fixture seeds `TESTIDX`.
That returns an empty list, so nothing was ever scored, `n_signals` was always
zero, and every assertion sat behind `if n_signals > 0:` and never ran. The suite
passed by testing nothing. Assertions here are unconditional on purpose: if the
fixture stops producing signals, these tests must fail rather than go quiet.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import httpx
import pandas as pd
import pytest
from conftest import make_fundamentals

from stockanalysis.db.database import Database
from stockanalysis.factors.composite import (
    CompositeModel,
    ScoringConfig,
    family_percentiles,
    persist,
)
from stockanalysis.serve import explain, queries
from stockanalysis.serve.narrative import (
    NarrativeGenerator,
    NarrativeInput,
    NarrativeUnavailable,
    build_inputs,
    build_user_prompt,
)
from stockanalysis.serve.queries import SentimentCounts

INDEX = "TESTIDX"
AS_OF = dt.date(2023, 1, 31)


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _seed_scored(db: Database) -> Database:
    """Fundamentals, news, and one persisted scoring run on top of `seeded_db`.

    Shared by the in-memory fixture and the file-backed one the dashboard needs
    — the dashboard opens the database by path, so it cannot be handed an
    in-memory handle.
    """
    isins = db.as_of_universe(INDEX, AS_OF)
    assert isins, "fixture regression: TESTIDX universe is empty"

    # One company is given a qualified audit opinion and one persistently weak
    # cash conversion, so the red-flag overlay actually fires. Without a tripped
    # flag every `all(row.red_flags ...)` assertion below would be vacuously
    # true over an empty list — the same way the old suite passed.
    years = [2020, 2021, 2022]
    overrides = {(isins[0], year): {"auditor_opinion": "QUALIFIED"} for year in years}
    overrides.update(
        {(isins[1], year): {"ocf": 40.0, "pat": 150.0} for year in years}
    )
    db.upsert_df(
        "fundamentals_annual",
        make_fundamentals(isins, years, overrides=overrides),
        ["isin", "fiscal_year", "basis"],
    )

    # Two companies get news so the sentiment paths have something to read and
    # the "no coverage" path stays exercised by everyone else.
    news, sentiment = [], []
    for isin in isins[:2]:
        for i, label in enumerate(["positive", "positive", "negative"]):
            news_id = f"{isin}-news-{i}"
            news.append(
                {
                    "news_id": news_id,
                    "isin": isin,
                    "published_at": dt.datetime.combine(
                        AS_OF - dt.timedelta(days=i + 1), dt.time(9, 0)
                    ),
                    "ingested_at": dt.datetime.combine(AS_OF, dt.time(10, 0)),
                    "headline": f"Headline {i} for {isin}",
                    "body": "body",
                    "source": "TEST",
                    "url": f"https://example.test/{news_id}",
                }
            )
            sentiment.append(
                {
                    "news_id": news_id,
                    "model": "test-model",
                    "label": label,
                    "score": 0.8 if label == "positive" else -0.6,
                    "computed_at": dt.datetime.combine(AS_OF, dt.time(11, 0)),
                }
            )
    db.upsert_df("news", pd.DataFrame(news), ["news_id"])
    db.upsert_df("news_sentiment", pd.DataFrame(sentiment), ["news_id", "model"])

    result = CompositeModel(config=ScoringConfig(min_coverage=0.0)).score(
        db, isins, AS_OF
    )
    n_factors, n_signals = persist(db, result, generate_narratives=False)
    assert n_factors > 0 and n_signals > 0, "fixture produced no rows to test against"
    return db


@pytest.fixture
def scored_db(seeded_db: Database) -> Database:
    return _seed_scored(seeded_db)


@pytest.fixture
def scored_db_path(tmp_path, monkeypatch):
    """A file-backed scored database, with `settings.db_path` pointed at it.

    Built by replaying the same generators `seeded_db` uses. The dashboard and
    the API's own `get_db` both resolve the path from settings, so this is what
    lets them be exercised for real rather than through an injected handle.
    """
    from conftest import (
        DEFAULT_MOMENTUM_STRENGTH,
        make_instruments,
        make_membership,
        make_prices,
    )

    from stockanalysis.config import settings

    path = tmp_path / "scored.duckdb"
    start, end = dt.date(2019, 1, 1), dt.date(2024, 1, 1)

    db = Database(path)
    instruments = make_instruments(30)
    db.upsert_df("instruments", instruments, ["isin"])
    isins = instruments["isin"].tolist()
    db.upsert_df(
        "prices_daily",
        make_prices(isins, start, end, momentum_strength=DEFAULT_MOMENTUM_STRENGTH),
        ["isin", "date"],
    )
    db.upsert_df(
        "index_membership",
        make_membership(isins, INDEX, start),
        ["index_name", "isin", "from_date"],
    )
    _seed_scored(db)
    db.close()   # release the write lock before anything opens it read-only

    monkeypatch.setattr(settings, "db_path", path)
    return path


@pytest.fixture
def client(scored_db: Database):
    """TestClient wired to the fixture database via dependency override."""
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from stockanalysis.serve.api import app, get_db

    app.dependency_overrides[get_db] = lambda: scored_db
    try:
        yield fastapi_testclient.TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _fake_message(text: str = "Ranks well on momentum.", stop_reason: str = "end_turn"):
    """A response shaped like the SDK's: a thinking block, then the text."""
    thinking = MagicMock()
    thinking.type = "thinking"
    block = MagicMock()
    block.type = "text"
    block.text = text
    message = MagicMock()
    message.content = [thinking, block]
    message.stop_reason = stop_reason
    return message


def _fake_client(message=None):
    client = MagicMock()
    client.messages.create.return_value = message or _fake_message()
    return client


# ----------------------------------------------------------------------
# Read layer
# ----------------------------------------------------------------------


class TestCoercion:
    """NaN must never escape this layer — it is not valid JSON."""

    @pytest.mark.parametrize("value", [float("nan"), None, pd.NA, float("inf")])
    def test_opt_float_rejects_non_finite(self, value):
        assert queries._opt_float(value) is None

    def test_opt_float_passes_real_numbers(self):
        assert queries._opt_float(72.5) == 72.5
        assert queries._opt_float(0) == 0.0

    @pytest.mark.parametrize("value", [None, pd.NA, "", "   "])
    def test_opt_str_rejects_empty(self, value):
        assert queries._opt_str(value) is None

    def test_flag_list_splits_and_trims(self):
        assert queries._flag_list("a, b ,c") == ["a", "b", "c"]

    @pytest.mark.parametrize("value", ["", None, ","])
    def test_flag_list_empty(self, value):
        assert queries._flag_list(value) == []

    def test_opt_date_from_timestamp(self):
        assert queries._opt_date(pd.Timestamp("2023-01-31")) == dt.date(2023, 1, 31)


class TestQueries:
    def test_latest_as_of(self, scored_db):
        assert queries.latest_as_of(scored_db) == AS_OF

    def test_latest_as_of_none_when_unscored(self, seeded_db):
        assert queries.latest_as_of(seeded_db) is None

    def test_scored_dates(self, scored_db):
        assert queries.scored_dates(scored_db) == [AS_OF]

    def test_list_instruments(self, scored_db):
        instruments = queries.list_instruments(scored_db)
        assert len(instruments) == 30
        assert instruments[0].nse_symbol < instruments[-1].nse_symbol

    def test_list_instruments_by_sector(self, scored_db):
        sector = queries.sectors(scored_db)[0]
        rows = queries.list_instruments(scored_db, sector=sector)
        assert rows and all(r.sector == sector for r in rows)

    def test_get_instrument_missing(self, scored_db):
        assert queries.get_instrument(scored_db, "NOPE") is None

    def test_resolve_symbol_is_case_insensitive(self, scored_db):
        upper = queries.resolve_symbol(scored_db, "TEST000")
        lower = queries.resolve_symbol(scored_db, "test000")
        assert upper is not None and lower is not None
        assert upper.isin == lower.isin

    def test_signals_on_returns_universe(self, scored_db):
        signals = queries.signals_on(scored_db)
        assert len(signals) == 30
        assert all(s.as_of == AS_OF for s in signals)

    def test_signals_sorted_by_score_desc(self, scored_db):
        scores = [
            s.composite_score
            for s in queries.signals_on(scored_db)
            if s.composite_score is not None
        ]
        assert scores == sorted(scores, reverse=True)

    def test_signals_filter_by_signal(self, scored_db):
        for name in ("BUY", "HOLD", "SELL"):
            rows = queries.signals_on(scored_db, signal=name)
            assert all(r.signal == name for r in rows)

    def test_signal_filters_partition_the_universe(self, scored_db):
        total = len(queries.signals_on(scored_db))
        parts = sum(
            len(queries.signals_on(scored_db, signal=name))
            for name in ("BUY", "HOLD", "SELL")
        )
        unscored = [s for s in queries.signals_on(scored_db) if s.signal is None]
        assert parts + len(unscored) == total

    def test_signals_limit(self, scored_db):
        assert len(queries.signals_on(scored_db, limit=5)) == 5

    def test_flagged_only(self, scored_db):
        rows = queries.signals_on(scored_db, flagged_only=True)
        # Non-vacuous: the fixture deliberately trips two flags. Without this
        # floor, `all(...)` over an empty list would pass while testing nothing.
        assert len(rows) == 2
        assert all(r.red_flags for r in rows)
        assert all(r.has_red_flag for r in rows)
        assert {"auditor_qualification", "weak_cash_conversion"} == {
            flag for row in rows for flag in row.red_flags
        }

    def test_red_flag_forces_sell(self, scored_db):
        """The overlay caps at SELL regardless of the factor score."""
        for row in queries.signals_on(scored_db, flagged_only=True):
            assert row.signal == "SELL"

    def test_signals_on_unknown_date_is_empty(self, scored_db):
        assert queries.signals_on(scored_db, as_of=dt.date(1999, 1, 1)) == []

    def test_latest_signal_and_factors(self, scored_db):
        isin = queries.signals_on(scored_db)[0].isin
        signal = queries.latest_signal(scored_db, isin)
        assert signal is not None and signal.as_of == AS_OF
        factors = queries.factor_breakdown(scored_db, isin, AS_OF)
        assert factors, "expected stored factor values"
        assert all(f.factor_name for f in factors)

    def test_history_is_chronological(self, scored_db):
        isin = queries.signals_on(scored_db)[0].isin
        history = queries.signal_history(scored_db, isin)
        dates = [s.as_of for s in history]
        assert dates == sorted(dates)

    def test_signal_counts_match_rows(self, scored_db):
        counts = queries.signal_counts(scored_db, AS_OF)
        for name, count in counts.items():
            assert len(queries.signals_on(scored_db, signal=name)) == count

    def test_recent_news(self, scored_db):
        isin = sorted(scored_db.as_of_universe(INDEX, AS_OF))[0]
        news = queries.recent_news(scored_db, isin)
        assert len(news) == 3
        assert all(n.headline for n in news)

    def test_sentiment_counts_batches_and_omits_uncovered(self, scored_db):
        isins = sorted(scored_db.as_of_universe(INDEX, AS_OF))
        counts = queries.sentiment_counts(scored_db, isins, AS_OF)
        # Only the two seeded companies have news; the rest are absent rather
        # than present with zeros.
        assert len(counts) == 2
        first = counts[isins[0]]
        assert (first.positive, first.negative, first.neutral) == (2, 1, 0)
        assert first.total == 3

    def test_sentiment_counts_empty_input(self, scored_db):
        assert queries.sentiment_counts(scored_db, [], AS_OF) == {}


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------


class TestAPI:
    def test_health(self, client):
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["instruments"] == 30
        assert body["signals"] == 30
        assert body["latest_as_of"] == AS_OF.isoformat()

    def test_model_info_reflects_live_model(self, client):
        from stockanalysis.factors.composite import BUY_THRESHOLD, FAMILY_WEIGHTS

        body = client.get("/model").json()
        assert body["family_weights"] == FAMILY_WEIGHTS
        assert body["buy_threshold"] == BUY_THRESHOLD

    def test_instruments(self, client):
        body = client.get("/instruments").json()
        assert len(body) == 30
        assert {"isin", "nse_symbol", "name"} <= set(body[0])

    def test_instrument_404(self, client):
        assert client.get("/instruments/NOSUCHISIN").status_code == 404

    def test_lookup_by_symbol(self, client):
        response = client.get("/instruments/by-symbol/TEST000")
        assert response.status_code == 200
        assert response.json()["nse_symbol"] == "TEST000"

    def test_lookup_by_unknown_symbol_is_404(self, client):
        assert client.get("/instruments/by-symbol/NOSUCH").status_code == 404

    def test_signals_default_to_latest_date(self, client):
        body = client.get("/signals").json()
        assert len(body) == 30
        assert all(row["as_of"] == AS_OF.isoformat() for row in body)

    def test_signals_are_json_safe(self, client):
        """No NaN tokens: strict JSON parsing of the raw body must succeed."""
        import json

        raw = client.get("/signals").text
        assert "NaN" not in raw and "Infinity" not in raw
        json.loads(raw, parse_constant=_reject_constant)

    def test_signal_filter(self, client):
        body = client.get("/signals", params={"signal": "SELL"}).json()
        assert all(row["signal"] == "SELL" for row in body)

    def test_signal_filter_is_case_insensitive(self, client):
        assert client.get("/signals", params={"signal": "sell"}).status_code == 200

    def test_bad_signal_is_422(self, client):
        assert client.get("/signals", params={"signal": "MAYBE"}).status_code == 422

    def test_bad_date_is_422_not_500(self, client):
        response = client.get("/signals", params={"as_of": "31-01-2023"})
        assert response.status_code == 422

    def test_limit_bounds_enforced(self, client):
        assert client.get("/signals", params={"limit": 0}).status_code == 422
        assert client.get("/signals", params={"limit": 5}).json().__len__() == 5

    def test_latest_signal_has_factor_breakdown(self, client):
        isin = client.get("/signals").json()[0]["isin"]
        body = client.get(f"/instruments/{isin}/latest").json()
        assert body["isin"] == isin
        assert body["factors"], "expected a factor breakdown"
        assert {"factor_name", "raw_value", "sector_zscore"} <= set(body["factors"][0])

    def test_latest_signal_404_for_unknown_instrument(self, client):
        assert client.get("/instruments/NOPE/latest").status_code == 404

    def test_history(self, client):
        isin = client.get("/signals").json()[0]["isin"]
        body = client.get(f"/instruments/{isin}/history").json()
        assert len(body) == 1
        assert body[0]["as_of"] == AS_OF.isoformat()

    def test_news_endpoint(self, client):
        isin = sorted(i["isin"] for i in client.get("/instruments").json())[0]
        body = client.get(f"/instruments/{isin}/news").json()
        assert len(body) == 3

    def test_red_flags_endpoint(self, client):
        response = client.get("/signals/red-flags")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 2, "fixture should trip exactly two flags"
        assert all(row["red_flags"] for row in body)
        assert all(row["signal"] == "SELL" for row in body)

    def test_no_dynamic_route_shadows_a_literal_one(self):
        """Ordering guard for the hazard that actually exists.

        Starlette matches in declaration order, but a dynamic route only shadows
        a literal one when it genuinely matches that path — `/instruments/{isin}`
        cannot capture `/signals/red-flags`, and cannot capture the three-segment
        `/instruments/by-symbol/X` either. What *would* break is a same-prefix,
        same-shape sibling declared first: add `/signals/{isin}` above
        `/signals/red-flags` and "red-flags" silently becomes an ISIN.

        Asking Starlette itself which route wins, rather than comparing path
        shapes by hand, is what keeps this from firing on pairs that cannot
        collide.
        """
        from starlette.routing import Match

        from stockanalysis.serve.api import app

        literals = [
            route
            for route in app.routes
            if "{" not in getattr(route, "path", "{")
        ]
        assert literals, "expected some literal routes to check"

        for literal in literals:
            scope = {
                "type": "http",
                "method": "GET",
                "path": literal.path,
                "root_path": "",
                "path_params": {},
                "headers": [],
            }
            winner = next(
                (
                    route
                    for route in app.routes
                    if route.matches(scope)[0] is Match.FULL
                ),
                None,
            )
            assert winner is literal, (
                f"{literal.path} is declared after "
                f"{getattr(winner, 'path', None)!r}, which matches it first — "
                f"the literal route will never be reached"
            )

    def test_sectors(self, client):
        assert client.get("/sectors").json()

    def test_openapi_schema_builds(self, client):
        """A malformed response model surfaces here rather than at runtime."""
        assert client.get("/openapi.json").status_code == 200


def _reject_constant(name: str):
    raise AssertionError(f"non-JSON constant {name} in response body")


class TestAPIWithoutDatabase:
    def test_missing_database_is_503(self, tmp_path, monkeypatch):
        """A missing file is an operator error, reported as one, not a traceback."""
        fastapi_testclient = pytest.importorskip("fastapi.testclient")
        from stockanalysis.config import settings
        from stockanalysis.serve.api import app

        monkeypatch.setattr(settings, "db_path", tmp_path / "absent.duckdb")
        response = fastapi_testclient.TestClient(app).get("/health")
        assert response.status_code == 503
        assert "init" in response.json()["detail"]


# ----------------------------------------------------------------------
# Narrative generation
# ----------------------------------------------------------------------


def _input(**overrides) -> NarrativeInput:
    base = dict(
        isin="INE000000001",
        nse_symbol="TEST001",
        name="Test Company 1",
        as_of=AS_OF,
        composite_score=81.4,
        signal="BUY",
        sector="IT",
        coverage=0.77,
        family_scores={"value": 70.0, "momentum": 88.0},
    )
    base.update(overrides)
    return NarrativeInput(**base)


class TestPromptConstruction:
    def test_user_prompt_carries_the_facts(self):
        prompt = build_user_prompt(_input())
        for fragment in ["TEST001", "81.4", "BUY", "77%", "IT"]:
            assert fragment in prompt

    def test_missing_family_is_not_zero(self):
        """A family with no data must not read as 'worst in sector'."""
        prompt = build_user_prompt(_input(family_scores={"value": 70.0}))
        assert "Quality: not measured" in prompt
        assert "Quality: 0/100" not in prompt

    def test_flags_are_listed(self):
        prompt = build_user_prompt(
            _input(red_flags=("auditor_qualification",), unknown_flags=("rating_downgrade",))
        )
        assert "Red flags tripped: auditor_qualification" in prompt
        assert "Red flags not evaluable: rating_downgrade" in prompt

    def test_news_counts_are_labelled_correctly(self):
        """Regression: the old prompt counted `negative` twice and dropped neutral."""
        prompt = build_user_prompt(
            _input(news=SentimentCounts(positive=4, negative=1, neutral=2))
        )
        assert "4 positive, 1 negative, 2 neutral" in prompt
        assert "(7 articles)" in prompt

    def test_no_news_says_so(self):
        assert "no scored coverage" in build_user_prompt(_input(news=None))

    def test_system_prompt_stays_above_the_cache_minimum(self):
        """A prefix under the model's minimum is silently not cached.

        Opus 5's floor is 512 tokens. There is no offline tokeniser for this
        model family, so this guards a character-count proxy with enough margin
        to survive any plausible tokenisation — the point is that trimming the
        prompt can never quietly turn caching off without failing a test.
        """
        from stockanalysis.serve.narrative import _system_prompt

        # 512 tokens at a pessimistic 4.0 chars/token is ~2048 characters.
        assert len(_system_prompt()) >= 2200

    def test_system_prompt_tracks_the_live_model(self):
        from stockanalysis.factors.composite import FAMILY_WEIGHTS
        from stockanalysis.serve.narrative import _system_prompt

        system = _system_prompt()
        assert f"{FAMILY_WEIGHTS['quality']:.0%}" in system
        assert "relative" in system
        # Every reachable flag must be described, or the model cannot explain one.
        from stockanalysis.factors import redflags

        for definition in redflags.DEFINITIONS:
            if definition.reachable:
                assert definition.name in system


class TestNarrativeGeneration:
    def test_returns_text(self):
        generator = NarrativeGenerator(client=_fake_client())
        assert generator.generate(_input()) == "Ranks well on momentum."

    def test_skips_thinking_blocks(self):
        """`content[0]` is a thinking block; reading .text off it would raise."""
        generator = NarrativeGenerator(client=_fake_client(_fake_message("Prose.")))
        assert generator.generate(_input()) == "Prose."

    def test_request_uses_configured_model_and_caching(self):
        client = _fake_client()
        NarrativeGenerator(client=client, model="claude-opus-5", effort="low").generate(
            _input()
        )
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-opus-5"
        assert kwargs["output_config"] == {"effort": "low"}
        assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
        # A fixed thinking budget is rejected by this model family.
        assert "budget_tokens" not in str(kwargs)
        assert "temperature" not in kwargs

    def test_volatile_content_stays_out_of_the_cached_prefix(self):
        """A symbol leaking into the system prompt would break caching every call."""
        client = _fake_client()
        NarrativeGenerator(client=client).generate(_input())
        kwargs = client.messages.create.call_args.kwargs
        assert "TEST001" not in kwargs["system"][0]["text"]
        assert "TEST001" in kwargs["messages"][0]["content"]

    def test_refusal_returns_none(self):
        message = _fake_message("", stop_reason="refusal")
        generator = NarrativeGenerator(client=_fake_client(message))
        assert generator.generate(_input()) is None

    def test_transient_error_is_per_company(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("connection reset")
        assert NarrativeGenerator(client=client).generate(_input()) is None

    def test_auth_error_aborts_the_pass(self):
        """A bad key fails for every company; retrying it 100 times is waste."""
        anthropic = pytest.importorskip("anthropic")
        response = httpx.Response(
            401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        )
        client = MagicMock()
        client.messages.create.side_effect = anthropic.AuthenticationError(
            "invalid x-api-key", response=response, body=None
        )
        with pytest.raises(NarrativeUnavailable):
            NarrativeGenerator(client=client).generate(_input())

    def test_internal_tags_are_stripped(self):
        message = _fake_message("<thinking>hmm</thinking>Clean prose.")
        generator = NarrativeGenerator(client=_fake_client(message))
        assert generator.generate(_input()) == "hmmClean prose."

    def test_generate_many_covers_every_input(self):
        client = _fake_client()
        items = [_input(isin=f"INE{i:09d}", nse_symbol=f"TEST{i:03d}") for i in range(5)]
        results = NarrativeGenerator(client=client, max_workers=2).generate_many(items)
        assert set(results) == {i.isin for i in items}
        assert client.messages.create.call_count == 5

    def test_generate_many_empty(self):
        assert NarrativeGenerator(client=_fake_client()).generate_many([]) == {}

    def test_build_inputs_attaches_news(self, scored_db):
        isins = sorted(scored_db.as_of_universe(INDEX, AS_OF))
        rows = [
            {
                "isin": isins[0],
                "nse_symbol": "TEST000",
                "name": "Test Company 0",
                "composite_score": 60.0,
                "signal": "HOLD",
            },
            {
                "isin": isins[-1],
                "nse_symbol": "TEST029",
                "name": "Test Company 29",
                "composite_score": 40.0,
                "signal": "SELL",
            },
        ]
        built = build_inputs(scored_db, AS_OF, rows)
        assert built[0].news is not None and built[0].news.total == 3
        assert built[1].news is None


class TestPersistWithNarratives:
    def test_narratives_are_stored(self, seeded_db, monkeypatch):
        isins = seeded_db.as_of_universe(INDEX, AS_OF)
        seeded_db.upsert_df(
            "fundamentals_annual",
            make_fundamentals(isins, [2020, 2021, 2022]),
            ["isin", "fiscal_year", "basis"],
        )
        import stockanalysis.serve.narrative as narrative_module

        monkeypatch.setattr(
            narrative_module.NarrativeGenerator,
            "client",
            property(lambda self: _fake_client(_fake_message("Momentum drives this."))),
        )

        result = CompositeModel(config=ScoringConfig(min_coverage=0.0)).score(
            seeded_db, isins, AS_OF
        )
        _, n_signals = persist(seeded_db, result, generate_narratives=True)

        stored = seeded_db.query(
            "SELECT narrative FROM signals WHERE narrative IS NOT NULL"
        )
        assert n_signals > 0
        assert len(stored) == n_signals
        assert stored.iloc[0]["narrative"] == "Momentum drives this."

    def test_each_narrative_lands_on_its_own_row(self, seeded_db, monkeypatch):
        """Alignment, not just presence.

        `persist` maps generated text back onto the signal rows by ISIN. A test
        where every company gets the same string cannot tell a correct mapping
        from an off-by-one one, so here each company's narrative names it.
        """
        isins = seeded_db.as_of_universe(INDEX, AS_OF)
        seeded_db.upsert_df(
            "fundamentals_annual",
            make_fundamentals(isins, [2020, 2021, 2022]),
            ["isin", "fiscal_year", "basis"],
        )

        import stockanalysis.serve.narrative as narrative_module

        def per_company(self, item):
            return f"narrative for {item.isin}"

        monkeypatch.setattr(
            narrative_module.NarrativeGenerator, "generate", per_company
        )

        result = CompositeModel(config=ScoringConfig(min_coverage=0.0)).score(
            seeded_db, isins, AS_OF
        )
        persist(seeded_db, result, generate_narratives=True)

        stored = seeded_db.query("SELECT isin, narrative FROM signals")
        assert len(stored) > 1, "need several rows for alignment to mean anything"
        for row in stored.itertuples(index=False):
            assert row.narrative == f"narrative for {row.isin}"

    def test_narratives_off_by_default(self, scored_db):
        stored = scored_db.query("SELECT narrative FROM signals")
        assert stored["narrative"].isna().all()

    def test_family_percentiles_are_on_the_score_scale(self, scored_db):
        isins = scored_db.as_of_universe(INDEX, AS_OF)
        result = CompositeModel(config=ScoringConfig(min_coverage=0.0)).score(
            scored_db, isins, AS_OF
        )
        percentiles = family_percentiles(result)
        values = percentiles.to_numpy().ravel()
        finite = values[pd.notna(values)]
        assert finite.size > 0
        assert finite.min() >= 0.0 and finite.max() <= 100.0


# ----------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------


class TestExplain:
    """The attribution must reproduce the model, not merely look plausible."""

    def test_contributions_sum_to_the_composite(self, scored_db):
        """The decomposition is exact or it is not an explanation.

        Each family's contribution is its weighted z renormalised over the
        families present — which is how `_combine` builds the composite. If
        these stopped summing to it, the breakdown would be attributing the
        score to the wrong things.
        """
        from stockanalysis.factors.composite import CompositeModel

        config = ScoringConfig(min_coverage=0.0)
        model = CompositeModel(config=config)
        panel = explain.factor_panel(scored_db, AS_OF).reindex(
            columns=[f.name for f in model.factors]
        )
        family_z, _ = model._aggregate_families(panel)
        composite_z = model._combine(family_z)

        checked = 0
        for isin in panel.index:
            rows = explain._family_rows(panel, isin, model)
            total = sum(r.contribution for r in rows if r.contribution is not None)
            expected = composite_z.loc[isin]
            if pd.notna(expected):
                assert total == pytest.approx(expected, abs=1e-9)
                checked += 1
        assert checked > 0, "no scored names to check the decomposition against"

    def test_reconstruction_matches_a_fresh_scoring_run(self, scored_db):
        """Replaying stored z-scores must reproduce the model's own family scores.

        This is the load-bearing claim of the module: the explanation is derived
        from `factor_scores` without re-scoring, so it can only be trusted if
        the replay is faithful.
        """
        from stockanalysis.factors.composite import CompositeModel

        config = ScoringConfig(min_coverage=0.0)
        model = CompositeModel(config=config)
        isins = scored_db.as_of_universe(INDEX, AS_OF)

        fresh = model.score(scored_db, isins, AS_OF)
        panel = explain.factor_panel(scored_db, AS_OF).reindex(
            index=isins, columns=[f.name for f in model.factors]
        )
        replayed, _ = model._aggregate_families(panel)

        for family in fresh.family_z.columns:
            pd.testing.assert_series_equal(
                replayed[family].dropna(),
                fresh.family_z[family].dropna(),
                check_names=False,
            )

    def test_explains_every_signal_type(self, scored_db):
        for name in ("BUY", "HOLD", "SELL"):
            rows = queries.signals_on(scored_db, signal=name)
            if not rows:
                continue
            result = explain.explain(scored_db, rows[0].isin, AS_OF)
            assert result is not None
            assert name in result.headline
            assert result.reasons, f"{name} produced no reasons"

    def test_red_flag_headline_names_the_override(self, scored_db):
        flagged = queries.signals_on(scored_db, flagged_only=True)
        assert flagged, "fixture must trip a flag for this path to be tested"
        result = explain.explain(scored_db, flagged[0].isin, AS_OF)
        assert "red-flag overlay" in result.headline
        # The flag itself, and the score it overrode, both have to be named —
        # "82, SELL, auditor_qualification" is the finding; "SELL" alone is not.
        assert flagged[0].red_flags[0] in result.headline
        assert any("caps the signal at SELL" in r for r in result.reasons)
        assert any(
            "Promoter" in r or "Auditor" in r or "CFO/PAT" in r
            for r in result.reasons
        ), "the flag's rule text should be spelled out, not just its name"

    def test_strengths_and_weaknesses_split_on_sign(self, scored_db):
        isin = queries.signals_on(scored_db)[0].isin
        result = explain.explain(scored_db, isin, AS_OF)
        assert all(d.z > 0 for d in result.strengths)
        assert all(d.z < 0 for d in result.weaknesses)
        # Sorted by how strong the argument is, not by name.
        assert result.strengths == sorted(
            result.strengths, key=lambda d: d.z, reverse=True
        )

    def test_unmeasured_family_is_reported_not_scored_as_zero(self, scored_db):
        """A family with no data must read as absent, never as average.

        Asserted against a company the fixture deliberately gives no news, so
        this cannot pass by the family turning out to be measured — guarding the
        assertions behind `if not measured` is how the old suite tested nothing.
        """
        without_news = sorted(scored_db.as_of_universe(INDEX, AS_OF))[5]
        assert not queries.recent_news(scored_db, without_news), (
            "fixture regression: this company was supposed to have no news"
        )

        result = explain.explain(scored_db, without_news, AS_OF)
        sentiment = {f.family: f for f in result.families}["sentiment"]

        assert sentiment.measured is False
        assert sentiment.percentile is None
        assert sentiment.contribution is None
        assert sentiment.factors_measured == 0
        assert sentiment.verdict == "not measured"
        assert any("Not measured for want of data" in r for r in result.reasons)

    def test_measured_and_unmeasured_families_coexist(self, scored_db):
        """Momentum always has data here; sentiment never does for this name."""
        without_news = sorted(scored_db.as_of_universe(INDEX, AS_OF))[5]
        by_family = {
            f.family: f for f in explain.explain(scored_db, without_news, AS_OF).families
        }
        assert by_family["momentum"].measured is True
        assert by_family["momentum"].contribution is not None
        assert by_family["sentiment"].measured is False

    def test_stale_config_is_flagged(self, scored_db):
        """Explaining an old signal with today's weights must say so."""
        isin = queries.signals_on(scored_db)[0].isin
        matching = explain.explain(
            scored_db, isin, AS_OF, config=ScoringConfig(min_coverage=0.0)
        )
        assert matching.stale is False

        # A different config is a different model, and the label must change.
        divergent = explain.explain(
            scored_db, isin, AS_OF, config=ScoringConfig(min_coverage=0.9)
        )
        assert divergent.stale is True
        assert divergent.stored_version != divergent.current_version

    def test_dominant_family_matches_the_full_breakdown(self, scored_db):
        drivers = explain.dominant_families(scored_db, AS_OF)
        assert drivers
        for isin, (family, contribution) in list(drivers.items())[:5]:
            result = explain.explain(scored_db, isin, AS_OF)
            largest = max(
                (f for f in result.families if f.contribution is not None),
                key=lambda f: abs(f.contribution),
            )
            assert family == largest.family
            assert contribution == pytest.approx(largest.contribution)

    def test_labels_cover_every_factor_in_the_model(self):
        """An unlabelled factor would surface to users as a raw column name."""
        from stockanalysis.factors.composite import default_factors

        missing = [
            f.name for f in default_factors() if f.name not in explain.FACTOR_LABELS
        ]
        assert not missing, f"factors without a human label: {missing}"

    def test_families_all_have_a_plain_language_meaning(self):
        from stockanalysis.factors.composite import FAMILY_WEIGHTS

        missing = [f for f in FAMILY_WEIGHTS if f not in explain.FAMILY_MEANING]
        assert not missing, f"families without a description: {missing}"

    def test_unknown_instrument_returns_none(self, scored_db):
        assert explain.explain(scored_db, "NOSUCHISIN", AS_OF) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"),
            (11, "11th"), (12, "12th"), (13, "13th"),
            (21, "21st"), (32, "32nd"), (43, "43rd"), (100, "100th"),
        ],
    )
    def test_ordinal(self, value, expected):
        assert explain.ordinal(value) == expected

    def test_percentiles_read_as_ordinals(self, scored_db):
        """"the 3th percentile" is the kind of thing a reader notices."""
        for signal in queries.signals_on(scored_db)[:10]:
            for reason in explain.explain(scored_db, signal.isin, AS_OF).reasons:
                assert "1th" not in reason
                assert "2th percentile" not in reason
                assert "3th percentile" not in reason


class TestDashboardHelpers:
    @pytest.fixture(autouse=True)
    def _needs_streamlit(self):
        pytest.importorskip("streamlit")

    def test_signal_color(self):
        from stockanalysis.serve.dashboard import signal_color

        assert signal_color("BUY") == "🟢"
        assert signal_color("HOLD") == "🟡"
        assert signal_color("SELL") == "🔴"
        assert signal_color(None) == "⚫"

    def test_format_score_uses_model_thresholds(self):
        from stockanalysis.factors.composite import BUY_THRESHOLD, SELL_THRESHOLD
        from stockanalysis.serve.dashboard import format_score

        assert "🟢" in format_score(BUY_THRESHOLD)
        assert "🟡" in format_score(SELL_THRESHOLD)
        assert "🔴" in format_score(SELL_THRESHOLD - 1)
        assert format_score(float("nan")) == "—"
        assert format_score(None) == "—"

    def test_format_pct(self):
        from stockanalysis.serve.dashboard import format_pct

        assert format_pct(0.77) == "77%"
        assert format_pct(None) == "—"

    def test_frames_render_without_error(self, scored_db):
        from stockanalysis.serve.dashboard import export_frame, signals_frame

        signals = queries.signals_on(scored_db)
        display = signals_frame(signals)
        assert len(display) == len(signals)
        assert "Symbol" in display.columns

        export = export_frame(signals)
        assert len(export) == len(signals)
        # Export keeps real numbers so a spreadsheet can sort on them.
        assert export["composite_score"].dtype.kind == "f"

    def test_empty_frame_keeps_columns(self):
        from stockanalysis.serve.dashboard import signals_frame

        assert list(signals_frame([]).columns)[:2] == ["Symbol", "Name"]
        assert "Main driver" in signals_frame([]).columns

    def test_format_driver(self):
        from stockanalysis.serve.dashboard import format_driver

        assert format_driver(("growth", 0.42)) == "Growth ↑"
        assert format_driver(("quality", -0.31)) == "Quality ↓"
        assert format_driver(None) == "—"

    def test_readiness_frames_render(self, scored_db):
        from stockanalysis.factors import redflags
        from stockanalysis.serve import readiness as rd
        from stockanalysis.serve.dashboard import (
            blocked_frame,
            flags_frame,
            sources_frame,
        )

        isin = queries.list_instruments(scored_db)[0].isin
        report = rd.readiness(scored_db, isin, AS_OF)

        sources = sources_frame(report)
        assert len(sources) == len(rd.DATASETS)
        assert "What is missing" in sources.columns

        flags = flags_frame(report)
        assert len(flags) == len(redflags.DEFINITIONS)
        # An unreachable flag must never be shown as something a run would fix.
        unreachable = flags[flags["Flag"].isin(redflags.unreachable_flags())]
        assert (unreachable["Needs"] == "no source ingests this — it cannot be "
                "cleared").all()

        blocked = blocked_frame(report)
        assert len(blocked) == sum(1 for f in report.factors if not f.computable)
        if len(blocked) > 1:
            # Heaviest first — the order to close the gaps in.
            weights = [float(w.rstrip("%")) for w in blocked["Weight"]]
            assert weights == sorted(weights, reverse=True)

    def test_signals_frame_shows_the_driver(self, scored_db):
        from stockanalysis.serve.dashboard import signals_frame

        signals = queries.signals_on(scored_db)
        drivers = explain.dominant_families(scored_db, AS_OF)
        shown = signals_frame(signals, drivers)["Main driver"].tolist()
        assert any(cell != "—" for cell in shown), "no driver resolved for any row"

        # The arrow must follow the sign of the contribution, not be decorative.
        for cell, signal in zip(shown, signals, strict=True):
            driver = drivers.get(signal.isin)
            if driver is None:
                assert cell == "—"
                continue
            family, contribution = driver
            assert cell.startswith(family.capitalize())
            assert cell.endswith("↑" if contribution > 0 else "↓")


class TestDashboardPages:
    """Actually run the app. Importing it proves almost nothing.

    Every page is script-level Streamlit code that only executes when a session
    renders it, so an import-only test passes happily while a bad query, a
    removed keyword argument, or a renamed field breaks the page for real users.
    `AppTest` runs the script the way the server does and surfaces the exception.
    """

    @pytest.fixture(autouse=True)
    def _needs_streamlit(self):
        pytest.importorskip("streamlit.testing.v1")

    @staticmethod
    def _run(page: str):
        from streamlit.testing.v1 import AppTest

        from stockanalysis.serve import dashboard

        app = AppTest.from_file(dashboard.__file__, default_timeout=120).run()
        assert not app.exception, f"initial render raised: {app.exception}"
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, f"{page} raised: {app.exception}"
        return app

    @pytest.mark.parametrize(
        "page", ["Overview", "Signals", "Instrument", "Red flags", "About"]
    )
    def test_page_renders(self, page, scored_db_path):
        assert self._run(page) is not None

    def test_overview_counts_match_the_database(self, scored_db_path):
        """Counted with raw SQL, not with the helpers the page itself calls.

        Checking the page against `queries.signal_counts` would compare the code
        with itself: a bug there moves both sides equally and the test passes.
        """
        app = self._run("Overview")
        shown = {metric.label: int(metric.value) for metric in app.metric}

        db = Database(scored_db_path, read_only=True)
        try:
            rows = db.query(
                "SELECT signal, red_flags, composite_score FROM signals "
                "WHERE as_of_date = ?",
                [AS_OF],
            )
        finally:
            db.close()

        expected_buy = int((rows["signal"] == "BUY").sum())
        expected_sell = int((rows["signal"] == "SELL").sum())
        expected_flagged = int(rows["red_flags"].fillna("").str.strip().ne("").sum())
        expected_unscored = int(rows["composite_score"].isna().sum())

        assert shown["🟢 BUY"] == expected_buy
        assert shown["🔴 SELL"] == expected_sell
        assert shown["🚩 Red flags"] == expected_flagged
        assert shown["⚫ Unscored"] == expected_unscored
        # A sanity floor: an all-zero dashboard would satisfy equality above if
        # the fixture ever stopped producing signals.
        assert expected_buy + expected_sell > 0

    def test_instrument_page_explains_the_signal(self, scored_db_path):
        """The 'why' must be on the page without an LLM narrative present."""
        app = self._run("Instrument")
        text = " ".join(m.value for m in app.markdown)
        assert "Why this signal" in text
        assert "Where the score came from" in text
        assert "Arguing for it" in text and "Arguing against it" in text
        # A verdict banner naming a threshold, not just a bare signal word.
        banners = [b.value for b in list(app.success) + list(app.error) + list(app.warning)]
        assert any("threshold" in b or "red-flag overlay" in b for b in banners)

    def test_news_outside_the_scoring_window_is_marked(self, scored_db_path):
        """Listing news the sentiment factor never read made the page self-contradict."""
        app = self._run("Instrument")
        caption = next(
            (c.value for c in app.caption if "sentiment factor reads" in c.value), None
        )
        assert caption is not None, "no scoring-window caption on the news section"
        # The fixture's articles all sit 1-3 days before the scoring date.
        assert "3 of these 3 articles" in caption

    def test_instrument_page_shows_the_data_inventory(self, scored_db_path):
        """The gap report renders alongside the rating, not instead of it."""
        app = self._run("Instrument")
        labels = {metric.label for metric in app.metric}
        assert {"Model coverage", "Scorable today", "Sources with gaps"} <= labels

        text = " ".join(m.value for m in app.markdown)
        assert "Fill the gaps and re-evaluate" in text

    def test_data_tab_survives_a_company_with_no_signal(self, scored_db_path):
        """The failure this tab exists for.

        `show_instrument` used to return early when `latest_signal` was None, so
        the one screen that could say *why* a company is unrated went blank in
        precisely that case. Deleting every signal reproduces it.
        """
        from streamlit.testing.v1 import AppTest

        from stockanalysis.serve import dashboard

        db = Database(scored_db_path)
        try:
            db.conn.execute("DELETE FROM signals")
        finally:
            db.close()

        app = AppTest.from_file(dashboard.__file__, default_timeout=120).run()
        app.sidebar.radio[0].set_value("Instrument").run()

        assert not app.exception
        assert {"Model coverage", "Factors measured"} <= {m.label for m in app.metric}
        assert any("No signal stored" in info.value for info in app.info)

    def test_missing_database_stops_with_a_message(self, tmp_path, monkeypatch):
        """The old version raised FileNotFoundError and showed a traceback."""
        from streamlit.testing.v1 import AppTest

        from stockanalysis.config import settings
        from stockanalysis.serve import dashboard

        monkeypatch.setattr(settings, "db_path", tmp_path / "absent.duckdb")
        app = AppTest.from_file(dashboard.__file__, default_timeout=120).run()
        assert not app.exception
        assert any("init" in error.value for error in app.error)
