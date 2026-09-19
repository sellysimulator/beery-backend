"""Role assignment -- ``claim_role``, ``release_role``, ``assign_role`` and
``set_role_mode`` (``11-socket-lobby.md §3.5``).

Covers acceptance criteria 22-26, 24a, 24b, and failure modes 10 (partial)
and 11.
"""

from __future__ import annotations

import asyncio

import pytest

from app.sockets.handlers.lobby import (
    assign_role,
    claim_role,
    join,
    release_role,
    set_role_mode,
)
from tests.test_sockets.conftest import new_guest

pytestmark = pytest.mark.asyncio


def _contains_key_recursive(payload, key: str) -> bool:
    if isinstance(payload, dict):
        if key in payload:
            return True
        return any(_contains_key_recursive(value, key) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_contains_key_recursive(item, key) for item in payload)
    return False


async def _seat_two(make_room, connect_identity, mode="PLAYER_CHOOSES"):
    room = await make_room(role_assignment_mode=mode)
    await connect_identity("sid-1", new_guest())
    await connect_identity("sid-2", new_guest())
    await join("sid-1", {"room_id": room["room_code"]})
    await join("sid-2", {"room_id": room["room_code"]})
    return room


# --- AC 22 -------------------------------------------------------------------- #


async def test_claim_role_seats_caller_and_blocks_a_second_claimant(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room = await _seat_two(make_room, connect_identity)

    await claim_role("sid-1", {"room_id": room["room_code"], "role": "FACTORY"})
    fake_socket_manager.clear()
    await claim_role("sid-2", {"room_id": room["room_code"], "role": "FACTORY"})

    stored = await state_svc.get_room(room["room_code"])
    assert stored["role_to_alias"]["FACTORY"] == "P1"
    assert fake_socket_manager.events_for("sid-2") == ["join_error"]


# --- AC 23 / failure mode 11 --------------------------------------------------- #


async def test_concurrent_claim_role_for_same_role_yields_exactly_one_holder(
    make_room, connect_identity, fake_socket_manager, state_svc, monkeypatch
):
    room = await _seat_two(make_room, connect_identity)

    original_get_room = state_svc.get_room

    async def slow_get_room(room_code):
        result = await original_get_room(room_code)
        # Force genuine interleaving: FakeRedis never truly suspends, so
        # without this the two calls below would run back-to-back inside
        # one another's "await" regardless of locking, and the assertion
        # would hold vacuously.
        await asyncio.sleep(0)
        return result

    monkeypatch.setattr(state_svc, "get_room", slow_get_room)

    await asyncio.gather(
        claim_role("sid-1", {"room_id": room["room_code"], "role": "FACTORY"}),
        claim_role("sid-2", {"room_id": room["room_code"], "role": "FACTORY"}),
    )

    stored = await original_get_room(room["room_code"])
    holder = stored["role_to_alias"]["FACTORY"]
    assert holder in ("P1", "P2")

    errors = [
        event
        for sid, event, payload in fake_socket_manager.sid_emits
        if event == "join_error"
    ]
    assert len(errors) == 1


# --- AC 24 -------------------------------------------------------------------- #


async def test_claim_role_rejected_in_host_assigns_mode(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room = await _seat_two(make_room, connect_identity, mode="HOST_ASSIGNS")
    fake_socket_manager.clear()

    await claim_role("sid-1", {"room_id": room["room_code"], "role": "FACTORY"})

    stored = await state_svc.get_room(room["room_code"])
    assert stored["role_to_alias"]["FACTORY"] is None
    message = fake_socket_manager.emits_for("sid-1")[0][1]["message"]
    assert message == "The host is assigning roles."


async def test_claim_role_rejected_in_random_mode(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room = await _seat_two(make_room, connect_identity, mode="RANDOM")
    fake_socket_manager.clear()

    await claim_role("sid-1", {"room_id": room["room_code"], "role": "FACTORY"})

    stored = await state_svc.get_room(room["room_code"])
    assert stored["role_to_alias"]["FACTORY"] is None
    message = fake_socket_manager.emits_for("sid-1")[0][1]["message"]
    assert message == "Roles are dealt at random when the game starts."


async def test_host_assigns_and_random_reject_with_different_messages(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room_a = await _seat_two(make_room, connect_identity, mode="HOST_ASSIGNS")
    fake_socket_manager.clear()
    await claim_role("sid-1", {"room_id": room_a["room_code"], "role": "FACTORY"})
    message_host_assigns = fake_socket_manager.emits_for("sid-1")[0][1]["message"]

    fake_socket_manager.clear()
    room_b = await _seat_two(make_room, connect_identity, mode="RANDOM")
    fake_socket_manager.clear()
    await claim_role("sid-1", {"room_id": room_b["room_code"], "role": "FACTORY"})
    message_random = fake_socket_manager.emits_for("sid-1")[0][1]["message"]

    assert message_host_assigns != message_random


# --- AC 24a ------------------------------------------------------------------- #


@pytest.mark.parametrize("new_mode", ["PLAYER_CHOOSES", "HOST_ASSIGNS"])
async def test_set_role_mode_clears_every_assignment(
    make_room, connect_identity, state_svc, new_mode
):
    room = await _seat_two(make_room, connect_identity, mode="PLAYER_CHOOSES")
    await claim_role("sid-1", {"room_id": room["room_code"], "role": "FACTORY"})
    await claim_role("sid-2", {"room_id": room["room_code"], "role": "DISTRIBUTOR"})

    stored = await state_svc.get_room(room["room_code"])
    assert stored["role_to_alias"]["FACTORY"] == "P1"

    await set_role_mode(
        "sid-does-not-matter",
        {
            "room_id": room["room_code"],
            "host_secret": room["host_secret"],
            "mode": new_mode,
        },
    )

    stored = await state_svc.get_room(room["room_code"])
    assert all(alias is None for alias in stored["role_to_alias"].values())
    assert all(p["role"] is None for p in stored["participants"].values())
    assert stored["config"]["role_assignment_mode"] == new_mode


async def test_set_role_mode_clears_even_when_new_mode_equals_old(
    make_room, connect_identity, state_svc
):
    room = await _seat_two(make_room, connect_identity, mode="PLAYER_CHOOSES")
    await claim_role("sid-1", {"room_id": room["room_code"], "role": "FACTORY"})

    await set_role_mode(
        "sid-x",
        {
            "room_id": room["room_code"],
            "host_secret": room["host_secret"],
            "mode": "PLAYER_CHOOSES",
        },
    )

    stored = await state_svc.get_room(room["room_code"])
    assert all(alias is None for alias in stored["role_to_alias"].values())
    assert stored["participants"]["P1"]["role"] is None


# --- AC 24b ------------------------------------------------------------------- #


async def test_set_role_mode_after_start_is_rejected(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room = await _seat_two(make_room, connect_identity, mode="PLAYER_CHOOSES")
    running = await state_svc.get_room(room["room_code"])
    running["state"] = "RUNNING"
    await state_svc.save_room(room["room_code"], running)
    before = await state_svc.get_room(room["room_code"])

    await set_role_mode(
        "sid-x",
        {
            "room_id": room["room_code"],
            "host_secret": room["host_secret"],
            "mode": "RANDOM",
        },
    )

    after = await state_svc.get_room(room["room_code"])
    assert (
        after["config"]["role_assignment_mode"]
        == before["config"]["role_assignment_mode"]
    )
    assert fake_socket_manager.events_for("sid-x") == ["join_error"]


# --- AC 25 -------------------------------------------------------------------- #


async def test_assign_role_moves_role_atomically(
    make_room, connect_identity, state_svc
):
    room = await _seat_two(make_room, connect_identity, mode="HOST_ASSIGNS")

    await assign_role(
        "sid-host",
        {
            "room_id": room["room_code"],
            "host_secret": room["host_secret"],
            "alias": "P1",
            "role": "FACTORY",
        },
    )
    await assign_role(
        "sid-host",
        {
            "room_id": room["room_code"],
            "host_secret": room["host_secret"],
            "alias": "P2",
            "role": "FACTORY",
        },
    )

    stored = await state_svc.get_room(room["room_code"])
    assert stored["role_to_alias"]["FACTORY"] == "P2"
    assert stored["participants"]["P1"]["role"] is None
    assert stored["participants"]["P2"]["role"] == "FACTORY"


# --- AC 26 -------------------------------------------------------------------- #


async def test_assign_role_null_unseats(make_room, connect_identity, state_svc):
    room = await _seat_two(make_room, connect_identity, mode="HOST_ASSIGNS")
    await assign_role(
        "sid-host",
        {
            "room_id": room["room_code"],
            "host_secret": room["host_secret"],
            "alias": "P1",
            "role": "FACTORY",
        },
    )

    await assign_role(
        "sid-host",
        {
            "room_id": room["room_code"],
            "host_secret": room["host_secret"],
            "alias": "P1",
            "role": None,
        },
    )

    stored = await state_svc.get_room(room["room_code"])
    assert stored["role_to_alias"]["FACTORY"] is None
    assert stored["participants"]["P1"]["role"] is None


async def test_assign_role_to_unknown_alias_is_a_join_error(
    make_room, connect_identity, fake_socket_manager
):
    room = await _seat_two(make_room, connect_identity, mode="HOST_ASSIGNS")

    await assign_role(
        "sid-host",
        {
            "room_id": room["room_code"],
            "host_secret": room["host_secret"],
            "alias": "P9",
            "role": "FACTORY",
        },
    )

    assert fake_socket_manager.events_for("sid-host") == ["join_error"]


# --- release_role -------------------------------------------------------------- #


async def test_release_role_gives_up_the_seat(make_room, connect_identity, state_svc):
    room = await _seat_two(make_room, connect_identity, mode="PLAYER_CHOOSES")
    await claim_role("sid-1", {"room_id": room["room_code"], "role": "FACTORY"})

    await release_role("sid-1", {"room_id": room["room_code"]})

    stored = await state_svc.get_room(room["room_code"])
    assert stored["role_to_alias"]["FACTORY"] is None
    assert stored["participants"]["P1"]["role"] is None


# --- failure mode 10 (partial): roles_assigned / lobby_update carry no secrets - #


async def test_role_events_never_carry_secrets(
    make_room, connect_identity, fake_socket_manager
):
    room = await _seat_two(make_room, connect_identity, mode="PLAYER_CHOOSES")

    await claim_role("sid-1", {"room_id": room["room_code"], "role": "FACTORY"})

    for _room_id, event, payload in fake_socket_manager.room_emits:
        for forbidden in ("identity", "session_token", "host_secret"):
            assert not _contains_key_recursive(payload, forbidden), (event, forbidden)
