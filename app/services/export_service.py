"""Pure formatting for the export (spec §9.5).

**No chosen name leaves in the file.** Players keep
their usernames on every screen, but the export names each one by seat only:
``display_name`` holds ``P<N>`` (``seat_alias``: Retailer ``P1`` through
Factory ``P4``, ``ROLE_ORDER``), the same in every row and in the JSON's
``per_role``, beside the ``role`` column. The host's name is in neither format.

``ExportService`` does no database access and takes no ``Session``. The route
(``app/api/v1/games.py``) assembles ``weeks`` -- one already-joined,
denormalised dict per ``(week, role)`` -- in a single query, and this module
only turns that data into CSV text or a JSON-ready dict, so the formatting
is testable without a database. No cell holds text a player typed (names are
``P<N>``, ``room_code`` is server-made, ``role`` is an enum, the rest are
numbers and flags), which is why no cell is escaped against spreadsheet
formulas: adding a free-text column means adding that escaping back.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from ..core.enums import ROLE_ORDER, Role
from ..schemas.results import ResultsResponse

__all__ = ["WEEK_EXPORT_COLUMNS", "ExportService", "seat_alias"]


def seat_alias(role: Role | str) -> str:
    """The export's name for whoever held `role`: `P<N>` by seat, `ROLE_ORDER`.

    By seat, not by join order: it is the same for every game, survives a
    `substitute_bot` or a `reclaim_seat` unchanged, and gives a bot seat a
    number like any other.
    """
    return f"P{ROLE_ORDER.index(Role(role)) + 1}"


# The exact column order (spec §9.5). `is_bot` is the *seat* attribution from
# `participants` (who holds the role as the game ended); `was_bot` is
# `WeekRecord.was_bot` -- who actually played *that* week. The two differ for
# every week before a `substitute_bot`, which is exactly what a host reading
# the export wants to see, and `was_bot` is otherwise never read anywhere in
# the build even though the column is `NOT NULL`. `was_forced` is also a
# per-week value. This is the one frozen column list -- both the CSV header
# and the keys of every dict the route hands to `to_json` / `to_csv` must
# match it exactly.
#
# Two names to read with care:
# - `incoming_orders_in_flight_after` (the `weeks` column's
#   `orders_in_flight_after`, renamed at this boundary only): orders the
#   downstream customer placed that are still travelling TO this role. Not the
#   player's on-screen "orders in flight", which are the player's own orders
#   going upstream.
# - `total_week_cost` (not `week_cost`): holding + backlog + fixed order +
#   purchase cost for the week. The screens show "Holding & backlog" and
#   "Order cost" separately and never a single "week cost".
WEEK_EXPORT_COLUMNS: tuple[str, ...] = (
    "room_code",
    "week",
    "role",
    "display_name",
    "is_bot",
    "was_bot",
    "was_forced",
    "customer_demand",
    "opening_inventory",
    "opening_backlog",
    "arrived",
    "incoming_order",
    "obligation",
    "shipped",
    "unfulfilled",
    "closing_inventory",
    "closing_backlog",
    "supply_line_after",
    "incoming_orders_in_flight_after",
    "order_qty",
    "holding_cost",
    "backlog_cost",
    "fixed_order_cost",
    "purchase_cost",
    "total_week_cost",
    "cumulative_cost",
    "production_started",
    "production_queued",
)

_MONEY_COLUMNS = frozenset(
    {
        "holding_cost",
        "backlog_cost",
        "fixed_order_cost",
        "purchase_cost",
        "total_week_cost",
        "cumulative_cost",
    }
)


def _csv_cell(column: str, value: Any) -> Any:
    """One cell's value, formatted the way the export requires.

    `None` becomes an empty string (the two Factory-only columns on other
    roles). Money is formatted to exactly two decimals. Everything else is
    handed to `csv.writer` as-is; it stringifies non-string values itself.
    """
    if value is None:
        return ""
    if column in _MONEY_COLUMNS:
        return f"{float(value):.2f}"
    return value


class ExportService:
    """Formats an already-assembled record. No I/O, no `Session`."""

    def to_json(self, results: ResultsResponse, weeks: list[dict]) -> dict:
        """The results payload plus a `weeks` array of the same rows as the
        CSV, as plain JSON objects. `display_name` is the seat's `P<N>`
        (`seat_alias`) in both `weeks` and `per_role`, never the chosen name.
        """
        payload = results.model_dump(mode="json")
        for entry in payload["per_role"]:
            entry["display_name"] = seat_alias(entry["role"])
        payload["weeks"] = [
            {**row, "display_name": seat_alias(row["role"])} for row in weeks
        ]
        return payload

    def to_csv(self, weeks: list[dict]) -> str:
        """One row per `(week, role)`, plus a header, in the export's column order.

        Every `display_name` is the seat's `P<N>` (`seat_alias`), never the
        chosen name. Always goes through `csv.writer` -- never manual
        `",".join(...)` -- so any cell with a comma, quote or newline still
        round-trips through `csv.reader` as one field.
        """
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(WEEK_EXPORT_COLUMNS)
        for row in weeks:
            named = {**row, "display_name": seat_alias(row["role"])}
            writer.writerow(
                [_csv_cell(column, named.get(column)) for column in WEEK_EXPORT_COLUMNS]
            )
        return buffer.getvalue()
