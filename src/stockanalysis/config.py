"""Configuration. Everything tunable lives here, loaded from env / .env."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    data_dir: Path = Path("./data")
    db_path: Path = Path("./data/stockanalysis.duckdb")

    # Ingest politeness. NseIndiaApi and yfinance are unofficial wrappers over
    # public endpoints; the documented guidance is 0.5-1s between requests and
    # bulk work after market hours.
    request_delay_seconds: float = 1.0
    max_retries: int = 4

    default_index: str = "NIFTY100"

    # Backtest
    rebalance_freq: str = "M"
    top_n: int = 20

    # ----------------------------------------------------------------
    # Phase 1 — annual-report extraction
    # ----------------------------------------------------------------
    # ANTHROPIC_API_KEY is deliberately absent: the anthropic SDK resolves its
    # own credentials (env var, or an `ant auth login` profile), and shadowing
    # that here would mean one more place for a stale key to hide.

    # Also selects the *backend*, via the prefix `extract.factory` reads:
    # bare for the Developer Platform API, `cli:` for the Claude Code CLI,
    # `local:` for LM Studio. One setting rather than a separate backend flag,
    # because the two are never chosen independently — and because everything
    # that resolves a model from configuration goes through `make_extractor`,
    # so `SA_EXTRACTION_MODEL=cli:claude-opus-5` is the whole switch.
    #
    # The batch commands are the exception and cannot honour a prefix: neither
    # the CLI nor LM Studio has a Batch API. They refuse rather than silently
    # falling back to the API, which would spend the wrong balance.
    extraction_model: str = "cli:claude-opus-5"

    # The section locator narrows a 200-400pp report to roughly this many pages.
    # Cost scales linearly with it; extraction accuracy falls off a cliff if the
    # notes get truncated, so this is a floor-and-ceiling, not a target.
    extraction_max_pages: int = 60

    # The API rejects requests over 32MB. Stay under it with room for the
    # base64 expansion (~4/3) plus the prompt.
    extraction_max_pdf_mb: float = 22.0

    # Caps thinking + response together. The JSON payload is ~1KB; the rest is
    # headroom for adaptive thinking on a dense set of financial statements.
    extraction_max_tokens: int = 16000
    extraction_timeout_seconds: float = 900.0

    # Rows at or above this persist to fundamentals_annual. Below it they go to
    # the review queue only. 1.0 means "every validator passed".
    min_persist_confidence: float = 0.6

    # How many years of annual reports to pull per company.
    filing_years: int = 3

    # ----------------------------------------------------------------
    # Phase 3 — news and sentiment
    # ----------------------------------------------------------------

    # RSS is the backbone: free, unlimited, best India coverage, and no key.
    # It has no history at all — a feed returns the last 15-50 items — so it
    # supports live scoring and cannot support a backtest. That is what GDELT
    # is for.
    news_feeds: list[str] = [
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
        "https://www.moneycontrol.com/rss/marketreports.xml",
        "https://www.moneycontrol.com/rss/results.xml",
        "https://www.moneycontrol.com/rss/business.xml",
        "https://www.livemint.com/rss/markets",
        "https://www.livemint.com/rss/companies",
        "https://www.business-standard.com/rss/markets-106.rss",
        "https://www.business-standard.com/rss/companies-101.rss",
    ]

    # Entity-tagged, so it needs no resolver — but the free tier is 100
    # requests/day, which is a supplement to RSS rather than a replacement.
    marketaux_api_key: str | None = None
    marketaux_base_url: str = "https://api.marketaux.com/v1/news/all"

    # GDELT asks for one request every five seconds and returns HTTP 429 with a
    # plain-text scolding when you do not. Slower than the NSE default on
    # purpose — this is their documented figure, not a guess.
    gdelt_delay_seconds: float = 6.0
    gdelt_max_records: int = 250
    gdelt_base_url: str = "https://api.gdeltproject.org/api/v2/doc/doc"

    # A mention below this is stored with its ISIN attached but is not scored
    # and not read by the factor. 0.7 keeps symbol and full-name matches and
    # drops the single-token guesses.
    news_min_resolution_confidence: float = 0.7

    sentiment_model: str = "ProsusAI/finbert"
    sentiment_batch_size: int = 32

    # ----------------------------------------------------------------
    # Phase 4 — serving
    # ----------------------------------------------------------------

    narrative_model: str = "cli:claude-opus-5"

    # Effort, not a token budget: `budget_tokens` is rejected on this model
    # family. A narrative restates a computed score in two sentences and needs
    # no deliberation, so the floor is the right setting — and it is the lever
    # that actually controls cost here.
    narrative_effort: str = "low"

    # Caps thinking *and* response together. Thinking is on by default on Opus 5
    # and cannot be disabled without capping effort, so a budget sized to the
    # ~120-token answer alone would truncate mid-sentence.
    narrative_max_tokens: int = 1000

    # Concurrency for the narrative pass. Modest on purpose: the first call is
    # issued alone to write the prompt cache, and the gain from more workers is
    # small next to the rate-limit risk.
    narrative_max_workers: int = 4

    narrative_news_window_days: int = 30

    # ----------------------------------------------------------------
    # Serving database — Postgres, fed one way from DuckDB
    # ----------------------------------------------------------------
    # DuckDB stays the engine: ingest, scoring, backtests and the Streamlit
    # console all run on it. Postgres holds a *published copy* of the tables the
    # API reads, rebuilt wholesale by `stockanalysis publish`. Nothing else
    # writes to it, so it behaves like a cache and can always be dropped and
    # recreated from DuckDB. Everything below is inert until `pg_dsn` is set.

    # A secret because a DSN usually carries a password, and a settings object
    # is exactly the kind of thing that ends up in a log line or a traceback.
    # Use a direct (session) connection string: the API keeps its own pool and
    # sets `search_path` per connection, which a transaction-mode pooler would
    # not preserve between statements.
    pg_dsn: SecretStr | None = None

    # The schema the API reads. A publish loads `<schema>_release_<timestamp>`
    # beside it and renames it into place, so this name always means "the live
    # release". Restricted to a plain lowercase identifier because publish
    # composes schema names from it, and kept off `public` because publish
    # renames and drops schemas — see `db.publish`.
    pg_schema: str = Field(default="site", pattern=r"^[a-z][a-z0-9_]{0,39}$")

    # Releases kept after a publish, besides the live one, for
    # `publish --rollback`. Each is a full copy, prices included, so this is
    # the knob that decides how much disk a rollback window costs.
    pg_keep_releases: int = Field(default=1, ge=0)

    # Which database the API reads. `duckdb` changes nothing and is the
    # default; `postgres` needs `pg_dsn` and at least one publish. The
    # Streamlit console always reads DuckDB — it is the operator's view of the
    # engine, not of what the site serves.
    serve_backend: Literal["duckdb", "postgres"] = "duckdb"

    # The API's Postgres pool. The timeout is how long a request waits for a
    # connection before it is answered with a 503 — short, because a client
    # left hanging for the library default of 30s is worse than a fast error.
    pg_pool_max_size: int = Field(default=10, ge=1)
    pg_pool_timeout_seconds: float = 5.0

    # ----------------------------------------------------------------
    # The public API — what the site and any CDN in front of it see
    # ----------------------------------------------------------------
    # How long a successful response may be reused, by a browser or a CDN.
    # The data moves once a day, when a pipeline run publishes, so minutes of
    # staleness cost nothing and spare the database every repeat page view.
    # 0 marks everything `no-store`. Errors are never cacheable whatever this
    # says: a 503 during a publish must not outlive the publish.
    api_cache_seconds: int = Field(default=300, ge=0)

    # Browser origins allowed to call the API directly, as a JSON list:
    # SA_API_CORS_ORIGINS='["https://example.com"]'. Empty by default, which
    # leaves server-to-server calls (the site rendering a page) unaffected and
    # refuses every browser page served from anywhere else.
    api_cors_origins: list[str] = []

    @field_validator("pg_schema")
    @classmethod
    def _not_a_shared_schema(cls, value: str) -> str:
        if value == "public" or value.startswith("pg_"):
            raise ValueError(
                f"SA_PG_SCHEMA={value!r} names a schema publish does not own; it "
                f"renames and drops the schema it is given. Use a dedicated name."
            )
        return value

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "cache").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "filings").mkdir(parents=True, exist_ok=True)


class CostModel(BaseSettings):
    """Indian equity delivery-segment transaction costs.

    Rates as of 2026. All values are fractions of turnover unless noted.
    Ignoring these is one of the most common ways to produce a backtest that
    cannot be traded, so they are on by default and must be explicitly zeroed.
    """

    model_config = SettingsConfigDict(env_prefix="SA_COST_", extra="ignore")

    # Securities Transaction Tax — delivery equity, charged on both legs.
    stt_buy: float = 0.001
    stt_sell: float = 0.001

    # NSE exchange transaction charge.
    exchange_txn: float = 0.0000297

    # SEBI turnover fee.
    sebi_turnover: float = 0.000001

    # Stamp duty — buy side only.
    stamp_duty_buy: float = 0.00015

    # Discount-broker delivery brokerage. Many are zero-brokerage on delivery;
    # default assumes a flat cap per order.
    brokerage_pct: float = 0.0
    brokerage_flat_per_order: float = 0.0

    # GST applies to brokerage + exchange + SEBI fees (not to STT or stamp duty).
    gst: float = 0.18

    # Slippage in basis points of turnover. Scaled up for illiquid names by the
    # engine via participation rate.
    base_slippage_bps: float = 5.0

    # If a trade is this fraction of median daily traded value, slippage doubles.
    participation_penalty_threshold: float = 0.01


settings = Settings()
cost_model = CostModel()
