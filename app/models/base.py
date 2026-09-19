"""Declarative base and the shared timestamp mixin."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for every Beery model."""


class TimestampMixin:
    """`created_at` / `updated_at` for the persisted game record.

    `default=` and `onupdate=` are ORM-side and fire only for ORM inserts. The
    end-of-game write is a batched raw insert, which bypasses them — a NOT NULL
    column with no database-side default then fails under a strict `sql_mode`
    with "Field 'created_at' doesn't have a default value". `server_default`
    makes the database supply the value, which keeps both paths correct.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )
