# Portfolio: login, Zerodha import and a portfolio page

Status: design, awaiting the owner's approval. Date: 30 Oct 2026.

## 1. Intent

Every visitor to the public site can sign in, upload the holdings file they
download from Zerodha, and see their own holdings next to the model's latest
view of each stock. The owner's words: "analyze my portfolio and tell me which
one is good to sell and which one is good to buy", where buy means **adding
to something already held**, not new ideas.

The site already rates NIFTY 100 companies BUY / HOLD / SELL. This feature does
not add a new model. It applies the existing ratings to a person's own holdings
and shows them clearly: how much of their money sits in each rating, which
holdings carry a red flag, and which ratings changed since the last run.

### What success looks like

The owner downloads `holdings.csv` from Kite, signs in, uploads it, picks
**Replace my portfolio**, and lands on `/portfolio`. A "Saved 23 holdings"
banner sits at the top. All 23 rows from the file are there with value and
profit/loss. The 13 rated holdings sit under BUY, HOLD or SELL. The other 10
sit under "Not rated", each with a plain reason. The page looks like the rest
of the site, on a phone and on a desktop.

## 2. Built in three stages, in this order

1. **Login.** Accounts, sign-in, sign-out, delete account. Nothing else needs
   it today, but stages 2 and 3 do.
2. **Import.** The upload page, the Kite parser, Replace or Add, and saving
   under the signed-in account.
3. **Portfolio page.** Holdings with prices, profit/loss and ratings, the
   share-of-value bar, the empty state, and Delete portfolio.

Each stage ships on its own and leaves main working.

## 3. Goals, non-goals, anti-goals

**Goals**
- Anyone can create an account and sign in (§4).
- Upload the Kite holdings CSV. Choose **Replace my portfolio** (the file
  becomes the whole portfolio) or **Add to my portfolio** (keep what is saved
  and add the file's holdings). *(Owner's decision.)*
- After a successful save, land on the portfolio page with a success message.
  *(Owner's decision.)*
- The portfolio page groups holdings by the model's rating, with a
  share-of-value bar on top. This is mockup A's grouping with B's bar, shown
  to and accepted by the owner. *(Owner's decision.)*
- The page matches the site's existing design: same app shell, cards, chips,
  colours and type. *(Owner's decision: "match the design theme".)*

**Non-goals (not in this version)**
- A live connection to Zerodha (Kite Connect), or any broker other than
  Zerodha.
- Mutual funds, tradebooks, transaction history, XIRR or returns over time,
  or tax profit/loss.
- More than one portfolio per account.
- Editing a holding by hand. To change it, upload again.
- Suggesting stocks the person does not hold.
- Alerts, emails about rating changes, or sharing a portfolio.

**Anti-goals: it fails even if it technically works when it**
- **Tells a person what to do.** No "sell this", "you should", or "we
  recommend" anywhere. The page shows the model's rating of a stock, labelled
  as a model rating on a date and not advice, in the wording the rest of the
  site already uses (§8).
- **Loses a saved portfolio on a bad upload.** A rejected or half-read file
  changes nothing.
- **Drops rows silently.** Every row in the file is saved and shown, matched
  or not.
- **Shows one person another person's portfolio.** Every read and write is
  scoped to the signed-in user, and tests prove it.
- **Keeps what it doesn't need.** The uploaded file itself is not stored;
  only the parsed rows are. No passwords are stored at all.

## 4. Login (stage 1)

*My decision. The owner handed it to me.*

- **Ways to sign in:** Google, or a one-time code sent to the person's email.
  There are no passwords, so there is no password storage and no reset flow.
- **Library:** [Better Auth](https://www.better-auth.com/), running inside the
  Next.js site. It uses its Google provider, its email-OTP plugin and its JWT
  plugin. It handles OAuth state, code expiry and rate limits, so none of that
  is hand-written.
- **Where accounts live:** the existing Postgres, in a schema of their own
  (`auth`). They are never in `site`, which every publish replaces (§6).
- **Local development needs no outside accounts.** With no Google key set, the
  Google button is hidden. With no email provider set, the code is written to
  the Next.js server log instead of being emailed.
- **Before going live, the owner sets up:**
  - a Google OAuth client (free, in Google Cloud);
  - an email-sending service.
  The builder picks the email service and documents its env vars. Its free
  tier must cover a small site.
- **Pages and UI:**
  - `/login`: a Google button and an email-then-code form, in the site's
    style. It takes a `next` parameter so people return where they were.
  - The top bar's right side shows **Sign in** when signed out. When signed in
    it shows an avatar menu with Portfolio, Sign out and Delete account.
    Delete account removes the account and its portfolio, after a
    confirmation.
- **How the Python API knows who is calling:** see §7.

## 5. Import (stage 2)

### The file

The owner's real file is Kite's holdings download (Kite → Holdings →
Download). Its header is:

```
"Instrument","Qty.","Avg. cost","LTP","Invested","Cur. val","P&L","Net chg.","Day chg.",""
```

There is one row per holding and a trailing empty column. The `Instrument`
column holds the NSE trading symbol.

The owner's file includes these awkward cases, and the parser must handle all
of them:
- ETFs: `NIFTYBEES`, `MID150BEES`, `MOM100`.
- A sovereign gold bond: `SGBJUN31I`.
- A REIT: `EMBASSY-RR`.
- Stocks outside NIFTY 100: `BHEL`, `SBICARD`.
- Banks and insurers the model can't score: `HDFCBANK`, `HDFCLIFE`, `SBIN`.
- The Tata Motors demerger symbols: `TMPV`, `TMCV`.

**Zerodha Console's .xlsx export** has a different layout with an ISIN column.
It is added only once a real Console file is available to test against. Until
then the upload accepts Kite's .csv, and the page says so. *(My decision. I
said .xlsx would be accepted, but nobody has a real file to build it against
yet.)*

**Recognising the file:** by its header, not its extension. Columns are found
by name, so a reordered or extra column doesn't break it. A file missing
`Instrument`, `Qty.` or `Avg. cost` is refused with a message saying what was
expected and where to download it.

**Messy input the parser must survive:**
- a byte-order mark, and CRLF line endings;
- quoted numbers, and numbers with thousands separators;
- blank lines;
- the same symbol appearing twice in one file. Those rows are combined, using
  the same rule as Add below.

**Rows the parser refuses:** a row with quantity ≤ 0, or a non-numeric
quantity or cost, is refused, and the message names the row. If any row is
refused, nothing is saved.

**Limits:** files up to 1 MB and 2,000 rows. *(My decision.)*

### Matching a row to a company

A row is matched on `Instrument` against `instruments.nse_symbol`, ignoring
case. The raw symbol is always kept.

An unmatched row is still saved, with no ISIN, and shown under "Not rated".
Nothing in the data says which unmatched symbols are ETFs, bonds or REITs, so
this version doesn't try to label them (§8).

### Replace and Add

Both run in one Postgres transaction.

- **Replace:** delete the user's holdings, then insert the file's.
- **Add:** a holding already saved under the same key is combined with the
  file's row. Anything else is inserted.
  - The key is the ISIN when matched, otherwise the upper-cased raw symbol.
  - Combined quantity is `q1 + q2`.
  - Combined average cost is `(q1·c1 + q2·c2) / (q1 + q2)`.
  - The file price comes from the newer file.

  *(My decision. The owner saw this rule and didn't object.)*

### What is stored

Two tables, in a Postgres schema of their own (`app`), never in `site`.

| Table | Columns |
|---|---|
| `holdings` | user id, raw symbol, ISIN (nullable), quantity, average cost, the file's price and when it was uploaded, updated-at |
| `imports` | id, user id, original file name, format, mode, row count, created-at |

`imports` exists so the portfolio page can show "Saved 23 holdings from
holdings.csv". The builder picks exact names and types.

Quantities and money are stored as exact decimals, not floats.

### The upload page

`/portfolio/import`, signed in only. It has:
- a drop zone with a "choose a file" link, and one line on where to download
  the file in Kite;
- the Replace / Add choice, using the site's existing `Segmented` control.
  The default is Replace when nothing is saved and Add otherwise. *(My
  decision.)*
- a **Save portfolio** button.

On success the browser goes to `/portfolio` and the page shows the success
banner. On failure the page stays put and shows the error with the
`Notice` component. Nothing saved changes.

## 6. Where the data lives

**One Postgres database, three schemas:**

| Schema | Holds | Changed by |
|---|---|---|
| `site` | Published stock data | Every publish swaps it whole (`db/publish.py`) |
| `auth` | Accounts and sessions | Better Auth |
| `app` | Portfolios | The portfolio routes |

`publish` must never touch `auth` or `app`. Today it only creates, renames and
drops `site` and `site_release_*` schemas. A test pins that it leaves other
schemas alone.

**Postgres is required for this feature.** With `SA_SERVE_BACKEND=duckdb`:
- the portfolio routes answer 503 "Portfolio needs Postgres";
- the site hides Sign in and Portfolio.

The stock pages keep working on either backend.

The portfolio SQL **joins** `app.holdings` to the published tables
(`instruments`, `prices_daily`, `signals`, `corporate_actions`) in the same
database. That join is why portfolios live in this Postgres and not in a
separate store.

### Postgres roles

*My decision.* Today the one role in `SA_PG_DSN` both publishes and serves.
The read pool is read-only only because of a per-session setting, not because
of permissions. A writer pool on that same role could write to `site`. So:

**Optional separate roles,** each with its own DSN setting:

| Role | Can do | Set by |
|---|---|---|
| Portfolio role | Write `app`, read `site` | `SA_PG_APP_DSN` |
| Better Auth role | Write `auth` only | Next.js `DATABASE_URL` |

When `SA_PG_APP_DSN` is unset, the portfolio pool falls back to `SA_PG_DSN`.
That keeps local development to one DSN.

**Publish grants read access to each release.** Every publish creates a fresh
`site_release_*` schema, so it grants `USAGE` and `SELECT` on that new schema
to the roles named in a new setting, `SA_PG_READ_ROLES` (empty by default). It
does this inside the same transaction, so the portfolio role can always read
the live `site`.

**Setup:** a short script or doc section that creates the roles.

## 7. API

**Portfolio data belongs to the Python API (FastAPI).** Login belongs to the
Next.js site. *(My decision.)* The reasons:
- the portfolio query joins to stock data the Python API already reads;
- parsing and portfolio maths are Python's strengths here;
- a future mobile app can call the same API with the same token.

**Identity:**
1. Better Auth's JWT plugin issues short-lived signed tokens for the
   signed-in user.
2. The Next.js server sends the token to the API as `Authorization: Bearer`.
3. The API checks the signature against Better Auth's public keys, which it
   fetches from the site's JWKS URL and caches. It also checks the expiry,
   issuer and audience.
4. The user id comes from the token's `sub`.

No route trusts a user id from anywhere else.

**New settings:** `SA_AUTH_JWKS_URL`, `SA_AUTH_ISSUER`, `SA_AUTH_AUDIENCE`.

**New routes:**

| Route | Does | Answers |
|---|---|---|
| `GET /v1/me` | Confirms the token | User id (stage 1) |
| `POST /v1/me/portfolio/import` | Multipart file plus `mode=replace\|add` | Import id and counts, or 422 with the reasons |
| `GET /v1/me/portfolio` | Everything the page renders, in one response (§8) | |
| `DELETE /v1/me/portfolio` | Removes all holdings | |

All of them:
- answer `Cache-Control: no-store`;
- answer 401 without a valid token;
- write through a separate connection pool that can write to `app`. Every
  existing route keeps its read-only pool, so the existing guarantee that
  nothing public can write still holds for the stock data.

**Three existing guards need deliberate updates, not workarounds:**
- `tests/test_postgres_serving.py::test_parity_urls_cover_every_route`
  requires every GET route to have a DuckDB-vs-Postgres parity URL. Portfolio
  routes are per-user and Postgres-only, so they get an explicit, named
  exemption list instead.
- `test_every_table_the_read_layer_queries_is_published` scans the read layer
  for table names. Portfolio SQL lives in its own module, with schema-qualified
  `app.` names, so it doesn't trip this check.
- **CORS** allows GET only. The browser never calls these routes directly. It
  calls Next.js route handlers, which attach the token. Those handlers check
  the request's `Origin`, and they are separate from the existing
  `/api/v1/*` proxy, which stays GET-only and allow-listed.

`web/openapi.json` and the generated types are regenerated as usual.

## 8. Portfolio page (stage 3)

`/portfolio`, signed in only. A signed-out visitor goes to
`/login?next=/portfolio`. The sidebar and mobile nav get a **Portfolio** item.
The mockup is in `.superpowers/brainstorm/` (`portfolio-v3.html`).

### Content, top to bottom

1. **Success banner,** only right after an import. It reads "Saved 23
   holdings from holdings.csv (replaced your portfolio)" or "… (added to your
   portfolio)".
2. **Header:** "Your portfolio", an as-of line, and an **Import from Zerodha**
   button.
3. **Summary card:**
   - current value, invested, and profit/loss in ₹ and %;
   - a bar showing the share of value in BUY, HOLD, SELL and Not rated,
     coloured with the site's existing zone colours, with a legend giving the
     percentages;
   - the site's existing `VerdictNote` line: "Model ratings on 24 Oct 2026,
     not advice · How to read this".
4. **Holdings card,** grouped in the order BUY, HOLD, SELL, Not rated.
   - Each group header shows the existing `SignalChip`, the number of
     holdings, and the group's share of value.
   - Rows inside a group are sorted by value, largest first.
   - Each row shows the symbol (linked to its stock page when matched), the
     name, quantity, average cost, value, weight, profit/loss %, and the score.
   - Each row also shows:
     - a red-flag pill when the stock has any;
     - a "was SELL" pill when the rating differs from the previous run's;
     - under Not rated, the reason.
5. **Delete portfolio,** a quiet link at the foot, with a confirmation.

### Which rating a holding gets

*My decision, following the stock page's existing rule.*

- **The rating:** each matched holding takes its own newest rating, the one
  the stock page shows.
- **Older ratings:** when that rating is not from the latest full run, the row
  gets a pill: "Rated 14 Aug 2026, earlier model". A partial run doesn't count
  as a full run; use the site's existing full-run logic in
  `web/src/lib/runs.ts`. Today that covers NESTLEIND, NTPC and TATAPOWER. The
  pending re-run with the XBRL fixes would bring them up to date.
- **As-of line:** names the latest full run's date, and says how many holdings
  carry older ratings.
- **The "was SELL" pill:** shown only when the stock's previous rating is a
  like-for-like comparison:
  - the same model version;
  - not from a partial run.
  
  Otherwise the row shows no pill. This matches what the signals page already
  treats as comparable. Without the rule, about a third of rated holdings
  would show "was X" today just because the model changed on 24 Oct.

### Not-rated reasons

| Holding | Reason shown |
|---|---|
| Matched but not scored | The existing `score-status` explanation, e.g. "The model can't read annual statements from banks' results filings…" |
| Unmatched | "Outside the NIFTY 100 companies the model covers. ETFs, gold bonds and REITs are never rated." |

### Prices

- **Matched holdings:** the newer of the site's latest close
  (`prices_daily`) and the price in the file.
- **Unmatched holdings:** the file's price.
- The as-of line names the date range when prices come from different days,
  e.g. "Prices 24–30 Oct 2026". *(My decision.)*
- **Splits and bonuses after an upload.** Kite's quantity and average cost are
  on the share basis of the upload day, but the site's close moves to the new
  basis after a split or bonus. When `corporate_actions` has a split or bonus
  for the holding with an ex-date after the upload:
  - the row uses the file's price, not the site's close;
  - it shows "Split or bonus since your upload. Import again to update".
  
  The holding itself is never adjusted. *(My decision. Without this, a 2:1
  split would show a false −50%.)*
- Value is `quantity × price`. Profit/loss is against `quantity × average
  cost`.

### Wording

- Rating labels come only from `web/src/config/signals.ts`, as everywhere
  else. If legal advice renames BUY/HOLD/SELL, this page follows.
- The page never uses imperative advice wording (§3).

### Empty state

When signed in with no holdings: one card explaining what the page does, and
an **Import from Zerodha** button.

### Layout

- **Desktop:** the grouped table.
- **Phone:** each row becomes two lines. Symbol, value and P&L go on the
  first; quantity, cost and score on the second. No horizontal page scroll.

The builder reuses `Card`, `SignalChip`, `Pill`, `Notice`, `VerdictNote`,
`Segmented` and the app shell rather than adding new styles where one exists.

## 9. Testing

**Python (pytest):**
- **Parser:** a fixture copied from the owner's file with made-up numbers,
  keeping every awkward case in §5. Also:
  - BOM, CRLF, blank lines and duplicate symbols;
  - missing columns and bad rows;
  - the size and row limits.
- **Replace and Add:** the arithmetic, and that a refused file leaves the
  saved portfolio unchanged.
- **Token checks:** valid; expired; wrong issuer, audience or signature; and a
  missing token.
- **Isolation:** user A can never read, change or delete user B's holdings.
- **The routes,** under `SA_TEST_PG_DSN`.
- **Publish:** it leaves `auth` and `app` untouched.

**Web (Vitest):**
- the grouping and share-of-value maths;
- the reason text;
- the import form's states;
- the nav item;
- signed-out redirects.

**Playwright e2e,** at phone and desktop sizes, against the live API: sign in
with an email code, upload the fixture with Replace, land on `/portfolio` with
the banner, and see the right groups. The builder decides how a test reads the
code; logging it in dev is enough.

**The existing suites stay green:**
- Python: 876 passed with Postgres.
- Web: 209 unit tests, plus 22 e2e tests.

## 10. Where it runs

Nothing is deployed today. There is no hosting or CI config, and the site and
API run on the owner's Mac. So:
- **"Ships" in §2 means "merged to main and working locally".**
- **The plan ends at local setup,** plus a documented list of every env var
  production will need.
- **The dev-only shortcut must not reach production.** When
  `NODE_ENV=production` and no email service is set, the email sign-in
  refuses to start rather than writing codes to the log.

Hosting is a later project. It decides:
- whether Better Auth's Postgres connection runs on a long-lived server or
  serverless;
- how the API reaches the JWKS URL;
- whether the API is public.

The design doesn't depend on those answers.

## 11. Before this goes public (not part of the build)

**Legal: the owner's call, with their lawyer.** Showing BUY/SELL beside a
specific person's holdings may count as investment advice under SEBI's
Investment Adviser rules. Those rules are stricter than the ones for
publishing ratings in general. The build follows the site's existing "model
rating, not advice" wording, and labels sit in one file so they are easy to
change.

**Personal data.** Holdings are personal financial data under India's DPDP
Act. The build supplies:
- deleting a portfolio, and deleting an account;
- no stored files.

The owner adds a privacy notice before launch.

## 12. Left to the builder

- Table and column names.
- How each schema's tables are created and migrated.
- The email service.
- The Better Auth configuration details.
- How the Next.js handlers attach the token.
- JWKS caching.
- Exact copy for error messages.
- How e2e gets the sign-in code.

Each is decided within the constraints above.

## 13. Decisions made in this document, for the owner to check

- Google plus an email code, using Better Auth; no passwords.
- Portfolio data in the Python API; login in Next.js; a signed token between
  them.
- Separate `auth` and `app` schemas in the same Postgres; the feature requires
  Postgres.
- Kite .csv only, until a real Console .xlsx is available.
- Add's combining rule, and duplicate symbols in one file treated the same
  way.
- Unmatched rows kept and shown, not refused.
- The price rule (newer of site close and file price), and the file price
  after a split or bonus.
- Each holding takes its own newest rating, with a pill when it's older than
  the latest full run.
- "Was SELL" only for like-for-like comparisons.
- Optional separate Postgres roles; publish grants read access to them.
- Nothing deployed: the plan ends at local setup plus documented env vars.
- The upload page's default mode.
- The 1 MB / 2,000-row limits.
- Delete account and Delete portfolio included.
- No manual editing.
