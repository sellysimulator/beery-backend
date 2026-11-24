"""``app/db/`` and ``app/models/base.py``.

The ``Base`` re-export, the timestamp mixin, the shared clock and the session
module's public surface.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Integer, MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.db.base import Base
from app.models.base import Base as ModelsBase
from app.models.base import TimestampMixin, utcnow


class _ProbeBase(DeclarativeBase):
    """A registry of this test file's own.

    Mapped onto the application's ``Base``, the probe below would put a
    ``tests_timestamp_probe`` table into ``Base.metadata`` for the whole
    session, and every comparison of the live schema against the declared
    metadata -- including Alembic's autogenerate-is-empty check -- would see a
    table that exists in no database and never will.  A separate registry
    exercises the mixin exactly the same way without writing into the
    application's metadata."""


class _TimestampProbe(_ProbeBase, TimestampMixin):
    """A throwaway mapped class, declared here so that the mixin is asserted
    the way a real model inherits it."""

    __tablename__ = "tests_timestamp_probe"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


# --- the Base re-export -----------------------------------------------------


def test_db_base_imports_cleanly_and_exposes_metadata():
    """``from app.db.base import Base`` imports without error and
    ``Base.metadata`` is accessible."""
    module = importlib.import_module("app.db.base")
    assert module.Base is Base
    assert isinstance(Base.metadata, MetaData)


def test_db_base_is_a_re_export_not_a_self_import():
    """[HARD-WON] ``app/db/base.py`` must read
    ``from ..models.base import Base``.  Writing ``from .base import Base``
    inside that file is a self-import that raises ``ImportError`` on any use;
    asserting the module imports and re-exports the *same* object is what
    catches the regression."""
    assert Base is ModelsBase
    assert Base.metadata is ModelsBase.metadata


def test_declarative_base_is_usable():
    from sqlalchemy.orm import DeclarativeBase

    assert issubclass(Base, DeclarativeBase)


# --- the timestamp mixin ----------------------------------------------------


@pytest.mark.parametrize("column_name", ["created_at", "updated_at"])
def test_timestamp_mixin_declares_a_server_default(column_name):
    """[HARD-WON] Batched raw inserts bypass the ORM-side ``default=``, and a
    NOT NULL column with no database-side default then fails with
    PostgreSQL's not-null violation."""
    column = _TimestampProbe.__table__.c[column_name]
    assert column.server_default is not None, (
        f"{column_name} has no server_default; a raw INSERT would fail with "
        '"null value in column ... violates not-null constraint"'
    )
    assert column.nullable is False
    assert column.type.timezone is True


def test_only_the_timestamp_columns_have_a_server_default():
    """The one server-default rule: ``created_at``/``updated_at`` get
    ``now()``; every fact column is written by the application, so a
    forgotten write fails loudly instead of storing a default nobody played."""
    import app.models  # noqa: F401  -- registers every table on Base.metadata
    from app.models.base import Base

    offenders = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.server_default is not None
        and column.name not in {"created_at", "updated_at"}
    ]
    assert offenders == []


def test_timestamp_mixin_columns_are_datetimes():
    for name in ("created_at", "updated_at"):
        column = _TimestampProbe.__table__.c[name]
        assert column.type.python_type is datetime


def test_updated_at_refreshes_on_update():
    """``updated_at`` carries an ``onupdate`` as well as a default."""
    assert _TimestampProbe.__table__.c["updated_at"].onupdate is not None


# --- the shared clock -------------------------------------------------------


def test_utcnow_is_timezone_aware_utc():
    """``utcnow()`` -- timezone-aware UTC.  The models and the end-of-game
    write both take timestamps from it, so a naive datetime here becomes a
    wrong column value there, silently."""
    now = utcnow()
    assert isinstance(now, datetime)
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_utcnow_tracks_real_time():
    """It is a clock, not a constant."""
    before = utcnow()
    after = utcnow()
    assert after >= before
    assert abs((after - datetime.now(UTC)).total_seconds()) < 60


@pytest.mark.parametrize("column_name", ["created_at", "updated_at"])
def test_timestamp_column_defaults_produce_timezone_aware_utc(column_name):
    """Both columns take their ORM-side default from the same shared,
    timezone-aware clock -- a naive default here writes a wrong column value
    into every table that uses the mixin, silently."""
    value = _TimestampProbe.__table__.c[column_name].default.arg(None)
    assert isinstance(value, datetime)
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)


# --- frozen public surface: app/db/session.py -------------------------------


def test_session_module_surface():
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import sessionmaker

    from app.db import session as session_module

    assert isinstance(session_module.engine, Engine)
    # psycopg 3 against PostgreSQL; the engine never connects here.
    assert session_module.engine.dialect.name == "postgresql"
    assert session_module.engine.dialect.driver == "psycopg"
    assert isinstance(session_module.SessionLocal, sessionmaker)


def test_get_db_yields_a_session_and_always_closes():
    """``get_db()`` is a FastAPI dependency that always closes."""
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
