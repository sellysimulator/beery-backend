"""Pure formatting for the host export (``15-results-and-export-api.md`` §3.2).

``ExportService`` does no database access and takes no ``Session``. The route
(``app/api/v1/games.py``) assembles ``weeks`` -- one already-joined,
denormalised dict per ``(week, role)`` -- in a single query, and this module
only turns that data into CSV text or a JSON-ready dict. Keeping the
formatting free of I/O is what makes the CSV-injection and Unicode failure
modes testable without a database.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from ..schemas.results import ResultsResponse

__all__ = ["WEEK_EXPORT_COLUMNS", "ExportService"]

# The exact column order from §3.2. `is_bot` is the *seat* attribution from
# `participants` (who holds the role as the game ended); `was_bot` is
# `WeekRecord.was_bot` -- who actually played *that* week. The two differ for
# every week before a `substitute_bot`, which is exactly what a host reading
# the export wants to see, and `was_bot` is otherwise never read anywhere in
# the build even though the column is `NOT NULL`. `was_forced` is also a
# per-week value. This is the one frozen column list -- both the CSV header
# and the keys of every dict the route hands to `to_json` / `to_csv` must
# match it exactly.
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
    "orders_in_flight_after",
    "order_qty",
    "holding_cost",
    "backlog_cost",
    "fixed_order_cost",
    "purchase_cost",
    "week_cost",
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
        "week_cost",
        "cumulative_cost",
    }
)

# A leading one of these turns a spreadsheet cell into a formula. Prefixing it
# with a single quote defuses it while leaving the value visibly intact
# (failure mode 9).
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _sanitise_display_name(value: str) -> str:
    """Defuse a CSV-injection payload in a display name.

    Only the leading character matters: a name starting with `=`, `+`, `-` or
    `@` is prefixed with `'`, which every common spreadsheet treats as "this
    cell is text" and does not render. Anything else is left untouched, so a
    name that merely *contains* a comma, quote, newline or non-ASCII
    character round-trips unchanged (`csv.writer` handles the quoting).
    """
    if value and value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def _csv_cell(column: str, value: Any) -> Any:
    """One cell's value, formatted the way §3.2 requires.

    `None` becomes an empty string (the two Factory-only columns on other
    roles). Money is formatted to exactly two decimals. Everything else is
    handed to `csv.writer` as-is; it stringifies non-string values itself.
    """
    if value is None:
        return ""
    if column == "display_name" and isinstance(value, str):
        return _sanitise_display_name(value)
    if column in _MONEY_COLUMNS:
        return f"{float(value):.2f}"
    return value


class ExportService:
    """Formats an already-assembled record. No I/O, no `Session`."""

    def to_json(self, results: ResultsResponse, weeks: list[dict]) -> dict:
        """The results payload plus a `weeks` array of the same rows as the
        CSV, as plain JSON objects -- unsanitised, since JSON has no formula
        cells to defuse.
        """
        payload = results.model_dump(mode="json")
        payload["weeks"] = weeks
        return payload

    def to_csv(self, weeks: list[dict]) -> str:
        """One row per `(week, role)`, plus a header, in the §3.2 order.

        Always goes through `csv.writer` -- never manual `",".join(...)` --
        so a display name carrying a comma, a quote, a newline or a
        non-ASCII character round-trips through `csv.reader` as one field
        (failure mode 10).
        """
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(WEEK_EXPORT_COLUMNS)
        for row in weeks:
            writer.writerow(
                [_csv_cell(column, row.get(column)) for column in WEEK_EXPORT_COLUMNS]
            )
        return buffer.getvalue()
