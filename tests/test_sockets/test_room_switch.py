"""One socket, a second room: the tab moved from one game to another.

A tab keeps its socket when the host opens a new room or a player joins
another. Claiming or joining the new room leaves the old one as a disconnect
from it would, so the old room stops naming the socket, the socket stops
hearing the old room, and when the socket finally drops it is the new room
that hears about it (spec §9.6, D7).
"""

from __future__ import annotations

import pytest

from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.lobby import join, join_waiting
from app.sockets.manager import sio
from tests.test_sockets.conftest import new_guest

pytestmark = pytest.mark.asyncio


def _claim(room: dict) -> dict:
    return {"room_id": room["room_code"], "host_secret": room["host_secret"]}


async def test_a_host_who_opens_a_new_room_leaves_the_old_one(
    make_room, connect_identity, state_svc, fake_socket_manager
):
    old = await make_room()
    new = await make_room()
    await connect_identity("sid-host", new_guest())
    await join_waiting("sid-host", _claim(old))

    await join_waiting("sid-host", _claim(new))

    assert old["room_code"] not in sio.rooms("sid-host")
    assert new["room_code"] in sio.rooms("sid-host")
    assert (await state_svc.get_room(old["room_code"]))["host_sid"] is None
    assert (await state_svc.get_room(new["room_code"]))["host_sid"] == "sid-host"


async def test_the_new_room_hears_the_host_drop(
    make_room, connect_identity, state_svc, fake_socket_manager
):
    """Were the socket still in the old room, the disconnect would find that
    room first and the new one would keep a host who is gone."""
    old = await make_room()
    new = await make_room()
    await connect_identity("sid-host", new_guest())
    await join_waiting("sid-host", _claim(old))
    await join_waiting("sid-host", _claim(new))

    await disconnect("sid-host")

    assert (await state_svc.get_room(new["room_code"]))["host_sid"] is None


async def test_a_host_leaving_a_running_game_for_a_new_room_is_announced(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES")
    )
    new = await make_room()
    fake_socket_manager.clear()

    await join_waiting(game["host_sid"], _claim(new))

    assert "host_disconnected" in fake_socket_manager.events_for(game["room_code"])
    stored = await state_svc.get_room(game["room_code"])
    assert stored["host_sid"] is None
    assert stored["state"] == "RUNNING"  # a host leaving never pauses


async def test_a_player_who_joins_another_room_leaves_the_old_seat(
    make_room, connect_identity, state_svc, fake_socket_manager
):
    old = await make_room()
    new = await make_room()
    await connect_identity("sid-p", new_guest())
    await join("sid-p", {"room_id": old["room_code"]})
    # The fake socket manager records the join; the real one also enters the
    # Socket.IO room, as here.
    await sio.enter_room("sid-p", old["room_code"])

    await join("sid-p", {"room_id": new["room_code"]})

    assert old["room_code"] not in sio.rooms("sid-p")
    stored = await state_svc.get_room(old["room_code"])
    assert "sid-p" not in stored["sid_to_alias"]
    assert stored["participants"]["P1"]["connected"] is False
    assert (await state_svc.get_room(new["room_code"]))["sid_to_alias"] == {
        "sid-p": "P1"
    }


async def test_rejoining_the_same_room_leaves_nothing(
    make_room, connect_identity, state_svc, fake_socket_manager
):
    room = await make_room()
    await connect_identity("sid-host", new_guest())
    await join_waiting("sid-host", _claim(room))

    await join_waiting("sid-host", _claim(room))

    assert room["room_code"] in sio.rooms("sid-host")
    assert (await state_svc.get_room(room["room_code"]))["host_sid"] == "sid-host"
