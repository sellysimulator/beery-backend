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


def _hearers(fake_socket_manager, sid: str) -> list[tuple[str, str, int]]:
    """Records, for every room broadcast from now on, whether ``sid`` was in
    that Socket.IO room when it went out: ``[(room, event, seq), ...]`` of the
    ones it heard. The fake records the target room only; the real manager
    delivers to whoever is in the room at that moment."""
    heard: list[tuple[str, str, int]] = []
    record = fake_socket_manager.emit_to_room

    async def emit_to_room(room_id: str, event: str, data) -> None:
        if room_id in sio.rooms(sid):
            heard.append((room_id, event, data.get("seq", -1)))
        await record(room_id, event, data)

    fake_socket_manager.emit_to_room = emit_to_room
    return heard


async def test_a_player_switching_rooms_hears_nothing_more_from_the_old_one(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    """The tab has already started its store over for the new room. One
    late ``participant_disconnected`` or ``game_paused`` from the old room
    carries that room's high ``seq``: heard, it would make the tab drop every
    event of the new room."""
    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES")
    )
    sid = game["sids_by_role"]["RETAILER"]
    await sio.enter_room(sid, game["room_code"])
    new = await make_room()
    heard = _hearers(fake_socket_manager, sid)
    # The fake records the join without entering the Socket.IO room; the
    # real `socket_manager.join_room` enters it before `leave_other_rooms`.
    await sio.enter_room(sid, new["room_code"])

    await join(sid, {"room_id": new["room_code"]})

    assert [h for h in heard if h[0] == game["room_code"]] == []
    # The old room itself was still told, and paused (D7).
    told = fake_socket_manager.events_for(game["room_code"])
    assert "participant_disconnected" in told and "game_paused" in told
    assert (await state_svc.get_room(game["room_code"]))["state"] == "PAUSED"
    # The new room's events do reach the socket.
    assert [event for _room, event, _seq in heard] == ["lobby_update"]


async def test_a_player_who_becomes_a_host_elsewhere_is_charged_to_the_new_room(
    make_room, start_running_game, connect_identity, state_svc, fake_socket_manager
):
    """Seated in one room, then host of another: the per-process map still
    named the old room, so the drop was looked up there, found nothing, and
    the new room kept a host who was gone and never heard
    ``host_disconnected``."""
    old = await make_room()
    await connect_identity("sid-tab", new_guest())
    await join("sid-tab", {"room_id": old["room_code"]})
    await sio.enter_room("sid-tab", old["room_code"])
    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES")
    )

    await join_waiting("sid-tab", _claim(game))
    assert (await state_svc.get_room(game["room_code"]))["host_sid"] == "sid-tab"
    fake_socket_manager.clear()

    await disconnect("sid-tab")

    assert (await state_svc.get_room(game["room_code"]))["host_sid"] is None
    assert "host_disconnected" in fake_socket_manager.events_for(game["room_code"])
