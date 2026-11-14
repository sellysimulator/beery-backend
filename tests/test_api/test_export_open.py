"""The open export by `public_id` (2026-11-14), without Docker.

`GET /games/id/{public_id}/export` is open to anyone who can see the game's
results screen, so what it carries matters as much as who may fetch it: the
figures and names the results screen shows, and no account or credential.
This file plays and persists one game into an in-memory SQLite database (the
route reads nothing dialect-specific) so the privacy check runs in the
ordinary `-m "not dbschema"` suite; `test_export.py` repeats the routes'
behaviour against the real MySQL schema.

What must never appear, in CSV or JSON: a player's Firebase uid or email, an
internal user id, a guest identity, a session token, the host's secret or the
host's own uid.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.enums import Role
from app.models.game import Participant
from app.models.user import User
from app.services.db_service import DbService, build_snapshot

from .conftest import (
    keys_recursive,
    make_config,
    make_participant,
    make_room_document,
    run_full_game,
    unique_room_code,
    write_live_redis_room,
)

REGISTERED_UID = "uid-open-export-registered-7f3a"
REGISTERED_EMAIL = "retailer.private@example.org"
HOST_UID = "uid-open-export-host-91c2"
HOST_EMAIL = "host.private@example.org"
GUEST_ID = "guest_0b5e7c1a-1111-4222-8333-944455556666"
HOST_SECRET = "host-secret-that-must-not-leak-5d1e"
LIVE_SECRET = "live-room-secret-that-must-not-leak-77aa"

# Keys an export must not carry at any depth, whatever their value.
FORBIDDEN_KEYS = {
    "firebase_uid",
    "uid",
    "email",
    "user_id",
    "identity",
    "guest_identity",
    "guest_id",
    "session_token",
    "host_secret",
    "host_identity",
    "host_user_id",
    "game_id",
}


@pytest.fixture()
def sqlite_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    # `persist_game` binds `Decimal` money through raw `text()` inserts, which
    # the sqlite3 driver cannot bind by itself; MySQL's driver can. Scoped to
    # this test with monkeypatch, so no other SQLite test sees the adapter.
    monkeypatch.setitem(sqlite3.adapters, (Decimal, sqlite3.PrepareProtocol), str)
    import app.models  # noqa: F401  -- registers every table on Base.metadata
    from app.models.base import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def db_factory(sqlite_engine: Engine):
    return sessionmaker(bind=sqlite_engine, expire_on_commit=False)


@pytest.fixture()
def open_client(client, db_factory):
    from app.db.session import get_db
    from app.main import app

    def _override() -> Iterator[Session]:
        session = db_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _persist(db_factory) -> dict[str, Any]:
    """One finished game: a registered player, a guest, a named player and a
    bot, hosted by a registered account whose secret is in the room document."""
    db = db_factory()
    db.add_all(
        [
            User(
                firebase_uid=REGISTERED_UID,
                email=REGISTERED_EMAIL,
                display_name="Ana",
            ),
            User(firebase_uid=HOST_UID, email=HOST_EMAIL, display_name="Teacher"),
        ]
    )
    db.commit()

    config = make_config(duration_weeks=8)
    engine = run_full_game(config, seed=11)
    participants = {
        "P1": make_participant(
            "P1", identity=REGISTERED_UID, display_name="Ana", role=Role.RETAILER
        ),
        "P2": make_participant(
            "P2", identity=GUEST_ID, display_name="Bea", role=Role.WHOLESALER
        ),
        "P3": make_participant(
            "P3",
            identity="guest_9a9a9a9a-0000-4000-8000-000000000003",
            display_name="Cid",
            role=Role.DISTRIBUTOR,
        ),
        "P4": make_participant(
            "P4", identity=None, display_name="Bot", role=Role.FACTORY, is_bot=True
        ),
    }
    room = make_room_document(
        config,
        engine,
        room_code=unique_room_code(),
        host_identity=HOST_UID,
        host_display_name="Teacher",
        participants=participants,
        started_at=datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
    )
    room["host_secret"] = HOST_SECRET
    snapshot = build_snapshot(room, config, engine)
    DbService().persist_game(db, snapshot)
    db.commit()

    user_ids = [str(uid) for uid in db.execute(select(User.id)).scalars()]
    participant_user_ids = [
        str(uid)
        for uid in db.execute(select(Participant.user_id)).scalars()
        if uid is not None
    ]
    db.close()
    return {
        "room_code": room["room_code"],
        "session_tokens": [p["session_token"] for p in participants.values()],
        "user_ids": user_ids + participant_user_ids,
    }


def _public_id(client, room_code: str) -> str:
    return client.get(f"/api/v1/games/{room_code}/results").json()["public_id"]


@pytest.mark.asyncio
async def test_the_open_export_carries_no_account_or_credential(
    open_client, db_factory, fake_redis
):
    game = _persist(db_factory)
    # A live room under the same code holds a secret too: it must not leak.
    await write_live_redis_room(fake_redis, game["room_code"], host_secret=LIVE_SECRET)
    url = f"/api/v1/games/id/{_public_id(open_client, game['room_code'])}/export"

    csv_response = open_client.get(url, params={"format": "csv"})
    json_response = open_client.get(url, params={"format": "json"})
    assert csv_response.status_code == 200
    assert json_response.status_code == 200

    forbidden_values = [
        REGISTERED_UID,
        REGISTERED_EMAIL,
        HOST_UID,
        HOST_EMAIL,
        GUEST_ID,
        "guest_",
        HOST_SECRET,
        LIVE_SECRET,
        *game["session_tokens"],
    ]
    for response in (csv_response, json_response):
        for value in forbidden_values:
            assert value not in response.text, value

    # Keys, at any depth of the JSON and in the CSV header.
    payload = json_response.json()
    assert not keys_recursive(payload) & FORBIDDEN_KEYS
    header = next(csv.reader(io.StringIO(csv_response.text)))
    assert not set(header) & FORBIDDEN_KEYS

    # An internal user id is a bare integer, so it is checked where an id
    # could sit: no value in a week row or a role result is one, except the
    # figures the results screen itself shows.
    figure_keys = keys_recursive(payload)
    assert "id" not in figure_keys
    for row in payload["weeks"]:
        assert set(row) <= set(header)


def test_the_open_export_carries_only_what_the_results_screen_shows(
    open_client, db_factory, fake_redis
):
    """The JSON is the results payload plus the week rows: every top-level key
    but `weeks` is a key of `GET /results`, and every role's name is the one
    the results screen prints."""
    game = _persist(db_factory)
    results = open_client.get(f"/api/v1/games/{game['room_code']}/results").json()
    url = f"/api/v1/games/id/{results['public_id']}/export"

    payload = open_client.get(url, params={"format": "json"}).json()

    assert set(payload) - {"weeks"} == set(results)
    assert json.dumps(
        {k: v for k, v in payload.items() if k != "weeks"}, sort_keys=True
    ) == (json.dumps(results, sort_keys=True))
    names_on_screen = {entry["display_name"] for entry in results["per_role"]}
    assert {row["display_name"] for row in payload["weeks"]} <= names_on_screen


def test_anyone_may_export_by_public_id_but_not_by_room_code(
    open_client, db_factory, fake_redis
):
    game = _persist(db_factory)
    public_id = _public_id(open_client, game["room_code"])

    for fmt in ("csv", "json"):
        response = open_client.get(
            f"/api/v1/games/id/{public_id}/export", params={"format": fmt}
        )
        assert response.status_code == 200, fmt

    # The room code is short and reissued: still host-only.
    by_code = open_client.get(f"/api/v1/games/{game['room_code']}/export")
    assert by_code.status_code == 403
    assert "room_code" not in by_code.text


def test_an_unknown_public_id_is_a_404_like_its_results(open_client, fake_redis):
    response = open_client.get("/api/v1/games/id/" + "0" * 32 + "/export")

    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)
