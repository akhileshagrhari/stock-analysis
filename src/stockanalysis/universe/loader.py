"""Index universe loading.

NSE publishes authoritative constituent lists as CSV, including ISIN codes. We
fetch those rather than hardcoding a list: inventing or mistyping an ISIN
silently corrupts every downstream join, and the failure looks like missing data
rather than wrong data.

SURVIVORSHIP WARNING
--------------------
The published CSV is a *current* snapshot. It tells you who is in the index
today, not who was in it in 2021 — every company that collapsed out of the index
is already missing. Seeding from it therefore produces a survivorship-unsafe
universe, and `seed_index_from_nse` records no coverage row to say otherwise, so
`Database.membership_is_survivorship_safe` returns False and the backtest engine
emits a loud warning.

To make a window survivorship-safe you need historical membership (NSE index
maintenance circulars, or a vendor feed) loaded via `load_membership_history`.
That is phase-2 work; phase 0 runs unsafe and says so.
"""

from __future__ import annotations

import datetime as dt
import io

import pandas as pd
import requests

from stockanalysis.db.database import Database

NSE_INDEX_CSV = {
    "NIFTY50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY100": "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY200": "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "NIFTY500": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_index_constituents(index_name: str, timeout: int = 30) -> pd.DataFrame:
    """Fetch the current constituent list for an NSE index.

    Returns columns: isin, nse_symbol, name, industry.
    """
    index_name = index_name.upper()
    if index_name not in NSE_INDEX_CSV:
        raise ValueError(
            f"Unknown index {index_name!r}. Known: {sorted(NSE_INDEX_CSV)}"
        )

    session = requests.Session()
    session.headers.update(_HEADERS)
    # NSE hands out a cookie on the main site and rejects bare archive requests
    # from clients that never asked for one.
    try:
        session.get("https://www.nseindia.com", timeout=timeout)
    except requests.RequestException:
        pass

    resp = session.get(NSE_INDEX_CSV[index_name], timeout=timeout)
    resp.raise_for_status()

    df = pd.read_csv(io.StringIO(resp.text))
    df.columns = [c.strip() for c in df.columns]

    required = {"ISIN Code", "Symbol", "Company Name"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"NSE CSV for {index_name} is missing columns {missing}. "
            f"Got: {list(df.columns)}. The published format may have changed."
        )

    out = pd.DataFrame(
        {
            "isin": df["ISIN Code"].astype(str).str.strip(),
            "nse_symbol": df["Symbol"].astype(str).str.strip(),
            "name": df["Company Name"].astype(str).str.strip(),
            "industry": df.get("Industry", pd.Series([None] * len(df))),
        }
    )
    return out[out["isin"].str.startswith("INE", na=False)].reset_index(drop=True)


def seed_index_from_nse(
    db: Database, index_name: str, as_of: dt.date | None = None
) -> int:
    """Seed instruments + current index membership from NSE's published list.

    Deliberately does NOT write an `index_membership_coverage` row: this is a
    current snapshot, so nothing about history is verified.
    """
    as_of = as_of or dt.date.today()
    cons = fetch_index_constituents(index_name)

    instruments = pd.DataFrame(
        {
            "isin": cons["isin"],
            "nse_symbol": cons["nse_symbol"],
            "bse_code": None,
            "name": cons["name"],
            "sector": cons["industry"],
            "industry": cons["industry"],
            "listing_date": None,
            "delisting_date": None,
            "is_active": True,
        }
    )
    db.upsert_df("instruments", instruments, ["isin"])

    membership = pd.DataFrame(
        {
            "index_name": index_name.upper(),
            "isin": cons["isin"],
            # Snapshot: we only know they are members *now*. Claiming an earlier
            # from_date would fabricate history and mask survivorship bias.
            "from_date": as_of,
            "to_date": None,
        }
    )
    db.upsert_df("index_membership", membership, ["index_name", "isin", "from_date"])
    return len(cons)


def backfill_membership_start(
    db: Database, index_name: str, from_date: dt.date, source: str = "ASSUMED"
) -> None:
    """Push current members' `from_date` back so a backtest has any universe at all.

    This is a MODELLING ASSUMPTION, not data: it pretends today's constituents
    were always constituents. It makes phase-0 backtests runnable and their
    absolute returns optimistic. It still writes no coverage row, so the
    survivorship warning stays on.
    """
    db.conn.execute(
        "UPDATE index_membership SET from_date = ? "
        "WHERE index_name = ? AND from_date > ?",
        [from_date, index_name.upper(), from_date],
    )


def load_membership_history(
    db: Database,
    index_name: str,
    history: pd.DataFrame,
    verified_from: dt.date,
    verified_to: dt.date,
    source: str,
) -> int:
    """Load verified historical membership intervals and mark the window safe.

    `history` needs columns: isin, from_date, to_date (to_date NULL == current).
    Only call this with real membership data — writing the coverage row is what
    silences the survivorship warning.
    """
    required = {"isin", "from_date", "to_date"}
    if not required.issubset(history.columns):
        raise ValueError(f"history needs columns {required}, got {set(history.columns)}")

    rows = history.copy()
    rows["index_name"] = index_name.upper()
    n = db.upsert_df(
        "index_membership",
        rows[["index_name", "isin", "from_date", "to_date"]],
        ["index_name", "isin", "from_date"],
    )
    db.upsert_df(
        "index_membership_coverage",
        pd.DataFrame(
            [
                {
                    "index_name": index_name.upper(),
                    "verified_from": verified_from,
                    "verified_to": verified_to,
                    "source": source,
                    "loaded_at": dt.datetime.now(),
                }
            ]
        ),
        ["index_name", "verified_from"],
    )
    return n
