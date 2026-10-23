"""Corporate actions from NSE — splits, bonuses, dividends, rights.

Why this exists: the price series and a filing's per-share figures are on
different share bases whenever a split or bonus falls between them. yfinance
re-bases the whole price history to today's share count (see
`ingest.prices`), while an EPS figure stays on the share count of the day it was
filed. RELIANCE's FY2024 EPS of 102.90 was filed in April 2024, six months
before a 1:1 bonus, and dividing it by a bonus-adjusted ₹1,310 made the stock
look twice as cheap as it was. `factors.share_basis` restates such figures
using what this module stores.

WHY NSE AND NOT YAHOO'S OWN SPLIT LIST
--------------------------------------
Yahoo publishes the split events behind its adjustment, and taking them would
have been free — the same request as the prices. But its list is incomplete in
exactly the case that matters: BAJFINANCE's June 2025 4:1 bonus and ₹2 → ₹1
split appear as a single 2.0 "split", while its price series is adjusted
tenfold. Restating EPS by Yahoo's list would have left it five times off. NSE's
feed lists both events, with the exchange's own ex-dates.

What NSE cannot vouch for is that Yahoo applied the adjustment it lists, so
`unadjusted_in_prices` checks each split and bonus against the stored closes:
an adjusted series is continuous across the ex-date, an unadjusted one jumps by
the ratio.

WHAT `ratio` MEANS
------------------
One column, read per type, and documented on the API model the same way:

    SPLIT, BONUS   shares after ÷ shares before. Bonus 1:1 is 2.0, bonus 4:1 is
                   5.0, a split from ₹10 to ₹2 face value is 5.0. A
                   consolidation is below 1.
    RIGHTS         new shares offered per share held. Rights 1:15 is 0.0667.
    DIVIDEND       NULL — the amount is `amount`, ₹ per share on the share
                   basis of the ex-date.

`details` keeps NSE's subject line verbatim, so a parse can be checked against
what the exchange actually said.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from stockanalysis.config import settings
from stockanalysis.db.database import Database

log = logging.getLogger(__name__)

SOURCE_NSE = "NSE"

SPLIT, BONUS, DIVIDEND, RIGHTS, DEMERGER, BUYBACK = (
    "SPLIT", "BONUS", "DIVIDEND", "RIGHTS", "DEMERGER", "BUYBACK",
)
#: The two that change the share count without changing what a holder owns —
#: the ones a per-share figure has to be restated across.
SHARE_BASIS_ACTIONS = (SPLIT, BONUS)

# How far back a fetch reaches when the caller names no start. The price
# series begins in 2020 and the oldest XBRL fundamentals in 2023; ten years
# covers both with room for a longer backfill.
DEFAULT_YEARS = 10

_NUM = r"(\d+(?:\.\d+)?)"
_RUPEES = r"(?:rs|re|inr)\.?\s*"
_BONUS = re.compile(rf"\bbonus\b[^0-9]*{_NUM}\s*:\s*{_NUM}", re.IGNORECASE)
_FACE_VALUE_CHANGE = re.compile(
    rf"\b(split|sub-?division|consolidation)\b.*?from\s+{_RUPEES}{_NUM}.*?to\s+{_RUPEES}{_NUM}",
    re.IGNORECASE,
)
_DIVIDEND = re.compile(rf"\bdividend\b\s*-?\s*{_RUPEES}{_NUM}", re.IGNORECASE)
_RIGHTS = re.compile(rf"\brights?\b[^0-9]*{_NUM}\s*:\s*{_NUM}", re.IGNORECASE)
_DEMERGER = re.compile(r"\bdemerger\b|\bscheme of arrangement\b", re.IGNORECASE)
_BUYBACK = re.compile(r"\bbuy\s*-?\s*back\b", re.IGNORECASE)
_NAMES_A_SHARE_CHANGE = re.compile(
    r"\b(bonus|split|sub-?division|consolidation)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class CorporateAction:
    isin: str
    ex_date: dt.date
    action_type: str
    ratio: float | None
    amount: float | None
    record_date: dt.date | None
    details: str


def parse_subject(subject: str) -> list[tuple[str, float | None, float | None]]:
    """`(action_type, ratio, amount)` for each action one NSE subject line names.

    One line can name several — "Annual General Meeting/Dividend - Rs 6.50 Per
    Share" is a dividend; "Interim Dividend - Rs 10/Special Dividend - Rs 5" is
    one dividend of ₹15 on that ex-date. A line naming nothing this module
    stores (a bare AGM, an interest payment) returns an empty list.
    """
    text = " ".join(str(subject or "").split())
    out: list[tuple[str, float | None, float | None]] = []

    bonus = _BONUS.search(text)
    if bonus:
        new, held = float(bonus.group(1)), float(bonus.group(2))
        if held > 0:
            out.append((BONUS, (new + held) / held, None))

    change = _FACE_VALUE_CHANGE.search(text)
    if change:
        before, after = float(change.group(2)), float(change.group(3))
        if before > 0 and after > 0:
            out.append((SPLIT, before / after, None))

    dividends = [float(m.group(1)) for m in _DIVIDEND.finditer(text)]
    if dividends:
        out.append((DIVIDEND, None, sum(dividends)))

    rights = _RIGHTS.search(text)
    if rights:
        offered, held = float(rights.group(1)), float(rights.group(2))
        if held > 0:
            out.append((RIGHTS, offered / held, None))

    if _DEMERGER.search(text):
        out.append((DEMERGER, None, None))
    if _BUYBACK.search(text):
        out.append((BUYBACK, None, None))
    return out


def _date(value: object) -> dt.date | None:
    text = str(value or "").strip()
    if not text or text == "-":
        return None
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_actions(raw: list[dict] | None, isin: str) -> list[CorporateAction]:
    """NSE's corporate-actions response as one row per (ex-date, type).

    Two entries of one type on one ex-date are combined rather than one
    overwriting the other under the table's key: ratios multiply (a bonus and
    a split on the same day compound), dividend amounts add, and the subject
    lines are kept side by side.
    """
    combined: dict[tuple[dt.date, str], CorporateAction] = {}
    for record in raw or []:
        if not isinstance(record, dict):
            continue
        ex_date = _date(record.get("exDate"))
        if ex_date is None:
            continue
        subject = " ".join(str(record.get("subject") or "").split())
        record_date = _date(record.get("recDate"))
        parsed = parse_subject(subject)
        # Britannia's "Bonus - 1 Debenture For 1 Equity Share Held" is a bonus
        # of debentures: the share count does not move, and there is no ratio
        # to miss.
        if (
            _NAMES_A_SHARE_CHANGE.search(subject)
            and "debenture" not in subject.lower()
            and not any(t in SHARE_BASIS_ACTIONS for t, _r, _a in parsed)
        ):
            # A share-count change whose ratio could not be read would leave
            # every per-share figure across it on the wrong basis, silently.
            log.warning("%s %s: could not read a ratio from %r", isin, ex_date, subject)
        for action_type, ratio, amount in parsed:
            key = (ex_date, action_type)
            prior = combined.get(key)
            if prior is not None:
                ratio = _combine(prior.ratio, ratio, math.prod)
                amount = _combine(prior.amount, amount, sum)
                subject_text = f"{prior.details} / {subject}"
            else:
                subject_text = subject
            combined[key] = CorporateAction(
                isin=isin,
                ex_date=ex_date,
                action_type=action_type,
                ratio=ratio,
                amount=amount,
                record_date=record_date or (prior.record_date if prior else None),
                details=subject_text,
            )
    return sorted(combined.values(), key=lambda a: (a.ex_date, a.action_type))


def _combine(a: float | None, b: float | None, how: Callable) -> float | None:
    present = [v for v in (a, b) if v is not None]
    return how(present) if present else None


# ----------------------------------------------------------------------
# Ingest
# ----------------------------------------------------------------------


def ingest_corporate_actions(
    db: Database,
    isins: list[str] | None = None,
    since: dt.date | None = None,
    until: dt.date | None = None,
    delay: float | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    nse=None,
) -> int:
    """Fetch and store each company's actions since `since`. Returns rows stored.

    A company's stored rows in the window are replaced by what NSE returns
    now, so an action the exchange later corrects or withdraws does not linger.
    Only when NSE returned something, though: an empty answer is as likely to be
    a throttled request as a company with no actions, and wiping a bonus over a
    429 would silently undo the restatement it drives.

    `nse` is an `nse.NSE` session, injectable for tests.
    """
    until = until or dt.date.today()
    since = since or until - dt.timedelta(days=365 * DEFAULT_YEARS)
    delay = settings.request_delay_seconds if delay is None else delay

    where, params = "", []
    if isins:
        where, params = f"AND isin IN ({', '.join('?' for _ in isins)})", list(isins)
    companies = db.query(
        f"SELECT isin, nse_symbol FROM instruments "
        f"WHERE nse_symbol IS NOT NULL {where} ORDER BY isin",
        params,
    )

    owned = nse is None
    if owned:
        from nse import NSE

        nse = NSE(download_folder=settings.data_dir / "cache")
    total = 0
    try:
        for i, row in enumerate(companies.itertuples(index=False), start=1):
            if progress:
                progress(i, len(companies), row.nse_symbol)
            try:
                time.sleep(delay)
                raw = nse.actions(
                    symbol=row.nse_symbol,
                    from_date=dt.datetime.combine(since, dt.time()),
                    to_date=dt.datetime.combine(until, dt.time()),
                )
            except Exception as e:  # noqa: BLE001 - unofficial API, fails many ways
                log.warning("corporate actions failed for %s: %s", row.nse_symbol, e)
                continue
            if not raw:
                continue

            actions = parse_actions(raw, row.isin)
            db.conn.execute(
                "DELETE FROM corporate_actions "
                "WHERE isin = ? AND source = ? AND ex_date BETWEEN ? AND ?",
                [row.isin, SOURCE_NSE, since, until],
            )
            if actions:
                total += db.upsert_df(
                    "corporate_actions",
                    pd.DataFrame(
                        [{**vars(a), "source": SOURCE_NSE} for a in actions]
                    ),
                    ["isin", "ex_date", "action_type"],
                )
    finally:
        if owned:
            nse.exit()
    return total


# ----------------------------------------------------------------------
# Checking the price series against the actions
# ----------------------------------------------------------------------

# A split or bonus closer to 1 than this cannot be told apart from an ordinary
# day's move, so it is not checked rather than checked badly.
_CHECKABLE = 1.25


@dataclass(frozen=True)
class PriceCheck:
    isin: str
    symbol: str | None
    ex_date: dt.date
    ratio: float          # combined share multiplier on the ex-date
    observed: float       # last close before the ex-date ÷ close on it

    @property
    def adjusted(self) -> bool:
        """Continuous across the ex-date (adjusted), or a jump of about `ratio`.

        Whichever of the two the observed move is nearer to, on a log scale —
        a 1:1 bonus on an unadjusted series halves the price overnight, and no
        trading day in this universe moves it by that much on its own.
        """
        moved = abs(math.log(self.observed))
        return moved < abs(math.log(self.observed / self.ratio))


def price_checks(db: Database, isins: list[str] | None = None) -> list[PriceCheck]:
    """One check per split/bonus ex-date that falls inside a stored price series."""
    where, params = "", []
    if isins:
        where, params = f"AND a.isin IN ({', '.join('?' for _ in isins)})", list(isins)
    df = db.query(
        f"""
        SELECT a.isin, i.nse_symbol, a.ex_date, a.ratio,
               (SELECT p.close FROM prices_daily p
                 WHERE p.isin = a.isin AND p.date < a.ex_date
                 ORDER BY p.date DESC LIMIT 1) AS before_close,
               (SELECT p.close FROM prices_daily p
                 WHERE p.isin = a.isin AND p.date >= a.ex_date
                 ORDER BY p.date LIMIT 1) AS on_close
        FROM corporate_actions a
        LEFT JOIN instruments i ON i.isin = a.isin
        WHERE a.action_type IN ('{SPLIT}', '{BONUS}') AND a.ratio IS NOT NULL {where}
        ORDER BY a.isin, a.ex_date
        """,
        params,
    )
    by_date: dict[tuple[str, dt.date], dict] = {}
    for r in df.itertuples(index=False):
        key = (r.isin, pd.Timestamp(r.ex_date).date())
        entry = by_date.setdefault(
            key, {"symbol": r.nse_symbol, "ratio": 1.0,
                  "before": r.before_close, "on": r.on_close},
        )
        entry["ratio"] *= float(r.ratio)

    out = []
    for (isin, ex_date), e in by_date.items():
        if pd.isna(e["before"]) or pd.isna(e["on"]) or e["on"] <= 0:
            continue
        if 1 / _CHECKABLE < e["ratio"] < _CHECKABLE:
            continue
        out.append(PriceCheck(
            isin=isin,
            symbol=None if pd.isna(e["symbol"]) else e["symbol"],
            ex_date=ex_date,
            ratio=e["ratio"],
            observed=float(e["before"]) / float(e["on"]),
        ))
    return out


def unadjusted_in_prices(db: Database, isins: list[str] | None = None) -> list[PriceCheck]:
    """Splits and bonuses the stored price series did not adjust for.

    Each one is a seam in that company's series, and restating its per-share
    figures by the action would then be wrong on one side of it. The fix is a
    price source that adjusts; the job of this check is to make the seam
    visible rather than let it pass as a crash or a rally.
    """
    return [c for c in price_checks(db, isins) if not c.adjusted]
