-- StockAnalysis schema.
--
-- POINT-IN-TIME CONTRACT
-- ----------------------
-- Every table that feeds a backtest decision carries a *knowledge date*: the
-- date on which the row's contents first became publicly available.
--
--   prices_daily          knowledge date == date        (known at that close)
--   fundamentals_*        knowledge date == filing_date (NOT period_end_date)
--   shareholding          knowledge date == disclosed_date
--   news                  knowledge date == published_at
--   index_membership      explicit [from_date, to_date) interval
--
-- The backtest engine may only read rows whose knowledge date is <= the
-- decision date. See db/database.py::as_of_* helpers, which are the only
-- sanctioned read path. Bypassing them reintroduces lookahead bias silently.

CREATE TABLE IF NOT EXISTS instruments (
    isin            VARCHAR PRIMARY KEY,
    nse_symbol      VARCHAR,
    bse_code        VARCHAR,
    name            VARCHAR NOT NULL,
    sector          VARCHAR,
    industry        VARCHAR,
    listing_date    DATE,
    delisting_date  DATE,          -- NULL == still listed. Never delete rows.
    is_active       BOOLEAN DEFAULT TRUE
);

-- Survivorship-safe universe reconstruction.
-- A universe built from *today's* index constituents has silently deleted every
-- company that collapsed. This table stores membership as intervals so the
-- universe can be rebuilt as it actually stood on any past date.
-- to_date NULL == currently a member.
CREATE TABLE IF NOT EXISTS index_membership (
    index_name  VARCHAR NOT NULL,
    isin        VARCHAR NOT NULL,
    from_date   DATE NOT NULL,
    to_date     DATE,
    PRIMARY KEY (index_name, isin, from_date)
);

-- Records which index/date ranges we hold *verified historical* membership for.
-- Absent a row here, membership is assumed to be a current-constituents snapshot
-- and any backtest over that range is flagged survivorship-unsafe.
CREATE TABLE IF NOT EXISTS index_membership_coverage (
    index_name        VARCHAR NOT NULL,
    verified_from     DATE NOT NULL,
    verified_to       DATE NOT NULL,
    source            VARCHAR,
    loaded_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (index_name, verified_from)
);

CREATE TABLE IF NOT EXISTS prices_daily (
    isin          VARCHAR NOT NULL,
    date          DATE NOT NULL,
    open          DOUBLE,
    high          DOUBLE,
    low           DOUBLE,
    close         DOUBLE,          -- raw traded price
    adj_close     DOUBLE,          -- adjusted for splits/bonuses/dividends
    volume        BIGINT,
    traded_value  DOUBLE,          -- for liquidity / slippage modelling
    PRIMARY KEY (isin, date)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    isin        VARCHAR NOT NULL,
    ex_date     DATE NOT NULL,
    action_type VARCHAR NOT NULL,  -- SPLIT | BONUS | DIVIDEND | RIGHTS
    ratio       DOUBLE,
    details     VARCHAR,
    PRIMARY KEY (isin, ex_date, action_type)
);

CREATE TABLE IF NOT EXISTS filings (
    filing_id      VARCHAR PRIMARY KEY,
    isin           VARCHAR NOT NULL,
    doc_type       VARCHAR NOT NULL,   -- ANNUAL_REPORT | QUARTERLY_RESULT | ...
    fiscal_year    INTEGER,
    period_end     DATE,
    broadcast_date DATE NOT NULL,      -- knowledge date
    source_url     VARCHAR,
    local_path     VARCHAR,
    sha256         VARCHAR
);

CREATE TABLE IF NOT EXISTS fundamentals_annual (
    isin                    VARCHAR NOT NULL,
    fiscal_year             INTEGER NOT NULL,
    period_end_date         DATE NOT NULL,   -- what the numbers describe
    filing_date             DATE NOT NULL,   -- when they became knowable
    basis                   VARCHAR,         -- CONSOLIDATED | STANDALONE

    revenue                 DOUBLE,
    ebitda                  DOUBLE,
    pat                     DOUBLE,
    eps                     DOUBLE,
    ocf                     DOUBLE,
    fcf                     DOUBLE,
    capex                   DOUBLE,
    total_assets            DOUBLE,
    total_equity            DOUBLE,
    total_debt              DOUBLE,
    cash                    DOUBLE,
    interest_expense        DOUBLE,
    tax_expense             DOUBLE,
    contingent_liabilities  DOUBLE,
    auditor_opinion         VARCHAR,

    extraction_confidence   DOUBLE,
    source_filing_id        VARCHAR,
    PRIMARY KEY (isin, fiscal_year, basis)
);

CREATE TABLE IF NOT EXISTS fundamentals_quarterly (
    isin            VARCHAR NOT NULL,
    period_end_date DATE NOT NULL,
    filing_date     DATE NOT NULL,
    revenue         DOUBLE,
    pat             DOUBLE,
    eps             DOUBLE,
    source          VARCHAR,
    PRIMARY KEY (isin, period_end_date)
);

CREATE TABLE IF NOT EXISTS shareholding (
    isin                 VARCHAR NOT NULL,
    quarter_end          DATE NOT NULL,
    disclosed_date       DATE NOT NULL,   -- knowledge date
    promoter_pct         DOUBLE,
    promoter_pledged_pct DOUBLE,
    fii_pct              DOUBLE,
    dii_pct              DOUBLE,
    public_pct           DOUBLE,
    PRIMARY KEY (isin, quarter_end)
);

CREATE TABLE IF NOT EXISTS news (
    news_id      VARCHAR PRIMARY KEY,
    isin         VARCHAR,
    published_at TIMESTAMP NOT NULL,   -- knowledge date
    ingested_at  TIMESTAMP,
    headline     VARCHAR,
    body         VARCHAR,
    source       VARCHAR,
    url          VARCHAR
);

CREATE TABLE IF NOT EXISTS news_sentiment (
    news_id     VARCHAR NOT NULL,
    model       VARCHAR NOT NULL,
    label       VARCHAR,
    score       DOUBLE,
    computed_at TIMESTAMP,
    PRIMARY KEY (news_id, model)
);

CREATE TABLE IF NOT EXISTS factor_scores (
    isin          VARCHAR NOT NULL,
    as_of_date    DATE NOT NULL,
    factor_name   VARCHAR NOT NULL,
    raw_value     DOUBLE,
    sector_zscore DOUBLE,
    PRIMARY KEY (isin, as_of_date, factor_name)
);

CREATE TABLE IF NOT EXISTS signals (
    isin            VARCHAR NOT NULL,
    as_of_date      DATE NOT NULL,
    composite_score DOUBLE,
    signal          VARCHAR,
    red_flags       VARCHAR,
    narrative       VARCHAR,
    model_version   VARCHAR,
    PRIMARY KEY (isin, as_of_date)
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id       VARCHAR PRIMARY KEY,
    config_json  VARCHAR,
    started_at   TIMESTAMP,
    finished_at  TIMESTAMP,
    metrics_json VARCHAR,
    warnings     VARCHAR
);

CREATE TABLE IF NOT EXISTS backtest_positions (
    run_id     VARCHAR NOT NULL,
    as_of_date DATE NOT NULL,
    isin       VARCHAR NOT NULL,
    weight     DOUBLE,
    PRIMARY KEY (run_id, as_of_date, isin)
);

CREATE TABLE IF NOT EXISTS backtest_nav (
    run_id     VARCHAR NOT NULL,
    date       DATE NOT NULL,
    nav        DOUBLE,
    gross_nav  DOUBLE,
    costs_paid DOUBLE,
    PRIMARY KEY (run_id, date)
);

CREATE INDEX IF NOT EXISTS idx_prices_date ON prices_daily (date);
CREATE INDEX IF NOT EXISTS idx_fund_filing ON fundamentals_annual (filing_date);
CREATE INDEX IF NOT EXISTS idx_membership_dates ON index_membership (index_name, from_date);

-- ====================================================================
-- PHASE 1 — LLM extraction of annual reports
-- ====================================================================

-- Every extraction attempt, successful or not. This is the audit trail that
-- makes a factor traceable back to a page of a PDF, and the raw material for
-- the model bake-off. Never overwritten: a re-extraction adds a row.
CREATE TABLE IF NOT EXISTS extraction_attempts (
    attempt_id       VARCHAR PRIMARY KEY,
    filing_id        VARCHAR NOT NULL,
    isin             VARCHAR NOT NULL,
    fiscal_year      INTEGER,
    model            VARCHAR NOT NULL,
    run_label        VARCHAR,            -- 'bakeoff-...', 'backfill', ad hoc
    mode             VARCHAR,            -- SYNC | BATCH
    pages_sent       INTEGER,
    source_pages     VARCHAR,            -- which original pages the locator kept

    input_tokens          BIGINT,
    output_tokens         BIGINT,
    cache_read_tokens     BIGINT,
    cache_creation_tokens BIGINT,
    cost_usd              DOUBLE,
    latency_seconds       DOUBLE,

    confidence       DOUBLE,
    checks_json      VARCHAR,            -- full validator report
    payload_json     VARCHAR,            -- the extraction as returned
    error            VARCHAR,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Low-confidence extractions land here rather than silently poisoning factors.
CREATE TABLE IF NOT EXISTS extraction_review (
    attempt_id   VARCHAR PRIMARY KEY,
    filing_id    VARCHAR NOT NULL,
    isin         VARCHAR NOT NULL,
    fiscal_year  INTEGER,
    model        VARCHAR,
    confidence   DOUBLE,
    reasons      VARCHAR,
    status       VARCHAR DEFAULT 'PENDING',   -- PENDING | ACCEPTED | REJECTED
    queued_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at  TIMESTAMP,
    notes        VARCHAR
);

CREATE INDEX IF NOT EXISTS idx_attempts_filing ON extraction_attempts (filing_id);
CREATE INDEX IF NOT EXISTS idx_review_status ON extraction_review (status);

-- --------------------------------------------------------------------
-- Migrations for databases created before phase 1.
-- DuckDB has no schema-version table here; ADD COLUMN IF NOT EXISTS is
-- idempotent and keeps `init` safe to re-run.
-- --------------------------------------------------------------------

-- Whether broadcast_date is a real NSE timestamp or our conservative AGM-
-- deadline assumption. This decides whether a backtest's knowledge dates are
-- data or guesswork, so it is recorded rather than assumed.
ALTER TABLE filings ADD COLUMN IF NOT EXISTS broadcast_date_source VARCHAR;
ALTER TABLE filings ADD COLUMN IF NOT EXISTS page_count INTEGER;
ALTER TABLE filings ADD COLUMN IF NOT EXISTS bytes BIGINT;

ALTER TABLE fundamentals_annual ADD COLUMN IF NOT EXISTS total_liabilities DOUBLE;
ALTER TABLE fundamentals_annual ADD COLUMN IF NOT EXISTS total_expenses DOUBLE;
ALTER TABLE fundamentals_annual ADD COLUMN IF NOT EXISTS profit_before_tax DOUBLE;
ALTER TABLE fundamentals_annual ADD COLUMN IF NOT EXISTS other_income DOUBLE;
ALTER TABLE fundamentals_annual ADD COLUMN IF NOT EXISTS depreciation DOUBLE;
ALTER TABLE fundamentals_annual ADD COLUMN IF NOT EXISTS extraction_model VARCHAR;
ALTER TABLE fundamentals_annual ADD COLUMN IF NOT EXISTS extraction_attempt_id VARCHAR;
ALTER TABLE fundamentals_annual ADD COLUMN IF NOT EXISTS extracted_at TIMESTAMP;

-- The two legs of the consolidated profit identity. `pat` is the parent's share
-- alone, so without these the statement cannot be reconciled after the fact —
-- which is what let a correct RELIANCE extraction be discarded as a misread.
ALTER TABLE fundamentals_annual ADD COLUMN IF NOT EXISTS share_of_associates DOUBLE;
ALTER TABLE fundamentals_annual ADD COLUMN IF NOT EXISTS non_controlling_interest DOUBLE;

-- Which route produced the row: 'XBRL' for the exchange's own tagged filing,
-- 'LLM' for a model reading the annual-report PDF. They differ in more than
-- provenance — an XBRL row carries the results-filing broadcast date and needs
-- no confidence score, while an LLM row is dated to the annual report and can
-- be wrong in ways arithmetic does not catch. A backtest that cannot tell them
-- apart cannot tell you which of the two it is actually testing.
ALTER TABLE fundamentals_annual ADD COLUMN IF NOT EXISTS source VARCHAR;

-- Revenue and total income are different line items and the validators compare
-- them; storing only one of the pair means a persisted row cannot be
-- re-validated later without re-reading the source document.
ALTER TABLE fundamentals_annual ADD COLUMN IF NOT EXISTS total_income DOUBLE;

-- Every row written before `source` existed came from the model reading a PDF;
-- XBRL ingestion did not exist yet. Left NULL they would read as unattributed,
-- and the extraction step would treat them as not-yet-covered and pay to redo
-- them. Idempotent: after the first run there is nothing left to set.
UPDATE fundamentals_annual SET source = 'LLM' WHERE source IS NULL;

-- ====================================================================
-- PHASE 1b — free NSE fundamentals (no LLM required)
-- ====================================================================

-- Knowledge dates for quarterly results. NSE's financial-results filing index
-- carries a real broadcast timestamp, so where we have it the quarterly rows
-- stop relying on the SEBI 45-day fallback.
ALTER TABLE fundamentals_quarterly ADD COLUMN IF NOT EXISTS filing_date_source VARCHAR;
ALTER TABLE fundamentals_quarterly ADD COLUMN IF NOT EXISTS relating_to VARCHAR;
ALTER TABLE fundamentals_quarterly ADD COLUMN IF NOT EXISTS is_consolidated BOOLEAN;
ALTER TABLE fundamentals_quarterly ADD COLUMN IF NOT EXISTS is_audited BOOLEAN;
ALTER TABLE fundamentals_quarterly ADD COLUMN IF NOT EXISTS xbrl_url VARCHAR;

-- NSE's financial-results filing index, kept in its own right rather than only
-- folded into `fundamentals_quarterly`.
--
-- The index reaches back as many years as it is asked for and carries an XBRL
-- link on essentially every entry. `fundamentals_quarterly` cannot hold that:
-- its rows come from `results_comparison`, which returns only about five
-- quarters, so an update-only apply attached those links to nothing and every
-- year but the newest was fetched and dropped. That capped the annual XBRL path
-- at a single fiscal year per company regardless of how far back the index was
-- walked.
--
-- Keyed on basis as well as period, because a company files consolidated and
-- standalone results for the same period as two separate entries. Under
-- `fundamentals_quarterly`'s (isin, period_end) key one silently overwrites the
-- other, which decides the basis by broadcast order rather than by preference —
-- RELIANCE FY2024 landed on standalone by seven minutes that way.
CREATE TABLE IF NOT EXISTS results_filings (
    isin            VARCHAR NOT NULL,
    period_end_date DATE NOT NULL,
    basis           VARCHAR NOT NULL,
    broadcast_date  DATE,
    relating_to     VARCHAR,
    is_consolidated BOOLEAN,
    is_audited      BOOLEAN,
    xbrl_url        VARCHAR,
    PRIMARY KEY (isin, period_end_date, basis)
);

ALTER TABLE shareholding ADD COLUMN IF NOT EXISTS disclosed_date_source VARCHAR;
ALTER TABLE shareholding ADD COLUMN IF NOT EXISTS employee_trust_pct DOUBLE;

-- ====================================================================
-- PHASE 2 — factor model
-- ====================================================================

-- Red flags DESIGN §6.2 lists but no ingested source can evaluate — promoter
-- pledge (NSE.shareholding() carries no pledged-shares figure) and credit
-- rating downgrades (nothing ingests ratings). Stored per signal rather than
-- documented once, because "this company tripped nothing" and "this company
-- tripped nothing we were able to check" are different statements and only one
-- of them is true here.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS unknown_flags VARCHAR;

-- Fraction of the model's total factor weight actually backed by data for this
-- company on this date. A composite built from 15% of the model is not a worse
-- estimate of the same thing — it is an estimate of something else, and the
-- number that says so belongs next to the score.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS coverage DOUBLE;

CREATE INDEX IF NOT EXISTS idx_signals_date ON signals (as_of_date);
CREATE INDEX IF NOT EXISTS idx_factor_scores_date ON factor_scores (as_of_date, factor_name);

-- ====================================================================
-- PHASE 3 — news and sentiment
-- ====================================================================

-- One `news` row is one (article, company) pair, not one article. A headline
-- naming three companies produces three rows sharing an `article_id`, because
-- the sentiment factor asks "what was said about this ISIN" and a nullable
-- single-isin column cannot answer that for a multi-company story. The article
-- text is scored once — the scorer keys its cache on `content_hash` — so the
-- duplication costs storage, not inference.
--
-- Articles whose company could NOT be resolved are stored too, with isin NULL.
-- They are excluded from every factor (as_of_sentiment filters on isin) but
-- they are the denominator of the resolution rate, and keeping their text means
-- a better resolver can be re-run over history without re-fetching anything.
ALTER TABLE news ADD COLUMN IF NOT EXISTS article_id VARCHAR;
ALTER TABLE news ADD COLUMN IF NOT EXISTS content_hash VARCHAR;
ALTER TABLE news ADD COLUMN IF NOT EXISTS provider VARCHAR;         -- RSS | MARKETAUX | GDELT
ALTER TABLE news ADD COLUMN IF NOT EXISTS published_at_source VARCHAR;
ALTER TABLE news ADD COLUMN IF NOT EXISTS resolution_method VARCHAR;
ALTER TABLE news ADD COLUMN IF NOT EXISTS resolution_confidence DOUBLE;
ALTER TABLE news ADD COLUMN IF NOT EXISTS matched_alias VARCHAR;
ALTER TABLE news ADD COLUMN IF NOT EXISTS matched_in VARCHAR;       -- HEADLINE | BODY

-- NO SECONDARY INDEXES ON `news`. They were here, and they made the table
-- undeletable.
--
-- `reresolve` replaces the rows for an article whose attribution changed. On
-- the live 922-row database that DELETE died with
--
--   FATAL Error: Failed to delete all rows from index.
--   Only deleted 35 out of 61 rows.
--
-- and took the connection with it. DuckDB's ART indexes do not store NULLs, so
-- an index over a nullable column holds fewer entries than the table has rows,
-- and a delete that spans NULL and non-NULL rows finds fewer index entries
-- than it expects. Every column worth indexing here — `isin` on unresolved
-- articles, `content_hash` on anything written before phase 3 — is nullable.
--
-- Nothing is lost. `news` is thousands of rows against DuckDB's columnar
-- scans, and the reads are all aggregate or windowed anyway; these indexes
-- were speculative and cost a working write path.
DROP INDEX IF EXISTS idx_news_published;
DROP INDEX IF EXISTS idx_news_isin;
DROP INDEX IF EXISTS idx_news_content;
DROP INDEX IF EXISTS idx_news_sentiment_model;

-- Headline text -> ISIN. Built from `instruments` plus a curated file; see
-- news/aliases.py for why an alias that maps to more than one company is
-- deleted rather than disambiguated.
CREATE TABLE IF NOT EXISTS instrument_aliases (
    isin       VARCHAR NOT NULL,
    alias      VARCHAR NOT NULL,     -- normalised: lowercase, punctuation stripped
    source     VARCHAR,              -- SYMBOL | NAME | NAME_SHORT | CURATED
    confidence DOUBLE,
    built_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (isin, alias)
);

-- Which (provider, company, window) fetches have already happened. GDELT asks
-- for one request per company per month and rate-limits to one request every
-- five seconds; a 100-company, 3-year backfill is ~3,600 requests and five
-- hours. Re-running it must not start over, so every window is checkpointed as
-- it completes — including the empty ones, which are a result, not a gap.
CREATE TABLE IF NOT EXISTS news_backfill_log (
    provider     VARCHAR NOT NULL,
    isin         VARCHAR NOT NULL,
    window_start DATE NOT NULL,
    window_end   DATE NOT NULL,
    articles     INTEGER,
    status       VARCHAR,            -- OK | ERROR
    detail       VARCHAR,
    fetched_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider, isin, window_start)
);
