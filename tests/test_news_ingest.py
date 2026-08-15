"""News ingest: feed parsing, knowledge dates, deduplication, backfill.

The point-in-time tests here are the phase-3 equivalents of
`test_point_in_time.py`'s fundamentals tests. A news factor is the easiest
place in the system to leak the future — an article's *ingest* time is always
today, and using it anywhere in the read path hands the backtest every
subsequent headline at once.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from stockanalysis.ingest import gdelt
from stockanalysis.ingest.rss import parse_datetime, parse_feed, strip_html
from stockanalysis.news.resolve import TickerResolver
from stockanalysis.news.store import Article, content_hash, store_articles, to_ist

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Markets</title>
  <item>
    <title>Tata Motors Q1 profit jumps 42% on JLR strength</title>
    <link>https://example.com/tata-motors-q1</link>
    <description>&lt;img src="thumb.jpg"/&gt; The carmaker beat estimates.</description>
    <pubDate>Tue, 23 Apr 2024 22:36:32 +0530</pubDate>
    <guid>https://example.com/tata-motors-q1</guid>
  </item>
  <item>
    <title><![CDATA[Infosys cuts FY25 revenue guidance]]></title>
    <link>https://example.com/infosys-guidance</link>
    <description><![CDATA[The IT major trimmed its outlook.]]></description>
    <pubDate>Wed, 24 Apr 2024 09:05:00 +0530</pubDate>
  </item>
  <item>
    <title>Undated filler</title>
    <link>https://example.com/filler</link>
  </item>
</channel></rss>
"""

ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Companies</title>
  <entry>
    <title>Infosys wins $1.5bn deal</title>
    <link href="https://example.com/atom-infosys"/>
    <summary>Multi-year contract.</summary>
    <published>2024-04-25T04:30:00Z</published>
  </entry>
</feed>
"""


@pytest.fixture
def resolver() -> TickerResolver:
    return TickerResolver.from_pairs(
        {
            "tata motors": ("INE_TM", "CURATED", 0.95),
            "infosys": ("INE_INFY", "NAME", 0.95),
            "jlr": ("INE_TM", "CURATED", 0.95),
        }
    )


# ----------------------------------------------------------------------
# Feed parsing
# ----------------------------------------------------------------------


def test_rss_items_parse_with_markup_stripped():
    articles = parse_feed(RSS_FEED, "https://www.example.com/rss/markets.xml")
    assert [a.headline for a in articles] == [
        "Tata Motors Q1 profit jumps 42% on JLR strength",
        "Infosys cuts FY25 revenue guidance",
    ]
    # Moneycontrol prefixes every description with a thumbnail. Scoring an
    # <img> tag is scoring a filename.
    assert "<img" not in articles[0].body
    assert articles[0].body == "The carmaker beat estimates."
    assert articles[0].source == "example.com"


def test_an_item_with_no_date_is_skipped_not_dated_to_now():
    # Guessing "now" for an undated item dates a week-old story to today,
    # which in a point-in-time system is forgery.
    assert all(a.headline != "Undated filler" for a in parse_feed(RSS_FEED))


def test_atom_feeds_parse_too():
    (article,) = parse_feed(ATOM_FEED, "https://example.com/atom")
    assert article.headline == "Infosys wins $1.5bn deal"
    assert article.url == "https://example.com/atom-infosys"


def test_a_broken_feed_returns_nothing_rather_than_raising():
    # One outlet changing its URL must not cost the other eight.
    assert parse_feed("<html>404 not found</html>", "https://example.com") == []


def test_strip_html_unescapes_twice_encoded_markup():
    assert strip_html("&lt;p&gt;Profit &amp;amp; loss&lt;/p&gt;") == "Profit & loss"


# ----------------------------------------------------------------------
# Knowledge dates
# ----------------------------------------------------------------------


def test_timestamps_are_stored_in_ist_not_utc():
    # 22:36 IST is 17:06 UTC the same day. Stored as UTC, this article would
    # appear on the correct date; but a 05:00 IST article is 23:30 UTC the
    # *previous* day, and would then be visible to a decision made a day early.
    ist = parse_datetime("Tue, 23 Apr 2024 22:36:32 +0530")
    assert (ist.hour, ist.day) == (22, 23)

    early = parse_datetime("Wed, 24 Apr 2024 05:00:00 +0530")
    assert early.date() == dt.date(2024, 4, 24)

    utc_input = parse_datetime("2024-04-23T23:30:00Z")
    assert utc_input.date() == dt.date(2024, 4, 24)   # 05:00 IST next morning


def test_an_article_is_invisible_to_a_decision_made_before_it_was_published(
    db, resolver
):
    store_articles(
        db,
        [
            _article("Tata Motors Q1 profit jumps", dt.datetime(2024, 5, 10, 9, 0)),
            _article("Tata Motors recalls 20,000 cars", dt.datetime(2024, 6, 20, 9, 0)),
        ],
        resolver,
        min_confidence=0.7,
    )
    _score_everything(db)

    may = db.as_of_sentiment(["INE_TM"], dt.date(2024, 5, 31), window_days=30)
    assert len(may) == 1

    june = db.as_of_sentiment(["INE_TM"], dt.date(2024, 6, 30), window_days=30)
    assert len(june) == 1        # the May story has aged out of the 30-day window
    assert june["news_id"].iloc[0] != may["news_id"].iloc[0]


def test_ingesting_old_news_today_does_not_backdate_it_to_today(db, resolver):
    # The whole point of the GDELT backfill: articles arrive now, but must be
    # readable as of their publication date, not their ingest date.
    store_articles(
        db,
        [_article("Infosys wins $1.5bn deal", dt.datetime(2022, 3, 4, 11, 0))],
        resolver,
        min_confidence=0.7,
        now=dt.datetime(2026, 8, 14, 12, 0),
    )
    _score_everything(db)

    assert len(db.as_of_sentiment(["INE_INFY"], dt.date(2022, 3, 10))) == 1
    assert len(db.as_of_sentiment(["INE_INFY"], dt.date(2022, 2, 1))) == 0


def test_an_article_published_at_2355_is_visible_the_same_day(db, resolver):
    store_articles(
        db,
        [_article("Infosys wins $1.5bn deal", dt.datetime(2024, 5, 10, 23, 55))],
        resolver,
        min_confidence=0.7,
    )
    _score_everything(db)
    assert len(db.as_of_sentiment(["INE_INFY"], dt.date(2024, 5, 10))) == 1


# ----------------------------------------------------------------------
# Deduplication
# ----------------------------------------------------------------------


def test_re_running_an_ingest_does_not_duplicate_anything(db, resolver):
    articles = [_article("Tata Motors Q1 profit jumps", dt.datetime(2024, 5, 10, 9, 0))]
    store_articles(db, articles, resolver, 0.7)
    stats = store_articles(db, articles, resolver, 0.7)

    assert db.query("SELECT COUNT(*) c FROM news")["c"].iloc[0] == 1
    assert stats.duplicates == 1


def test_the_same_wire_story_from_four_outlets_counts_once(db, resolver):
    # Syndication is not sentiment: four carriers of one PTI story would
    # otherwise quadruple-weight it in a 30-day average.
    stats = store_articles(
        db,
        [
            _article("Tata Motors Q1 profit jumps", dt.datetime(2024, 5, 10, 9, 0),
                     url=f"https://outlet{i}.com/story", source=f"outlet{i}.com")
            for i in range(4)
        ],
        resolver,
        0.7,
    )
    assert db.query("SELECT COUNT(*) c FROM news WHERE isin IS NOT NULL")["c"].iloc[0] == 1
    assert stats.duplicates == 3


def test_one_article_carried_by_two_feeds_in_one_batch_does_not_abort_the_ingest(
    db, resolver
):
    # Found live: an Economic Times story sits in both the markets feed and the
    # stocks feed, and a macro story that resolves to no company skips the
    # ISIN-keyed dedupe. Two rows, one primary key, DuckDB aborts the batch.
    same_url = "https://example.com/same-story"
    stats = store_articles(
        db,
        [
            _article("Sensex ends 400 points higher", dt.datetime(2024, 5, 10, 9, 0),
                     url=same_url),
            _article("Sensex ends 400 points higher", dt.datetime(2024, 5, 10, 9, 0),
                     url=same_url),
        ],
        resolver,
        0.7,
    )
    assert db.query("SELECT COUNT(*) c FROM news")["c"].iloc[0] == 1
    assert stats.duplicates == 1


def test_a_recurring_headline_on_a_different_day_is_a_different_story(db, resolver):
    # "Sensex ends higher" every day is not one article. The dedupe key
    # includes the publication date for exactly this reason.
    for day in (10, 11):
        store_articles(
            db,
            [_article("Tata Motors gains", dt.datetime(2024, 5, day, 9, 0),
                      url=f"https://example.com/{day}")],
            resolver,
            0.7,
        )
    assert db.query("SELECT COUNT(*) c FROM news")["c"].iloc[0] == 2


def test_content_hash_ignores_punctuation_and_case():
    assert content_hash("Tata Motors' Q1 profit") == content_hash("TATA MOTORS Q1 PROFIT")


# ----------------------------------------------------------------------
# Attribution
# ----------------------------------------------------------------------


def test_a_multi_company_article_becomes_one_row_per_company(db, resolver):
    store_articles(
        db,
        [_article("Infosys and Tata Motors lead Nifty gainers",
                  dt.datetime(2024, 5, 10, 9, 0))],
        resolver,
        0.7,
    )
    rows = db.query("SELECT isin, article_id FROM news ORDER BY isin")
    assert set(rows["isin"]) == {"INE_INFY", "INE_TM"}
    assert rows["article_id"].nunique() == 1


def test_an_unresolved_article_is_kept_but_hidden_from_the_factor(db, resolver):
    stats = store_articles(
        db,
        [_article("Sensex ends 400 points higher", dt.datetime(2024, 5, 10, 9, 0))],
        resolver,
        0.7,
    )
    assert stats.unresolved == 1
    # Stored — it is the denominator of the resolution rate, and re-resolvable
    # later without another fetch.
    assert db.query("SELECT COUNT(*) c FROM news")["c"].iloc[0] == 1
    assert db.query("SELECT COUNT(*) c FROM news WHERE isin IS NOT NULL")["c"].iloc[0] == 0


def test_a_below_threshold_mention_is_stored_but_never_scored(db):
    weak = TickerResolver.from_pairs({"nestle": ("INE_NEST", "NAME_SHORT", 0.6)})
    stats = store_articles(
        db, [_article("Nestle flat in early trade", dt.datetime(2024, 5, 10, 9, 0))],
        weak, min_confidence=0.7,
    )
    assert stats.below_threshold == 1
    assert stats.resolved == 0

    from stockanalysis.news.scoring import pending_news

    assert pending_news(db, "any-model", min_confidence=0.7).empty
    # Lower the threshold and the same stored row becomes scoreable — no
    # re-fetch involved.
    assert len(pending_news(db, "any-model", min_confidence=0.5)) == 1


def test_provider_entity_tags_bypass_the_text_resolver(db):
    stats = store_articles(
        db,
        [
            Article(
                provider="MARKETAUX", source="mc.com", url="https://x.com/1",
                headline="A headline naming nobody the resolver knows",
                published_at=dt.datetime(2024, 5, 10, 9, 0),
                published_at_source="MARKETAUX",
                entity_isins=("INE_TM",),
            )
        ],
        resolver=None,
        min_confidence=0.7,
    )
    assert stats.resolved == 1
    assert db.query("SELECT resolution_method FROM news")["resolution_method"].iloc[0] == (
        "PROVIDER_ENTITY"
    )


def test_a_roundup_naming_five_companies_is_not_company_news(db):
    # From the first live FinBERT run: "Top Gainers & Losers on 13 August:
    # Apar Industries, Hindalco, Ather Energy, Force Motors" scored -0.97, and
    # Hindalco — in the *gainers* half — was handed that number. A document
    # sentiment is only attributable when the document is about one thing.
    wide = TickerResolver.from_pairs(
        {name: (f"INE_{i}", "NAME", 0.95) for i, name in enumerate(
            ["alpha corp", "beta corp", "gamma corp", "delta corp", "omega corp"]
        )}
    )
    stats = store_articles(
        db,
        [_article("Top gainers: Alpha Corp, Beta Corp, Gamma Corp, Delta Corp, "
                  "Omega Corp", dt.datetime(2024, 5, 10, 9, 0))],
        wide,
        0.7,
    )
    assert stats.resolved == 0
    assert stats.below_threshold == 5

    methods = db.query("SELECT DISTINCT resolution_method FROM news")
    assert methods["resolution_method"].tolist() == ["ROUNDUP"]
    # Stored and countable, but invisible to the factor.
    assert db.query("SELECT COUNT(*) c FROM news")["c"].iloc[0] == 5


def test_a_two_company_story_is_not_treated_as_a_roundup(db, resolver):
    stats = store_articles(
        db,
        [_article("Infosys and Tata Motors lead Nifty gainers",
                  dt.datetime(2024, 5, 10, 9, 0))],
        resolver,
        0.7,
    )
    assert stats.resolved == 1
    assert stats.below_threshold == 0


def test_a_demoted_row_stops_feeding_the_factor_even_with_an_old_score(db, resolver):
    # The read path, not the scorer, is what keeps a retired attribution out:
    # a row demoted after it was scored still has its news_sentiment row.
    store_articles(
        db, [_article("Tata Motors Q1 profit jumps", dt.datetime(2024, 5, 10, 9, 0))],
        resolver, 0.7,
    )
    _score_everything(db)
    assert len(db.as_of_sentiment(["INE_TM"], dt.date(2024, 5, 15))) == 1

    db.conn.execute("UPDATE news SET resolution_confidence = 0.5")
    assert len(db.as_of_sentiment(["INE_TM"], dt.date(2024, 5, 15))) == 0
    assert db.query("SELECT COUNT(*) c FROM news_sentiment")["c"].iloc[0] == 1


def test_a_provider_search_hit_inside_a_roundup_is_still_a_roundup(db):
    # Found in the live GDELT pilot. A query for ABB returned "Stocks to Watch
    # Today: Vedanta, Hindustan Zinc, TCS, Tata Power, ABB, LIC, Bajaj Finance,
    # RVNL". Filtering to the queried company *before* counting companies
    # leaves a one-mention article that no roundup rule can recognise, and it
    # went into the table at 0.90. Demotion has to look at the whole article.
    wide = TickerResolver.from_pairs(
        {name: (f"INE_{i}", "NAME", 0.95) for i, name in enumerate(
            ["alpha corp", "beta corp", "gamma corp", "delta corp", "omega corp"]
        )}
    )
    store_articles(
        db,
        [
            Article(
                provider="GDELT", source="x.com", url="https://x.com/1",
                headline="Stocks to watch: Alpha Corp, Beta Corp, Gamma Corp, "
                         "Delta Corp, Omega Corp",
                published_at=dt.datetime(2024, 5, 10, 9, 0),
                published_at_source="GDELT_SEENDATE",
                query_isin="INE_0",
            )
        ],
        wide,
        0.7,
    )
    rows = db.query(
        "SELECT isin, resolution_method, resolution_confidence AS conf FROM news"
    )
    assert set(rows["resolution_method"]) == {"ROUNDUP"}
    assert (rows["conf"] < 0.7).all()
    # Including the company the query was for.
    assert "INE_0" in set(rows["isin"])


def test_a_search_hit_naming_a_different_company_belongs_to_that_company(db, resolver):
    # Fetched under a query for Tata Motors, titled about Infosys. The text
    # decides, here and everywhere — otherwise `reresolve`, which has no record
    # of what was searched for, could not reproduce what the ingest did.
    stats = store_articles(
        db,
        [
            Article(
                provider="GDELT", source="x.com", url="https://x.com/2",
                headline="Infosys wins $1.5bn deal",
                published_at=dt.datetime(2024, 5, 10, 9, 0),
                published_at_source="GDELT_SEENDATE",
                query_isin="INE_TM",
            )
        ],
        resolver,
        0.7,
    )
    assert stats.unconfirmed == 1        # Tata Motors is not in the title
    assert db.query("SELECT isin FROM news")["isin"].tolist() == ["INE_INFY"]


def test_a_search_hit_the_text_does_not_confirm_is_never_that_companys_news(
    db, resolver
):
    # GDELT matched something in the body it will not show us. Trusting the
    # query would make the backfill self-confirming.
    stats = store_articles(
        db,
        [
            Article(
                provider="GDELT", source="x.com", url="https://x.com/1",
                headline="Titanium prices surge on supply squeeze",
                published_at=dt.datetime(2024, 5, 10, 9, 0),
                published_at_source="GDELT_SEENDATE",
                query_isin="INE_TM",
            )
        ],
        resolver,
        0.7,
    )
    assert stats.unconfirmed == 1
    assert db.query("SELECT COUNT(*) c FROM news WHERE isin IS NOT NULL")["c"].iloc[0] == 0


# ----------------------------------------------------------------------
# GDELT
# ----------------------------------------------------------------------


def test_reresolving_a_better_alias_table_needs_no_refetch(db):
    from stockanalysis.news.store import reresolve

    thin = TickerResolver.from_pairs({"infosys": ("INE_INFY", "NAME", 0.95)})
    store_articles(
        db,
        [
            _article("Apollo Hospitals beats estimates", dt.datetime(2024, 5, 10, 9, 0)),
            _article("Infosys wins deal", dt.datetime(2024, 5, 10, 10, 0)),
        ],
        thin,
        0.7,
    )
    assert db.query("SELECT COUNT(*) c FROM news WHERE isin IS NULL")["c"].iloc[0] == 1

    better = TickerResolver.from_pairs(
        {"infosys": ("INE_INFY", "NAME", 0.95),
         "apollo hospitals": ("INE_APOLLO", "NAME_PREFIX", 0.90)}
    )
    stats = reresolve(db, better, 0.7)

    assert stats.newly_resolved == 1
    assert db.query("SELECT COUNT(*) c FROM news WHERE isin IS NULL")["c"].iloc[0] == 0
    # And it stays put: a second pass must find nothing to do.
    assert reresolve(db, better, 0.7).changed == 0


def test_reresolving_a_large_batch_of_unattributed_articles_does_not_fatal(tmp_path):
    """Regression: DuckDB index deletion on a table full of NULLs.

    `reresolve` deletes the rows it is replacing. With ART indexes on `news`'s
    nullable columns, deleting a few hundred rows whose `isin` is NULL failed
    with `FATAL Error: Failed to delete all rows from index. Only deleted 35
    out of 61 rows` and killed the connection — DuckDB does not index NULLs, so
    the index has fewer entries than the delete expects.

    It reproduces only on a **file-backed** database with a few hundred rows —
    not in `:memory:`, and not at three rows — which is why every unit test
    passed while the live command could not run at all.
    """
    from stockanalysis.db.database import Database
    from stockanalysis.news.store import reresolve

    db = Database(tmp_path / "news.duckdb")
    thin = TickerResolver.from_pairs({"infosys": ("INE_INFY", "NAME", 0.95)})
    store_articles(
        db,
        [
            _article(f"Sensex ends {i} points higher on FII buying",
                     dt.datetime(2024, 5, 10, 9, 0),
                     url=f"https://example.com/macro-{i}")
            for i in range(300)
        ],
        thin,
        0.7,
    )
    assert db.query("SELECT COUNT(*) c FROM news WHERE isin IS NULL")["c"].iloc[0] == 300

    better = TickerResolver.from_pairs(
        {"infosys": ("INE_INFY", "NAME", 0.95), "sensex": ("INE_X", "NAME", 0.95)}
    )
    stats = reresolve(db, better, 0.7)
    assert stats.newly_resolved == 300
    assert db.query("SELECT COUNT(*) c FROM news WHERE isin IS NULL")["c"].iloc[0] == 0


def test_reresolution_does_not_recreate_a_syndicated_duplicate(db, resolver):
    from stockanalysis.news.store import reresolve

    # Two URLs, one story. The first ingest resolves one of them and leaves the
    # other unattributed; a better alias table then resolves the second onto a
    # story the first already carries.
    thin = TickerResolver.from_pairs({"tata motors": ("INE_TM", "CURATED", 0.95)})
    store_articles(
        db,
        [
            _article("Tata Motors Q1 profit jumps", dt.datetime(2024, 5, 10, 9, 0),
                     url="https://mc.com/a"),
            _article("Apollo Hospitals beats estimates", dt.datetime(2024, 5, 10, 9, 0),
                     url="https://mc.com/b"),
        ],
        thin,
        0.7,
    )
    db.conn.execute(
        "UPDATE news SET content_hash = 'shared-story' WHERE url = 'https://mc.com/b'"
    )
    db.conn.execute(
        "UPDATE news SET content_hash = 'shared-story' WHERE url = 'https://mc.com/a'"
    )

    wider = TickerResolver.from_pairs(
        {"tata motors": ("INE_TM", "CURATED", 0.95),
         "apollo hospitals": ("INE_TM", "NAME_PREFIX", 0.90)}
    )
    reresolve(db, wider, 0.7)

    attributed = db.query(
        "SELECT COUNT(*) c FROM news WHERE isin IS NOT NULL"
    )["c"].iloc[0]
    assert attributed == 1


def test_a_reattributed_row_does_not_keep_the_old_companys_score(db):
    from stockanalysis.news.store import reresolve

    wrong = TickerResolver.from_pairs({"apollo": ("INE_WRONG", "NAME_SHORT", 0.95)})
    store_articles(
        db, [_article("Apollo Hospitals beats estimates", dt.datetime(2024, 5, 10, 9, 0))],
        wrong, 0.7,
    )
    _score_everything(db)
    assert db.query("SELECT COUNT(*) c FROM news_sentiment")["c"].iloc[0] == 1

    right = TickerResolver.from_pairs(
        {"apollo hospitals": ("INE_APOLLO", "NAME_PREFIX", 0.90)}
    )
    reresolve(db, right, 0.7)

    assert db.query("SELECT isin FROM news")["isin"].tolist() == ["INE_APOLLO"]
    # The score belonged to an attribution that turned out to be wrong.
    assert db.query("SELECT COUNT(*) c FROM news_sentiment")["c"].iloc[0] == 0


def test_month_windows_cover_the_range_without_overlap():
    windows = gdelt.month_windows(dt.date(2024, 1, 15), dt.date(2024, 3, 10))
    assert windows == [
        (dt.date(2024, 1, 15), dt.date(2024, 1, 31)),
        (dt.date(2024, 2, 1), dt.date(2024, 2, 29)),
        (dt.date(2024, 3, 1), dt.date(2024, 3, 10)),
    ]


def test_windows_before_gdelts_index_are_not_requested():
    windows = gdelt.month_windows(dt.date(2010, 1, 1), dt.date(2017, 2, 1))
    assert windows[0][0] == gdelt.GDELT_EPOCH


def test_seendate_is_read_as_utc_and_stored_as_ist():
    (article,) = gdelt.parse_artlist(
        '{"articles":[{"url":"https://x.com/a","title":"Tata Motors gains",'
        '"seendate":"20240510T233000Z","domain":"x.com"}]}',
        "INE_TM",
    )
    assert to_ist(article.published_at) == dt.datetime(2024, 5, 11, 5, 0)
    # seendate is a crawl time, at or after publication — recorded as such so
    # nobody later mistakes it for a byline.
    assert article.published_at_source == "GDELT_SEENDATE"


def test_a_throttled_window_is_retried_before_being_given_up(monkeypatch):
    # Measured live: 3 of 4 requests at the documented 5s spacing came back
    # 429. A single attempt per window would abandon most of a backfill.
    attempts = []

    def flaky(query, start, end, max_records):
        attempts.append(1)
        if len(attempts) < 3:
            raise gdelt.GdeltThrottledError("Please limit requests")
        return '{"articles":[]}'

    monkeypatch.setattr(gdelt, "_fetch_once", flaky)
    monkeypatch.setattr(gdelt.time, "sleep", lambda s: None)
    assert gdelt._fetch("q", dt.date(2024, 1, 1), dt.date(2024, 1, 31), 50) == (
        '{"articles":[]}'
    )
    assert len(attempts) == 3


def test_an_http_429_is_the_same_condition_as_the_text_scolding(monkeypatch):
    class Resp:
        status_code = 429
        text = "Please limit requests to one every 5 seconds"

    monkeypatch.setattr(gdelt.requests, "get", lambda *a, **k: Resp())
    with pytest.raises(gdelt.GdeltThrottledError):
        gdelt._fetch_once("q", dt.date(2024, 1, 1), dt.date(2024, 1, 31), 50)


def test_the_rate_limit_message_is_an_error_not_an_empty_month():
    # GDELT returns plain text with HTTP 200 when throttled. Reading that as
    # "no articles" would checkpoint the window and lose the month for good.
    with pytest.raises(gdelt.GdeltThrottledError):
        gdelt.parse_artlist("Please limit requests to one every 5 seconds", "INE_TM")


def test_a_throttled_window_is_retried_on_the_next_run(db, resolver):
    _seed_instrument(db, "INE_TM", "TATAMOTORS", "Tata Motors Ltd")
    calls = []

    def throttled(query, start, end, max_records):
        calls.append(start)
        return "Please limit requests to one every 5 seconds"

    gdelt.backfill_gdelt(db, ["INE_TM"], dt.date(2024, 1, 1), dt.date(2024, 1, 31),
                         delay=0, fetch=throttled)
    outstanding = gdelt.pending_windows(
        db, ["INE_TM"], dt.date(2024, 1, 1), dt.date(2024, 1, 31)
    )
    assert len(outstanding) == 1
    assert db.query(
        "SELECT status FROM news_backfill_log"
    )["status"].iloc[0] == "ERROR"


def test_a_completed_window_is_not_fetched_twice(db, resolver):
    _seed_instrument(db, "INE_TM", "TATAMOTORS", "Tata Motors Ltd")
    calls = []

    def ok(query, start, end, max_records):
        calls.append(start)
        return (
            '{"articles":[{"url":"https://x.com/a","title":"Tata Motors Q1 profit",'
            '"seendate":"20240110T100000Z","domain":"x.com"}]}'
        )

    for _ in range(2):
        gdelt.backfill_gdelt(db, ["INE_TM"], dt.date(2024, 1, 1), dt.date(2024, 1, 31),
                             delay=0, fetch=ok)

    # Six hours of requests must not restart from zero on a re-run.
    assert len(calls) == 1


def test_an_empty_month_is_checkpointed_as_a_result(db, resolver):
    _seed_instrument(db, "INE_TM", "TATAMOTORS", "Tata Motors Ltd")
    gdelt.backfill_gdelt(
        db, ["INE_TM"], dt.date(2024, 1, 1), dt.date(2024, 1, 31),
        delay=0, fetch=lambda *a: '{"articles":[]}',
    )
    assert gdelt.pending_windows(
        db, ["INE_TM"], dt.date(2024, 1, 1), dt.date(2024, 1, 31)
    ) == []


def test_the_query_is_a_quoted_phrase():
    # Unquoted, "Tata Motors" matches any article containing both words
    # anywhere — which for a two-token group name is most of the business page.
    assert gdelt.build_query("Tata Motors Ltd") == '"tata motors" sourcelang:english'


# ----------------------------------------------------------------------
# Marketaux
# ----------------------------------------------------------------------


def test_marketaux_entities_outside_the_universe_are_ignored():
    from stockanalysis.ingest.marketaux import parse_response

    payload = {
        "data": [
            {
                "uuid": "1",
                "title": "Reliance Q1 beats estimates",
                "description": "Refining margins improved.",
                "url": "https://example.com/ril",
                "published_at": "2024-05-10T04:30:00.000000Z",
                "source": "moneycontrol.com",
                "entities": [
                    {"symbol": "RELIANCE.NS", "sentiment_score": 0.42},
                    {"symbol": "XOM", "sentiment_score": -0.1},
                ],
            }
        ]
    }
    articles, sentiments = parse_response(payload, {"RELIANCE": "INE002A01018"})

    assert len(articles) == 1
    assert articles[0].entity_isins == ("INE002A01018",)
    assert sentiments[0][1] == "positive"


def _article(headline, published, url=None, source="example.com") -> Article:
    return Article(
        provider="RSS",
        source=source,
        url=url or f"https://example.com/{abs(hash(headline)) % 10**8}",
        headline=headline,
        body=None,
        published_at=published,
        published_at_source="FEED",
    )


def _seed_instrument(db, isin, symbol, name) -> None:
    db.upsert_df(
        "instruments",
        pd.DataFrame([{"isin": isin, "nse_symbol": symbol, "name": name}]),
        ["isin"],
    )
    db.upsert_df(
        "instrument_aliases",
        pd.DataFrame(
            [{"isin": isin, "alias": "tata motors", "source": "CURATED",
              "confidence": 0.95}]
        ),
        ["isin", "alias"],
    )


def _score_everything(db, model: str = "test-scorer") -> None:
    """Attach a score to every resolved row so as_of_sentiment's join sees it."""
    ids = db.query("SELECT news_id FROM news WHERE isin IS NOT NULL")["news_id"]
    if ids.empty:
        return
    db.upsert_df(
        "news_sentiment",
        pd.DataFrame(
            [
                {"news_id": i, "model": model, "label": "neutral", "score": 0.0,
                 "computed_at": dt.datetime.now()}
                for i in ids
            ]
        ),
        ["news_id", "model"],
    )
