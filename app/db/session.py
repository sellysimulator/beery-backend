"""Synchronous SQLAlchemy engine, session factory and FastAPI dependency."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings

engine = create_engine(
    settings.db_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    # At most DB_POOL_SIZE + DB_MAX_OVERFLOW (2 + 1) connections per process,
    # this game's share of the Aiven service all four Novus games use (budget
    # in DEPLOY.md, "Managed PostgreSQL version"). Database
    # work is short (one write per finished game, a few reads per results
    # page); a request that cannot get a connection within 10 s fails rather
    # than queueing forever.
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=10,
    echo=settings.DEBUG,
    # Includes `options=-c timezone=UTC`: every session reads and writes
    # timestamptz in UTC (`app/config.py`).
    connect_args=settings.db_connect_args,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a database session that always closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
