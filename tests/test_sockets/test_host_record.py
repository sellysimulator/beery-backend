"""The host record taken at ``start_game`` (spec D23).

``room["host_uid"]`` is the verified Firebase uid of the connection whose
``start_game`` started the game, and nothing for a guest. It comes from the
handshake's verification (``socket_manager.sid_to_verified_uid``), never from
the shape of ``sid_to_identity``, and it is server-only: no emit, to the room
or to one socket, may carry it.
"""

from __future__ import annotations

import pytest

from app.services import game_service as game_service_module
from app.services.db_service import build_snapshot
from app.sockets.handlers.connection import connect
from app.sockets.handlers.lobby import claim_role, join, join_waiting, start_game
from app.sockets.handlers.play import submit_order
from tests.test_sockets.conftest import contains_key, contains_value, new_guest

pytestmark = pytest.mark.asyncio

HOST_UID = "firebase-uid-host-0001"


async def _seat_four_guests(connect_identity, room_code: str) -> dict[str, str]:
    sids: dict[str, str] = {}
    for role in ("RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY"):
        sid = f"sid-{role.lower()}"
        await connect_identity(sid, new_guest())
        await join(sid, {"room_id": room_code, "display_name": role.title()})
        await claim_role(sid, {"room_id": room_code, "role": role})
        sids[role] = sid
    return sids


async def _ready_room(make_room, connect_identity) -> tuple[dict, dict[str, str]]:
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES", duration_weeks=8)
    sids = await _seat_four_guests(connect_identity, room["room_code"])
    return room, sids


def _start_payload(room: dict, secret: str | None = None) -> dict:
    return {
        "room_id": room["room_code"],
        "host_secret": room["host_secret"] if secret is None else secret,
    }


async def test_a_signed_in_host_is_recorded_at_start(
    make_room, connect_identity, state_svc
):
    room, _sids = await _ready_room(make_room, connect_identity)
    await connect_identity("sid-host", HOST_UID, verified=True)
    await join_waiting("sid-host", _start_payload(room))

    stored = await state_svc.get_room(room["room_code"])
    assert stored["host_uid"] is None  # the claim records nothing; the start does

    await start_game("sid-host", _start_payload(room))

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] == "RUNNING"
    assert stored["host_uid"] == HOST_UID


async def test_a_guest_host_is_never_recorded(make_room, connect_identity, state_svc):
    room, _sids = await _ready_room(make_room, connect_identity)
    await connect_identity("sid-host", new_guest())
    await join_waiting("sid-host", _start_payload(room))
    await start_game("sid-host", _start_payload(room))

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] == "RUNNING"
    assert stored["host_uid"] is None


async def test_an_identity_without_a_verified_token_is_never_recorded(
    make_room, connect_identity, state_svc
):
    """A uid-shaped identity is not proof of a token: only the handshake's
    verification puts a sid in ``sid_to_verified_uid``. Inferring "signed in"
    from a missing ``guest_`` prefix would record this one."""
    room, _sids = await _ready_room(make_room, connect_identity)
    await connect_identity("sid-host", HOST_UID, verified=False)
    await start_game("sid-host", _start_payload(room))

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] == "RUNNING"
    assert stored["host_uid"] is None


async def test_a_forged_guest_id_through_the_real_handshake_is_never_recorded(
    make_room, connect_identity, fake_socket_manager, state_svc, firebase_tokens
):
    """A client offering a Firebase-looking value as its ``guestId`` gets a
    fresh guest identity from the real ``connect``, and no verified uid."""
    firebase_tokens.configure()
    room, _sids = await _ready_room(make_room, connect_identity)

    assert await connect("sid-forger", {}, {"guestId": HOST_UID}) is not False
    assert fake_socket_manager.sid_to_identity["sid-forger"] != HOST_UID
    assert "sid-forger" not in fake_socket_manager.sid_to_verified_uid

    await start_game("sid-forger", _start_payload(room))

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] == "RUNNING"
    assert stored["host_uid"] is None


async def test_a_verified_token_through_the_real_handshake_is_recorded(
    make_room, connect_identity, fake_socket_manager, state_svc, firebase_tokens
):
    firebase_tokens.configure()
    firebase_tokens.add("host-token", uid=HOST_UID)
    room, _sids = await _ready_room(make_room, connect_identity)

    assert await connect("sid-host", {}, {"idToken": "host-token"}) is not False
    await start_game("sid-host", _start_payload(room))

    stored = await state_svc.get_room(room["room_code"])
    assert stored["host_uid"] == HOST_UID


async def test_the_dashboard_start_without_join_waiting_is_recorded(
    make_room, connect_identity, state_svc
):
    """The multi-game dashboard emits ``start_game`` from its own socket and
    never claims the room with ``join_waiting``; the start is still the one
    capture point."""
    room, _sids = await _ready_room(make_room, connect_identity)
    await connect_identity("sid-dashboard", HOST_UID, verified=True)

    await start_game("sid-dashboard", _start_payload(room))

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] == "RUNNING"
    assert stored["host_identity"] is None  # no claim ran
    assert stored["host_uid"] == HOST_UID


async def test_a_wrong_secret_records_nothing(make_room, connect_identity, state_svc):
    room, _sids = await _ready_room(make_room, connect_identity)
    await connect_identity("sid-stranger", HOST_UID, verified=True)

    await start_game("sid-stranger", _start_payload(room, secret="not-the-secret"))

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] != "RUNNING"
    assert stored["host_uid"] is None


async def test_a_refused_start_records_nothing(make_room, connect_identity, state_svc):
    """The secret is right but ``can_start`` refuses (no seats filled): the
    game did not start, so nobody hosted it yet."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    await connect_identity("sid-host", HOST_UID, verified=True)

    await start_game("sid-host", _start_payload(room))

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] != "RUNNING"
    assert stored["host_uid"] is None


async def test_the_first_start_wins(make_room, connect_identity, state_svc):
    """A second ``start_game`` on a running room is a silent no-op, so a
    different signed-in tab with the secret cannot rewrite the record."""
    room, _sids = await _ready_room(make_room, connect_identity)
    await connect_identity("sid-host", HOST_UID, verified=True)
    await connect_identity("sid-other", "firebase-uid-other", verified=True)

    await start_game("sid-host", _start_payload(room))
    await start_game("sid-other", _start_payload(room))

    stored = await state_svc.get_room(room["room_code"])
    assert stored["host_uid"] == HOST_UID


async def test_host_uid_reaches_persistence_and_never_any_emit(
    make_room, connect_identity, fake_socket_manager, state_svc, monkeypatch
):
    """Played to the end: the snapshot persistence builds carries the uid,
    and not one emit of the whole game -- room broadcast or private -- carries
    the ``host_uid`` key or the uid itself."""
    snapshots = []

    async def spy_persist(room_code, room_doc, engine, stats):
        snapshots.append(build_snapshot(room_doc, engine.config, engine))
        return "d" * 32

    monkeypatch.setattr(game_service_module, "persist_finished_game", spy_persist)

    room, sids = await _ready_room(make_room, connect_identity)
    await connect_identity("sid-host", HOST_UID, verified=True)
    await join_waiting("sid-host", _start_payload(room))
    await start_game("sid-host", _start_payload(room))

    for _ in range(8):
        stored = await state_svc.get_room(room["room_code"])
        week = state_svc.load_engine(stored).week
        for sid in sids.values():
            await submit_order(
                sid, {"room_id": room["room_code"], "week": week, "order": 4}
            )

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] == "FINISHED"
    assert [snapshot.host_uid for snapshot in snapshots] == [HOST_UID]

    events = {event for _target, event, _data in fake_socket_manager.emits}
    assert {"game_started", "game_finished", "game_persisted"} <= events
    for target, event, payload in fake_socket_manager.emits:
        assert not contains_key(payload, "host_uid"), (target, event)
        assert not contains_value(payload, HOST_UID), (target, event)
