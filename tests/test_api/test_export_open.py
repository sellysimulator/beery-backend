"""The open export by `public_id`, without Docker.

`GET /games/id/{public_id}/export` is open to anyone who can see the game's
results screen, so what it carries matters as much as who may fetch it: the
figures and names the results screen shows, and no account or credential.
This file plays and persists one game into an in-memory SQLite database (the
route reads nothing dialect-specific) so the privacy check runs in the
ordinary `-m "not dbschema"` suite; `test_export.py` repeats the routes'
behaviour against the real PostgreSQL schema.

What must never appear, in CSV or JSON: a player's Firebase uid or email, an
internal user id, a guest identity, a session token, the host's secret or the
host's own uid -- and any name a player or the host chose: every player is
their seat's `P<N>`.

The same file covers the in-process export cache (`services/export_cache.py`):
a repeat download reads nothing from the database, and still counts against
the hourly export budget.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import abuse
from app.core.abuse import RateLimiter
from app.core.enums import ROLE_ORDER, Role
from app.models.game import Game, Participant
from app.models.user import User
from app.services.db_service import DbService, build_snapshot
from app.services.export_cache import CachedExport, ExportCache, export_cache

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

# Distinctive chosen names, one per seat, plus the host's. A formula-like one
# (CSV injection shape) and a non-ASCII one: no part of any may reach the file.
SEAT_NAMES = {
    Role.RETAILER: "Quetzalcoatl",
    Role.WHOLESALER: "=HYPERLINK(Zxq)",
    Role.DISTRIBUTOR: "Zoë Ñandú",
    Role.FACTORY: "Robotnikov",
}
HOST_NAME = "Mrs Teacherly"
# The accounts' own (Google) names, which no export reads either.
REGISTERED_ACCOUNT_NAME = "Anacleta Cuentagoogle"
HOST_ACCOUNT_NAME = "Profesora Perfilgoogle"
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
    # the sqlite3 driver cannot bind by itself; psycopg can. Scoped to
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
                display_name=REGISTERED_ACCOUNT_NAME,
            ),
            User(
                firebase_uid=HOST_UID,
                email=HOST_EMAIL,
                display_name=HOST_ACCOUNT_NAME,
            ),
        ]
    )
    db.commit()

    config = make_config(duration_weeks=8)
    engine = run_full_game(config, seed=11)
    # Aliases deliberately out of seat order: the export numbers by seat.
    participants = {
        "P3": make_participant(
            "P3",
            identity=REGISTERED_UID,
            display_name=SEAT_NAMES[Role.RETAILER],
            role=Role.RETAILER,
        ),
        "P1": make_participant(
            "P1",
            identity=GUEST_ID,
            display_name=SEAT_NAMES[Role.WHOLESALER],
            role=Role.WHOLESALER,
        ),
        "P4": make_participant(
            "P4",
            identity="guest_9a9a9a9a-0000-4000-8000-000000000003",
            display_name=SEAT_NAMES[Role.DISTRIBUTOR],
            role=Role.DISTRIBUTOR,
        ),
        "P2": make_participant(
            "P2",
            identity=None,
            display_name=SEAT_NAMES[Role.FACTORY],
            role=Role.FACTORY,
            is_bot=True,
        ),
    }
    room = make_room_document(
        config,
        engine,
        room_code=unique_room_code(),
        host_identity=HOST_UID,
        host_uid=HOST_UID,
        host_display_name=HOST_NAME,
        participants=participants,
        started_at=datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
        finished_at=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
    )
    room["host_secret"] = HOST_SECRET
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)
    db.commit()
    public_id = db.execute(
        select(Game.public_id).where(Game.id == game_id)
    ).scalar_one()

    user_ids = [str(uid) for uid in db.execute(select(User.id)).scalars()]
    participant_user_ids = [
        str(uid)
        for uid in db.execute(select(Participant.user_id)).scalars()
        if uid is not None
    ]
    db.close()
    return {
        "public_id": public_id,
        "room_code": room["room_code"],
        "session_tokens": [p["session_token"] for p in participants.values()],
        "user_ids": user_ids + participant_user_ids,
    }


@pytest.mark.asyncio
async def test_the_open_export_carries_no_account_or_credential(
    open_client, db_factory, fake_redis
):
    game = _persist(db_factory)
    # A live room under the same code holds a secret too: it must not leak.
    await write_live_redis_room(fake_redis, game["room_code"], host_secret=LIVE_SECRET)
    url = f"/api/v1/games/id/{game['public_id']}/export"

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
    """The JSON is the results payload plus the week rows: the same keys and
    the same figures as `GET /results`, with each role's name replaced by its
    seat's `P<N>`."""
    game = _persist(db_factory)
    results = open_client.get(f"/api/v1/games/id/{game['public_id']}/results").json()
    url = f"/api/v1/games/id/{results['public_id']}/export"

    payload = open_client.get(url, params={"format": "json"}).json()

    assert set(payload) - {"weeks"} == set(results)
    expected = json.loads(json.dumps(results))
    for entry in expected["per_role"]:
        entry["display_name"] = f"P{ROLE_ORDER.index(Role(entry['role'])) + 1}"
    assert json.dumps(
        {k: v for k, v in payload.items() if k != "weeks"}, sort_keys=True
    ) == json.dumps(expected, sort_keys=True)


def test_timestamps_and_money_keep_their_wire_types(
    open_client, db_factory, fake_redis
):
    """Results and the JSON export carry timestamps as ISO-8601 UTC with a
    `Z` (a naive SQLite value is UTC too) and money as JSON numbers, never as
    `Decimal` strings, whichever database the row came from."""
    game = _persist(db_factory)
    results = open_client.get(f"/api/v1/games/id/{game['public_id']}/results").json()
    payload = open_client.get(
        f"/api/v1/games/id/{results['public_id']}/export", params={"format": "json"}
    ).json()

    for body in (results, payload):
        assert body["started_at"] == "2026-11-01T09:00:00Z"
        assert body["finished_at"] == "2026-11-01T10:00:00Z"
        assert isinstance(body["chain_total_cost"], float)
        assert all(isinstance(r["total_cost"], float) for r in body["per_role"])
    assert all(isinstance(w["cumulative_cost"], float) for w in payload["weeks"])


# --- names: P<N> only, in the export ---------------------------------------


def _name_parts() -> list[str]:
    """Every chosen name and account name, whole and word by word, raw and as
    JSON escapes it."""
    parts: list[str] = []
    account_names = [REGISTERED_ACCOUNT_NAME, HOST_ACCOUNT_NAME]
    for name in [*SEAT_NAMES.values(), HOST_NAME, *account_names]:
        for piece in [name, *name.split()]:
            parts.append(piece)
            parts.append(json.dumps(piece)[1:-1])  # \u-escaped non-ASCII
    parts += ["HYPERLINK", "Zxq", "Teacherly"]
    return parts


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_no_part_of_any_chosen_name_is_in_the_export(
    open_client, db_factory, fake_redis, fmt
):
    game = _persist(db_factory)
    results = open_client.get(f"/api/v1/games/id/{game['public_id']}/results").json()
    # The results screen still shows the names the players chose.
    assert {e["display_name"] for e in results["per_role"]} == set(SEAT_NAMES.values())

    response = open_client.get(
        f"/api/v1/games/id/{results['public_id']}/export", params={"format": fmt}
    )
    assert response.status_code == 200
    texts = [response.text]
    if fmt == "json":
        texts += _strings(response.json())
    for part in _name_parts():
        for text in texts:
            assert part not in text, part


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_every_player_is_their_seats_p_n_in_every_row(
    open_client, db_factory, fake_redis, fmt
):
    """Numbered by seat (Retailer P1 .. Factory P4), the same in every row,
    whatever alias the player had in the room; the role column stays."""
    game = _persist(db_factory)
    url = f"/api/v1/games/id/{game['public_id']}/export"
    response = open_client.get(url, params={"format": fmt})

    if fmt == "csv":
        rows = list(csv.DictReader(io.StringIO(response.text)))
    else:
        payload = response.json()
        rows = payload["weeks"]
        assert {(e["role"], e["display_name"]) for e in payload["per_role"]} == {
            (role.value, f"P{index + 1}") for index, role in enumerate(ROLE_ORDER)
        }
    assert rows
    assert {(row["role"], row["display_name"]) for row in rows} == {
        (role.value, f"P{index + 1}") for index, role in enumerate(ROLE_ORDER)
    }


@pytest.mark.asyncio
async def test_anyone_may_export_by_public_id_and_nobody_by_room_code(
    open_client, db_factory, fake_redis
):
    """D22: the export and the results are by `public_id` only. The room code
    is short and reissued, so there is no route by code at all -- not even
    for the live room's secret."""
    game = _persist(db_factory)
    public_id = game["public_id"]
    await write_live_redis_room(fake_redis, game["room_code"], host_secret=LIVE_SECRET)

    for fmt in ("csv", "json"):
        response = open_client.get(
            f"/api/v1/games/id/{public_id}/export", params={"format": fmt}
        )
        assert response.status_code == 200, fmt

    for path in ("export", "results"):
        for headers in ({}, {"X-Host-Secret": LIVE_SECRET}):
            by_code = open_client.get(
                f"/api/v1/games/{game['room_code']}/{path}", headers=headers
            )
            assert by_code.status_code in (404, 405), (path, headers)
            assert "room_code" not in by_code.text


def test_an_unknown_public_id_is_a_404_like_its_results(open_client, fake_redis):
    response = open_client.get("/api/v1/games/id/" + "0" * 32 + "/export")

    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)


# --- the in-process export cache -------------------------------------------


@pytest.fixture()
def statements(sqlite_engine: Engine) -> Iterator[list[str]]:
    """Every SQL statement the app runs, as Selly counts them."""
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(sqlite_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(sqlite_engine, "before_cursor_execute", _record)


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_a_repeat_download_reads_nothing_from_the_database(
    open_client, db_factory, fake_redis, statements, fmt
):
    game = _persist(db_factory)
    url = f"/api/v1/games/id/{game['public_id']}/export"

    statements.clear()
    first = open_client.get(url, params={"format": fmt})
    assert first.status_code == 200
    assert statements, "the first download must build from the database"

    statements.clear()
    second = open_client.get(url, params={"format": fmt})
    assert second.status_code == 200
    assert statements == []
    assert second.content == first.content
    assert second.headers["content-type"] == first.headers["content-type"]
    assert second.headers.get("content-disposition") == first.headers.get(
        "content-disposition"
    )


def test_the_cache_is_keyed_by_format(open_client, db_factory, fake_redis, statements):
    game = _persist(db_factory)
    public_id = game["public_id"]
    url = f"/api/v1/games/id/{public_id}/export"

    open_client.get(url, params={"format": "csv"})
    statements.clear()
    json_response = open_client.get(url, params={"format": "json"})

    assert json_response.headers["content-type"].startswith("application/json")
    assert statements, "JSON is its own file, built once"
    assert (public_id, "csv") in export_cache
    assert (public_id, "json") in export_cache


def test_only_a_200_is_cached(open_client, fake_redis):
    assert (
        open_client.get("/api/v1/games/id/" + "0" * 32 + "/export").status_code == 404
    )
    assert len(export_cache) == 0


def test_a_cached_download_still_counts_against_the_hourly_budget(
    open_client, db_factory, fake_redis, monkeypatch
):
    monkeypatch.setattr(abuse, "_game_export", RateLimiter(2, window=3600.0))
    game = _persist(db_factory)
    url = f"/api/v1/games/id/{game['public_id']}/export"

    assert open_client.get(url).status_code == 200
    assert open_client.get(url).status_code == 200  # served from the cache
    assert open_client.get(url).status_code == 429


def test_the_cache_is_a_bounded_lru() -> None:
    cache = ExportCache(max_entries=2)
    entry = CachedExport(body=b"x", media_type="text/csv", headers={})

    cache.put(("a", "csv"), entry)
    cache.put(("b", "csv"), entry)
    assert cache.get(("a", "csv")) is entry  # "a" is now the most recent
    cache.put(("c", "csv"), entry)

    assert ("a", "csv") in cache
    assert ("b", "csv") not in cache
    assert ("c", "csv") in cache
    assert len(cache) == 2


def test_the_process_cache_holds_sixty_four_files() -> None:
    assert export_cache.max_entries == 64
