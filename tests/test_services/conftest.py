"""Shared fixtures and helpers for section 14's black-box test suite.

``14-end-of-game-persistence.md`` writes ``app/services/db_service.py``,
``app/services/stats_service.py`` and ``app/services/claim_service.py`` --
none of which are opened from this package. Everything here is built only
from:

* the frozen public surfaces of sections 07 (``app/core/game_engine.py``,
  ``app/core/stats.py``), 09 (the room document, ``09-state-service.md``
  §2) and 13 (``app/models/game.py``, ``app/models/user.py``);
* ``00-decisions.md`` and ``00-conventions.md``.

Like section 13's suite, this one needs a **real** MySQL: batched raw
inserts, an idempotency lookup, a grouped statistics query and DECIMAL
rounding are all things a fake or a SQLite connection would either not
enforce or would silently get right for the wrong reason. So it shares
section 13's Testcontainers MySQL (``tests/test_models/conftest.py``)
rather than inventing a second container: the fixtures below are *imported*
from that module, not re-implemented, and every test that needs the
database is marked ``dbschema`` so it lands in the same opt-in set.

The three services under test commit real transactions (``persist_game``'s
"one transaction ... commit" per §3.1), so -- unlike section 13's own
``db`` fixture, which never commits and is cleaned up by a rollback -- the
``db`` fixture here truncates every table it touches *after* each test.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config_models import (
    DEFAULT_LIMITS,
    ConstantDemand,
    FactoryConfig,
    GameConfig,
    RoleConfig,
)
from app.core.enums import ROLE_ORDER, Role
from app.core.game_engine import GameEngine, GamePhase

# Reused wholesale from section 13's suite (00-conventions.md §5 / this
# section's own instructions): the container, the migration, the engine,
# and a couple of its raw-insert helpers. Importing them into this module's
# namespace is what makes pytest see them as fixtures here too.
from tests.test_models.conftest import (  # noqa: F401 -- fixtures, used by name
    alembic,
    columns,
    docker_daemon,
    insert_game,
    insert_row,
    insert_user,
    migrated_url,
    mysql_container,
    mysql_url,
    schema_engine,
    table_names,
)

__all__ = [
    "StatementLog",
    "columns",
    "count_statements",
    "insert_game",
    "insert_row",
    "insert_user",
    "make_config",
    "make_participant",
    "make_room_document",
    "run_full_game",
    "schema_engine",
    "table_names",
    "unique_room_code",
]

_counter = itertools.count(1)


def unique_room_code() -> str:
    return f"R{next(_counter):06d}"[:8]


# ---------------------------------------------------------------------------
# Database session, bound to the shared Testcontainers MySQL
# ---------------------------------------------------------------------------

_TABLES_IN_DEPENDENCY_ORDER = (
    "weeks",
    "demand_series",
    "participants",
    "demand_configs",
    "role_configs",
    "game_configs",
    "games",
    "user_stats",
    "users",
)


@pytest.fixture()
def session_factory(schema_engine: Engine) -> Iterator[Callable[[], Session]]:
    """Build as many independent ``Session`` objects as a test needs.

    Section 14's services commit for real (persistence is idempotent by a
    lookup, not by an outer rollback), so cleanup here is a truncate after
    the test rather than the rollback section 13's ``db`` fixture uses.
    """
    factory = sessionmaker(bind=schema_engine, future=True)
    opened: list[Session] = []

    def _make() -> Session:
        session = factory()
        opened.append(session)
        return session

    try:
        yield _make
    finally:
        for session in opened:
            session.close()
        with schema_engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            for table in _TABLES_IN_DEPENDENCY_ORDER:
                conn.execute(text(f"DELETE FROM {table}"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))


@pytest.fixture()
def db(session_factory: Callable[[], Session]) -> Session:
    """A single database session, for tests that only need one."""
    return session_factory()


@pytest.fixture()
def app_db_pointed_at_container(migrated_url: str) -> Iterator[None]:  # noqa: F811
    """Point the *application's own* database session at the container.

    ``persist_finished_game`` (``app/services/game_service.py``) opens its
    own session rather than taking one from the caller (§3.1: "Open a
    session"), so it goes through ``app/db/session.py``'s module-level
    engine -- which ``tests/conftest.py`` (section 01) deliberately points
    at a dead port for every other test, so that nothing touches a real
    database by accident. Tests that call ``persist_finished_game`` (rather
    than ``DbService.persist_game`` with an explicit session) need that
    engine repointed at the throwaway container for the duration of the
    test, exactly as section 13's own ``app_connection`` fixture
    (``tests/test_models/conftest.py``) does for its failure mode 9.
    """
    import importlib

    from sqlalchemy.engine import make_url

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
    try:
        yield
    finally:
        module.engine.dispose()
        for name, value in previous.items():
            setattr(settings, name, value)
        importlib.reload(session_module)


# ---------------------------------------------------------------------------
# Statement counting (failure modes 4 and 5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatementRecord:
    statement: str
    executemany: bool
    batch_size: int | None


@dataclass
class StatementLog:
    """What ran against the engine while the log was attached.

    Records ``executemany`` per statement -- via ``before_cursor_execute``'s
    own flag, not by guessing from parameter shape -- because failure mode 5
    is specifically about *one* ``executemany`` with 144 parameter dicts
    versus 144 separate single-row executes; a plain statement count
    conflates the two.
    """

    records: list[StatementRecord] = field(default_factory=list)

    @property
    def single_statements(self) -> int:
        return sum(1 for r in self.records if not r.executemany)

    @property
    def executemany_statements(self) -> int:
        return sum(1 for r in self.records if r.executemany)

    @property
    def total(self) -> int:
        return len(self.records)

    def matching(self, needle: str) -> list[StatementRecord]:
        """Every record whose statement text contains ``needle`` (a table
        name fragment such as ``"into weeks"``), case-insensitively."""
        lowered = needle.lower()
        return [r for r in self.records if lowered in r.statement.lower()]


@contextmanager
def count_statements(engine: Engine) -> Iterator[StatementLog]:
    """Count statements run against ``engine`` for the life of the block."""
    log = StatementLog()

    def _listener(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        batch_size = (
            len(parameters) if executemany and hasattr(parameters, "__len__") else None
        )
        log.records.append(StatementRecord(statement, executemany, batch_size))

    event.listen(engine, "before_cursor_execute", _listener)
    try:
        yield log
    finally:
        event.remove(engine, "before_cursor_execute", _listener)


# ---------------------------------------------------------------------------
# GameConfig / GameEngine helpers (07-game-engine.md, 03-game-config.md via
# app/core/config_models.py, which every other section's suite already
# imports directly -- see tests/test_services/test_state_service.py)
# ---------------------------------------------------------------------------


def make_config(
    *,
    duration_weeks: int = 8,
    demand: Any = None,
    role_overrides: dict[str, dict[str, Any]] | None = None,
) -> GameConfig:
    """A minimal, valid ``GameConfig`` built only through the frozen
    ``GameConfig.from_host_input`` surface."""
    overrides = role_overrides or {}
    roles = {
        Role.RETAILER.value: RoleConfig(**overrides.get("RETAILER", {})),
        Role.WHOLESALER.value: RoleConfig(**overrides.get("WHOLESALER", {})),
        Role.DISTRIBUTOR.value: RoleConfig(**overrides.get("DISTRIBUTOR", {})),
        Role.FACTORY.value: FactoryConfig(**overrides.get("FACTORY", {})),
    }
    payload = {
        "roles": roles,
        "demand": demand if demand is not None else ConstantDemand(value=4),
        "duration_weeks": duration_weeks,
    }
    return GameConfig.from_host_input(payload, DEFAULT_LIMITS)


def run_full_game(
    config: GameConfig,
    seed: int = 1,
    *,
    stop_after: int | None = None,
    order_fn: Callable[[Role, int], int] | None = None,
    force_missing_role: Role | None = None,
    force_missing_at_week: int | None = None,
    bot_roles: frozenset[Role] = frozenset(),
) -> GameEngine:
    """Play a whole game through the frozen public surface only.

    ``stop_after`` calls ``end_early()`` once that many weeks have closed,
    modelling **acceptance criterion 5**'s "ended early at week 10 of 36".

    ``force_missing_role`` / ``force_missing_at_week`` skips submitting an
    order for one role in one week and force-closes that week instead, so a
    real ``WeekRecord.was_forced`` shows up without hand-building one.

    ``bot_roles`` calls ``set_bot(role, True)`` up front, so the resulting
    ``WeekRecord.was_bot`` is real rather than hand-set.
    """
    order_fn = order_fn or (lambda role, week: 4)
    engine = GameEngine.start(config, seed)
    for role in bot_roles:
        engine.set_bot(role, True)
    played = 0
    while engine.phase != GamePhase.FINISHED:
        if stop_after is not None and played >= stop_after:
            engine.end_early()
            break
        current_week = engine.week
        skip_role = (
            force_missing_role if force_missing_at_week == current_week else None
        )
        for role in ROLE_ORDER:
            if role is skip_role:
                continue
            engine.submit_order(role, order_fn(role, current_week))
        engine.close_week(force=skip_role is not None)
        played += 1
    return engine


# ---------------------------------------------------------------------------
# Room document (09-state-service.md §2, FROZEN) -- built by hand rather
# than through StateService, which is section 09's own public surface
# (§3) and out of this section's reading list.
# ---------------------------------------------------------------------------


def make_participant(
    alias: str,
    *,
    identity: str | None = None,
    display_name: str = "Player",
    role: Role | None = None,
    is_bot: bool = False,
    connected: bool = True,
) -> dict[str, Any]:
    return {
        "alias": alias,
        "identity": identity,
        "session_token": f"token-{alias}",
        "display_name": display_name,
        "role": role.value if isinstance(role, Role) else role,
        "is_bot": is_bot,
        "connected": connected,
    }


def make_room_document(
    config: GameConfig,
    engine: GameEngine,
    *,
    room_code: str | None = None,
    host_identity: str | None = None,
    host_display_name: str = "Host",
    participants: dict[str, dict[str, Any]] | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> dict[str, Any]:
    """A room document matching ``09-state-service.md §2`` exactly, for a
    finished game -- the shape ``build_snapshot`` reads."""
    participants = participants or {}
    started = started_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
    finished = finished_at or started
    role_to_alias: dict[str, str | None] = {role.value: None for role in Role}
    for alias, participant in participants.items():
        if participant.get("role"):
            role_to_alias[participant["role"]] = alias
    return {
        "schema_version": 1,
        "room_code": room_code or unique_room_code(),
        "state": "FINISHED",
        "created_at": started.isoformat(),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "host_secret": "test-host-secret",
        "host_sid": None,
        "host_identity": host_identity,
        "host_display_name": host_display_name,
        "participants": participants,
        "sid_to_alias": {},
        "role_to_alias": role_to_alias,
        "seed": engine.seed,
        "config": config.to_payload(),
        "engine": engine.to_payload(),
        "bots": {},
        "seq": 0,
        "paused_reason": None,
        "persisted": False,
    }
