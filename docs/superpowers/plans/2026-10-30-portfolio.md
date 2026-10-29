# Portfolio: Login, Zerodha Import and Portfolio Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Visitors sign in, upload Kite's holdings CSV (replace or add), and land on `/portfolio`. That page shows each holding beside the model's latest rating.

**Architecture:** There are three stages, and each one leaves main working.
- **Login.** Better Auth inside Next.js (Google plus email code). Accounts live in Postgres schema `auth`. Its JWT plugin issues short-lived EdDSA tokens. FastAPI verifies those tokens against the site's JWKS.
- **Import.** FastAPI parses the file and saves rows to Postgres schema `app` through a separate writable pool. Existing routes keep their read-only pool.
- **Page.** `GET /v1/me/portfolio` reads holdings from `app`. It then reads stock data through the existing read layer by ISIN, so there is no cross-schema SQL. It returns everything the page renders, maths included. Next.js renders it with the site's existing components.

**Tech Stack:**
- **API:** Python 3.12, FastAPI, psycopg 3 + psycopg_pool, PyJWT[crypto], pytest.
- **Site:** Next.js 16 / React 19 / TypeScript, better-auth, pg, Vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-30-portfolio-design.md`. Read it first. Section numbers below (§n) refer to it.

## Global Constraints

**Privacy and wording**
- Never read, copy or commit `holdings.csv` in the repo root. It is the owner's private file. The tests use `tests/fixtures/portfolio/kite_holdings.csv` (Task 5), whose numbers are made up.
- Rating labels come only from `web/src/config/signals.ts`.
- No page or message may contain "sell this", "you should" or "we recommend" (§3, anti-goals). A Vitest test in Task 12 greps the portfolio components for them.

**Postgres**
- Schemas:
  - `site` (published; existing)
  - `auth` (Better Auth)
  - `app` (portfolios; setting `SA_PG_APP_SCHEMA`, default `app`)
- Publish must never touch `auth` or `app`.
- Read-layer SQL added to `serve/queries.py` must be portable:
  - alias every subquery;
  - `?` placeholders;
  - no `QUALIFY`, `FIRST()`/`LAST()` or list functions;
  - upper-case `FROM`/`JOIN` keywords.
- Store quantities and money as `NUMERIC` in Postgres and `Decimal` in Python. JSON carries floats, as every other route does.

**Limits and API behaviour**
- Upload limits: 1 MB (`MAX_BYTES = 1_000_000`) and 2,000 data rows (`MAX_ROWS = 2_000`).
- Every `/v1/me*` response carries `Cache-Control: no-store`.
- The existing `/api/v1/*` proxy stays GET-only and allow-listed. CORS stays GET-only.

**Repo conventions**
- `web/package.json` pins exact versions, with no `^` or `~`. Install the newest `better-auth` 1.x and `pg` 8.x, and pin what installs.
- Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Work happens on branch `portfolio`.
- After each stage, the existing suites still pass:
  - `.venv/bin/python -m pytest` (and again with `SA_TEST_PG_DSN=postgresql:///stockanalysis_test`)
  - `.venv/bin/ruff check src tests`
  - in `web/`: `npm test`, `npm run typecheck`, `npm run lint` and `npm run e2e`

**Local services**
- Postgres: `postgresql:///stockanalysis` (dev) and `postgresql:///stockanalysis_test` (tests).
- API on `:8000`; site on `:3000`.

## Review Focus

These are the five inputs most likely to hurt a real user that the spec doesn't spell out. Each line's test lives in the named task.

1. **Double-clicking Save with Add selected doubles every quantity.** The button must disable while a save is in flight (Task 8).
2. **The wrong file.** Examples: Console's `.xlsx` (a zip, starting `PK`), a tradebook CSV, or an empty file. Each must be refused with the "isn't a Kite holdings file" message, and nothing saved may change (Tasks 5 and 7).
3. **A signed-out or expired token, or an API outage, mid-flow.**
   - The page redirects to `/login?next=…`.
   - The upload form shows an error and keeps the saved portfolio.
   - Neither crashes into the error boundary (Tasks 8 and 12).
4. **A holding with no price at all,** because LTP is blank and the site has no close. It shows "—" and is left out of the totals and shares. The summary counts it as unpriced (Task 10).
5. **A symbol saved unmatched that later matches.** For example, it joins NIFTY 100 between two uploads. An Add then combines it with the new matched row rather than keeping two rows (Task 6).

---

## File map

**Python**

| File | Responsibility |
|---|---|
| `src/stockanalysis/config.py` (modify) | New settings: `auth_jwks_url`, `auth_issuer`, `auth_audience`, `pg_app_dsn`, `pg_app_schema`, `pg_read_roles` |
| `src/stockanalysis/serve/auth.py` (create) | Verifies the bearer token; the `CurrentUser` dependency |
| `src/stockanalysis/db/postgres.py` (modify) | The writable `app` pool: `app_connection()` |
| `src/stockanalysis/db/publish.py` (modify) | Grants `SA_PG_READ_ROLES` read access on each new release |
| `src/stockanalysis/portfolio/__init__.py` (create) | Empty |
| `src/stockanalysis/portfolio/kite.py` (create) | Parses the Kite CSV |
| `src/stockanalysis/portfolio/lots.py` (create) | `combine()`: two lots of one holding become one |
| `src/stockanalysis/portfolio/store.py` (create) | `app` schema DDL; save, delete and load holdings |
| `src/stockanalysis/portfolio/view.py` (create) | Pure maths: prices, ratings, groups, shares |
| `src/stockanalysis/serve/queries.py` (modify) | Four batch read functions by ISIN or symbol |
| `src/stockanalysis/serve/portfolio_api.py` (create) | The `/v1/me` routes and their response models; imported at the end of `serve/api.py` |
| `src/stockanalysis/serve/api.py` (modify) | `no-store` for `/v1/me*`; imports `portfolio_api` |
| `src/stockanalysis/cli.py` (modify) | `stockanalysis setup-accounts` |

**Tests**

- `tests/test_portfolio_auth.py`
- `tests/test_portfolio_kite.py`
- `tests/test_portfolio_store.py`
- `tests/test_portfolio_view.py`
- `tests/test_portfolio_api.py`
- `tests/fixtures/portfolio/kite_holdings.csv`
- edits to `tests/test_postgres_serving.py`

**Site**

| File | Responsibility |
|---|---|
| `web/src/lib/auth/config.ts` | `accountsEnabled()` and `safeNext()` |
| `web/src/lib/auth/email.ts` | `sendSignInCode()` and `fixedOtp()` |
| `web/src/lib/auth/server.ts` | `auth` (Better Auth), `currentUser()`, `apiToken()` |
| `web/src/lib/auth/client.ts` | `authClient` |
| `web/src/app/api/auth/[...all]/route.ts` | Better Auth's handler |
| `web/src/app/login/page.tsx`, `web/src/components/account/LoginView.tsx` | The sign-in page |
| `web/src/components/account/AccountMenu.tsx` | Sign in, or the avatar menu |
| `web/src/lib/api/user.ts` | `userGet()` and `userSend()`, which attach the token |
| `web/src/app/api/portfolio/import/route.ts` | POST, forwarded to the API |
| `web/src/app/api/portfolio/route.ts` | DELETE, forwarded to the API |
| `web/src/app/portfolio/page.tsx`, `web/src/app/portfolio/import/page.tsx` | The two pages |
| `web/src/components/portfolio/` | `PortfolioView.tsx`, `SummaryCard.tsx`, `HoldingsCard.tsx`, `ImportForm.tsx`, `DangerZone.tsx`, `portfolio.module.css` |
| `web/src/lib/api/types.ts` (modify) | `Portfolio`, `Holding`, `ImportResult` types |

**Tests:** in `web/tests/unit/` and `web/tests/e2e/portfolio.spec.ts`.

**Docs:** a new "Accounts and portfolios" section in `README.md`, plus `.env.example` and `web/.env.example`.

---

# Stage 1 — Login

### Task 1: The API verifies Better Auth tokens

**Files:**
- Create: `src/stockanalysis/serve/auth.py`, `src/stockanalysis/serve/portfolio_api.py`, `tests/test_portfolio_auth.py`
- Modify:
  - `src/stockanalysis/config.py`: settings
  - `src/stockanalysis/serve/api.py`: the `cache_control` middleware, plus `from stockanalysis.serve import portfolio_api  # noqa: E402,F401` as the module's last import, after `app.add_middleware(ConfiguredCORS)`
  - `pyproject.toml`: add `"pyjwt[crypto]>=2.9"` to `dependencies`
  - `tests/test_postgres_serving.py`: the parity exemption

**Interfaces:**
- Produces:
  - `settings.auth_jwks_url: str | None`, `settings.auth_issuer: str | None`, `settings.auth_audience: str | None` (env `SA_AUTH_JWKS_URL`, `SA_AUTH_ISSUER`, `SA_AUTH_AUDIENCE`)
  - `serve.auth.verify(token: str) -> str`, which returns the `sub` claim or raises `AuthError`
  - `serve.auth.CurrentUser = Annotated[str, Depends(current_user_id)]`
  - `GET /v1/me` → `{"user_id": str}`
  - `serve.portfolio_api.PER_USER_PATHS: frozenset[str]`, the GET templates exempt from parity

- [ ] **Step 1: Write the failing tests.** Generate an Ed25519 key with `cryptography`. Expose it as a JWKS dict with `alg: "EdDSA"` and a `kid`. Monkeypatch `jwt.PyJWKClient.fetch_data` to return it. Set the three settings to `http://localhost:3000/api/auth/jwks`, `http://localhost:3000` and `http://localhost:3000`. Sign tokens with `jwt.encode(..., algorithm="EdDSA", headers={"kid": ...})`.

```python
def test_valid_token_returns_its_subject(client, sign):
    r = client.get("/v1/me", headers={"Authorization": f"Bearer {sign(sub='u1')}"})
    assert r.status_code == 200 and r.json() == {"user_id": "u1"}
    assert r.headers["cache-control"] == "no-store"

@pytest.mark.parametrize("claims", [
    {"exp": -60},                       # expired (seconds from now)
    {"iss": "http://evil.test"},
    {"aud": "someone-else"},
    {"sub": None},                      # no subject
])
def test_bad_claims_are_401(client, sign, claims): ...

def test_signature_from_another_key_is_401(client, sign_with_other_key): ...
def test_missing_or_non_bearer_header_is_401(client): ...   # no header; "Basic abc"
def test_unconfigured_auth_is_503(client, monkeypatch):     # auth_jwks_url None
    assert client.get("/v1/me", headers=...).json()["detail"] == "Sign-in is not configured on this API."
def test_me_is_not_mounted_at_the_legacy_root(client):
    assert client.get("/me").status_code == 404
```

- [ ] **Step 2: Run the tests.** `.venv/bin/python -m pytest tests/test_portfolio_auth.py -v`. Expected: FAIL (no module `stockanalysis.serve.auth`).

- [ ] **Step 3: Implement.**
  - **`serve/auth.py`:**
    - Use one `jwt.PyJWKClient(settings.auth_jwks_url, cache_keys=True, lifespan=300)`, cached per URL.
    - Call `jwt.decode(token, key, algorithms=["EdDSA"], audience=settings.auth_audience, issuer=settings.auth_issuer, options={"require": ["exp", "sub", "iss", "aud"]})`.
    - `current_user_id(authorization: str | None = Header(None))` maps `AuthError` to `HTTPException(401, "Sign in again.", headers={"WWW-Authenticate": "Bearer"})`.
    - Unconfigured settings give 503 with the detail above.
    - A JWKS fetch failure gives 503 "Sign-in keys are unavailable."
  - **`portfolio_api.py`:** register `/v1/me` with `app.get("/v1/me", tags=["account"])` directly. Don't use `route()`, which would also mount it at the root.
  - **Parity exemption:** in `test_parity_urls_cover_every_route`, change the template set to `{...} - PER_USER_PATHS`.
  - **`cache_control` middleware:** send `no-store` when `request.url.path.startswith("/v1/me")`, whatever the status.

- [ ] **Step 4: Run the tests.** `.venv/bin/python -m pytest tests/test_portfolio_auth.py tests/test_postgres_serving.py -v`. Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "API: verify sign-in tokens and add /v1/me"`

### Task 2: Better Auth in the site

**Files:**
- Create:
  - `web/src/lib/auth/config.ts`, `email.ts`, `server.ts` and `client.ts`
  - `web/src/app/api/auth/[...all]/route.ts`
  - `web/tests/unit/auth-config.test.ts`, `web/tests/unit/auth-email.test.ts`
  - `web/.env.example`
- Modify:
  - `web/package.json`: add `better-auth`, `pg` and `@types/pg` (dev); add the script `"auth:migrate": "npx @better-auth/cli migrate --config src/lib/auth/server.ts --yes"`
  - `src/stockanalysis/cli.py`: `setup-accounts`
  - `.env.example`

**Interfaces:**
- Produces:
  - `accountsEnabled(): boolean`. True when `DATABASE_URL` and `BETTER_AUTH_SECRET` are both set.
  - `safeNext(raw: string | null | undefined): string`
  - `sendSignInCode(email: string, otp: string): Promise<void>`
  - `fixedOtp(): string | null`
  - `auth` (the Better Auth instance)
  - `currentUser(): Promise<{ id: string; email: string; name: string } | null>`
  - `apiToken(headers: Headers): Promise<string | null>`
  - `authClient` (from `better-auth/react`, with `emailOTPClient()`)
  - The CLI `stockanalysis setup-accounts`, which creates schemas `auth` and `app`. Task 6 adds `app`'s tables to it.

- [ ] **Step 1: Write the failing tests.**

```ts
// auth-config.test.ts
it.each([["/portfolio", "/portfolio"], ["/stocks/M%26M?x=1", "/stocks/M%26M?x=1"],
         ["//evil.test", "/portfolio"], ["/\\evil.test", "/portfolio"], ["https://evil.test", "/portfolio"],
         [undefined, "/portfolio"], ["", "/portfolio"]])("safeNext(%s) is %s", (raw, out) => expect(safeNext(raw)).toBe(out));
it("accounts are enabled only with a database and a secret", ...);   // vi.stubEnv each combination

// auth-email.test.ts
it("logs the code in development when no email service is set", ...); // console.info called with email and code
it("refuses in production without RESEND_API_KEY", ...);  // rejects /email service is not configured/
it("sends through Resend when configured", ...);           // fetch to https://api.resend.com/emails, Bearer key, from EMAIL_FROM
it("fixedOtp is honoured outside production only", ...);   // AUTH_DEV_FIXED_OTP=123456 → "123456"; NODE_ENV=production → null
```

- [ ] **Step 2: Run the tests.** In `web/`: `npm test -- auth-`. Expected: FAIL (modules missing).

- [ ] **Step 3: Implement.**
  - **`server.ts`:**
    - `betterAuth({ database: new Pool({ connectionString: process.env.DATABASE_URL, options: "-c search_path=auth" }), ... })`.
    - `socialProviders.google` only when `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set.
    - Plugins:
      - `emailOTP({ sendVerificationOTP: ({ email, otp }) => sendSignInCode(email, otp), generateOTP: fixedOtp() ? () => fixedOtp()! : undefined })`
      - `jwt({ jwt: { issuer: BASE, audience: BASE, expirationTime: "15m" } })`, where `BASE = process.env.BETTER_AUTH_URL ?? "http://localhost:3000"`
      - `nextCookies()` last
    - `user.deleteUser.enabled: true`.
    - `apiToken(headers)` returns `(await auth.api.getToken({ headers })).token`, or null when there is no session.
  - **`email.ts`:**
    - Resend over `fetch`, with no SDK.
    - Env: `RESEND_API_KEY` and `EMAIL_FROM`.
    - Subject: `Your sign-in code`.
    - Body: `Your code is {otp}. It expires in 5 minutes.`
  - **The route:** `export const { GET, POST } = toNextJsHandler(auth)`.
  - **`setup-accounts`:** runs `CREATE SCHEMA IF NOT EXISTS auth` and the same for `settings.pg_app_schema`, using `SA_PG_DSN`.
  - **Env docs:**
    - `web/.env.example` documents `DATABASE_URL=postgresql:///stockanalysis`, `BETTER_AUTH_SECRET`, `BETTER_AUTH_URL=http://localhost:3000`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `RESEND_API_KEY`, `EMAIL_FROM` and `AUTH_DEV_FIXED_OTP`. Mark the last as dev and e2e only.
    - `.env.example` documents `SA_AUTH_JWKS_URL=http://localhost:3000/api/auth/jwks`, `SA_AUTH_ISSUER=http://localhost:3000` and `SA_AUTH_AUDIENCE=http://localhost:3000`.

- [ ] **Step 4: Run the tests and set up locally.**
  - `npm test -- auth-`. Expected: PASS.
  - Then:
    - `.venv/bin/stockanalysis setup-accounts`
    - `npm --prefix web run auth:migrate`
    - `psql stockanalysis -c '\dt auth.*'`
  - Expected: Better Auth's tables are listed. Write `BETTER_AUTH_SECRET` to `web/.env.local` and never print it.

- [ ] **Step 5: Commit.** Include `package-lock.json`.
  `git commit -m "Site: Better Auth with Google and email codes"`

### Task 3: Login page, account menu and the Portfolio nav item

**Files:**
- Create:
  - `web/src/app/login/page.tsx`
  - `web/src/components/account/LoginView.tsx`, `AccountMenu.tsx`, `account.module.css`
  - `web/tests/unit/login.test.tsx`, `web/tests/unit/account-menu.test.tsx`, `web/tests/e2e/portfolio.spec.ts`
- Modify:
  - `web/src/components/shell/NavList.tsx`, `AppShell.tsx`, `MobileNav.tsx`, `icons.tsx`
  - `web/playwright.config.ts`: `webServer.env: { AUTH_DEV_FIXED_OTP: "246810" }`

**Interfaces:**
- Consumes: Task 2's `authClient`, `safeNext` and `accountsEnabled`.
- Produces:
  - `NavKey` gains `"portfolio"`.
  - `NavList({ active, accounts }: { active?: NavKey; accounts: boolean })`.
  - `AccountMenu({ enabled }: { enabled: boolean })`, which is a client component.
  - The `/login?next=` page.

- [ ] **Step 1: Write the failing tests.**
  - **LoginView:**
    - It shows a "Continue with Google" button only when `google` is true.
    - Entering an email and pressing "Send code" calls `authClient.emailOtp.sendVerificationOtp({ email, type: "sign-in" })` and then shows the code field.
    - A wrong code shows a `Notice` "That code didn't work. Check it or send a new one."
    - Success calls `router.push(next)`.
  - **AccountMenu:**
    - It renders nothing when disabled.
    - Signed out, it shows a "Sign in" link to `/login?next=<pathname>`.
    - Signed in, it shows a button labelled with the first letter of the name or email, and the menu holds "Portfolio", "Sign out" and "Delete account".
  - **NavList:** it shows "Portfolio" (href `/portfolio`) only when `accounts`.
  - **e2e:** go to `/login?next=/`, fill `e2e-<Date.now()>@example.test`, send the code, fill `246810`, and expect the avatar button. Then sign out and expect "Sign in". Run at both sizes.

- [ ] **Step 2: Run the tests.** `npm test -- login account-menu`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - `login/page.tsx` is a server component.
    - If already signed in, `redirect(safeNext(next))`.
    - Otherwise render `AppShell` with crumbs `[{label: "Sign in"}]` and `<LoginView google={...} next={safeNext(next)} />`.
  - `AccountMenu` uses `authClient.useSession()` on the client, so pages that read no cookies stay statically cacheable.
  - `AppShell` places `<AccountMenu enabled={accountsEnabled()} />` in `.topbar` after the search. `MobileNav` places it in `.mobileTop` before the search icon.
  - Delete account is wired in Task 12. Until then the item is hidden.

- [ ] **Step 4: Run the tests.**
  - `npm test && npm run typecheck && npm run lint`
  - Then, with the API and Postgres up, `npm run e2e`.
  - Expected: all pass. The existing 22 e2e tests are unchanged.

- [ ] **Step 5: Verify the API end to end, then commit.**
  - Sign in in the browser pane.
  - Fetch `/api/auth/token` there.
  - `curl` `localhost:8000/v1/me` with that bearer token. Expected: `{"user_id": …}`.
  - `git commit -m "Site: sign-in page and account menu"`

# Stage 2 — Import

### Task 4: The writable app pool, publish grants and read roles

**Files:**
- Modify:
  - `src/stockanalysis/config.py`
  - `src/stockanalysis/db/postgres.py`
  - `src/stockanalysis/db/publish.py`
  - `tests/test_postgres_serving.py`

**Interfaces:**
- Produces:
  - `settings.pg_app_dsn: SecretStr | None`
  - `settings.pg_app_schema: str = "app"`, with the same pattern and validator as `pg_schema`
  - `settings.pg_read_roles: list[str] = []`. Each name must match `^[a-z_][a-z0-9_]{0,62}$`.
  - `db.postgres.app_connection() -> ContextManager[psycopg.Connection]`. This is a pooled connection in a transaction, with `search_path` set to `pg_app_schema`, then `pg_schema`. It is not read-only. It raises `ServingUnavailable` exactly as `pooled_reader` does.

- [ ] **Step 1: Write the failing tests.** All need Postgres (`@needs_pg`).
  - **`test_publish_leaves_other_schemas_alone`:**
    - Create schemas `f"{site.schema}x_app"` and `f"{site.schema}x_auth"`, each with a one-row table.
    - Publish three times with `keep=1`, so retention drops releases, then roll back once.
    - Both schemas and their rows survive.
  - **`test_publish_grants_read_roles`:**
    - Create a `NOLOGIN` role `sa_test_reader_<hex>`. Skip if `InsufficientPrivilege`.
    - Set `pg_read_roles=[role]` and publish.
    - `has_schema_privilege(role, site.schema, 'USAGE')` and `has_table_privilege(role, site.schema || '.instruments', 'SELECT')` are both true.
    - Clean up with `DROP OWNED BY` and `DROP ROLE`.
  - **`test_app_connection_can_write_and_falls_back_to_pg_dsn`:**
    - With `pg_app_dsn=None` and `pg_dsn=TEST_DSN`, `app_connection()` can `CREATE TABLE` in a temporary app schema.
    - `pooled_reader()` still refuses writes with `ReadOnlySqlTransaction`.
  - **`test_bad_read_role_name_is_refused`:** `Settings(pg_read_roles=["x; drop"])` raises.

- [ ] **Step 2: Run the tests.** `SA_TEST_PG_DSN=postgresql:///stockanalysis_test .venv/bin/python -m pytest tests/test_postgres_serving.py -k "other_schemas or read_roles or app_connection or read_role" -v`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - **Grants:** in `publish()`, after loading and verifying the target schema and inside the same transaction, run `GRANT USAGE ON SCHEMA {target} TO {role}; GRANT SELECT ON ALL TABLES IN SCHEMA {target} TO {role}` for each role, using `sql.Identifier`.
  - **`app_connection()`:** mirror `reader_pool` with a second pool keyed the same way.
    - Name it `stockanalysis-app`.
    - Use `autocommit=False`.
    - Don't set `default_transaction_read_only`.
    - `configure` sets the search path.
    - Yield inside `conn.transaction()`.

- [ ] **Step 4: Run the tests.** Same command. Expected: PASS. Then run the whole `tests/test_postgres_serving.py` with `SA_TEST_PG_DSN`. Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "Postgres: a writable app pool, and publish grants read roles"`

### Task 5: The Kite parser

**Files:**
- Create:
  - `src/stockanalysis/portfolio/__init__.py` (empty)
  - `src/stockanalysis/portfolio/lots.py`
  - `src/stockanalysis/portfolio/kite.py`
  - `tests/test_portfolio_kite.py`
  - `tests/fixtures/portfolio/kite_holdings.csv`, with exactly the content below. Every number is made up.

```
"Instrument","Qty.","Avg. cost","LTP","Invested","Cur. val","P&L","Net chg.","Day chg.",""
"BAJFINANCE",30,2846.2,2778.96,85386.0,83368.8,-2017.2,-2.36,-1.81,""
"BEL",60,327.68,363.05,19660.8,21783.0,2122.2,10.79,1.64,""
"BHEL",20,160.61,161.18,3212.2,3223.6,11.4,0.35,-1.72,""
"CGPOWER",10,1675.59,1242.24,16755.9,12422.4,-4333.5,-25.86,0.26,""
"DMART",20,1910.35,2116.85,38207.0,42337.0,4130.0,10.81,-1.75,""
"EMBASSY-RR",75,1220.21,1688.01,91515.75,126600.75,35085.0,38.34,-1.81,""
"HDFCBANK",12,904.35,724.36,10852.2,8692.32,-2159.88,-19.9,-1.53,""
"HDFCLIFE",25,1702.76,2004.83,42569.0,50120.75,7551.75,17.74,-1.59,""
"HINDUNILVR",75,1934.79,1858.71,145109.25,139403.25,-5706.0,-3.93,0.19,""
"IRFC",10,1714.89,1943.5,17148.9,19435.0,2286.1,13.33,-0.01,""
"MARUTI",60,1311.4,1206.36,78684.0,72381.6,-6302.4,-8.01,0.34,""
"MID150BEES",50,1116.67,975.86,55833.5,48793.0,-7040.5,-12.61,-1.28,""
"MOM100",20,291.47,265.29,5829.4,5305.8,-523.6,-8.98,-0.02,""
"MOTHERSON",30,2201.86,1985.1,66055.8,59553.0,-6502.8,-9.84,1.92,""
"NIFTYBEES",10,1560.2,1272.3,15602.0,12723.0,-2879.0,-18.45,-0.63,""
"RELIANCE",50,1294.01,1777.21,64700.5,88860.5,24160.0,37.34,-1.69,""
"SBICARD",60,1740.43,2284.9,104425.8,137094.0,32668.2,31.28,-0.75,""
"SBIN",30,1803.39,1994.42,54101.7,59832.6,5730.9,10.59,-0.18,""
"SGBJUN31I",10,2836.81,2927.22,28368.1,29272.2,904.1,3.19,0.66,""
"TATASTEEL",5,2206.92,2023.14,11034.6,10115.7,-918.9,-8.33,0.31,""
"TCS",100,2474.68,2225.27,247468.0,222527.0,-24941.0,-10.08,-0.46,""
"TMCV",100,1073.67,1458.53,107367.0,145853.0,38486.0,35.85,-0.58,""
"TMPV",75,395.43,293.12,29657.25,21984.0,-7673.25,-25.87,1.07,""
```

**Interfaces:**
- Produces:

```python
MAX_BYTES = 1_000_000
MAX_ROWS = 2_000
NOT_KITE = "This isn't a Kite holdings file. In Kite, open Holdings and choose Download to get one."

@dataclass(frozen=True)
class ParsedRow:
    symbol: str                 # stripped, as written (case kept)
    quantity: Decimal
    avg_cost: Decimal
    file_price: Decimal | None  # LTP; None when blank

@dataclass(frozen=True)
class ParsedFile:
    format: Literal["kite_csv"]
    rows: list[ParsedRow]

class ImportRefused(ValueError):
    def __init__(self, message: str, problems: list[str] | None = None): ...

def parse_kite_holdings(data: bytes) -> ParsedFile

# lots.py
def combine(q1: Decimal, c1: Decimal, q2: Decimal, c2: Decimal) -> tuple[Decimal, Decimal]
```

`combine` returns `(q1 + q2, (q1*c1 + q2*c2) / (q1 + q2))`, quantized to 4 places with `ROUND_HALF_UP`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_combine_weights_the_cost():
    assert combine(Decimal(10), Decimal(100), Decimal(30), Decimal(200)) == (Decimal(40), Decimal("175.0000"))

def test_reads_the_fixture():
    f = parse_kite_holdings(FIXTURE.read_bytes())
    assert len(f.rows) == 23 and f.rows[0] == ParsedRow("BAJFINANCE", Decimal(30), Decimal("2846.2"), Decimal("2778.96"))
    assert {r.symbol for r in f.rows} >= {"EMBASSY-RR", "SGBJUN31I", "NIFTYBEES", "TMPV"}

def test_survives_bom_crlf_blank_lines_quotes_and_thousands(): ...   # b"\xef\xbb\xbf", "\r\n", "\"1,234.50\"", empty lines
def test_columns_found_by_name_not_position(): ...                  # reordered + an extra column
def test_duplicate_symbols_are_combined(): ...                      # "TCS" 10@100 and "tcs " 30@200 → one row 40@175
def test_missing_required_column_is_refused():                       # no "Avg. cost"
    with pytest.raises(ImportRefused, match="isn't a Kite holdings file"): ...
@pytest.mark.parametrize("data", [b"", b"PK\x03\x04...", b"Trade date,Symbol,Quantity\n"])
def test_wrong_files_are_refused(data): ...                          # Review Focus 2
def test_bad_rows_are_named():                                       # qty 0, qty "abc", cost "-5"
    with pytest.raises(ImportRefused) as e: ...
    assert e.value.problems == ["Row 2: quantity must be more than 0.", "Row 3: quantity isn't a number.", "Row 4: average cost can't be negative."]
def test_limits():                                                   # MAX_BYTES + 1 bytes; MAX_ROWS + 1 rows
    ...  # messages: "The file is larger than 1 MB." / "The file has more than 2,000 holdings."
```

- [ ] **Step 2: Run the tests.** `.venv/bin/python -m pytest tests/test_portfolio_kite.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement** `parse_kite_holdings`.
  - Decode as `utf-8-sig`. A decode error means `NOT_KITE`.
  - Read with `csv.DictReader`.
  - Match headers after stripping, ignoring case.
  - Required: `Instrument`, `Qty.` and `Avg. cost`. `LTP` is optional.
  - Strip `,` from numbers.
  - Skip rows whose every cell is blank.
  - Number rows from 2, the line after the header.
  - Report at most 5 problems, then add `"…and N more."`.
  - Combine duplicates with `lots.combine`. Its file price comes from the last occurrence.

- [ ] **Step 4: Run the tests.** Same command. Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "Portfolio: parse Kite's holdings CSV"`

### Task 6: The holdings store

**Files:**
- Create: `src/stockanalysis/portfolio/store.py`, `tests/test_portfolio_store.py`
- Modify: `src/stockanalysis/cli.py`. `setup-accounts` also calls `store.ensure_schema`.

**Interfaces:**
- Consumes: Task 4's `app_connection()`, and Task 5's `ParsedRow` and `lots.combine`.
- Produces:

```python
Mode = Literal["replace", "add"]

@dataclass(frozen=True)
class StoredHolding:
    symbol: str; isin: str | None; quantity: Decimal; avg_cost: Decimal
    file_price: Decimal | None; uploaded_at: dt.datetime

@dataclass(frozen=True)
class ImportRecord:
    id: int; filename: str; mode: Mode; rows: int; matched: int; created_at: dt.datetime

def ensure_schema(conn) -> None
def save_import(conn, user_id: str, rows: list[ParsedRow], isins: dict[str, str],
                mode: Mode, filename: str, now: dt.datetime) -> ImportRecord   # isins: upper symbol -> ISIN
def load_holdings(conn, user_id: str) -> list[StoredHolding]
def last_import(conn, user_id: str) -> ImportRecord | None
def delete_portfolio(conn, user_id: str) -> int      # rows removed
```

**The DDL** (in `{schema}`; `ensure_schema` is idempotent):

```sql
CREATE TABLE IF NOT EXISTS {schema}.imports (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, user_id TEXT NOT NULL,
  filename TEXT NOT NULL, format TEXT NOT NULL, mode TEXT NOT NULL CHECK (mode IN ('replace','add')),
  row_count INTEGER NOT NULL, matched INTEGER NOT NULL, created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS {schema}.holdings (
  user_id TEXT NOT NULL, key TEXT NOT NULL, symbol TEXT NOT NULL, isin TEXT,
  quantity NUMERIC(20,4) NOT NULL CHECK (quantity > 0), avg_cost NUMERIC(20,4) NOT NULL CHECK (avg_cost >= 0),
  file_price NUMERIC(20,4), uploaded_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (user_id, key));
CREATE INDEX IF NOT EXISTS imports_user ON {schema}.imports (user_id, id DESC);
```

**Keys and combining:**
- `key` is the ISIN when matched, otherwise `upper(symbol)`.
- On Add, a matched incoming row also folds in any saved row for the same user where `isin IS NULL AND upper(symbol) = upper(new symbol)`. This covers Review Focus 5.

- [ ] **Step 1: Write the failing tests.** These need Postgres. Each test uses its own `pg_app_schema` named `apptest_<hex>`, dropped afterwards.

```python
def test_replace_swaps_everything(store): ...        # save A,B; replace with C → only C
def test_add_combines_same_isin(store): ...           # 10@100 + 30@200 → 40@175; file_price from the newer upload
def test_add_folds_an_unmatched_row_once_it_matches(store): ...   # saved NEWCO (isin None), add NEWCO matched → one row, key=ISIN
def test_users_never_see_each_other(store): ...       # A saves; B loads → []; B delete_portfolio → 0; A still has rows
def test_failed_save_changes_nothing(store): ...      # second save raises mid-way (monkeypatch) → first portfolio intact
def test_last_import_is_the_newest(store): ...
```

- [ ] **Step 2: Run the tests.** `SA_TEST_PG_DSN=… .venv/bin/python -m pytest tests/test_portfolio_store.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement** `store.py` as above. Each `save_import` runs in the caller's transaction (`app_connection()`). Replace is `DELETE … WHERE user_id = %s`, then inserts. Add is one `INSERT … ON CONFLICT (user_id, key) DO UPDATE` per row, with the cost from `lots.combine` worked out in Python first, plus the fold.

- [ ] **Step 4: Run the tests.** Same command. Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "Portfolio: store holdings per user with replace and add"`

### Task 7: Import and delete routes

**Files:**
- Modify: `src/stockanalysis/serve/portfolio_api.py`, `src/stockanalysis/serve/queries.py`
- Create: `tests/test_portfolio_api.py`

**Interfaces:**
- Consumes: Task 1's `CurrentUser`; Task 4's `app_connection`; Task 5's parser; Task 6's store; the existing `DB` dependency from `serve.api`.
- Produces:
  - **`queries.resolve_symbols(db, symbols: list[str]) -> dict[str, Instrument]`.** Keyed by upper-cased symbol. Matching is `UPPER(i.nse_symbol) IN (…)`. It skips instruments with a `delisting_date`.
  - **`POST /v1/me/portfolio/import?mode=replace|add&filename=<str ≤200>`.** The body is the raw file bytes (any content type). It returns `ImportOut {id:int, filename:str, mode:str, rows:int, matched:int, created_at:datetime}`. A refusal is 422 with `{"detail": {"message": str, "problems": list[str]}}`.
  - **`DELETE /v1/me/portfolio`** returns `{"deleted": int}`.
  - **The `require_postgres` dependency.** It answers 503 "Portfolio needs Postgres." when `serve_backend != "postgres"`.

- [ ] **Step 1: Write the failing tests.** Reuse `test_postgres_serving`'s `site`/`published` fixtures, by importing them or moving them to `conftest.py`. Sign tokens as in Task 1.

```python
def test_import_replace_then_read_back(...):   # fixture CSV → 200, rows=23, matched = count of fixture symbols in the test universe
def test_add_twice_doubles_quantities(...):
def test_refused_file_changes_nothing(...):     # save fixture, then POST b"PK\x03\x04" → 422, NOT_KITE; holdings unchanged
def test_body_over_limit_is_413(...):            # MAX_BYTES + 1 → 413 "The file is larger than 1 MB."
def test_other_users_portfolio_untouched(...):   # user B DELETE → {"deleted": 0}; user A intact
def test_duckdb_backend_is_503(...):             # serve_backend=duckdb → 503 "Portfolio needs Postgres."
def test_no_token_is_401(...):
```

The test universe needs a symbol that resolves. Use `TEST001`, plus one that doesn't, `NIFTYBEES`, in a small inline CSV. The fixture CSV checks counts only.

- [ ] **Step 2: Run the tests.** `SA_TEST_PG_DSN=… .venv/bin/python -m pytest tests/test_portfolio_api.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement the routes.**
  - Make them `async def`.
  - Read the body from `request.stream()` up to `MAX_BYTES + 1`, then return 413.
  - Parse, then `resolve_symbols` through `DB`.
  - Then `with app_connection() as conn: store.save_import(...)`.
  - Use `now = dt.datetime.now(dt.UTC)`.
  - Run the blocking parts with `starlette.concurrency.run_in_threadpool`.

- [ ] **Step 4: Run the tests.** Same command, and also `tests/test_postgres_serving.py`, because `resolve_symbols` is new read-layer SQL. Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "API: import and delete a portfolio"`

### Task 8: Import page and the site's forwarding handlers

**Files:**
- Create:
  - `web/src/lib/api/user.ts`
  - `web/src/app/api/portfolio/import/route.ts`, `web/src/app/api/portfolio/route.ts`
  - `web/src/app/portfolio/import/page.tsx`
  - `web/src/components/portfolio/ImportForm.tsx`, `portfolio.module.css`
  - `web/tests/unit/portfolio-routes.test.ts`, `web/tests/unit/import-form.test.tsx`
- Modify:
  - `web/openapi.json`, regenerated with `.venv/bin/stockanalysis export-openapi web/openapi.json`
  - `web/src/lib/api/schema.d.ts`, regenerated with `npm run gen:api`
  - `web/src/lib/api/types.ts`: `ImportResult`

**Interfaces:**
- Consumes: Task 2's `apiToken`, the existing `apiBase()`, and Task 7's routes.
- Produces:
  - `class SignedOutError extends Error`
  - `userGet<T>(path: string): Promise<T | null>`. Server only. It reads the token from `headers()`, sends `cache: "no-store"`, throws `SignedOutError` on a 401 or a missing token, and otherwise behaves like `serverGet`.
  - `userSend(path, init, headers): Promise<Response>` for the route handlers.
  - `ImportForm({ hasHoldings }: { hasHoldings: boolean })`.

- [ ] **Step 1: Write the failing tests.**
  - **Route handlers:**
    - A POST whose `Origin` differs from the request URL's origin gets 403.
    - No session gets 401 `{detail: "Sign in again."}`.
    - A valid request forwards the file bytes to `${apiBase()}/v1/me/portfolio/import?mode=add&filename=holdings.csv` with `authorization: Bearer <token>`, and passes the status and body through.
    - A fetch rejection gives 502 `{detail: "Couldn't reach the server. Nothing was changed."}`.
    - DELETE forwards in the same way.
  - **ImportForm:**
    - The default mode is "Replace my portfolio" when `hasHoldings` is false and "Add to my portfolio" when true.
    - Save is disabled until a file is chosen, and disabled while saving. Two quick clicks send one request (Review Focus 1).
    - A 422 shows the message and the problems in `<Notice tone="alert">`.
    - A 401 sends the browser to `/login?next=/portfolio/import`.
    - Success calls `router.push("/portfolio?saved=<id>")`.
  - Mock `fetch` and `authClient` as the existing tests mock `browserGet`.

- [ ] **Step 2: Run the tests.** `npm test -- portfolio-routes import-form`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - The page redirects signed-out visitors to `/login?next=/portfolio/import`.
  - It fetches `/me/portfolio` with `userGet` only to learn `hasHoldings`. Before Task 11, the GET answers 404 FastAPI "Not Found". Treat that as no holdings.
  - Copy:
    - title "Import from Zerodha"
    - drop zone "Drop your Kite holdings file here, or choose a file"
    - help line "In Kite: Holdings → Download (.csv)"
    - modes "Replace my portfolio" (sub "The file becomes your whole portfolio") and "Add to my portfolio" (sub "Keep what's saved and add this file's holdings"), via `Segmented`
    - button "Save portfolio"
  - The input accepts `.csv,text/csv`.
  - The handlers check that `request.headers.get("origin") === new URL(request.url).origin`.

- [ ] **Step 4: Run the tests.** `npm test && npm run typecheck && npm run lint`. Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "Site: import page for Kite holdings"`

# Stage 3 — Portfolio page

### Task 9: Batch read functions for the page

**Files:**
- Modify: `src/stockanalysis/serve/queries.py`, `tests/test_postgres_serving.py`

**Interfaces:**
- Produces. All take a `Database`/`PostgresReader` and a list of ISINs, and return `{}` for an empty list.
  - `latest_closes(db, isins) -> dict[str, tuple[dt.date, float]]`: the newest non-null `close`.
  - `latest_signals(db, isins) -> dict[str, Signal]`: each one's newest row, with `previous` filled, using `_SIGNALS` / `_SIGNAL_COLUMNS` and the `per_instrument` join, filtered by ISIN.
  - `split_bonus_dates(db, isins) -> dict[str, list[dt.date]]`: `action_type IN ('SPLIT','BONUS')`.
  - `complete_run_dates(db) -> set[dt.date]`: the dates where `scored >= COMPLETE_RUN_SHARE * instruments`, from `scored_date_counts(db, limit=None)`.

- [ ] **Step 1: Write the failing tests** in `tests/test_postgres_serving.py`.
  - Unit tests on the DuckDB fixture assert the values for `FIRST`, `MM` and `NOSIGNAL`. `NOSIGNAL` is absent from `latest_signals`.
  - One `@needs_pg` parity test calls each function against `Database(site.path, read_only=True)` and against a `pooled_reader()` on the published copy, and asserts equal results.

- [ ] **Step 2: Run the tests.** `.venv/bin/python -m pytest tests/test_postgres_serving.py -k "latest_closes or latest_signals or split_bonus or complete_run" -v`, with and without `SA_TEST_PG_DSN`. Expected: FAIL.

- [ ] **Step 3: Implement** with portable SQL (Global Constraints).

- [ ] **Step 4: Run the tests.** Same commands. Expected: PASS. Also confirm that `test_every_table_the_read_layer_queries_is_published` passes, since `corporate_actions` and `prices_daily` are already published.

- [ ] **Step 5: Commit.** `git commit -m "Read layer: batch closes, signals, splits and complete runs"`

### Task 10: Portfolio maths

**Files:**
- Create: `src/stockanalysis/portfolio/view.py`, `tests/test_portfolio_view.py`

**Interfaces:**
- Consumes: Task 6's `StoredHolding`, Task 9's shapes, and `queries.Instrument` / `queries.Signal`.
- Produces:

```python
Group = Literal["BUY", "HOLD", "SELL", "NOT_RATED"]
GROUP_ORDER: tuple[Group, ...] = ("BUY", "HOLD", "SELL", "NOT_RATED")
OUTSIDE_UNIVERSE = "Outside the NIFTY 100 companies the model covers. ETFs, gold bonds and REITs are never rated."

@dataclass(frozen=True)
class Lookups:
    instruments: dict[str, Instrument]            # by ISIN
    closes: dict[str, tuple[dt.date, float]]
    signals: dict[str, Signal]
    split_bonus: dict[str, list[dt.date]]
    complete_runs: set[dt.date]
    not_scored_reason: dict[str, str]             # ISIN -> explain.score_status(...).detail
    ratings_as_of: dt.date | None                 # max(complete_runs), or None
    run_model: str | None                         # model_version of the signals on ratings_as_of

@dataclass(frozen=True)
class HoldingRow:
    symbol: str; isin: str | None; name: str | None
    quantity: float; avg_cost: float
    price: float | None; price_date: dt.date | None; price_source: Literal["site", "file"] | None
    value: float | None; invested: float; pnl: float | None; pnl_pct: float | None; weight: float | None
    group: Group; score: float | None; signal_as_of: dt.date | None
    older_rating: bool; earlier_model: bool; was_signal: str | None
    red_flags: list[str]; not_rated_reason: str | None; split_since_upload: bool

@dataclass(frozen=True)
class GroupShare:
    group: Group; holdings: int; value: float; share: float

@dataclass(frozen=True)
class Portfolio:
    holdings: list[HoldingRow]        # grouped in GROUP_ORDER, value desc within, unpriced last
    groups: list[GroupShare]          # all four, in GROUP_ORDER, zeros included
    value: float; invested: float; pnl: float; pnl_pct: float | None
    unpriced: int
    ratings_as_of: dt.date | None     # latest complete run
    older_ratings: int
    prices_from: dt.date | None; prices_to: dt.date | None

def build_portfolio(holdings: list[StoredHolding], look: Lookups) -> Portfolio
```

**Rules,** copied from the spec (§8):

- **Price.**
  - For a matched holding, take the site close if its date is on or after `uploaded_at.date()`; otherwise the file price.
  - If any split or bonus ex-date is later than `uploaded_at.date()`, take the file price and set `split_since_upload`.
  - For an unmatched holding, take the file price.
  - With no price at all, `value`, `pnl` and `weight` are None, and the holding is left out of the totals.
- **Group.** A matched holding with a newest signal in BUY, HOLD or SELL goes in that group. Everything else is `NOT_RATED`.
- **`older_rating`** is `signal_as_of != ratings_as_of`. **`earlier_model`** means `older_rating` and the signal's `model_version` differs from `look.run_model`.
- **`was_signal`** is `previous.signal` when all of these hold:
  - `previous` exists;
  - `previous.model_version == model_version`;
  - `previous.as_of in complete_runs`;
  - `previous.signal != signal`.

  Otherwise it is None.
- **`not_rated_reason`:**
  - unmatched: `OUTSIDE_UNIVERSE`;
  - matched and not rated: `look.not_scored_reason.get(isin, "Not scored yet.")`;
  - rated: None.
- **Weight and share** are value ÷ total priced value. `pnl_pct` is pnl ÷ invested, or None when invested is 0.

- [ ] **Step 1: Write the failing tests.** Build `Lookups` by hand. No database is needed.

```python
def test_site_close_used_when_newer_than_the_upload(): ...
def test_file_price_used_when_newer_than_the_site_close(): ...
def test_split_after_upload_uses_file_price_and_flags_it(): ...      # ex_date upload+1 → price_source "file", split_since_upload
def test_unpriced_holding_is_left_out_of_totals():                    # Review Focus 4
    p = build_portfolio([priced_100, unpriced], look)
    assert p.unpriced == 1 and p.value == 100 and p.holdings[-1].weight is None
def test_groups_order_and_shares_sum_to_one(): ...                    # 4 groups always present; sum(share) == pytest.approx(1)
def test_rows_sorted_by_value_within_group(): ...
def test_older_rating_and_earlier_model_flags(): ...                   # signal 14 Aug model a, run 24 Oct model b → both True
def test_was_signal_only_like_for_like():                              # same model + complete prev date → "SELL"
    ...  # different model → None; previous on a partial date → None; same signal → None
def test_not_rated_reasons(): ...                                      # unmatched → OUTSIDE_UNIVERSE; matched unscored → status detail
```

- [ ] **Step 2: Run the tests.** `.venv/bin/python -m pytest tests/test_portfolio_view.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement** `build_portfolio`. The rules above fix the algorithm. Do the maths in `Decimal`, then convert to float at the edge.

- [ ] **Step 4: Run the tests.** Same command. Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "Portfolio: prices, ratings, groups and shares"`

### Task 11: `GET /v1/me/portfolio`

**Files:**
- Modify: `src/stockanalysis/serve/portfolio_api.py`, `tests/test_portfolio_api.py`
- Regenerate: `web/openapi.json`, `web/src/lib/api/schema.d.ts`
- Modify: `web/src/lib/api/types.ts` (`Portfolio`, `Holding`, `GroupShare`)

**Interfaces:**
- Consumes: Tasks 6, 9 and 10.
- Produces: `GET /v1/me/portfolio` returns `PortfolioOut`. That is `Portfolio`'s fields as a pydantic model (`HoldingOut`, `GroupShareOut`), plus `last_import: ImportOut | None`. `PER_USER_PATHS` gains `/v1/me/portfolio`.

- [ ] **Step 1: Write the failing tests.**
  - After an import of an inline CSV, `TEST001` sits in its signal's group with `score` set.
  - `NIFTYBEES` is `NOT_RATED` with `OUTSIDE_UNIVERSE`.
  - `last_import.rows == 2`.
  - `groups` has 4 entries.
  - A user with nothing saved gets 200 with `holdings == []`, `value == 0` and `last_import is None`.
  - User B never sees user A's rows.

- [ ] **Step 2: Run the tests.** `SA_TEST_PG_DSN=… .venv/bin/python -m pytest tests/test_portfolio_api.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - Load holdings and `last_import` in one `app_connection()`.
  - Build `Lookups` through `DB` with Task 9's functions. `ratings_as_of = max(complete_runs, default=None)`; `run_model` is `queries.signals_on(db, as_of=ratings_as_of, limit=1)[0].model_version` when that date exists.
  - Call `explain.score_status(db, isin).detail` for each matched holding without a signal.
  - Then `build_portfolio`.

- [ ] **Step 4: Run the tests.** Run the full Python suite twice, once without and once with `SA_TEST_PG_DSN`. Run `ruff`. Then `npm --prefix web run gen:api && npm --prefix web run typecheck`. Expected: all pass.

- [ ] **Step 5: Commit.** `git commit -m "API: the portfolio page's data"`

### Task 12: The portfolio page

**Files:**
- Create:
  - `web/src/app/portfolio/page.tsx`
  - `web/src/components/portfolio/PortfolioView.tsx`, `SummaryCard.tsx`, `HoldingsCard.tsx`, `DangerZone.tsx`
  - `web/tests/unit/portfolio-view.test.tsx`
  - `web/tests/fixtures/portfolio.json`: a `Portfolio` built from the Task 11 API on the dev data, with the fixture CSV imported
- Modify:
  - `web/src/components/portfolio/portfolio.module.css`
  - `web/src/components/account/AccountMenu.tsx`: un-hide Delete account

**Interfaces:**
- Consumes: Task 8's `userGet` and `SignedOutError`, Task 11's `Portfolio` type, and the existing `SignalChip`, `Pill`, `Card`, `Notice`, `VerdictNote` and `AppShell`.
- Produces: `PortfolioView({ data, saved }: { data: Portfolio; saved: boolean })`.

**Page content, in order (§8):**

1. **Banner.** Shown when `?saved=<id>` equals `last_import.id`. `Saved {rows} holdings from {filename} (replaced your portfolio)`, or `(added to your portfolio)`. Use a `pos`-toned notice styled like `Notice`; add a `success` tone to `Notice` if needed.
2. **Header.** "Your portfolio". The as-of line:
   - `Ratings as of {fmtDate(ratings_as_of)}`;
   - plus ` · {n} holdings carry older ratings` when n > 0;
   - plus ` · Prices {fmtDay(from)}–{fmtDate(to)}` when the two differ, otherwise `Prices {fmtDate(to)}`.

   A link-button "Import from Zerodha" goes to `/portfolio/import`.
3. **SummaryCard.**
   - Current value, Invested, and Profit / loss as `inr`, plus `pct(…, 1, true)`.
   - A bar of `groups`, coloured with `--zone-buy`, `--zone-hold`, `--zone-sell` and `--zone-neutral`, with a legend `{label} {pct(share)}`. Labels come from `SIGNALS` and "Not rated".
   - `VerdictNote date={ratings_as_of} many`.
   - When `unpriced > 0`: `{n} holdings have no price and aren't counted.`
4. **HoldingsCard.** One section per non-empty group.
   - The header shows `SignalChip`, or `Pill tone="grey"` "Not rated", then `{n} holding(s) · {pct(share)}`.
   - Columns: Stock, Qty, Avg cost, Value, Weight, P&L, Score.
   - The symbol links to `/stocks/{encodeURIComponent(symbol)}` when `isin` is set.
   - Pills:
     - `red_flags` use the labels from `config/redflags.ts`, tone `neg`;
     - `was {SIGNALS[was_signal].label}`;
     - `Rated {fmtDate(signal_as_of)}` + `, earlier model` when `earlier_model`;
     - `Split or bonus since your upload. Import again to update` when `split_since_upload`.
   - Not-rated rows show `not_rated_reason` under the name.
   - Missing numbers show "—".
5. **DangerZone,** a quiet link "Delete portfolio".
   - It asks `confirm("Delete your saved portfolio? This can't be undone.")`.
   - Then `fetch("/api/portfolio", {method: "DELETE"})`, then `router.refresh()`.
   - The AccountMenu's "Delete account" asks `confirm("Delete your account and portfolio? This can't be undone.")`, then DELETEs `/api/portfolio`, then calls `authClient.deleteUser()`, then goes to `/`.

**Empty state** (no holdings): one `Card` reading "See your Zerodha holdings beside the model's latest rating of each stock.", plus the "Import from Zerodha" button.

**Layout:** below 720px each row becomes two lines. Symbol, Value and P&L go on the first; Qty, Avg cost and Score on the second. There is no horizontal page scroll.

- [ ] **Step 1: Write the failing tests** on `web/tests/fixtures/portfolio.json`.
  - Groups render in BUY, HOLD, SELL, Not rated order.
  - Legend percentages match `groups[].share`.
  - The banner shows only when `saved`.
  - The "was" pill shows only when `was_signal` is set.
  - The older-rating pill text is exactly `Rated 14 Aug 2026, earlier model` for such a row.
  - The unmatched reason equals `OUTSIDE_UNIVERSE`'s text.
  - The empty state renders for `holdings: []`.
  - A wording check: the rendered text of `PortfolioView`, `ImportForm` and `AccountMenu` contains none of `/sell this|you should|we recommend/i`.
  - `page.tsx` turns `SignedOutError` into `redirect("/login?next=/portfolio")` (Review Focus 3). Test this by mocking `userGet` and `next/navigation`.

- [ ] **Step 2: Run the tests.** `npm test -- portfolio-view`. Expected: FAIL.

- [ ] **Step 3: Implement.** The page is dynamic: it reads cookies through `userGet`. `AppShell` gets `active="portfolio"` and `crumbs={[{label: "Portfolio"}]}`, and its freshness comes from `ratings_as_of`.

- [ ] **Step 4: Run the tests.** `npm test && npm run typecheck && npm run lint`. Expected: PASS.

- [ ] **Step 5: Commit.** `git commit -m "Site: the portfolio page"`

### Task 13: End to end, docs, and the whole-branch check

**Files:**
- Modify:
  - `web/tests/e2e/portfolio.spec.ts`
  - `README.md`: a new section "Accounts and portfolios"
  - `.env.example`, `web/.env.example`

- [ ] **Step 1: Write the e2e test.**
  - Sign in with the fixed code.
  - Go to `/portfolio`. Expect the empty state.
  - Import `tests/fixtures/portfolio/kite_holdings.csv` with Replace. Use `setInputFiles` with a path relative to `web/`: `../tests/fixtures/portfolio/kite_holdings.csv`.
  - Expect the URL to match `/portfolio?saved=`, with the text `Saved 23 holdings from kite_holdings.csv (replaced your portfolio)`.
  - Expect a "Not rated" section containing `NIFTYBEES`, a "SELL", "HOLD" or "BUY" chip, no horizontal scroll, and no console errors.
  - Import again with Add. Expect the `(added to your portfolio)` banner.
  - Delete the portfolio. Expect the empty state.
  - Delete the account. Expect "Sign in".
  - Run at desktop and phone sizes.

- [ ] **Step 2: Run e2e.**
  - Start the API with `preview_start api`, using `.env` with `SA_SERVE_BACKEND=postgres` and the three `SA_AUTH_*` settings.
  - Then run `npm --prefix web run e2e`.
  - Expected: every test passes, the existing ones included.

- [ ] **Step 3: Write the docs.** In the README section:
  - **Local setup:** `stockanalysis setup-accounts`, then `npm --prefix web run auth:migrate`, then the env vars.
  - **The two optional roles, as SQL:**
    - `CREATE ROLE sa_app LOGIN …; ALTER SCHEMA app OWNER TO sa_app;`
    - `CREATE ROLE sa_auth LOGIN …; ALTER SCHEMA auth OWNER TO sa_auth;`
    - `SA_PG_READ_ROLES=["sa_app"]`
    - `SA_PG_APP_DSN`, and the site's `DATABASE_URL` as `sa_auth`.
  - **Every production env var:** the Google OAuth client, `RESEND_API_KEY`/`EMAIL_FROM`, `BETTER_AUTH_SECRET`/`BETTER_AUTH_URL` and the `SA_AUTH_*` settings.
  - **The launch gate (§11):** the lawyer's sign-off and a privacy notice.

- [ ] **Step 4: Run everything.**
  - `.venv/bin/python -m pytest`
  - `SA_TEST_PG_DSN=postgresql:///stockanalysis_test .venv/bin/python -m pytest`
  - `.venv/bin/ruff check src tests`
  - in `web/`: `npm test`, `npm run typecheck`, `npm run lint`, `npm run build` and `npm run e2e`
  - Expected: all pass. `git status` shows no `holdings.csv`.

- [ ] **Step 5: Commit.** `git commit -m "Portfolio: end-to-end test and setup docs"`

---

## Notes for the executor

- **Spec deviations, both deliberate:**
  - §6 says the portfolio SQL "joins" `app` to the stock tables. This plan does two reads instead (holdings from `app`, then stock data by ISIN through the read layer), in the same database, so no SQL crosses schemas and the existing guards stay untouched.
  - §9 lists the grouping maths under web tests. This plan puts the maths in Python (Task 10), and the web tests cover the rendering.
- **When "Portfolio" and "Sign in" show:** whenever the site has `DATABASE_URL` and `BETTER_AUTH_SECRET`. The spec says the site should hide them when the API runs on DuckDB, but the site can't see the API's backend. If the API runs on DuckDB, `/portfolio` shows the API's 503 message through the existing error view.
