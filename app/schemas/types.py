"""Shared field types for response schemas.

`UtcDateTime` is how every timestamp leaves the API: ISO-8601 in UTC with a
`Z` suffix (`2026-11-18T14:00:00Z`), so a browser's `new Date(...)` reads it
as UTC rather than as local time. The columns are `timestamptz` and the
database session runs in UTC, so PostgreSQL hands back aware UTC values; a
SQLite test database hands back naive ones, which are UTC too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import PlainSerializer, WithJsonSchema

__all__ = ["UtcDateTime", "utc_z"]


def utc_z(value: datetime) -> str:
    """`value` as ISO-8601 UTC with a `Z` suffix; a naive value is UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# `WithJsonSchema` keeps `format: date-time` in the OpenAPI schema, which a
# bare `PlainSerializer` would reduce to a plain string.
UtcDateTime = Annotated[
    datetime,
    PlainSerializer(utc_z, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}, mode="serialization"),
]
