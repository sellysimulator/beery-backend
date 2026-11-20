"""Shared helpers for ``tests/test_api/``.

Plain helper functions used by both ``test_rooms.py`` and
``test_room_config.py`` so the room-code oracle, recursive key/value scans and
the "create a room and grab its secret" dance are not three implementations of
the same thing. Nothing here overrides a fixture from the shared
``tests/conftest.py`` -- ``client`` and ``fake_redis`` still come from there.

It also carries the Testcontainers PostgreSQL fixture chain and the
game-persistence/auth helpers the three results/export/profile test files
(``test_results.py``, ``test_export.py``, ``test_profile.py``) all need. Every
other test package that needs those fixtures lives in a directory whose *own*
conftest.py reaches them (``tests/test_models/conftest.py``,
``tests/test_services/conftest.py``); this one imports the chain here, once: a
conftest.py's fixtures resolve for every module in this directory without an
import, so the three files need neither the chain, the ``api_client`` fixture,
nor an ``__all__`` workaround for the linter.

Nothing here is ``autouse`` and none of it runs anything at import time (a
pytest fixture body only runs when a test requests it), so
``test_rooms.py``/``test_room_config.py`` are unaffected by it -- they never
request ``api_client``, ``session_factory`` or any of the Testcontainers
fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import text

from app.core.enums import Role
from app.services.db_service import DbService, build_snapshot

# --- the Testcontainers PostgreSQL fixtures, imported wholesale (see module
# docstring for why this file, rather than each test module, imports it) ---
from tests.test_models.conftest import (
    alembic,
    docker_daemon,
    insert_user,
    migrated_url,
    postgres_container,
    postgres_url,
    schema_engine,
)

# --- GameConfig/GameEngine/room-document helpers --------------------------
from tests.test_services.conftest import (
    count_statements,
    make_config,
    make_participant,
    make_room_document,
    run_full_game,
    session_factory,
    unique_room_code,
)

__all__ = [
    "CREATE",
    "PRESETS",
    "alembic",
    "api_client",
    "bearer_for",
    "config_url",
    "count_statements",
    "create_room",
    "docker_daemon",
    "insert_user",
    "iter_pairs",
    "keys_recursive",
    "make_config",
    "make_participant",
    "make_room_document",
    "migrated_url",
    "persist_full_game",
    "postgres_container",
    "postgres_url",
    "register_user",
    "run_full_game",
    "schema_engine",
    "session_factory",
    "status_url",
    "unique_room_code",
    "values_recursive",
    "write_live_redis_room",
]

CREATE = "/api/v1/rooms/create"
PRESETS = "/api/v1/rooms/presets"


def status_url(room_code: str) -> str:
    return f"/api/v1/rooms/{room_code}/status"


def config_url(room_code: str) -> str:
    return f"/api/v1/rooms/{room_code}/config"


def create_room(client: Any, **body: Any) -> tuple[str, str]:
    """``POST /rooms/create`` and return ``(room_code, host_secret)``."""
    response = client.post(CREATE, json=body)
    payload = response.json()
    return payload["room_code"], payload["host_secret"]


def iter_pairs(obj: Any):
    """Yield every ``(key, value)`` pair at any depth of a JSON-like value.

    The recursive-assertion tool the *failure modes to test* keep asking
    for: a broadcast or response leaking a secret three levels deep is not
    caught by checking the top-level keys alone.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from iter_pairs(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_pairs(item)


def keys_recursive(obj: Any) -> set[str]:
    return {key for key, _value in iter_pairs(obj)}


def values_recursive(obj: Any) -> list[Any]:
    return [value for _key, value in iter_pairs(obj)]


# ---------------------------------------------------------------------------
# An ``api_client`` bound to the Testcontainers PostgreSQL,
# and the game-persistence / auth helpers ``test_results.py``, ``test_export.py``
# and ``test_profile.py`` all need.
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_client(client, session_factory):
    """``client`` with ``get_db`` overridden onto the Testcontainers PostgreSQL.

    Mirrors ``tests/test_auth/conftest.py::api_client`` exactly, except the
    session comes from ``session_factory`` (a real, migrated
    PostgreSQL) rather than an in-memory SQLite: the routes under test read
    ``NUMERIC`` columns and run a grouped statistics query, neither of which
    a SQLite double would exercise honestly.
    """
    from app.db.session import get_db
    from app.main import app

    def _override():
        yield session_factory()

    app.dependency_overrides[get_db] = _override
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def register_user(session_factory, *, firebase_uid: str, display_name: str = "Ana"):
    """Insert a ``users`` row for ``firebase_uid`` and commit it.

    Idempotent by ``firebase_uid`` -- a caller may need the same identity
    registered ahead of several persisted games, and ``users.firebase_uid``
    is UNIQUE, so a second raw insert would raise ``IntegrityError`` rather
    than being a harmless no-op.
    """
    db = session_factory()
    existing = db.execute(
        text("SELECT id FROM users WHERE firebase_uid = :uid"),
        {"uid": firebase_uid},
    ).scalar_one_or_none()
    if existing is None:
        insert_user(
            db.connection(), firebase_uid=firebase_uid, display_name=display_name
        )
        db.commit()
    else:
        db.close()


def bearer_for(firebase_tokens, *, firebase_uid: str, token: str) -> dict[str, str]:
    """Register ``token`` with the fake Firebase SDK and return the header."""
    firebase_tokens.configure()
    firebase_tokens.add(token, uid=firebase_uid)
    return {"Authorization": f"Bearer {token}"}


DEFAULT_PARTICIPANTS_TEMPLATE = {
    "RETAILER": {"identity": "uid-retailer-default", "is_bot": False},
    "WHOLESALER": {
        "identity": "guest_aaaaaaaa-0000-0000-0000-000000000001",
        "is_bot": False,
    },
    "DISTRIBUTOR": {
        "identity": "guest_bbbbbbbb-0000-0000-0000-000000000002",
        "is_bot": False,
    },
    "FACTORY": {"identity": None, "is_bot": True},
}


def persist_full_game(
    session_factory,
    *,
    room_code: str | None = None,
    duration_weeks: int = 8,
    seed: int = 1,
    host_identity: str | None = None,
    host_display_name: str = "Host",
    participants: dict[str, dict[str, Any]] | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    order_fn=None,
    demand=None,
) -> dict[str, Any]:
    """Play and persist one full game through only the public surface of the
    engine, the room document and ``DbService``. Returns everything a test
    might need to build expectations against the HTTP response.
    """
    config = make_config(duration_weeks=duration_weeks, demand=demand)
    engine = run_full_game(config, seed=seed, order_fn=order_fn)

    if participants is None:
        participants = {
            f"P{i + 1}": make_participant(
                f"P{i + 1}",
                identity=info["identity"],
                display_name=f"P{i + 1}",
                role=Role(role_value),
                is_bot=info["is_bot"],
            )
            for i, (role_value, info) in enumerate(
                DEFAULT_PARTICIPANTS_TEMPLATE.items()
            )
        }

    room = make_room_document(
        config,
        engine,
        room_code=room_code,
        host_identity=host_identity,
        host_display_name=host_display_name,
        participants=participants,
        started_at=started_at,
        finished_at=finished_at,
    )
    snapshot = build_snapshot(room, config, engine)
    db = session_factory()
    game_id = DbService().persist_game(db, snapshot)
    db.commit()
    return {
        "room_code": room["room_code"],
        "engine": engine,
        "config": config,
        "snapshot": snapshot,
        "game_id": game_id,
        "room": room,
    }


async def write_live_redis_room(fake_redis, room_code: str, **overrides: Any) -> None:
    """A minimal room document (spec §11.3), written directly into the shared
    ``fake_redis`` -- the same "built by hand rather than through
    StateService" pattern ``tests/test_services/conftest.py``'s
    ``make_room_document`` uses.
    """
    doc = {
        "schema_version": 1,
        "room_code": room_code,
        "state": "RUNNING",
        "created_at": "2026-01-01T00:00:00+00:00",
        "started_at": "2026-01-01T00:00:05+00:00",
        "finished_at": None,
        "host_secret": "live-secret-value",
        "host_sid": "sid-1",
        "host_identity": None,
        "host_display_name": "Host",
        "participants": {},
        "sid_to_alias": {},
        "role_to_alias": {role.value: None for role in Role},
        "seed": 1,
        "config": {},
        "engine": {},
        "bots": {},
        "seq": 0,
        "paused_reason": None,
        "persisted": False,
    }
    doc.update(overrides)
    await fake_redis.setex(f"room:{room_code}", 86_400, json.dumps(doc))
