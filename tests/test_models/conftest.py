"""Testcontainers fixtures for section 13's schema suite.

``13-db-models-and-migrations.md §3`` says this is the one backend section
that cannot be faked: ``alembic upgrade head``, the autogenerate drift check,
cascade deletes and the ``STRICT_ALL_TABLES`` raw-insert test all need a real
MySQL server, and SQLite would pass every one of them vacuously.

So the suite runs against an ephemeral MySQL started by Testcontainers and
thrown away afterwards.  Per ``§3.1``:

1. the image tag is pinned to the production engine, ``mysql:8.4``;
2. the container starts with ``--sql-mode=STRICT_ALL_TABLES``;
3. the container is **session** scoped and migrated once, while each test
   gets its own data and cleans up after itself (the ``db`` fixture runs
   every test inside a transaction it rolls back);
4. with no Docker daemon reachable the suite skips -- loudly: the reason is
   both a skip message and a warning, so an all-skipped run does not read
   like a passing one in a terminal.

**The application's ``DB_*`` credentials are never used.**  ``.env`` points at
the owner's live managed MySQL; ``tests/conftest.py`` (section 01) already
redirects them at a dead port, and every database call in this package goes
through a URL derived from the container.  ``alembic`` runs as a subprocess
whose ``DB_*`` environment is the container's own coordinates, and
``_assert_throwaway`` refuses any other target before a command is run.

``tests/conftest.py`` belongs to section 01 and is not edited from here
(**D19**), which is why these fixtures live in this file.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import warnings
from collections.abc import Callable, Iterator
from decimal import Decimal
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

# --- pinned by 13-db-models-and-migrations.md §3.1 -------------------------

#: Rule 1 -- the managed instance runs 8.4.  Proving the schema against a
#: different major version proves the wrong thing.
MYSQL_IMAGE = "mysql:8.4"

#: Rule 2 -- without this the §6.1 raw-insert test stops detecting a missing
#: ``server_default``.  Failure mode 11 is the proof that it is load-bearing.
STRICT_SQL_MODE = "STRICT_ALL_TABLES"

#: The sql_mode failure mode 11 contrasts against.  MySQL 8.4's *default*
#: sql_mode already contains ``STRICT_TRANS_TABLES``, which is strict for
#: InnoDB, so merely omitting the flag does not produce a permissive server;
#: strictness has to be cleared explicitly.
NON_STRICT_SQL_MODE = "NO_ENGINE_SUBSTITUTION"

MYSQL_ROOT_PASSWORD = "beery-throwaway"

#: The migrated database the schema tests read.
SCHEMA_DATABASE = "beery_schema"
#: Created empty and dropped again around every migration test.
MIGRATION_DATABASE = "beery_migration"
#: Holds failure mode 11's probe table, so it never pollutes the schema under
#: test (acceptance criterion 1 counts the tables).
PROBE_DATABASE = "beery_probe"

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
ALEMBIC_VERSIONS = BACKEND_ROOT / "alembic" / "versions"

#: §2.1 to §2.9.  Nine tables, no more.
EXPECTED_TABLES = frozenset(
    {
        "users",
        "games",
        "game_configs",
        "role_configs",
        "demand_configs",
        "participants",
        "demand_series",
        "weeks",
        "user_stats",
    }
)

#: Every table except the two §2.7 and §2.8 mark as carrying no
#: ``TimestampMixin`` -- "all tables use ``Base`` and, except where noted,
#: ``TimestampMixin``".
TIMESTAMP_TABLES = tuple(sorted(EXPECTED_TABLES - {"demand_series", "weeks"}))

#: §4.4 plus acceptance criterion 13.
FORBIDDEN_COLUMN_NAMES = frozenset({"order", "min", "max", "key", "rank"})

_counter = count(1)


# --- Docker guard (§3.1 rule 4, failure mode 10) ---------------------------


class DockerUnavailableWarning(UserWarning):
    """Raised alongside the skip so an all-skipped run is visible.

    A skipped suite looks identical to a passing one in a terminal, and the
    bug this section guards -- a ``NOT NULL`` column with no
    ``server_default`` -- is exactly the kind that ships while its test is
    skipped.
    """


def _ping_docker() -> None:
    """Raise unless a Docker daemon answers."""
    from testcontainers.core.docker_client import DockerClient

    DockerClient().client.ping()


def docker_unavailable_reason(probe: Callable[[], Any] = _ping_docker) -> str | None:
    """``None`` when Docker answers, otherwise the reason it did not."""
    try:
        probe()
    except Exception as exc:  # noqa: BLE001 - any failure means "no daemon"
        return (
            "No Docker daemon is reachable, so section 13's Testcontainers "
            f"MySQL suite cannot run: {type(exc).__name__}: {exc}. "
            "CI must reject a run in which this suite collected no "
            "non-skipped test."
        )
    return None


def require_docker(probe: Callable[[], Any] = _ping_docker) -> None:
    """Skip the calling test, loudly, when Docker is unreachable."""
    reason = docker_unavailable_reason(probe)
    if reason is not None:
        warnings.warn(reason, DockerUnavailableWarning, stacklevel=2)
        print(f"\nSKIPPING SECTION 13 SCHEMA SUITE: {reason}", file=sys.stderr)
        pytest.skip(reason)


# --- container -------------------------------------------------------------


def _new_container(sql_mode: str, dbname: str):
    # testcontainers 4.15 moved MySqlContainer to `testcontainers.community`;
    # the `testcontainers.mysql` path §3 shows still works but warns.
    from testcontainers.community.mysql import MySqlContainer

    container = MySqlContainer(
        MYSQL_IMAGE,
        username="root",
        password=MYSQL_ROOT_PASSWORD,
        dbname=dbname,
    )
    # root, because the migration tests create and drop databases of their own.
    return container.with_command(f"--sql-mode={sql_mode}")


def _connection_url(container: Any) -> str:
    return container.get_connection_url().replace("mysql://", "mysql+pymysql://")


@pytest.fixture(scope="session")
def docker_daemon() -> None:
    require_docker()


@pytest.fixture(scope="session")
def mysql_container(docker_daemon: None) -> Iterator[Any]:
    """The one strict-mode MySQL every schema test shares (§3.1 rule 3)."""
    started = time.monotonic()
    with _new_container(STRICT_SQL_MODE, SCHEMA_DATABASE) as container:
        elapsed = time.monotonic() - started
        print(
            f"\n{MYSQL_IMAGE} (--sql-mode={STRICT_SQL_MODE}) ready in "
            f"{elapsed:.1f}s",
            file=sys.stderr,
        )
        yield container


@pytest.fixture(scope="session")
def mysql_url(mysql_container: Any) -> str:
    """The container's own URL -- the only database this suite touches."""
    return _connection_url(mysql_container)


@pytest.fixture(scope="session")
def non_strict_mysql_url(docker_daemon: None) -> Iterator[str]:
    """A second container with strict mode switched off (failure mode 11)."""
    started = time.monotonic()
    with _new_container(NON_STRICT_SQL_MODE, PROBE_DATABASE) as container:
        elapsed = time.monotonic() - started
        print(
            f"\n{MYSQL_IMAGE} (--sql-mode={NON_STRICT_SQL_MODE}) ready in "
            f"{elapsed:.1f}s",
            file=sys.stderr,
        )
        yield _connection_url(container)


# --- alembic ---------------------------------------------------------------


def _assert_throwaway(url: str, container_url: str) -> None:
    """Refuse to run a migration against anything but the container.

    ``.env``'s ``DB_*`` point at a live managed MySQL.  Nothing in this suite
    may reach it, so every target is checked against the running container's
    host and port and must be one of the throwaway database names.
    """
    target, container = make_url(url), make_url(container_url)
    assert (target.host, target.port) == (container.host, container.port), (
        f"refusing to run against {target.host}:{target.port}; the "
        f"throwaway container is {container.host}:{container.port}"
    )
    assert target.database in {
        SCHEMA_DATABASE,
        MIGRATION_DATABASE,
        PROBE_DATABASE,
    }, f"refusing to run against database {target.database!r}"


def _alembic_env(url: str) -> dict[str, str]:
    """``DB_*`` for a subprocess, pointed at the container.

    ``alembic/env.py`` builds its URL from ``settings.db_url``, and an
    environment variable beats a ``.env`` entry in pydantic-settings.
    """
    parsed = make_url(url)
    env = dict(os.environ)
    env.update(
        {
            "DB_HOST": parsed.host or "127.0.0.1",
            "DB_PORT": str(parsed.port or 3306),
            "DB_USER": parsed.username or "root",
            "DB_PASSWORD": parsed.password or "",
            "DB_DATABASE": parsed.database or "",
            "DB_REQUIRE_SSL": "False",
            "DB_SSL_CA": "",
        }
    )
    return env


@pytest.fixture(scope="session")
def alembic(mysql_url: str) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run the real ``alembic`` CLI against a throwaway database."""

    def _run(
        url: str, *args: str, ini: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _assert_throwaway(url, mysql_url)
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(ini or ALEMBIC_INI), *args],
            cwd=str(BACKEND_ROOT),
            env=_alembic_env(url),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if check:
            assert completed.returncode == 0, (
                f"alembic {' '.join(args)} failed:\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        return completed

    return _run


@pytest.fixture(scope="session")
def migrated_url(
    mysql_url: str, alembic: Callable[..., subprocess.CompletedProcess[str]]
) -> str:
    """The container, migrated to head exactly once for the session."""
    alembic(mysql_url, "upgrade", "head")
    return mysql_url


# --- engines and per-test data --------------------------------------------


@pytest.fixture(scope="session")
def schema_engine(migrated_url: str) -> Iterator[Engine]:
    engine = create_engine(migrated_url, future=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def db(schema_engine: Engine) -> Iterator[Connection]:
    """A connection whose transaction is rolled back after the test.

    §3.1 rule 3: the container is migrated once, and each test's data is its
    own.  A rolled-back transaction is the cheapest possible cleanup and it
    survives a failing assertion.
    """
    connection = schema_engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture()
def admin_engine(mysql_url: str) -> Iterator[Engine]:
    """Autocommitting root connection, for ``CREATE``/``DROP DATABASE``."""
    engine = create_engine(mysql_url, isolation_level="AUTOCOMMIT", future=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def blank_database(admin_engine: Engine, mysql_url: str) -> Iterator[str]:
    """A freshly created, completely empty database (§5 criteria 1 to 4)."""
    with admin_engine.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE}"))
        connection.execute(text(f"CREATE DATABASE {MIGRATION_DATABASE}"))
    yield make_url(mysql_url).set(database=MIGRATION_DATABASE).render_as_string(
        hide_password=False
    )
    with admin_engine.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE}"))


# --- schema reflection helpers --------------------------------------------


def table_names(connection: Connection) -> set[str]:
    rows = connection.execute(
        text(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"
        )
    )
    return {row[0] for row in rows}


def columns(connection: Connection, table: str) -> dict[str, dict[str, Any]]:
    """``information_schema.COLUMNS`` for one table, keyed by column name."""
    rows = connection.execute(
        text(
            "SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, DATA_TYPE, "
            "COLUMN_TYPE, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, "
            "NUMERIC_SCALE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
            "ORDER BY ORDINAL_POSITION"
        ),
        {"table": table},
    ).mappings()
    return {row["COLUMN_NAME"]: dict(row) for row in rows}


def indexes(connection: Connection, table: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX"
        ),
        {"table": table},
    ).mappings()
    return [dict(row) for row in rows]


def unique_key_columns(connection: Connection, table: str) -> set[tuple[str, ...]]:
    """The column tuple of every unique index on ``table``."""
    grouped: dict[str, list[str]] = {}
    for row in indexes(connection, table):
        if row["NON_UNIQUE"] == 0:
            grouped.setdefault(row["INDEX_NAME"], []).append(row["COLUMN_NAME"])
    return {tuple(cols) for cols in grouped.values()}


# --- raw inserts -----------------------------------------------------------

_TIMESTAMP_COLUMNS = ("created_at", "updated_at")


def _placeholder(column: dict[str, Any]) -> Any:
    """A type-appropriate value for a NOT NULL column with no default."""
    data_type = column["DATA_TYPE"]
    if data_type in {"int", "bigint", "smallint", "mediumint", "tinyint"}:
        return 0 if column["COLUMN_TYPE"].startswith("tinyint(1)") else 1
    if data_type in {"decimal", "float", "double"}:
        return Decimal("1.00")
    if data_type in {"varchar", "char", "text", "tinytext", "mediumtext", "longtext"}:
        length = column["CHARACTER_MAXIMUM_LENGTH"] or 8
        return str(next(_counter))[: int(length)]
    if data_type in {"datetime", "timestamp", "date"}:
        return "2026-01-02 03:04:05"
    if data_type == "bool":  # pragma: no cover - MySQL reports tinyint(1)
        return 0
    raise AssertionError(f"no placeholder for {data_type!r}: {column}")


def insert_row(
    connection: Connection,
    table: str,
    *,
    with_timestamps: bool = False,
    **values: Any,
) -> int:
    """Raw ``INSERT`` naming only the columns that must be named.

    Auto-increment columns, nullable columns, columns with a default and --
    unless ``with_timestamps`` -- ``created_at``/``updated_at`` are all left
    out, which is exactly the insert failure mode 1 describes: section 14
    writes with batched raw inserts that bypass every ORM-side default.
    """
    spec = columns(connection, table)
    assert spec, f"table {table!r} does not exist"
    unknown = set(values) - set(spec)
    assert not unknown, f"{table} has no column(s) {sorted(unknown)}"

    payload: dict[str, Any] = {}
    for name, column in spec.items():
        if name in values:
            payload[name] = values[name]
            continue
        if "auto_increment" in column["EXTRA"]:
            continue
        if name in _TIMESTAMP_COLUMNS and not with_timestamps:
            continue
        if column["IS_NULLABLE"] == "YES" or column["COLUMN_DEFAULT"] is not None:
            continue
        payload[name] = _placeholder(column)

    names = ", ".join(payload)
    binds = ", ".join(f":{name}" for name in payload)
    result = connection.execute(
        text(f"INSERT INTO {table} ({names}) VALUES ({binds})"), payload
    )
    return int(result.lastrowid or 0)


def insert_user(connection: Connection, **values: Any) -> int:
    values.setdefault("firebase_uid", f"uid-{next(_counter)}")
    return insert_row(connection, "users", **values)


def insert_game(connection: Connection, **values: Any) -> int:
    values.setdefault("room_code", f"R{next(_counter)}")
    return insert_row(connection, "games", **values)


@pytest.fixture()
def make_game(db: Connection) -> Callable[..., int]:
    """``games`` row factory; every child-table test needs one."""

    def _make(**values: Any) -> int:
        return insert_game(db, **values)

    return _make


# --- section 01's engine, pointed at the container (failure mode 9) --------


@pytest.fixture()
def app_connection(migrated_url: str) -> Iterator[Connection]:
    """A connection from section 01's own ``app/db/session.py`` engine.

    Failure mode 9 turns on the ``SET time_zone = '+00:00'`` listener section
    01 registers on that engine, so the test has to use *that* engine rather
    than one this file builds.  The module is reloaded with ``settings``
    pointed at the container, and both are restored afterwards.
    """
    import importlib

    from app.config import settings

    parsed = make_url(migrated_url)
    overrides = {
        "DB_HOST": parsed.host,
        "DB_PORT": parsed.port,
        "DB_USER": parsed.username,
        "DB_PASSWORD": parsed.password or "",
        "DB_DATABASE": parsed.database,
        "DB_REQUIRE_SSL": False,
        "DB_SSL_CA": "",
    }
    previous = {name: getattr(settings, name) for name in overrides}
    for name, value in overrides.items():
        setattr(settings, name, value)

    import app.db.session as session_module

    module = importlib.reload(session_module)
    assert make_url(str(module.engine.url)).database == SCHEMA_DATABASE
    connection = module.engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()
        module.engine.dispose()
        for name, value in previous.items():
            setattr(settings, name, value)
        importlib.reload(session_module)


# --- misc ------------------------------------------------------------------


def is_reserved_word_error(exc: BaseException) -> bool:
    """MySQL's parse error for an unquoted reserved word."""
    return bool(re.search(r"\b1064\b", str(exc)))
