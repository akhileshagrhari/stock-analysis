"""Headline text -> company mentions.

The matcher itself is deliberately boring: normalise, scan n-grams longest
first, drop matches contained inside longer matches. All of the judgement lives
in `aliases.py`, which decides what is allowed to be an alias at all.

TWO RULES WORTH THE LINES THEY COST
-----------------------------------
**Longest match wins, and shorter overlapping matches are discarded.** Without
this, "Kotak Mahindra Bank" reports a Mahindra & Mahindra mention, and
"Bajaj Finserv" reports Bajaj Finance. The span bookkeeping is the only reason
those do not happen.

**A body-only mention is not news about that company.** This started as a
0.85x discount, on the theory that a strong alias in the body was still worth
something. An audit of the first 58 live attributions said otherwise: of nine
body-only mentions, eight were wrong — Airtel named in a story about Singtel's
results, Kotak Mahindra named as the bank running someone else's IPO, Tata
Capital named in a story about Tata Motors' share price. The one correct case
("GCPL sharpens focus on core") was a company referred to by an acronym the
alias table does not have, which is a recall problem to fix in the alias table
rather than a reason to keep the other eight.

So the multiplier now puts every body-only mention *below* the ingest
threshold. They are stored, counted, and unused — recoverable if a later
measurement says the trade was wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

from stockanalysis.db.database import Database
from stockanalysis.news.aliases import normalise

# Multiplier applied when the alias appears only in the body. Set so the
# strongest possible body-only match — a curated alias at 0.95 — lands at
# 0.665, under the 0.7 default. Measured, not chosen: see the module docstring.
BODY_ONLY_PENALTY = 0.70

HEADLINE = "HEADLINE"
BODY = "BODY"


@dataclass(frozen=True)
class Mention:
    isin: str
    alias: str
    method: str          # SYMBOL | NAME | NAME_SHORT | CURATED | PROVIDER_ENTITY
    confidence: float
    matched_in: str      # HEADLINE | BODY


@dataclass(frozen=True)
class _Alias:
    isin: str
    source: str
    confidence: float


class TickerResolver:
    """Resolves company mentions in text against a prebuilt alias table."""

    def __init__(self, aliases: dict[str, _Alias]) -> None:
        self.aliases = aliases
        self.max_tokens = max((len(a.split()) for a in aliases), default=1)

    @classmethod
    def from_db(cls, db: Database) -> TickerResolver:
        df = db.query("SELECT isin, alias, source, confidence FROM instrument_aliases")
        if df.empty:
            raise EmptyAliasTableError(
                "instrument_aliases is empty. Run `stockanalysis build-aliases` "
                "— without it every article resolves to nothing and the news "
                "ingest silently stores unattributed rows."
            )
        return cls(
            {
                r.alias: _Alias(r.isin, r.source, float(r.confidence))
                for r in df.itertuples(index=False)
            }
        )

    @classmethod
    def from_pairs(cls, pairs: dict[str, tuple[str, str, float]]) -> TickerResolver:
        """Build directly from {alias: (isin, source, confidence)}. For tests."""
        return cls({a: _Alias(*v) for a, v in pairs.items()})

    def resolve(self, headline: str, body: str | None = None) -> list[Mention]:
        """Every company named in the text, best mention per company.

        A story about two companies returns two mentions. That is not a
        failure to disambiguate — "Tata Motors gains as JLR margins beat" is
        news about both, and forcing a single winner would throw away the
        cheaper half of the coverage.
        """
        found: dict[str, Mention] = {}

        for text, where in ((headline, HEADLINE), (body, BODY)):
            if not text:
                continue
            penalty = 1.0 if where == HEADLINE else BODY_ONLY_PENALTY
            for alias, entry in self._scan(text):
                conf = round(entry.confidence * penalty, 4)
                prev = found.get(entry.isin)
                if prev is None or conf > prev.confidence:
                    found[entry.isin] = Mention(
                        isin=entry.isin,
                        alias=alias,
                        method=entry.source,
                        confidence=conf,
                        matched_in=where,
                    )

        return sorted(found.values(), key=lambda m: (-m.confidence, m.isin))

    def _scan(self, text: str) -> list[tuple[str, _Alias]]:
        """Aliases occurring in `text`, longest first, overlaps removed."""
        tokens = normalise(text).split()
        if not tokens:
            return []

        hits: list[tuple[int, int, str, _Alias]] = []
        taken: list[tuple[int, int]] = []

        for n in range(min(self.max_tokens, len(tokens)), 0, -1):
            for i in range(len(tokens) - n + 1):
                entry = self.aliases.get(" ".join(tokens[i : i + n]))
                if entry is None:
                    continue
                span = (i, i + n)
                if any(s <= span[0] and span[1] <= e for s, e in taken):
                    continue
                taken.append(span)
                hits.append((span[0], span[1], " ".join(tokens[i : i + n]), entry))

        return [(alias, entry) for _, _, alias, entry in hits]


class EmptyAliasTableError(RuntimeError):
    """Raised when resolution is attempted before the alias table is built."""


def confirm_mention(
    resolver: TickerResolver, isin: str, headline: str, body: str | None = None
) -> Mention | None:
    """Does `headline`/`body` actually name `isin`?

    Used for provider results that arrive already attributed — GDELT returns
    whatever its full-text index matched, which for a query of "Titan Company"
    includes articles about titanium mining. Accepting a provider's attribution
    unchecked is how a keyword search becomes a data point.
    """
    for m in resolver.resolve(headline, body):
        if m.isin == isin:
            return m
    return None
