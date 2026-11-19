"""Declarative base and the shared timestamp mixin."""

from datetime import datetime, timezone

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for every Beery model.

    The naming convention gives every constraint and index the model leaves
    unnamed a deterministic name, so the same name appears in PostgreSQL, in a
    SQLite test database, in `db/schema.postgres.sql` and in any later Alembic
    revision that has to drop it. Explicit names on the models still win.
    """

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class TimestampMixin:
    """`created_at` / `updated_at` for the persisted game record.

    These two are the ONLY columns with a `server_default`. Every fact column
    is written explicitly by the application; a database default that quietly
    fills a fact (`false`, `0`) hides a write bug and stores a fact nobody
    played. `tests/test_skeleton/test_db.py` enforces the rule on the metadata.

    `default=` and `onupdate=` are ORM-side and fire only for ORM inserts. The
    end-of-game write is a batched raw insert, which bypasses them — a NOT NULL
    column with no database-side default then fails with PostgreSQL's
    `null value in column "created_at" ... violates not-null constraint`.
    `server_default` makes the database supply the value (`now()`), which keeps
    both paths correct.
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
