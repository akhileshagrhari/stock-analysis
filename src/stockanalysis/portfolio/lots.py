"""Combining two lots of one holding into a single lot."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_PLACES = Decimal("0.0001")


def combine(
    q1: Decimal, c1: Decimal, q2: Decimal, c2: Decimal
) -> tuple[Decimal, Decimal]:
    """Merge two lots: total quantity and the quantity-weighted average cost.

    Both results are rounded to four places, half up. Callers guarantee the
    quantities are positive, so the divisor is never zero.
    """
    qty = q1 + q2
    cost = (q1 * c1 + q2 * c2) / qty
    return (
        qty.quantize(_PLACES, rounding=ROUND_HALF_UP),
        cost.quantize(_PLACES, rounding=ROUND_HALF_UP),
    )
