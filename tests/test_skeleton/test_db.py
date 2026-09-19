"""``app/db/`` and ``app/models/base.py`` (01 §2, §3.4-3.6).

Acceptance criteria 8 and 9, and failure mode 3.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Integer, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.db.base import Base
from app.models.base import Base as ModelsBase
from app.models.base import TimestampMixin, utcnow


class _ProbeBase(DeclarativeBase):
    """A registry of this test file's own.

    The probe below used to be mapped onto the application's ``Base``, which
    put a ``tests_timestamp_probe`` table into ``Base.metadata`` for the whole
    session.  Nothing noticed until section 13 shipped a migration: from then
    on, every comparison of the live schema against the declared metadata --
    including Alembic's autogenerate-is-empty check -- saw a table that exists
    in no database and never will.  A separate registry exercises the mixin
    exactly the same way without writing into the application's metadata."""


class _TimestampProbe(_ProbeBase, TimestampMixin):
    """A throwaway mapped class, declared here so that AC 9 is asserted the
    way a later section's real model would inherit the mixin."""

    __tablename__ = "tests_timestamp_probe"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


# --- AC 8 / FM 3 ------------------------------------------------------------


def test_db_base_imports_cleanly_and_exposes_metadata():
    """AC 8: ``from app.db.base import Base`` imports without error and
    ``Base.metadata`` is accessible."""
    module = importlib.import_module("app.db.base")
    assert module.Base is Base
    assert isinstance(Base.metadata, MetaData)


def test_db_base_is_a_re_export_not_a_self_import():
    """FM 3 [HARD-WON]: ``app/db/base.py`` must read
    ``from ..models.base import Base``.  Writing ``from .base import Base``
    inside that file is a self-import that raises ``ImportError`` on any use;
    asserting the module imports and re-exports the *same* object is what
    catches the regression."""
    assert Base is ModelsBase
    assert Base.metadata is ModelsBase.metadata


def test_declarative_base_is_usable():
    from sqlalchemy.orm import DeclarativeBase

    assert issubclass(Base, DeclarativeBase)


# --- AC 9 -------------------------------------------------------------------


@pytest.mark.parametrize("column_name", ["created_at", "updated_at"])
def test_timestamp_mixin_declares_a_server_default(column_name):
    """AC 9 / §3.5 [HARD-WON]: section 14's batched raw inserts bypass the
    ORM-side ``default=``, and a NOT NULL column with no database-side
    default then fails under a strict ``sql_mode``."""
    column = _TimestampProbe.__table__.c[column_name]
    assert column.server_default is not None, (
        f"{column_name} has no server_default; a raw INSERT would fail with "
        '"Field doesn\'t have a default value"'
    )
    assert column.nullable is False
    assert column.type.timezone is True


def test_timestamp_mixin_columns_are_datetimes():
    for name in ("created_at", "updated_at"):
        column = _TimestampProbe.__table__.c[name]
        assert column.type.python_type is datetime


def test_updated_at_refreshes_on_update():
    """§3.5: ``updated_at`` carries an ``onupdate`` as well as a default."""
    assert _TimestampProbe.__table__.c["updated_at"].onupdate is not None


# --- the shared clock (§2, §3.5) -------------------------------------------


def test_utcnow_is_timezone_aware_utc():
    """§2: ``utcnow()`` -- timezone-aware UTC.  Sections 13 and 14 both write
    timestamps through it, so a naive datetime here becomes a wrong column
    value there, silently."""
    now = utcnow()
    assert isinstance(now, datetime)
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_utcnow_tracks_real_time():
    """It is a clock, not a constant."""
    before = utcnow()
    after = utcnow()
    assert after >= before
    assert abs((after - datetime.now(timezone.utc)).total_seconds()) < 60


@pytest.mark.parametrize("column_name", ["created_at", "updated_at"])
def test_timestamp_column_defaults_produce_timezone_aware_utc(column_name):
    """§3.5: both columns take their ORM-side default from the same shared,
    timezone-aware clock -- a naive default here writes a wrong column value
    in sections 13 and 14, silently."""
    value = _TimestampProbe.__table__.c[column_name].default.arg(None)
    assert isinstance(value, datetime)
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)


# --- frozen public surface: app/db/session.py (§2) --------------------------


def test_session_module_surface():
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import sessionmaker

    from app.db import session as session_module

    assert isinstance(session_module.engine, Engine)
    assert isinstance(session_module.SessionLocal, sessionmaker)


def test_get_db_yields_a_session_and_always_closes():
    """§2: ``get_db()`` is a FastAPI dependency that always closes."""
    from app.db.session import get_db

    generator = get_db()
    db = next(generator)
    assert isinstance(db, Session)
    with pytest.raises(StopIteration):
        next(generator)


def test_get_db_closes_even_when_the_caller_raises():
    from app.db.session import get_db

    generator = get_db()
    db = next(generator)
    assert isinstance(db, Session)
    generator.close()
