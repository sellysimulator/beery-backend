"""Synchronous SQLAlchemy engine, session factory and FastAPI dependency."""

from collections.abc import Iterator
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import settings

engine = create_engine(
    settings.db_url,
    pool_pre_ping=True,
    pool_recycle=3600,
    # Stated explicitly rather than inheriting QueuePool's 5 + 10, which is too
    # small for a cloud database under a classroom's worth of concurrent rooms.
    pool_size=10,
    max_overflow=20,
    echo=settings.DEBUG,
    connect_args=settings.db_connect_args,
)


@event.listens_for(engine, "connect")
def _set_utc_timezone(dbapi_connection: Any, connection_record: Any) -> None:
    """Keep every timestamp the driver hands back in UTC."""
    cursor = dbapi_connection.cursor()
    cursor.execute("SET time_zone = '+00:00'")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a database session that always closes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
