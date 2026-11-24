"""Testcontainers fixtures for the schema suite.

This is the one backend suite that cannot be faked: ``alembic upgrade head``,
the autogenerate drift check, cascade deletes, ``timestamptz`` round trips and
the raw-insert ``server_default`` test all need a real PostgreSQL server, and
SQLite would pass every one of them vacuously.

So the suite runs against an ephemeral PostgreSQL started by Testcontainers
and thrown away afterwards:

1. the image tag is pinned to the production engine, ``postgres:18``;
2. the container is **session** scoped and migrated once, while each test
   gets its own data and cleans up after itself (the ``db`` fixture runs
   every test inside a transaction it rolls back);
3. with no Docker daemon reachable the suite skips -- loudly: the reason is
   both a skip message and a warning, so an all-skipped run does not read
   like a passing one in a terminal.

**The application's ``DB_*`` credentials are never used.**  ``.env`` points at
the owner's live managed PostgreSQL; ``tests/conftest.py`` already redirects
them at a dead port, and every database call in this package goes through a
URL derived from the container.  ``alembic`` runs as a
subprocess whose ``DB_*`` environment is the container's own coordinates, and
``_assert_throwaway`` refuses any other target before a command is run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from itertools import count
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

# --- the pinned container --------------------------------------------------

#: Rule 1 -- the managed instance runs PostgreSQL 18.  Proving the schema
#: against a different major version proves the wrong thing.  DEPLOY.md and
#: render.yaml document the same version (`test_config_surface.py`).
POSTGRES_IMAGE = "postgres:18"

POSTGRES_USER = "beery"
POSTGRES_PASSWORD = "beery-throwaway"

#: The migrated database the schema tests read.
SCHEMA_DATABASE = "beery_schema"
#: Created empty and dropped again around every migration test.
MIGRATION_DATABASE = "beery_migration"

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
ALEMBIC_VERSIONS = BACKEND_ROOT / "alembic" / "versions"
#: The committed DDL, written by `Novus/scripts/db/gen_ddl.py`.
DDL_SNAPSHOT = BACKEND_ROOT / "db" / "schema.postgres.sql"

#: Nine tables, no more (spec §10.1).
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

#: Every table except ``demand_series`` and ``weeks``, which carry no
#: ``TimestampMixin``.
TIMESTAMP_TABLES = tuple(sorted(EXPECTED_TABLES - {"demand_series", "weeks"}))

#: PostgreSQL's reserved key words (which cannot be a column name without
#: double quotes), plus the names ruled out because they read as SQL
#: functions (``min``, ``max``, ``rank``) or are reserved elsewhere (``key``).
FORBIDDEN_COLUMN_NAMES = frozenset(
    {
        # PostgreSQL reserved
        "all",
        "analyse",
        "analyze",
        "and",
        "any",
        "array",
        "as",
        "asc",
        "both",
        "case",
        "cast",
        "check",
        "collate",
        "column",
        "constraint",
        "create",
        "current_date",
        "current_time",
        "current_timestamp",
        "current_user",
        "default",
        "desc",
        "distinct",
        "do",
        "else",
        "end",
        "except",
        "false",
        "fetch",
        "for",
        "foreign",
        "from",
        "grant",
        "group",
        "having",
        "in",
        "initially",
        "intersect",
        "into",
        "lateral",
        "leading",
        "limit",
        "localtime",
        "not",
        "null",
        "offset",
        "on",
        "only",
        "or",
        "order",
        "placing",
        "primary",
        "references",
        "returning",
        "select",
        "session_user",
        "some",
        "symmetric",
        "table",
        "then",
        "to",
        "trailing",
        "true",
        "union",
        "unique",
        "user",
        "using",
        "variadic",
        "when",
        "where",
        "window",
        "with",
        # read as SQL functions, or reserved elsewhere
        "min",
        "max",
        "key",
        "rank",
    }
)

#: The SQLSTATE PostgreSQL raises for an unquoted reserved word in a column
#: position: ``syntax_error``.
SYNTAX_ERROR_SQLSTATE = "42601"

_counter = count(1)


# --- Docker guard ----------------------------------------------------------


class DockerUnavailableWarning(UserWarning):
    """Raised alongside the skip so an all-skipped run is visible.

    A skipped suite looks identical to a passing one in a terminal, and the
    bug this suite guards -- a ``NOT NULL`` column with no
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
    except Exception as exc:  # any failure means "no daemon"
        return (
            "No Docker daemon is reachable, so the schema suite's Testcontainers "
            f"PostgreSQL suite cannot run: {type(exc).__name__}: {exc}. "
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


def _new_container(dbname: str):
    from testcontainers.postgres import PostgresContainer

    # The container's own superuser, because the migration tests create and
    # drop databases of their own.
    return PostgresContainer(
        POSTGRES_IMAGE,
        username=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        dbname=dbname,
        driver="psycopg",
    )


@pytest.fixture(scope="session")
def docker_daemon() -> None:
    require_docker()


@pytest.fixture(scope="session")
def postgres_container(docker_daemon: None) -> Iterator[Any]:
    """The one PostgreSQL every schema test shares."""
    started = time.monotonic()
    with _new_container(SCHEMA_DATABASE) as container:
        elapsed = time.monotonic() - started
        print(f"\n{POSTGRES_IMAGE} ready in {elapsed:.1f}s", file=sys.stderr)
        yield container


@pytest.fixture(scope="session")
def postgres_url(postgres_container: Any) -> str:
    """The container's own URL -- the only database this suite touches."""
    url: str = postgres_container.get_connection_url()
    assert url.startswith("postgresql+psycopg://"), url
    return url


# --- alembic ---------------------------------------------------------------


def _assert_throwaway(url: str, container_url: str) -> None:
    """Refuse to run a migration against anything but the container.

    ``.env``'s ``DB_*`` point at a live managed PostgreSQL.  Nothing in this
    suite may reach it, so every target is checked against the running
    container's host and port and must be one of the throwaway database
    names.
    """
    target, container = make_url(url), make_url(container_url)
    assert (target.host, target.port) == (container.host, container.port), (
        f"refusing to run against {target.host}:{target.port}; the "
        f"throwaway container is {container.host}:{container.port}"
    )
    assert target.database in {
        SCHEMA_DATABASE,
        MIGRATION_DATABASE,
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
            "DB_PORT": str(parsed.port or 5432),
            "DB_USER": parsed.username or "postgres",
            "DB_PASSWORD": parsed.password or "",
            "DB_DATABASE": parsed.database or "",
            "DB_REQUIRE_SSL": "False",
            "DB_SSL_CA": "",
        }
    )
    return env


@pytest.fixture(scope="session")
def alembic(postgres_url: str) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run the real ``alembic`` CLI against a throwaway database."""

    def _run(
        url: str, *args: str, ini: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _assert_throwaway(url, postgres_url)
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
    postgres_url: str, alembic: Callable[..., subprocess.CompletedProcess[str]]
) -> str:
    """The container, migrated to head exactly once for the session."""
    alembic(postgres_url, "upgrade", "head")
    return postgres_url


# --- engines and per-test data --------------------------------------------


@pytest.fixture(scope="session")
def schema_engine(migrated_url: str) -> Iterator[Engine]:
    engine = create_engine(migrated_url, future=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def db(schema_engine: Engine) -> Iterator[Connection]:
    """A connection whose transaction is rolled back after the test.

    The container is migrated once, and each test's data is its own.  A
    rolled-back transaction is the cheapest possible cleanup and it survives
    a failing assertion.  A statement that fails aborts a
    PostgreSQL transaction, so a test that expects an error and then keeps
    using ``db`` wraps the failing statement in ``db.begin_nested()``.
    """
    connection = schema_engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture()
def admin_engine(postgres_url: str) -> Iterator[Engine]:
    """Autocommitting superuser connection, for ``CREATE``/``DROP DATABASE``
    (neither runs inside a transaction block)."""
    engine = create_engine(postgres_url, isolation_level="AUTOCOMMIT", future=True)
    yield engine
    engine.dispose()


@pytest.fixture()
def blank_database(admin_engine: Engine, postgres_url: str) -> Iterator[str]:
    """A freshly created, completely empty database."""
    drop = f"DROP DATABASE IF EXISTS {MIGRATION_DATABASE} WITH (FORCE)"
    with admin_engine.connect() as connection:
        connection.execute(text(drop))
        connection.execute(text(f"CREATE DATABASE {MIGRATION_DATABASE}"))
    yield make_url(postgres_url).set(database=MIGRATION_DATABASE).render_as_string(
        hide_password=False
    )
    with admin_engine.connect() as connection:
        connection.execute(text(drop))


def truncate_all(connection: Connection) -> None:
    """Empty every application table, resetting their id sequences.

    The tables are found in ``information_schema`` rather than listed, so a
    later table is cleaned too; ``alembic_version`` is kept so the database
    stays at head.  ``CASCADE`` makes foreign-key order irrelevant.
    """
    tables = sorted(table_names(connection) - {"alembic_version"})
    if tables:
        connection.execute(
            text(f"TRUNCATE TABLE {', '.join(tables)} RESTART IDENTITY CASCADE")
        )


# --- schema reflection helpers --------------------------------------------


def table_names(connection: Connection) -> set[str]:
    rows = connection.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        )
    )
    return {row[0] for row in rows}


def columns(connection: Connection, table: str) -> dict[str, dict[str, Any]]:
    """``information_schema.columns`` for one table, keyed by column name.

    The keys are upper-cased (``DATA_TYPE``, ``IS_NULLABLE``, ...) so the
    tests read the same whichever catalogue they came from.  ``DATA_TYPE`` is
    PostgreSQL's spelling: ``integer``, ``boolean``, ``numeric``,
    ``character varying``, ``timestamp with time zone``.
    """
    rows = connection.execute(
        text(
            'SELECT column_name AS "COLUMN_NAME", '
            'is_nullable AS "IS_NULLABLE", '
            'column_default AS "COLUMN_DEFAULT", '
            'is_identity AS "IS_IDENTITY", '
            'data_type AS "DATA_TYPE", '
            'character_maximum_length AS "CHARACTER_MAXIMUM_LENGTH", '
            'numeric_precision AS "NUMERIC_PRECISION", '
            'numeric_scale AS "NUMERIC_SCALE" '
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table "
            "ORDER BY ordinal_position"
        ),
        {"table": table},
    ).mappings()
    return {row["COLUMN_NAME"]: dict(row) for row in rows}


def is_autoincrement(column: dict[str, Any]) -> bool:
    """A ``SERIAL`` (``nextval(...)`` default) or identity column."""
    default = column["COLUMN_DEFAULT"] or ""
    return column["IS_IDENTITY"] == "YES" or default.startswith("nextval(")


def indexes(connection: Connection, table: str) -> list[dict[str, Any]]:
    """Every index on ``table``, one row per indexed column.

    Read from ``pg_index`` rather than ``pg_indexes`` so each column comes out
    on its own row, in index order. A unique or primary-key constraint is
    backed by a unique index, so it appears here too.
    """
    rows = connection.execute(
        text(
            'SELECT i.relname AS "INDEX_NAME", '
            'CASE WHEN ix.indisunique THEN 0 ELSE 1 END AS "NON_UNIQUE", '
            'k.ord AS "SEQ_IN_INDEX", '
            'a.attname AS "COLUMN_NAME" '
            "FROM pg_index ix "
            "JOIN pg_class t ON t.oid = ix.indrelid "
            "JOIN pg_class i ON i.oid = ix.indexrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "CROSS JOIN LATERAL unnest(ix.indkey::int2[]) "
            "WITH ORDINALITY AS k(attnum, ord) "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
            "WHERE n.nspname = current_schema() AND t.relname = :table "
            "ORDER BY i.relname, k.ord"
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
    """A type-appropriate value for a NOT NULL column with no default.

    PostgreSQL does not cast an integer to ``boolean`` implicitly, so a
    boolean column gets a real ``False`` -- never ``0``.
    """
    data_type = column["DATA_TYPE"]
    if data_type in {"integer", "bigint", "smallint"}:
        return 1
    if data_type == "boolean":
        return False
    if data_type in {"numeric", "double precision", "real"}:
        return Decimal("1.00")
    if data_type in {"character varying", "character", "text"}:
        length = column["CHARACTER_MAXIMUM_LENGTH"] or 8
        return str(next(_counter))[: int(length)]
    if data_type in {"timestamp with time zone", "timestamp without time zone"}:
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    raise AssertionError(f"no placeholder for {data_type!r}: {column}")


def insert_row(
    connection: Connection,
    table: str,
    *,
    with_timestamps: bool = False,
    **values: Any,
) -> int:
    """Raw ``INSERT`` naming only the columns that must be named.

    Serial columns, nullable columns, columns with a default and -- unless
    ``with_timestamps`` -- ``created_at``/``updated_at`` are all left out,
    which is exactly the insert end-of-game persistence makes: it writes
    with batched raw inserts that bypass every ORM-side default.

    Returns the new row's ``id`` (via ``RETURNING``), or 0 for a table with
    no ``id`` column (``user_stats`` is keyed by ``user_id``).
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
        if is_autoincrement(column):
            continue
        if name in _TIMESTAMP_COLUMNS and not with_timestamps:
            continue
        if column["IS_NULLABLE"] == "YES" or column["COLUMN_DEFAULT"] is not None:
            continue
        payload[name] = _placeholder(column)

    names = ", ".join(payload)
    binds = ", ".join(f":{name}" for name in payload)
    returning = " RETURNING id" if "id" in spec else ""
    result = connection.execute(
        text(f"INSERT INTO {table} ({names}) VALUES ({binds}){returning}"), payload
    )
    return int(result.scalar_one()) if returning else 0


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


# --- the app's engine, pointed at the container ----------------------------


@pytest.fixture()
def app_connection(migrated_url: str) -> Iterator[Connection]:
    """A connection from the app's own ``app/db/session.py`` engine.

    The timezone test turns on the ``options=-c timezone=UTC`` connect argument
    ``app/config.py`` hands that engine, so the test has to use *that* engine
    rather than one this file builds.  The module is reloaded with
    ``settings`` pointed at the container, and both are restored afterwards.
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
    """PostgreSQL's parse error for an unquoted reserved word (SQLSTATE
    ``42601``, ``syntax_error``)."""
    orig = getattr(exc, "orig", exc)
    return getattr(orig, "sqlstate", None) == SYNTAX_ERROR_SQLSTATE


_DDL_NOISE_PREFIXES = (
    "--",
    "\\restrict",
    "\\unrestrict",
    "SET ",
    "SELECT pg_catalog.set_config",
)


def normalize_ddl(dump: str) -> str:
    """Drop everything in a pg_dump that varies between runs or pg_dump minors.

    A copy of ``normalize()`` in ``Novus/scripts/db/gen_ddl.py``, which writes
    ``db/schema.postgres.sql``: the two must stay identical for the snapshot
    comparison to mean anything. Removed: comments, blank lines, session
    ``SET``/``set_config`` lines and the ``\\restrict``/``\\unrestrict`` guard
    lines (random token, pg_dump >= 17.6).
    """
    keep = []
    for line in dump.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(_DDL_NOISE_PREFIXES):
            continue
        keep.append(line.rstrip())
    return "\n".join(keep) + "\n"
