# Phase 3 — findings from building and running news and sentiment

**Date:** 2026-08-14
**Scope:** DESIGN §9 phase 3 — RSS + Marketaux ingest, ticker resolution,
FinBERT scoring, and the GDELT historical backfill "so the sentiment factor can
be backtested rather than assumed".

**Result:** the scoring half works end to end; the history half is throttled.
FinBERT runs locally, scores correctly, and the factor reads it through the
point-in-time path — the sentiment family computes on live data for the first
time. But **GDELT's public API sustains about 12 company-months an hour**,
which makes a Nifty-100 × 3-year backfill a two-week job rather than an
afternoon, and until it runs the sentiment factor is still not backtestable.

Six defects found and fixed, five of them in code written this phase. Three
were found by reading the data rather than by a failing test, which is the
pattern worth noting: **every rule in this phase that turned out to be wrong
was wrong in a way the test suite could not see.** Test suite went 209 → 281.

---

## 1. What the data made me change

### 1.1 A roundup article is not news about a company

**Severity:** high — the difference between a sentiment factor and a random
number generator with a plausible sign.
**Found by:** reading the first 79 live FinBERT scores instead of the summary.

The top and bottom of that list:

```
positive  +0.93  APOLLOHOSP  Apollo Hospitals beats estimates for Q1FY27, net profit up 34%
positive  +0.93  RELIANCE    Reliance Industries, Adani Enterprises among 10 stocks with...
positive  +0.93  BAJFINANCE  Reliance Industries, Adani Enterprises among 10 stocks with...
...
negative  -0.97  TMPV        Tata Motors PV Q1 PAT tumbles 80% YoY to Rs 775 cr
negative  -0.97  HINDALCO    Top Gainers & Losers on 13 August: Apar Industries, Hindalco...
```

The first and fourth are exactly what the factor is for. The others are not,
and they are not resolver errors — every one of those mentions is real. The
problem is that **a document-level sentiment score is only attributable when
the document is about one thing.** "Top Gainers & Losers" scored −0.97 and
handed that number to Hindalco, which was in the *gainers* half. Six companies
got an identical +0.93 for appearing in a list of DII purchases.

No improvement to attribution fixes this, because the attribution is correct.

**Fix:** an article naming more than three companies is a roundup, and its
mentions are demoted to 0.5 — stored and counted, below the 0.7 threshold,
never scored and never read. Three, because genuine two-company stories are
common ("Tata Motors gains as JLR margins beat") and genuine four-company ones
are not.

**Cost, measured:** 64 of 257 attributed rows are demoted this way — a quarter
of the resolver's output, discarded on purpose. It is the largest single
coverage loss in the phase and the right trade: a factor that averages five
wrong signs with three right ones is worse than one that reports nothing.

**Known residual.** The rule counts companies *in the universe*, so "Top
Gainers & Losers: Apar Industries, Hindalco, Ather Energy, Force Motors" still
attributes to Hindalco — the other three are not Nifty 100 and do not count
towards the limit. A universe-independent signal (comma-separated proper nouns,
or the handful of recurring headline templates) would catch it. Left as a
measured limitation rather than a guess at a rule.

---

### 1.2 The read path did not enforce the attribution threshold

**Severity:** high — a retired attribution kept feeding the factor.
**Where:** `db/database.py::as_of_sentiment`

Found while fixing §1.1. The confidence threshold was enforced in
`pending_news`, at scoring time. That is fine forwards and wrong backwards: a
row scored *before* it was demoted keeps its `news_sentiment` row, and
`as_of_sentiment` joined on `news_id` alone. Demoting a roundup would have left
its −0.97 in the factor permanently.

**Fix:** `as_of_sentiment` filters on `resolution_confidence` itself — one
filter, in the read path, beside the knowledge-date filter that exists for the
same reason. What the write path did earlier cannot leak through.

---

### 1.3 "Bank of Baroda" generated the alias `bank of`

**Severity:** high for precision — seven false attributions from one string.
**Found by:** a manual audit of all 58 live attributions.

The two-token name-prefix rule (§2.3) took "Bank of Baroda" → `bank of`, which
then matched Bank of America, Bank of Korea, Bank of Maharashtra and two Bank
of Japan stories, all filed under BANKBARODA at 0.90 confidence.

**Fix:** a generated alias may not end on a joining word. `bank of` reduces to
`bank`, a single common word, and is refused. "State Bank of India" still
yields `state bank`, which is fine.

---

### 1.4 Body-only mentions were wrong eight times out of nine

**Severity:** high for precision.
**Found by:** the same audit.

Body-only mentions started at a 0.85× discount, which kept a strong alias above
the usable threshold. The audit disagreed: of nine body-only attributions,
eight named a company the article was not about — Airtel in a story about
Singtel's results, Kotak Mahindra as the bank running someone else's IPO, Tata
Capital in a story about Tata Motors' share price, NTPC in a story about Time
Technoplast. The single correct one was a company the headline named by an
acronym the alias table lacks, which is a recall problem to fix in the alias
table rather than a reason to keep the other eight.

**Fix:** the multiplier is now 0.70, which puts every body-only mention —
including a curated alias at 0.95 — below the threshold. Attribution is
effectively headline-only. The rows are still stored, so if a later measurement
says this was too strict it costs a re-resolution, not a re-fetch.

**Measured effect of 1.3 and 1.4 together:** the same audit repeated after the
fixes. Before: 58 usable RSS attributions, 15 of them wrong (74% precision).
After: 31 usable, one arguable (Airtel Payments Bank, an associate rather than
the listed entity). The cost is recall, and it is visible — 58 → 31.

---

### 1.5 The provider-search filter ran before the roundup rule

**Severity:** medium — it disabled §1.1 for every GDELT article.
**Where:** `news/store.py::_mentions_for`

GDELT articles were filtered to the queried company *before* the roundup check
counted companies. A query for ABB returned "Stocks to Watch Today: Vedanta,
Hindustan Zinc, TCS, Tata Power, ABB, LIC, Bajaj Finance, RVNL", the filter
reduced it to one mention, and a one-mention article is not a roundup by any
rule. It went into the table at 0.90.

**Fix:** demote first, filter second. While fixing it the two paths were also
unified: attribution is now **always by text**, and the provider's query is
used only to count `unconfirmed`. That is what makes `reresolve` — which has no
record of what was searched for — able to reproduce what the ingest did.

---

### 1.6 DuckDB: indexes on nullable columns made `news` undeletable

**Severity:** critical for the phase — the command could not run at all.

`reresolve` deletes the rows it replaces. On the live 922-row database that
delete died with:

```
FATAL Error: Invalid Input Error: Failed to delete all rows from index.
Only deleted 35 out of 61 rows.
```

and took the connection with it. The transaction rolled back cleanly, which is
the only reason this was a bug report rather than a restore.

The cause is the three secondary indexes this phase added to `news`. DuckDB's
ART indexes do not store NULLs, so an index over a nullable column holds fewer
entries than the table has rows, and a delete spanning NULL and non-NULL rows
finds fewer index entries than it expects. Every column worth indexing here is
nullable — `isin` on unresolved articles, `content_hash` on anything written
before phase 3.

**Fix:** the indexes are dropped, with a `DROP INDEX IF EXISTS` migration so
existing databases repair themselves on `init`. Nothing is lost: `news` is
thousands of rows against DuckDB's columnar scans, and the reads are aggregate
or windowed anyway. The indexes were speculative and cost a working write path.

**On the test that does not reproduce it.** The regression test builds 300
unattributed articles in a file-backed database and re-resolves them. It passes
either way — the failure needs the real table's history of many separate write
transactions, and does not reproduce in `:memory:` at all. It is kept as a
smoke test over the delete path at scale, with its limits written into the
docstring, because a test that documents what it does *not* prove is worth more
than a deleted one.

---

### 1.7 Two bugs in the re-resolution bookkeeping

`reresolve` re-runs the resolver over stored article text so that improving the
alias table costs seconds rather than another pass over the sources. It got its
change detection wrong twice, in opposite directions:

- **Compared `{None}` against `{nan}`.** A SQL NULL arrives from pandas as
  `float('nan')`, so every unresolved article looked changed: "215 changed, 0
  newly resolved, 192 lost".
- **Then compared ISINs only.** After that fix it reported "0 changed" for
  §1.1's roundup demotion — which changes a mention's confidence without
  changing which company it names — and left every stale confidence in place.

Both were invisible except in the summary line, and the second one *looked like
success*. The fix compares the whole attribution, `(isin, method, confidence)`,
and the guard is an idempotency assertion: run it twice, the second run must
report nothing to do.

A third instance of the same family: `reresolve` did not apply the syndication
dedupe, so an article that only became resolvable when the alias table improved
could duplicate a story another article already carried. Two Moneycontrol URLs
for the same "Tata Consumer Q4 net profit falls 19%" story got in that way.
`reresolve` now seeds the claimed-story set before rewriting and sweeps
pre-existing duplicates.

**The common thread across 1.7 and 1.3's `isinstance` bug is pandas types not
being the Python types they resemble.** `pd.Timestamp` *subclasses*
`datetime.date`, so an `isinstance(x, dt.date)` guard keeps the Timestamp,
which then compares unequal to every `date` in a set key — silently disabling
both the article dedupe and the GDELT window checkpointing, the second of which
would have doubled a job already measured in days. And a nullable text column
arrives as `float('nan')`, which `(value or "")` does not catch; that one
surfaced three times as `AttributeError: 'float' object has no attribute
'strip'`. Both now have one helper each and no isinstance checks.

---

## 2. Ticker resolution: what precision costs

DESIGN §3.3 calls this "its own small problem" and it is where the phase's
judgement lives. An unresolved article costs recall and can be recovered later
from stored text; a *mis*-resolved one is indistinguishable from data once it
is in the table.

### 2.1 The alias table refuses more than people expect

258 aliases over 100 companies: registered name (87), NSE symbol (83), curated
(39), two-token name prefix (24), first-token short form (25, deliberately
below threshold).

**Five aliases were dropped for being claimed by two companies:**

| Alias | Claimed by |
|---|---|
| `tata motors` | Tata Motors **and** Tata Motors Passenger Vehicles |
| `siemens` | Siemens Ltd **and** Siemens Energy India |
| `sbi` | State Bank of India **and** SBI Life |
| `hindustan` | Hindustan Unilever, Hindustan Aeronautics, Hindustan Petroleum |
| `bharat` | Bharat Electronics **and** Bharat Petroleum |

The first two are corporate actions inside the backtest window — the Tata
Motors and Siemens Energy demergers both completed in 2025 — and neither was
known to me when the curated table was written. `tata motors` was *in* that
table as a hand-entered alias, and the build-time conflict check overrode it.
So the company most likely to be in the news is now unresolvable by name, which
is a real coverage hole and the correct answer: "Tata Motors" in a 2026
headline is genuinely ambiguous between two listed entities.

`hdfc` is blocked by rule rather than by conflict: until the July 2023 merger
it named HDFC Ltd, and after it, HDFC Bank. Aliases carry no validity dates, so
a backtest spanning the merger cannot have a single mapping and is not offered
one.

### 2.2 Two of my own curated aliases were wrong, and a test caught them

`nestle` → Nestle India and `lic` → LIC both shipped in the first draft of
`CURATED_ALIASES`. The first matches the Swiss parent's news as often as the
Indian subsidiary's; the second collides with LIC Housing Finance. Both were
caught by a test asserting that single-token short forms stay below the usable
threshold — written to check the *rule*, and it found the exceptions I had
hand-written above it.

### 2.3 Registered names are not what newsrooms write

The first live RSS pull resolved 18% of articles. Reading the misses showed the
systematic half immediately:

```
Apollo Hospitals beats estimates for Q1FY27      instruments: Apollo Hospitals Enterprise Ltd.
Godrej Consumer among 4 F&O stocks with...       instruments: Godrej Consumer Products Ltd.
Tata Consumer Q4 net profit falls 19%...         instruments: Tata Consumer Products Ltd.
```

Hence the two-token prefix rule, at 0.90 — enough tokens to clear the
group-name problem, since "tata consumer" names one company where "tata" names
a dozen, with the conflict pass catching what it does not. (And §1.3 for what
it cost before it was constrained.)

The *rest* of the gap is not a gap. A sample of 40 unresolved headlines was
dominated by companies outside the Nifty 100 (Rajputana Stainless, Khazanchi
Jewellers, Lenskart), IPO and mutual-fund coverage, macro ("Sensex, Nifty extend
gains to 3rd day"), and non-India stories. Against general market feeds, most
articles *should* resolve to nothing for a 100-company universe; a rate near
20% is the expected shape, and the number worth watching is misses within the
universe, not the rate.

---

## 3. FinBERT: the cheapest thing in the system that works

110M parameters, running on this laptop's MPS backend, 104 rows in about five
seconds after a one-off 440MB download. DESIGN §5.5's argument — that a
frontier API model would be strictly worse economics for no measurable gain —
holds, and the cost is not close to worth optimising.

**The label order is the trap.** FinBERT's `id2label` is
`{0: positive, 1: negative, 2: neutral}`: not alphabetical, and not the
negative-first order most classifiers use. Code that assumes an index inverts
the entire factor, and nothing downstream would notice — the backtest still
runs, the score still moves, and every conclusion is backwards. So the mapping
is read from `model.config.id2label` at load, a model without explicit polarity
labels is refused, and an end-to-end test runs the real model over three
headlines and asserts the signs.

The signed convention, `P(positive) − P(negative)`, lives in exactly one place
because `factors/sentiment.py` documents that it expects a signed score.

On real headlines the model is convincing wherever the article is about one
company. Its failures are the roundups, now excluded structurally rather than
by hoping the model notices. Of 240 scored rows: 110 neutral, 72 positive, 58
negative.

---

## 4. GDELT: the backfill is a two-week job, not a command

This is the finding that matters, because DESIGN §9 puts GDELT in the phase
*specifically* so the sentiment factor can be backtested rather than assumed.

### 4.1 Measured throughput

The DOC 2.0 API documents "one request every 5 seconds". Measured from this IP
on 2026-08-13/14:

| Request spacing | Successful | Attempted |
|---|---|---|
| 6s | 1 | 4 |
| 12s | 0 | 4 |
| 20s | 0 | 4 |
| 6s, inside the real client | 1 | 12 |
| with 4 retries per window | 4 windows | 16 windows, 19 min |

Failures are HTTP 429 with a plain-text body asking for one request every five
seconds — while the requests *are* five or more seconds apart. Slowing down did
not help, and response times of 11–17 seconds on successes and failures alike
point at a loaded shared backend rather than at our pacing.

Sustained rate with retries: **~12 completed company-months per hour.**

### 4.2 What that means

```
  Nifty 100 x 3 years   3,600 windows  ->  ~12 days of wall clock
  Nifty 100 x 1 year    1,200 windows  ->  ~4 days
```

DESIGN budgets one week for all of phase 3. The collection alone is longer than
that, and it is calendar time rather than work.

**What the code does about it.** Every (company, month) window is checkpointed
in `news_backfill_log` as it completes, including the empty ones, so the job is
interruptible and resumes rather than restarting. Throttled windows are retried
four times with a widening gap and, if they still fail, left *unrecorded* for
the next run — a throttle must never be checkpointed as "no articles", which
would lose that month permanently. That is the difference between something
that can run overnight across several nights and something that cannot run at
all.

### 4.3 What the completed windows show

Four windows completed, returning **661 articles**. The yield is not the
problem:

- Asian Paints averaged ~15 confirmed articles a month over four months —
  comfortably above the three the factor needs.
- Of 644 stored GDELT articles, **162 rows resolve to a company at usable
  confidence and 526 resolve to nobody.** GDELT's full-text index matches
  paraphrases, related coverage, and homonyms; the article's own title is the
  only evidence available, since the artlist response carries no body.

That 25% attribution rate is the justification for never trusting a provider's
search terms. Accepting them would have turned every article GDELT returned for
a query into evidence about that company, by construction — a dataset that
agrees with itself.

---

## 5. RSS: works, and cannot backfill

Nine feeds across four outlets; 274 articles fetched, 246 unique.

**Two outlets have opposite bot rules.** Moneycontrol's edge returns 403 to any
browser User-Agent and 200 to `python-requests/*`; Business Standard does the
reverse. A single header silently loses three of nine feeds — the first live
run reported three failures that would have been easy to write off as an
outage. The client identifies itself as the tool and falls back once on a 403.

**One feed served 17 stale items** dated April 2024 alongside current ones.
They were stored at their real publication dates, which is correct and worth
stating: a feed's contents are not evidence about today.

The structural limit is the one DESIGN names. A feed carries 15–50 current
items and no history, so RSS builds an archive **going forward** at roughly 30
usable attributed articles a day. After a year of daily runs it has a year of
history and not before.

Marketaux is implemented and unrun — it needs a key, and its free tier (100
requests/day × 3 articles) is a supplement rather than a source. Its per-entity
sentiment is stored under model `marketaux` alongside FinBERT's so the two can
be compared on identical text when a key exists.

---

## 6. Where this leaves the sentiment factor

The factor needs three articles for a company in the 30 days before a decision
date. Measured against the data now in the database:

| Decision date | Articles in window | Companies with news | With ≥3 | Factor computable |
|---|---|---|---|---|
| 2025-09-30 | 35 | 14 | 2 | 2 / 100 |
| 2025-11-30 | 75 | 25 | 7 | **7 / 100** |
| 2026-08-13 (RSS, one day) | 27 | 19 | 1 | 1 / 100 |

The 2025-11-30 row is the informative one: it is what a **partial backfill of
20 companies** buys, and it is the first date in this project's history on
which the sentiment factor produces a number for anyone. Scaled to the full
universe and a full year, the same yield would put most large caps over the
threshold — the blocker is collection time, not coverage per company.

The composite run for today reports the state plainly:

```
  universe 100   scored 98   median coverage 23%
  families with data: growth 85%, momentum 100%, quality 0%, sentiment 1%, value 0%
```

| | State |
|---|---|
| Ingest | Working — RSS live, GDELT built and throttled, Marketaux unrun |
| Attribution | Working, precision-biased; a quarter of output deliberately discarded |
| Scoring | Working — FinBERT, local, verified sign convention |
| Point-in-time | Enforced in the read path, tested in both directions |
| Live scoring | Yes, for the first time |
| **Backtestable** | **No.** History needs days of GDELT collection |

The honest summary: phase 3 built the whole pipeline and supplied one end of
it. `attribution` still reports `n = 0` for the sentiment factor, as it did
before — but the reason is now a throughput measurement with a resumable job
attached rather than an absence.

Phase 2's ranking of the work is unchanged and reinforced. Sentiment is 10% of
the model's weight; value and quality are 55% and still at zero.

---

## 7. Recommended order of work

1. **Resolve the NCI validator (PHASE1-FINDINGS §2.1) and run the annual-report
   backfill.** Unchanged from phases 1 and 2, and still the blocker on 55% of
   the factor weight and four of six red flags.
2. **Run `backfill-news` overnight, repeatedly.** It is checkpointed and
   resumable; the only cost is calendar time. Start with one year of the Nifty
   100 (~1,200 windows, ~4 nights) rather than three, and re-run `attribution`
   when it lands. The first question is whether the sentiment factor has any IC
   at all, and one year of monthly observations is enough to decide whether it
   is worth three.
3. **Put `ingest-news` on a daily schedule.** Free, under a minute, and every
   day it does not run is a day of history recoverable from no source but
   GDELT.
4. **Revisit the roundup rule with more data.** Three companies is defensible,
   not measured; with a year of history the test is whether including demoted
   rows helps or hurts the factor's IC. The same applies to the headline-only
   rule from §1.4.
5. Marketaux, only if attribution precision becomes the binding constraint. It
   is not currently what limits the factor.

---

## 8. Files changed

| File | Change |
|---|---|
| `news/aliases.py` | **new** — alias generation, group/common-word/connective blocks, curated table, build-time conflict pass |
| `news/resolve.py` | **new** — longest-match scan, headline-only attribution, provider confirmation |
| `news/store.py` | **new** — IST knowledge dates, syndication dedupe, roundup demotion, re-resolution and duplicate sweep |
| `news/finbert.py` | **new** — local scorer, signed convention, label order read from the model |
| `news/scoring.py` | **new** — score-once-per-text pipeline, coverage and attribution reports |
| `ingest/rss.py` | **new** — hand-rolled RSS/Atom parser, per-outlet User-Agent fallback |
| `ingest/gdelt.py` | **new** — month windows, checkpointing, retry through throttling |
| `ingest/marketaux.py` | **new** — entity-tagged ingest, provider sentiment stored separately |
| `db/schema.sql` | news attribution columns, `instrument_aliases`, `news_backfill_log`; secondary news indexes dropped (§1.6) |
| `db/database.py` | `as_of_sentiment` filters on attribution confidence |
| `config.py` | feeds, GDELT pacing, Marketaux key, resolution threshold, model name |
| `cli.py` | `build-aliases`, `ingest-news`, `reresolve-news`, `ingest-marketaux`, `backfill-news`, `score-news`, `news-status` |
| `pyproject.toml` | `[sentiment]` extra — torch and transformers, ~2GB, optional |
| `tests/test_ticker_resolution.py` | **new** — 18 tests |
| `tests/test_news_ingest.py` | **new** — 39 tests |
| `tests/test_sentiment_scoring.py` | **new** — 15 tests |
