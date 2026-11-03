"""A player takes back the seat the host swapped to a bot (2026-11-03).

``reclaim_seat`` is the way back from ``substitute_bot``: only the seat's own
player can call it, from a connection ``join`` mapped to them, and the week
each order belongs to is attributed to whoever placed it -- not to whoever
held the seat when the week closed.
"""

from __future__ import annotations

import pytest

from app.core.enums import Role
from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.lobby import join
from app.sockets.handlers.play import (
    end_game_early,
    pause_game,
    reclaim_seat,
    submit_order,
    substitute_bot,
)

pytestmark = pytest.mark.asyncio


async def _current(state_svc, room_code: str):
    stored = await state_svc.get_room(room_code)
    return stored, state_svc.load_engine(stored)


def _host(game, **extra):
    return {"room_id": game["room_code"], "host_secret": game["host_secret"], **extra}


async def _order(game, role: str, week: int, order: int = 4) -> None:
    await submit_order(
        game["sids_by_role"][role],
        {"room_id": game["room_code"], "week": week, "order": order},
    )


def _record(engine, week: int, role: Role):
    return next(r for r in engine.history if r.week == week and r.role is role)


def _errors_to(fake_socket_manager, sid: str) -> list[dict]:
    return [
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "error" and target == sid
    ]


async def test_the_swapped_player_takes_the_seat_back_and_orders_again(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    factory_sid = game["sids_by_role"]["FACTORY"]
    factory_alias = game["alias_by_role"]["FACTORY"]

    await substitute_bot(game["host_sid"], _host(game, role="FACTORY"))
    fake_socket_manager.clear()

    await reclaim_seat(factory_sid, {"room_id": game["room_code"]})

    stored, engine = await _current(state_svc, game["room_code"])
    assert stored["participants"][factory_alias]["is_bot"] is False
    assert engine.agents[Role.FACTORY].is_bot is False
    assert "FACTORY" not in stored["bots"]

    reclaimed = [
        data
        for _room, event, data in fake_socket_manager.room_emits
        if event == "seat_reclaimed"
    ]
    assert reclaimed == [
        {
            "seq": reclaimed[0]["seq"],
            "role": "FACTORY",
            "alias": factory_alias,
            "display_name": "Factory",
        }
    ]
    assert "your_state" in fake_socket_manager.events_for(factory_sid)
    host_states = [
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "host_state" and target == game["host_sid"]
    ]
    assert host_states and host_states[-1]["roles"]["FACTORY"]["is_bot"] is False

    # The bot's order for the open week stands, and the player can change it.
    await _order(game, "FACTORY", engine.week, order=9)
    _stored, engine = await _current(state_svc, game["room_code"])
    assert engine.pending_orders[Role.FACTORY] == 9
    assert _errors_to(fake_socket_manager, factory_sid) == []


async def test_a_bot_order_left_standing_stays_the_bots_week(
    make_room, start_running_game, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    await substitute_bot(
        game["host_sid"], _host(game, role="FACTORY")
    )  # bot orders week 1
    await reclaim_seat(game["sids_by_role"]["FACTORY"], {"room_id": game["room_code"]})
    for role in ("RETAILER", "WHOLESALER", "DISTRIBUTOR"):
        await _order(game, role, 1)

    _stored, engine = await _current(state_svc, game["room_code"])
    assert _record(engine, 1, Role.FACTORY).was_bot is True

    for role in ("RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY"):
        await _order(game, role, 2)
    _stored, engine = await _current(state_svc, game["room_code"])
    assert _record(engine, 2, Role.FACTORY).was_bot is False


async def test_changing_the_bots_order_makes_the_week_the_players(
    make_room, start_running_game, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    await substitute_bot(game["host_sid"], _host(game, role="FACTORY"))
    await reclaim_seat(game["sids_by_role"]["FACTORY"], {"room_id": game["room_code"]})
    for role in ("FACTORY", "RETAILER", "WHOLESALER", "DISTRIBUTOR"):
        await _order(game, role, 1)

    _stored, engine = await _current(state_svc, game["room_code"])
    assert _record(engine, 1, Role.FACTORY).was_bot is False


async def test_a_swap_after_the_player_ordered_leaves_that_week_the_players(
    make_room, start_running_game, state_svc
):
    """The week open at the swap used to be marked as the bot's even when the
    player's own order was the one that stood."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    await _order(game, "FACTORY", 1)
    await substitute_bot(game["host_sid"], _host(game, role="FACTORY"))
    for role in ("RETAILER", "WHOLESALER", "DISTRIBUTOR"):
        await _order(game, role, 1)

    _stored, engine = await _current(state_svc, game["room_code"])
    assert _record(engine, 1, Role.FACTORY).was_bot is False

    for role in ("RETAILER", "WHOLESALER", "DISTRIBUTOR"):
        await _order(game, role, 2)
    _stored, engine = await _current(state_svc, game["room_code"])
    assert _record(engine, 2, Role.FACTORY).was_bot is True


async def test_a_player_who_dropped_rejoins_and_takes_the_seat_back(
    make_room, start_running_game, connect_identity, state_svc, fake_socket_manager
):
    """The usual path: drop, auto-pause, swap, resume or not -- then the player
    comes back on a new connection and takes the seat."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    await disconnect(game["sids_by_role"]["WHOLESALER"])
    await substitute_bot(game["host_sid"], _host(game, role="WHOLESALER"))

    new_sid = "sid-wholesaler-back"
    await connect_identity(new_sid, game["identities_by_role"]["WHOLESALER"])
    await join(new_sid, {"room_id": game["room_code"]})

    # Rejoining alone does not take the seat: a laptop waking up in a bag
    # reconnects too. The player chooses to.
    stored, _engine = await _current(state_svc, game["room_code"])
    alias = game["alias_by_role"]["WHOLESALER"]
    assert stored["participants"][alias]["is_bot"] is True

    await reclaim_seat(new_sid, {"room_id": game["room_code"]})

    stored, engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "PAUSED"  # allowed while paused
    assert stored["participants"][alias]["is_bot"] is False
    assert engine.agents[Role.WHOLESALER].is_bot is False


async def test_only_the_seats_own_player_can_take_it(
    make_room, start_running_game, connect_identity, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await substitute_bot(game["host_sid"], _host(game, role="FACTORY"))
    fake_socket_manager.clear()

    # Another seated player: their own seat is theirs already -- a no-op.
    await reclaim_seat(game["sids_by_role"]["RETAILER"], {"room_id": game["room_code"]})
    # A stranger: not seated at all. A payload naming a role is ignored.
    await connect_identity("sid-stranger", "guest_stranger")
    await reclaim_seat(
        "sid-stranger", {"room_id": game["room_code"], "role": "FACTORY"}
    )

    stored, engine = await _current(state_svc, game["room_code"])
    assert stored["participants"][game["alias_by_role"]["FACTORY"]]["is_bot"] is True
    assert engine.agents[Role.FACTORY].is_bot is True
    assert "seat_reclaimed" not in fake_socket_manager.events_for(game["room_code"])
    assert _errors_to(fake_socket_manager, game["sids_by_role"]["RETAILER"]) == []
    assert [e["code"] for e in _errors_to(fake_socket_manager, "sid-stranger")] == [
        "NOT_IN_ROOM"
    ]


async def test_a_seat_cannot_be_taken_back_after_the_game_ends(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await substitute_bot(game["host_sid"], _host(game, role="FACTORY"))
    await pause_game(game["host_sid"], _host(game))
    await end_game_early(game["host_sid"], _host(game))
    fake_socket_manager.clear()

    await reclaim_seat(game["sids_by_role"]["FACTORY"], {"room_id": game["room_code"]})

    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["participants"][game["alias_by_role"]["FACTORY"]]["is_bot"] is True
    codes = [
        e["code"]
        for e in _errors_to(fake_socket_manager, game["sids_by_role"]["FACTORY"])
    ]
    assert codes == ["GAME_FINISHED"]
