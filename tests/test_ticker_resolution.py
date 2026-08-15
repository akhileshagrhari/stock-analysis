"""Ticker resolution — the precision tests.

Every case here is a way an alias table quietly attributes the wrong company's
news to a stock, which is the failure DESIGN §3.3 flags and the one that cannot
be detected downstream: a wrong sentiment score looks exactly like a right one.
"""

from __future__ import annotations

import pandas as pd
import pytest

from stockanalysis.news.aliases import (
    CONF_NAME,
    CONF_NAME_SHORT,
    build_aliases,
    candidates_for,
    is_blocked,
    normalise,
    strip_legal_suffix,
)
from stockanalysis.news.resolve import (
    BODY_ONLY_PENALTY,
    EmptyAliasTableError,
    TickerResolver,
    confirm_mention,
)


def _instruments(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"isin": i, "nse_symbol": s, "name": n, "sector": "X", "industry": "X",
             "bse_code": None, "listing_date": None, "delisting_date": None,
             "is_active": True}
            for i, s, n in rows
        ]
    )


# ----------------------------------------------------------------------
# Normalisation
# ----------------------------------------------------------------------


def test_punctuation_folds_the_same_way_on_both_sides():
    # "Dr. Reddy's Laboratories" in a headline and in `instruments` must land
    # on one string, or the company never resolves.
    assert normalise("Dr. Reddy's Laboratories Ltd.") == "dr reddy s laboratories ltd"
    assert normalise("DR REDDYS LABORATORIES LTD") == "dr reddys laboratories ltd"


def test_legal_suffix_is_stripped_only_from_the_end():
    assert strip_legal_suffix("reliance industries limited") == "reliance industries"
    # "of India" is part of the name, not a suffix. Stripping it anywhere would
    # turn two different banks into "state bank" and "bank".
    assert strip_legal_suffix("state bank of india") == "state bank of india"
    assert strip_legal_suffix("the indian hotels company limited") == "indian hotels"


# ----------------------------------------------------------------------
# What is allowed to be an alias
# ----------------------------------------------------------------------


def test_group_names_are_never_aliases():
    # Each of these fronts a dozen separately listed companies.
    for group in ("tata", "bajaj", "adani", "mahindra", "reliance"):
        assert is_blocked(group), group


def test_hdfc_is_refused_because_it_meant_two_companies():
    # Until the July 2023 merger, "HDFC" was HDFC Ltd and "HDFC Bank" was a
    # different listed company. A backtest spanning the merger cannot use a
    # single mapping, and aliases here carry no validity dates.
    assert is_blocked("hdfc")
    assert not is_blocked("hdfc bank")


def test_single_token_common_words_are_refused():
    assert is_blocked("page")      # Page Industries
    assert is_blocked("sun")       # Sun Pharma, Sun TV, the sun
    assert not is_blocked("page industries")


def test_a_distinctive_single_token_name_still_works():
    aliases = dict((a, (s, c)) for a, s, c in candidates_for("CIPLA", "Cipla Ltd"))
    assert "cipla" in aliases


def test_a_name_prefix_never_ends_on_a_joining_word():
    # "Bank of Baroda" -> "bank of" matched Bank of America, Bank of Korea,
    # Bank of Maharashtra and two Bank of Japan stories in one live run: seven
    # false attributions from a single alias.
    aliases = {a for a, _, _ in candidates_for("BANKBARODA", "Bank of Baroda")}
    assert "bank of" not in aliases
    assert "bank of baroda" in aliases
    # The rule must not eat legitimate prefixes that simply contain a stopword
    # later on.
    assert "state bank" in {
        a for a, _, _ in candidates_for("SBIN", "State Bank of India")
    }


def test_first_token_of_a_name_is_generated_but_below_threshold():
    # "Nestle" for Nestle India: real usage, and also matches Nestle SA news.
    # Kept, scored below the ingest threshold, and measured rather than assumed.
    got = {a: (s, c) for a, s, c in candidates_for("NESTLEIND", "Nestle India Ltd")}
    assert got["nestle india"][1] == CONF_NAME
    assert got["nestle"] == ("NAME_SHORT", CONF_NAME_SHORT)
    assert CONF_NAME_SHORT < 0.7  # the default news_min_resolution_confidence


# ----------------------------------------------------------------------
# Build-time conflict detection
# ----------------------------------------------------------------------


def test_an_alias_two_companies_claim_is_given_to_neither(db):
    db.upsert_df(
        "instruments",
        _instruments(
            [
                ("INE001", "APOLLOHOSP", "Apollo Hospitals Enterprise Ltd"),
                ("INE002", "APOLLOTYRE", "Apollo Tyres Ltd"),
            ]
        ),
        ["isin"],
    )
    n, conflicts = build_aliases(db)

    stored = db.query("SELECT alias FROM instrument_aliases")["alias"].tolist()
    assert "apollo hospitals enterprise" in stored
    assert "apollo tyres" in stored
    # "apollo" is blocked as a group name before it can even conflict.
    assert "apollo" not in stored
    assert n == len(stored)
    assert all(len(isins) > 1 for _, isins in conflicts)


def test_rebuilding_drops_aliases_of_a_renamed_company(db):
    db.upsert_df("instruments", _instruments([("INE001", "OLDCO", "Oldname Ltd")]), ["isin"])
    build_aliases(db)
    assert "oldname" in db.query("SELECT alias FROM instrument_aliases")["alias"].tolist()

    db.conn.execute("UPDATE instruments SET name = 'Newname Ltd', nse_symbol = 'NEWCO'")
    build_aliases(db)
    aliases = db.query("SELECT alias FROM instrument_aliases")["alias"].tolist()
    # A stale alias is worse than a missing one: the old name may now belong to
    # somebody else.
    assert "oldname" not in aliases
    assert "newname" in aliases


# ----------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------


@pytest.fixture
def resolver() -> TickerResolver:
    return TickerResolver.from_pairs(
        {
            "tata motors": ("INE_TATAMOTORS", "CURATED", 0.95),
            "mahindra and mahindra": ("INE_MM", "CURATED", 0.95),
            "kotak mahindra bank": ("INE_KOTAK", "CURATED", 0.95),
            "bajaj finance": ("INE_BAJFIN", "CURATED", 0.95),
            "bajaj finserv": ("INE_BAJFINSV", "CURATED", 0.95),
            "infosys": ("INE_INFY", "NAME", 0.95),
            "infy": ("INE_INFY", "SYMBOL", 0.90),
            "nestle": ("INE_NESTLE", "NAME_SHORT", 0.60),
        }
    )


def test_the_longer_match_wins_and_the_shorter_one_is_discarded(resolver):
    # Without span bookkeeping this reports a Mahindra & Mahindra mention.
    mentions = resolver.resolve("Kotak Mahindra Bank Q1 profit rises 12%")
    assert [m.isin for m in mentions] == ["INE_KOTAK"]


def test_two_companies_in_one_headline_both_resolve(resolver):
    mentions = resolver.resolve("Bajaj Finance and Bajaj Finserv slip after RBI order")
    assert {m.isin for m in mentions} == {"INE_BAJFIN", "INE_BAJFINSV"}


def test_a_symbol_resolves_the_same_company_as_its_name(resolver):
    by_name = resolver.resolve("Infosys wins $1bn deal")
    by_symbol = resolver.resolve("INFY, TCS lead IT rally")
    assert by_name[0].isin == by_symbol[0].isin == "INE_INFY"
    assert by_symbol[0].method == "SYMBOL"


def test_a_body_only_mention_falls_below_the_usable_threshold(resolver):
    # Measured on 58 live attributions: eight of nine body-only mentions named
    # a company the article was not about — Airtel in a story about Singtel,
    # Tata Capital in a story about Tata Motors' share price.
    headline_hit = resolver.resolve("Tata Motors gains 6%")[0]
    body_hit = resolver.resolve("Auto stocks rally", "Tata Motors led the gainers")[0]

    assert headline_hit.matched_in == "HEADLINE"
    assert body_hit.matched_in == "BODY"
    assert body_hit.confidence == pytest.approx(
        headline_hit.confidence * BODY_ONLY_PENALTY
    )
    assert headline_hit.confidence >= 0.7 > body_hit.confidence


def test_the_best_mention_per_company_survives(resolver):
    # Named in both headline and body: keep the headline's confidence, not the
    # discounted one, and do not emit the company twice.
    mentions = resolver.resolve("Infosys raises guidance", "Infosys said margins held")
    assert len(mentions) == 1
    assert mentions[0].matched_in == "HEADLINE"


def test_a_macro_headline_resolves_to_nothing(resolver):
    assert resolver.resolve("Sensex ends 400 points higher on FII buying") == []


def test_resolution_without_an_alias_table_is_an_error_not_silence(db):
    # An empty table would otherwise make every article "unresolved" and the
    # ingest would look like it worked.
    with pytest.raises(EmptyAliasTableError):
        TickerResolver.from_db(db)


# ----------------------------------------------------------------------
# Provider attribution
# ----------------------------------------------------------------------


def test_confirm_mention_rejects_a_search_hit_that_names_nobody(resolver):
    # GDELT's index matched something in the body we cannot see. Unconfirmed.
    assert confirm_mention(resolver, "INE_INFY", "Titanium prices surge") is None
    assert confirm_mention(resolver, "INE_INFY", "Infosys wins deal") is not None
