"""Building the headline-text -> ISIN alias table.

DESIGN §3.3 calls ticker resolution "its own small problem" and it is the whole
ballgame for the sentiment factor: an unresolved article costs recall, but a
*mis*-resolved one attributes Reliance Power's troubles to Reliance Industries
and feeds a wrong number into a 10%-weighted factor. Recall is recoverable
later by improving the resolver over stored text; a false attribution is
indistinguishable from data once it is in the table.

So every rule here is biased towards precision.

THE THREE WAYS AN ALIAS GOES WRONG
----------------------------------
1. **It names a group, not a company.** "Tata", "Bajaj", "Adani", "Mahindra"
   each front a dozen separately listed companies. Blocked outright — see
   `GROUP_NAMES`. This is not a lookup that can be repaired by choosing the
   biggest company in the group, because the whole point of a news factor is to
   catch the company having the bad week, which is rarely the biggest one.

2. **It is an ordinary English word.** "Page" (Page Industries), "Trent",
   "Titan", "Sun". A single-token alias that is also a common word matches
   sentences that have nothing to do with the company. Blocked via
   `COMMON_WORDS`.

3. **Two companies genuinely share it.** Detected at build time by counting
   ISINs per alias: an alias claimed by more than one instrument is deleted
   rather than assigned to either. `build_aliases` returns those conflicts so
   the count is reportable rather than silent.

HDFC IS THE WORKED EXAMPLE. Until the July 2023 merger, "HDFC" was HDFC Ltd
(the mortgage lender) and "HDFC Bank" was a different listed company. A
resolver that maps "HDFC" to HDFCBANK is right after the merger and wrong
before it, across a backtest window that spans both. Since aliases here carry
no validity dates, the honest resolution is to refuse the bare token.
"""

from __future__ import annotations

import re

import pandas as pd

from stockanalysis.db.database import Database

# Confidence by how the alias was derived. These are the numbers the ingest
# threshold (`news_min_resolution_confidence`, default 0.7) is compared
# against, so the gap between NAME_SHORT and everything else is the design:
# single-token guesses are stored and *not* used, and the findings report what
# that costs in recall.
CONF_CURATED = 0.95
CONF_NAME = 0.95        # full registered name, >= 2 tokens
CONF_NAME_SOLO = 0.85   # the entire registered name is one token ("Cipla")
CONF_SYMBOL = 0.90
CONF_NAME_PREFIX = 0.90  # first two tokens of a >= 3-token name
CONF_NAME_SHORT = 0.60   # first token of a multi-token name — below threshold

# Stripped only from the end of a name. Stripping them anywhere would turn
# "State Bank of India" into "state bank" and "Bank of India" into "bank".
LEGAL_SUFFIXES = (
    "ltd", "limited", "plc", "inc", "incorporated", "corp", "corporation",
    "company", "co", "the",
)

# A generated alias may not end on one of these. They are joining words: an
# alias ending in one is a fragment, and a fragment matches the start of every
# other name built the same way.
CONNECTIVES = frozenset({"of", "and", "the", "for", "in", "on", "at", "to", "a"})

# Group and family names fronting multiple separately listed companies. An
# alias equal to one of these is dropped whatever its source.
GROUP_NAMES = frozenset({
    "tata", "bajaj", "adani", "mahindra", "birla", "aditya", "godrej", "jsw",
    "jindal", "reliance", "hdfc", "kotak", "shriram", "muthoot", "torrent",
    "apollo", "sun", "hero", "indiabulls", "edelweiss", "piramal", "essar",
    "future", "vedanta", "emami", "dabur", "raymond", "wadia", "hinduja",
    "murugappa", "tvs", "sterlite", "bharti", "mothersons", "motherson",
    "welspun", "lodha", "oberoi", "prestige", "brigade", "sobha",
})

# Ordinary English and market vocabulary that some company also uses as its
# whole name or symbol. Only blocks *single-token* aliases; "Page Industries"
# and "Sun Pharmaceutical Industries" still resolve.
COMMON_WORDS = frozenset({
    "page", "trent", "titan", "sun", "asian", "national", "united", "india",
    "indian", "global", "general", "century", "power", "energy", "finance",
    "financial", "bank", "steel", "cement", "motors", "auto", "pharma",
    "healthcare", "hospital", "hospitals", "labs", "laboratories", "industries",
    "enterprises", "technologies", "technology", "services", "systems",
    "solutions", "products", "chemicals", "petroleum", "gas", "oil", "coal",
    "metals", "mining", "paints", "consumer", "retail", "foods", "beverages",
    "insurance", "life", "capital", "securities", "holdings", "ventures",
    "infrastructure", "infra", "construction", "realty", "properties", "estate",
    "telecom", "communications", "media", "entertainment", "networks",
    "transport", "logistics", "shipping", "ports", "airlines", "aviation",
    "electric", "electronics", "engineering", "manufacturing", "textiles",
    "apparel", "cotton", "sugar", "tea", "coffee", "paper", "glass", "tyres",
    "bearings", "tools", "machines", "trading", "exchange", "board", "council",
    "first", "new", "one", "prime", "grand", "royal", "crown", "star", "eagle",
    "delta", "alpha", "beta", "orbit", "vision", "focus", "impact", "force",
    "core", "edge", "peak", "summit", "bright", "smart", "swift", "rapid",
})

# Hand-maintained. Keyed by NSE symbol so it survives an ISIN change, and
# applied only when that symbol is actually in `instruments`.
#
# Deliberately excluded: "hdfc" (see module docstring), "bajaj", "tata",
# "adani", "m and m" style group shorthands, and anything a newsroom would use
# for more than one listed entity.
CURATED_ALIASES: dict[str, tuple[str, ...]] = {
    "RELIANCE": ("ril", "reliance industries"),
    "TCS": ("tata consultancy", "tata consultancy services"),
    "INFY": ("infosys",),
    "HINDUNILVR": ("hul", "hindustan unilever"),
    "SBIN": ("sbi", "state bank of india"),
    "LT": ("l and t", "l t", "larsen and toubro", "larsen toubro"),
    "M&M": ("m and m", "m m", "mahindra and mahindra", "mahindra mahindra"),
    "BAJFINANCE": ("bajaj finance",),
    "BAJAJFINSV": ("bajaj finserv",),
    "BAJAJ-AUTO": ("bajaj auto",),
    "HDFCBANK": ("hdfc bank",),
    "ICICIBANK": ("icici bank",),
    "AXISBANK": ("axis bank",),
    "KOTAKBANK": ("kotak mahindra bank", "kotak bank"),
    "BHARTIARTL": ("bharti airtel", "airtel"),
    "MARUTI": ("maruti suzuki", "maruti"),
    "ONGC": ("oil and natural gas corporation",),
    "IOC": ("indian oil", "indian oil corporation"),
    "BPCL": ("bharat petroleum",),
    "HINDALCO": ("hindalco",),
    "JSWSTEEL": ("jsw steel",),
    "TATASTEEL": ("tata steel",),
    "TATAMOTORS": ("tata motors",),
    "TATAPOWER": ("tata power",),
    "TECHM": ("tech mahindra",),
    "ULTRACEMCO": ("ultratech", "ultratech cement"),
    "ASIANPAINT": ("asian paints",),
    "SUNPHARMA": ("sun pharma", "sun pharmaceutical"),
    "DRREDDY": ("dr reddys", "dr reddy s", "dr reddys laboratories"),
    "ADANIENT": ("adani enterprises",),
    "ADANIPORTS": ("adani ports", "adani ports and special economic zone"),
    "POWERGRID": ("power grid", "power grid corporation"),
    "COALINDIA": ("coal india",),
    "GAIL": ("gail india",),
    "NTPC": ("ntpc",),
    "ITC": ("itc",),
    "WIPRO": ("wipro",),
    "HCLTECH": ("hcl technologies", "hcl tech"),
    # Not "nestle": that is the Swiss parent's news as often as the Indian
    # subsidiary's, and only one of them is in the universe.
    "NESTLEIND": ("nestle india",),
    "BRITANNIA": ("britannia",),
    "EICHERMOT": ("eicher motors", "royal enfield"),
    "HEROMOTOCO": ("hero motocorp",),
    "GRASIM": ("grasim industries",),
    "SHREECEM": ("shree cement",),
    "DIVISLAB": ("divis laboratories", "divi s laboratories"),
    "CIPLA": ("cipla",),
    "INDUSINDBK": ("indusind bank",),
    "SBILIFE": ("sbi life",),
    "HDFCLIFE": ("hdfc life",),
    "ICICIPRULI": ("icici prudential life", "icici pru life"),
    "ICICIGI": ("icici lombard",),
    "DMART": ("dmart", "avenue supermarts"),
    "PIDILITIND": ("pidilite",),
    "SIEMENS": ("siemens india",),
    "ZOMATO": ("zomato",),
    "PAYTM": ("paytm", "one 97 communications"),
    "NYKAA": ("nykaa", "fsn e commerce"),
    "IRCTC": ("irctc",),
    # Not "lic": LIC Housing Finance is separately listed and the press calls
    # it LIC too.
    "LICI": ("life insurance corporation",),
}

_PUNCT = re.compile(r"[^a-z0-9]+")


def normalise(text: str) -> str:
    """Lowercase, punctuation to single spaces, collapsed.

    Applied to both the alias and the article text, so "Dr. Reddy's" and
    "Dr Reddys" land on the same string from either side. Ampersands become
    spaces, which is why the curated table lists both "l and t" and "l t" —
    "L&T" normalises to "l t" while "L and T" normalises to "l and t".
    """
    return _PUNCT.sub(" ", (text or "").lower()).strip()


def strip_legal_suffix(name: str) -> str:
    """Drop trailing Ltd / Limited / Corporation / The from a normalised name."""
    tokens = name.split()
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    while tokens and tokens[0] in ("the",):
        tokens.pop(0)
    return " ".join(tokens)


def is_blocked(alias: str) -> bool:
    """True if this alias must never be used, whatever produced it."""
    tokens = alias.split()
    if not tokens:
        return True
    if len(alias) < 3:
        return True
    if len(tokens) == 1:
        return tokens[0] in GROUP_NAMES or tokens[0] in COMMON_WORDS
    # A two-token alias that is a group name plus a generic word ("tata
    # motors" is fine; "tata industries" is not a company we can pin down).
    return False


def _all_common(alias: str) -> bool:
    """Every token is ordinary vocabulary — "indian oil", "power grid".

    Those are real press names, but they are also ordinary sentences, so they
    are not *generated*. Where one is genuinely the company's public name it
    goes in `CURATED_ALIASES` instead, which is a decision someone made rather
    than a string-slicing accident.
    """
    return all(t in COMMON_WORDS for t in alias.split())


def candidates_for(nse_symbol: str | None, name: str) -> list[tuple[str, str, float]]:
    """Every (alias, source, confidence) this instrument could be called.

    Ambiguity *between* companies is not resolved here — this function does not
    know about the other 99 instruments. `build_aliases` does that pass.
    """
    out: list[tuple[str, str, float]] = []
    full = strip_legal_suffix(normalise(name))

    if full and not is_blocked(full):
        conf = CONF_NAME if len(full.split()) > 1 else CONF_NAME_SOLO
        out.append((full, "NAME", conf))

    if nse_symbol:
        sym = normalise(nse_symbol)
        if sym and not is_blocked(sym):
            out.append((sym, "SYMBOL", CONF_SYMBOL))
        for alias in CURATED_ALIASES.get(nse_symbol.upper(), ()):  # noqa: SIM118
            a = normalise(alias)
            if a and not is_blocked(a):
                out.append((a, "CURATED", CONF_CURATED))

    tokens = full.split()

    # First two tokens of a longer registered name. Newsrooms shorten:
    # "Apollo Hospitals Enterprise" is always "Apollo Hospitals", "Godrej
    # Consumer Products" is "Godrej Consumer", "Tata Consumer Products" is
    # "Tata Consumer". Measured on a live RSS pull, those three alone were the
    # majority of the resolver's misses on in-universe companies.
    #
    # Two tokens is enough to clear the group-name problem — "tata consumer"
    # names one company where "tata" names a dozen — and the build-time
    # conflict pass catches any pair that does not.
    if len(tokens) >= 3:
        # Trailing connectives are stripped first. "Bank of Baroda" otherwise
        # yields the prefix "bank of", which in one live run matched Bank of
        # America, Bank of Korea, Bank of Maharashtra and two Bank of Japan
        # stories — seven false attributions from one alias, and the single
        # worst thing the resolver did.
        prefix = " ".join(tokens[:2]).rstrip()
        while prefix and prefix.split()[-1] in CONNECTIVES:
            prefix = " ".join(prefix.split()[:-1])
        if len(prefix.split()) >= 2 and not is_blocked(prefix) and not _all_common(prefix):
            out.append((prefix, "NAME_PREFIX", CONF_NAME_PREFIX))

    # First token of a multi-token name. Kept below threshold rather than
    # discarded: it is the difference between "Nestle India" and a headline
    # that just says "Nestle", and measuring that gap is more useful than
    # assuming it either way.
    if len(tokens) > 1 and not is_blocked(tokens[0]):
        out.append((tokens[0], "NAME_SHORT", CONF_NAME_SHORT))

    # Highest confidence wins when two sources produce the same string.
    best: dict[str, tuple[str, str, float]] = {}
    for alias, source, conf in out:
        if alias not in best or conf > best[alias][2]:
            best[alias] = (alias, source, conf)
    return list(best.values())


def build_aliases(db: Database) -> tuple[int, list[tuple[str, list[str]]]]:
    """(Re)build `instrument_aliases` from `instruments` plus the curated table.

    Returns the number of aliases stored and the conflicts that were dropped,
    as (alias, [isins]). Rebuilds from scratch — an instrument renamed since
    the last run must not keep its old alias, because that alias is now
    somebody else's.
    """
    instruments = db.query(
        "SELECT isin, nse_symbol, name FROM instruments WHERE name IS NOT NULL"
    )

    claims: dict[str, list[tuple[str, str, float]]] = {}
    for row in instruments.itertuples(index=False):
        for alias, source, conf in candidates_for(row.nse_symbol, row.name):
            claims.setdefault(alias, []).append((row.isin, source, conf))

    rows, conflicts = [], []
    for alias, claimants in sorted(claims.items()):
        isins = sorted({c[0] for c in claimants})
        if len(isins) > 1:
            conflicts.append((alias, isins))
            continue
        isin, source, conf = claimants[0]
        rows.append(
            {"isin": isin, "alias": alias, "source": source, "confidence": conf}
        )

    db.conn.execute("DELETE FROM instrument_aliases")
    if rows:
        db.upsert_df("instrument_aliases", pd.DataFrame(rows), ["isin", "alias"])
    return len(rows), conflicts
