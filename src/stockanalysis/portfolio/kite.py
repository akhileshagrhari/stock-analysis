"""Parse the holdings CSV that Zerodha's Kite offers under Holdings > Download.

Columns are found by header name, not position, so a reordered or extended
file still reads. Everything is `Decimal`; no floats reach the store.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from stockanalysis.portfolio.lots import combine

MAX_BYTES = 1_000_000
MAX_ROWS = 2_000
MAX_PROBLEMS = 5
NOT_KITE = (
    "This isn't a Kite holdings file. "
    "In Kite, open Holdings and choose Download to get one."
)


@dataclass(frozen=True)
class ParsedRow:
    symbol: str  # stripped, as written (case kept)
    quantity: Decimal
    avg_cost: Decimal
    file_price: Decimal | None  # LTP; None when blank


@dataclass(frozen=True)
class ParsedFile:
    format: Literal["kite_csv"]
    rows: list[ParsedRow]


class ImportRefused(ValueError):
    """The file can't be imported; `problems` names the rows at fault."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.problems = problems or []


def _number(raw: str) -> Decimal | None:
    """A finite Decimal from text like "1,234.50", or None if it isn't one."""
    try:
        value = Decimal(raw.replace(",", "").strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _parse_row(
    symbol: str, qty_raw: str, cost_raw: str, ltp_raw: str | None
) -> tuple[ParsedRow | None, str | None]:
    """One row, or the reason it is unusable (without the "Row N:" prefix)."""
    if not symbol:
        return None, "instrument is missing."
    qty = _number(qty_raw)
    if qty is None:
        return None, "quantity isn't a number."
    if qty <= 0:
        return None, "quantity must be more than 0."
    cost = _number(cost_raw)
    if cost is None:
        return None, "average cost isn't a number."
    if cost < 0:
        return None, "average cost can't be negative."
    price = None
    if ltp_raw is not None and ltp_raw.strip():
        price = _number(ltp_raw)
        if price is None:
            return None, "last price isn't a number."
        if price < 0:
            return None, "last price can't be negative."
    return ParsedRow(symbol, qty, cost, price), None


def parse_kite_holdings(data: bytes) -> ParsedFile:
    if len(data) > MAX_BYTES:
        raise ImportRefused("The file is larger than 1 MB.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportRefused(NOT_KITE) from None

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        raise ImportRefused(NOT_KITE) from None
    index: dict[str, int] = {}
    for i, name in enumerate(header):
        index.setdefault(name.strip().lower(), i)
    try:
        i_sym, i_qty, i_cost = index["instrument"], index["qty."], index["avg. cost"]
    except KeyError:
        raise ImportRefused(NOT_KITE) from None
    i_ltp = index.get("ltp")

    def cell(row: list[str], i: int | None) -> str | None:
        return row[i].strip() if i is not None and i < len(row) else None

    parsed: list[tuple[int, ParsedRow]] = []
    problems: list[str] = []
    seen = 0
    try:
        for row in reader:
            if not any(c.strip() for c in row):
                continue
            seen += 1
            if seen > MAX_ROWS:
                raise ImportRefused("The file has more than 2,000 holdings.")
            line = reader.line_num
            got, why = _parse_row(
                cell(row, i_sym) or "",
                cell(row, i_qty) or "",
                cell(row, i_cost) or "",
                cell(row, i_ltp),
            )
            if got is None:
                problems.append(f"Row {line}: {why}")
            else:
                parsed.append((line, got))
    except csv.Error:
        raise ImportRefused(NOT_KITE) from None

    if problems:
        extra = len(problems) - MAX_PROBLEMS
        shown = problems[:MAX_PROBLEMS]
        if extra > 0:
            shown.append(f"…and {extra} more.")
        raise ImportRefused("Some rows can't be imported.", shown)
    if not parsed:
        raise ImportRefused("The file has no holdings in it.")

    merged: dict[str, ParsedRow] = {}
    for _, r in parsed:
        key = r.symbol.upper()
        prev = merged.get(key)
        if prev is None:
            merged[key] = r
            continue
        qty, cost = combine(prev.quantity, prev.avg_cost, r.quantity, r.avg_cost)
        # The file price comes from the last occurrence.
        merged[key] = ParsedRow(prev.symbol, qty, cost, r.file_price)
    return ParsedFile("kite_csv", list(merged.values()))
