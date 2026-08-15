# Phase 1 — findings from the first run against real data

**Date:** 2026-08-13
**Scope:** first end-to-end exercise of the phase-1 extraction pipeline against
live NSE data and real annual reports. Everything before this had run only
against synthetic PDFs and canned payloads.

**Result:** 4 bugs found and fixed, 1 found and left open pending a design
decision, plus a set of measurements that change what the roadmap should assume.
Test suite went 153 → 174.

---

## 1. Bugs fixed

### 1.1 The section locator crashed on the first real annual report

**Severity:** blocking — phase 1 could not process Reliance at all.
**Where:** `extract/locator.py`, `_build_pdf`

Reliance's FY2026 report carries AcroForm widgets whose parent field's `/Kids`
array does not list them. `pymupdf`'s `insert_pdf` walks that tree while copying
pages and raises:

```
ValueError: 558 is not in list
  at pymupdf/__init__.py:3590 in _do_widgets
```

The failure happens in the locator, before any extraction is attempted, so a
filing is lost with a raw traceback and no review-queue entry.

Every locator test used synthetic PDFs built by `pymupdf.open()`, which have
clean form trees. Nothing in the suite could have caught it.

**Fix:** copy page content only —
`insert_pdf(..., widgets=False, annots=False, links=False)`. None of it is
content the extractor reads, so dropping it removes the whole class of failure
rather than handling one instance.

**Test:** `test_malformed_form_widgets_do_not_break_the_narrowing` builds the
broken parent tree synthetically. Verified to fail against the unfixed locator.

**After the fix:**

| Report | Source | Located |
|---|---|---|
| Reliance FY2026 | 187 pp / 11.0 MB | 60 pp / 0.97 MB |
| Siemens FY2026 | 343 pp / 21.4 MB | 60 pp / 0.34 MB |

Page-count reduction of ~3× and ~5.7× matches DESIGN's claimed ~4× cost cut.

---

### 1.2 The cash flow statement was being silently deleted

**Severity:** high — removes the single most important extraction target.
**Where:** `extract/local.py`, `render_page`
**Affects:** the local backend (shipped) and the new CLI backend. **Not** the
API path, which sends real PDF document blocks.

`render_page` prefers detected tables over raw text flow, to preserve the column
alignment that distinguishes the current year from the prior-year comparative.
But it returned `heading + tables` whenever the table list was non-empty —
discarding all other text on the page.

On Reliance's cash flow pages the detector returned two fragments covering ~17%
of the page:

| Located page | Raw | Rendered | Tables | Lines lost |
|---|---|---|---|---|
| 5 | 4,515 chars | 438 chars | 2 | 183 of 221 |
| 34 | 4,712 chars | 484 chars | 2 | 190 of 230 |

The Statement of Cash Flows vanished. The extraction then returned `ocf: null`
and scored 0.0 confidence — which reads exactly like a model failure rather than
a text-rendering one.

**Why it matters:** CFO/PAT and the accruals ratio exist nowhere else in the
report. DESIGN §6.1 calls them the most reliable published warning signs in
Indian mid-caps, and they carry disproportionate weight in the Quality factor.
This bug removed both from every downstream factor, quietly.

**Fix:** measure what fraction of the page's substantive lines survive into the
table rendering. Below 60%, treat the detection as a misread and fall back to
raw text — approximate column alignment beats absent rows.

**Tests:**
- `test_a_misdetected_table_does_not_delete_the_rest_of_the_page`
- `test_a_well_covered_table_still_renders_as_columns` (guards the fallback from
  costing the column alignment it exists to protect)

**Effect on the same report:**

| | ocf | capex | Flattened text | Failing checks |
|---|---|---|---|---|
| Before | `None` | `None` | 168,466 chars | `required_fields`, `pbt_tax_pat` |
| After | 192,113 | 122,916 | 342,629 chars | `pbt_tax_pat` only |

Note the text roughly doubles. That is fine for the API and CLI paths, but it
doubles truncation pressure on the local path's 28k-character budget.

---

### 1.3 Read-only commands crashed on a database from an earlier version

**Severity:** medium — every reporting command broke after a schema addition.
**Where:** `db/database.py`

`Database._init_schema()` runs the idempotent `schema.sql` only when the file is
opened for writing. `status`, `review`, and `review-detail` all open read-only,
so against a phase-0 database they hit whichever phase-1 table they queried first
and surfaced a raw DuckDB exception:

```
CatalogException: Table with name extraction_attempts does not exist!
Did you mean "corporate_actions"?
```

Write commands worked fine, which is what kept it invisible.

**Fix:** on a read-only open, diff the tables `schema.sql` declares against the
tables present and raise `SchemaOutOfDateError` naming the missing ones and the
command that repairs them. Surfaced by the CLI alongside `DatabaseLockedError`.

**Tests:** `tests/test_operability.py` — read-only open names the missing
tables; writable open repairs the file itself; a current database still opens
read-only.

---

### 1.4 Missing credentials produced a raw SDK traceback

**Severity:** low — cosmetic, but it is the first thing a new user hits.
**Where:** `extract/claude.py`, `extract/factory.py`, `cli.py`

`stockanalysis extract` with no `ANTHROPIC_API_KEY` dumped a `TypeError` from
several frames inside the `anthropic` SDK, while the locked-database and
LM-Studio-unreachable paths both gave clean messages.

**Non-obvious detail:** the SDK constructs fine with no credentials —
`Anthropic()` returns a client with `api_key=None` and `auth_token=None`, and
only raises when it builds a request. A guard wrapped around construction never
fires. The check has to be an explicit preflight.

**Fix:** `ExtractorUnavailableError` raised from `ClaudeExtractor.__init__` when
we built the client ourselves and it carries no credentials. Placed there rather
than in the factory because the batch commands construct `ClaudeExtractor`
directly. The message names the free alternatives (`local:`, `cli:`).

**Tests:** `tests/test_operability.py` — missing credentials say what to do; the
factory fails the same way for all backends.

---

## 2. Open — needs a design decision

### 2.1 `pbt_tax_pat` ignores non-controlling interests

**Severity:** high — blocks DESIGN's phase-1 exit criterion.
**Where:** `extract/validate.py:198`, `extract/schema.py`, `db/schema.sql`

The check asserts:

```
profit_before_tax - tax_expense == pat        (±2%)
```

That identity holds only when non-controlling interests are zero. Measured on
Reliance FY2026:

```
PBT 123,162 − tax 27,552 = 95,610   total group profit
PAT reported             = 80,775   attributable to owners
gap                      = 14,835   non-controlling interests  (15.52%)
```

The extraction was correct. The check is wrong.

`AnnualReportExtraction` has no NCI field and `fundamentals_annual` has no NCI
column, so the validator has nothing to reconcile against. Because the check is
`HARD`, confidence pins to 0.0 and the row goes to the review queue instead of
`fundamentals_annual`.

**Consequences:**
- Affects most large Indian groups — Reliance, Tata, Adani, anything with
  material minority shareholders.
- DESIGN's exit criterion (">=95% of extractions pass arithmetic validation")
  is **unreachable on consolidated financials** as the code stands.
- A bake-off run today would compare models on a metric saturated at zero, so
  the model choice cannot be settled until this is resolved.

**Why it was not fixed here:** the fix needs a new field on the extraction
schema *and* a column on `fundamentals_annual` — a change to the extraction
contract and the database, not a mechanical correction.

**Suggested shape:** add `profit_attributable_to_nci`, change the check to
`pbt - tax == pat + nci`, and skip rather than fail when NCI is absent.

---

## 3. Measurements and gotchas

### 3.1 Knowledge dates — the assumption is over-conservative, sometimes wrongly so

`ingest-results-index` replaces assumed quarterly filing dates with NSE's real
broadcast dates. It works, and the direction is the safe one:

| Quarter end | Assumed (LODR) | Real (NSE) | Delta |
|---|---|---|---|
| 2023-12-31 | 2024-02-14 | 2024-01-19 | 26 days earlier |
| 2024-03-31 | 2024-05-15 | 2024-04-22 | 23 days earlier |
| 2024-12-31 | 2025-02-14 | 2025-01-16 | 29 days earlier |

All 15 quarterly rows upgraded from `ASSUMED_LODR_DEADLINE` to `NSE`.

**But the annual-report fallback can produce a knowledge date in the future.**
The FY2026 filings were downloaded on 2026-08-13 and assigned
`broadcast_date = 2026-09-30` (period end + 6 months, the statutory AGM
deadline). The document was demonstrably public on the day it was fetched. The
assumption is conservative in the safe direction, but it is provably wrong here
and throws away ~7 weeks of real signal.

**Cheap improvement:** cap the assumed date at the fetch date —
`min(period_end + 6mo, fetched_at)`.

### 3.2 `ingest-results-index --years N` must span the quarterly data

`--years 1` returned 91 index filings and applied **0** matches. Not a matcher
bug — a window mismatch. `NSE.results_comparison()` returned quarters ending
**2023-12-31 through 2024-12-31**, i.e. roughly 18 months stale relative to
2026-08-13, while `--years 1` scanned 2025-08-13 → 2026-08-13. Zero overlap.

`--years 3` applied 30 matches and upgraded all 15 rows.

The CLI reports "Applied 0 filing-index matches" without explaining why, which
looks like a failure when it is a configuration mismatch.

**Also worth noting:** `results_comparison`'s "last 5 quarters" is materially
stale. Treat it as a historical cross-check, not a current-quarter source.

### 3.3 Claude Code's `Read` cannot take PDFs without poppler

The CLI rasterises PDFs via `pdftoppm`, which is absent on a stock macOS box:

```
pdftoppm is not installed
(pdftotext, pdftoppm, mutool, qpdf, gs all absent)
```

Even with poppler installed, 60 pages of rasterised images would be both
expensive and close to image limits. This is why the CLI backend flattens to
text rather than handing over the PDF.

### 3.4 The CLI writes advisory text to stdout/stderr around the JSON

An untrusted workspace produces:

```
Ignoring 2 permissions.allow entries from .claude/settings.json: this workspace
has not been trusted.
```

Do **not** merge stderr into stdout when parsing the envelope, and seek to the
first `{` rather than parsing from position zero. Both are handled in
`claude_cli.py`; both broke a probe first.

### 3.5 Claude Code's own system prompt is ~19k tokens per invocation

A trivial `claude -p` round trip billed 6,750 cache-creation + 12,497 cache-read
tokens before any of our content. `--system-prompt` replaces it, which is why
the backend passes `EXTRACTION_SYSTEM_PROMPT` explicitly and runs with
`--allowed-tools ""` — that also makes each call a single turn (`num_turns: 1`)
rather than an agent loop.

### 3.6 Cost per report, measured

| Backend | Per report | Latency | Billed to |
|---|---|---|---|
| `cli:claude-opus-5` | **$1.88** | 42 s | Claude subscription |
| API `claude-opus-5`, unbatched | ~$0.75 (est.) | — | Developer Platform |
| API `claude-opus-5`, batched | ~$0.38 (est.) | — | Developer Platform |
| `local:gemma-3-12b-it-qat` | $0.00 | 8 s | — |

The CLI path is the **most** expensive per report, not the cheapest: every call
is a cold cache write at 1.25×, there is no Batch API discount, and flattened
statement text tokenises badly — 168k characters came to 95k tokens, and 343k
characters to 166k. It is cheaper only in that it spends a balance already paid
for.

No batching either: ~40 s per report means a Nifty 100 × 3yr backfill is 300
serial invocations. Use the CLI to prove the pipeline and run the bake-off; use
the API path for a backfill.

### 3.7 LM Studio defaults to a 4k context window

The first local run failed with:

```
HTTP 400: Trying to keep the first 10381 tokens when context overflows.
However, the model is loaded with context length of only 4096 tokens
```

Not a model limitation — a load-time setting. Reload with an explicit context
length before concluding anything about local-model quality.

---

## 4. What the live paths confirmed

Exercised against real NSE endpoints at small limits, with rate limiting on:

| Command | Result |
|---|---|
| `fetch-filings --years 1 --limit 2` | 2 real annual reports downloaded |
| `ingest-quarterly --limit 3` | 15 quarterly rows |
| `ingest-shareholding --limit 3` | 63 rows |
| `ingest-results-index --years 3` | 30 matches, all 15 rows upgraded |
| `extract --model cli:` | end-to-end, persisted to `extraction_attempts` |

Extraction quality on Reliance FY2026 after the fixes — the balance sheet
identity is exact, and the figures are plausible:

```
revenue        1,075,675  (Rs. crore, consolidated)
pat               80,775
ocf              192,113
capex            122,916
total_assets   2,178,140
total_equity   1,085,866
auditor_opinion  UNMODIFIED

balance_sheet_identity  assets 2,178,140 vs equity+liabilities 2,178,140 (0.00%)
```

---

## 5. Recommended order of work

1. **Resolve §2.1 (NCI).** Nothing downstream is measurable until confidence
   scores mean something on large caps. This blocks the bake-off, which blocks
   the model choice, which blocks the backfill.
2. Cap the assumed annual-report knowledge date at the fetch date (§3.1).
3. Make `ingest-results-index` say *why* it applied zero matches (§3.2).
4. Re-run the bake-off once (1) is done, then decide the extraction model on
   evidence rather than argument, as DESIGN §11.3 asks.
5. Only then start the Nifty 100 × 3yr backfill, via the API path with batching.

---

## 6. Files changed

| File | Change |
|---|---|
| `extract/locator.py` | widget/annot/link copy disabled in `_build_pdf` |
| `extract/local.py` | `render_page` coverage fallback, `_line_coverage` |
| `extract/claude.py` | credential preflight, `ExtractorUnavailableError`, `reported_cost_usd` |
| `extract/claude_cli.py` | **new** — `claude -p` backend |
| `extract/factory.py` | `cli:` prefix routing, `is_cli` |
| `db/database.py` | `SchemaOutOfDateError`, `_require_current_schema` |
| `cli.py` | `extractor_errors()`, schema-out-of-date handler |
| `tests/test_section_locator.py` | +1 regression test |
| `tests/test_local_extractor.py` | +2 regression tests |
| `tests/test_operability.py` | **new** — 5 tests |
| `tests/test_claude_cli_extractor.py` | **new** — 13 tests |
| `README.md` | CLI backend documented |
