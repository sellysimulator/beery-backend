"""Alembic environment.

The database URL is read from `app.config.settings.db_url` at runtime rather
than from `alembic.ini`, so migrations always run against the same credentials
the application uses and no password is written into a tracked file.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import create_engine, pool

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db.base import Base

# Populates `Base.metadata`: importing the package imports every model module.
# Without this the metadata is empty here and autogenerate would propose
# dropping every table that exists.
import app.models  # noqa: E402,F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a DBAPI connection, emitting SQL to stdout."""
    context.configure(
        url=settings.db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = create_engine(
        settings.db_url,
        poolclass=pool.NullPool,
        connect_args=settings.db_connect_args,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
