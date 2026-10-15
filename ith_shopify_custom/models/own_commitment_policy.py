"""Pure arithmetic for the Own physical-cover ledger.

This module deliberately has no Odoo import.  It is usable in the local
regression suite and is the only place where the extra-cover equation lives.
"""

import math


def quantity(value):
    """Return a finite non-negative integral quantity, or fail closed."""
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
        or int(value) != value
    ):
        raise ValueError("quantity must be finite, non-negative, and integral")
    return int(value)


def unreflected_extra(reserved, fulfilled, physical_reserved, physical_done):
    """Calculate the stock deduction not already reflected by physical moves.

    ``reserved`` and ``fulfilled`` are Shopify's cumulative Own allocation and
    successful shipment portions.  Physical reservation and completion are
    deliberately separate: a done move must not also cover a reservation.
    """
    reserved = quantity(reserved)
    fulfilled = quantity(fulfilled)
    physical_reserved = quantity(physical_reserved)
    physical_done = quantity(physical_done)
    return max(reserved - physical_reserved, 0) + max(fulfilled - physical_done, 0)


def total_unreflected_extra(lines):
    """Sum line extras while refusing an ambiguous duplicated sale line.

    Each item must be a mapping containing ``sale_line_id``, ``reserved``,
    ``fulfilled``, ``physical_reserved``, and ``physical_done``.  Counting one
    physical sale line twice would deduct it twice from the published quantity,
    so duplicate or invalid line identities are an error rather than a guess.
    """
    seen_sale_lines = set()
    total = 0
    for line in lines:
        if not isinstance(line, dict):
            raise ValueError("commitment line must be a mapping")
        sale_line_id = line.get("sale_line_id")
        if type(sale_line_id) is not int or sale_line_id <= 0:
            raise ValueError("commitment line has an invalid sale line")
        if sale_line_id in seen_sale_lines:
            raise ValueError("physical sale line appears in more than one commitment")
        seen_sale_lines.add(sale_line_id)
        total += unreflected_extra(
            line.get("reserved"),
            line.get("fulfilled"),
            line.get("physical_reserved"),
            line.get("physical_done"),
        )
    return total
