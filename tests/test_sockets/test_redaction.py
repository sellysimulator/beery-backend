"""Redaction (``12-socket-play.md §3.4``, ``00-conventions.md §3``).

Covers failure modes 1, 2 and 3 -- the reason this section has a dedicated
test agent. Only symbols from section 12's frozen *Public surface* are
imported, plus section 11's lobby handlers to drive a room to ``RUNNING``.
``app/sockets/handlers/play.py`` and ``app/services/game_service.py`` are
never read, only imported.
"""

from __future__ import annotations

import pytest

from app.core.config_models import (
    ConstantDemand,
    FactoryConfig,
    GameConfig,
    RoleConfig,
    VisibilityConfig,
)
from app.core.enums import Role
from app.sockets.handlers.play import (
    pause_game,
    request_state,
    resume_game,
    submit_order,
    substitute_bot,
)
from tests.test_sockets.conftest import contains_key, contains_value

pytestmark = pytest.mark.asyncio

# Fields a room broadcast may legitimately, and expectedly, carry a small
# varying integer under -- the per-room event counter and the week numbers
# (``00-conventions.md §3``, ``12-socket-play.md §2``). Excluding exactly
# these three keys from failure mode 1's probe-value scan avoids a false
# leak report when a probe value coincides with the room's current `seq` or
# `week`, without weakening the check anywhere else: none of the *other*
# frozen room-broadcast fields (`role`, `display_name`, `is_bot`,
# `awaiting_roles`) is ever numeric, so a real leak would surface under some
# other key regardless.
_SAFE_NUMERIC_KEYS = {"seq", "week", "next_week"}


def _contains_leaked_value(payload, value) -> bool:
    if isinstance(payload, dict):
        return any(
            _contains_leaked_value(v, value)
            for k, v in payload.items()
            if k not in _SAFE_NUMERIC_KEYS
        )
    if isinstance(payload, (list, tuple)):
        return any(_contains_leaked_value(item, value) for item in payload)
    if isinstance(payload, bool) or isinstance(value, bool):
        return False
    return payload == value


async def _current(state_svc, room_code: str):
    stored = await state_svc.get_room(room_code)
    return stored, state_svc.load_engine(stored)


async def _close_the_open_week(game, state_svc, order: int = 4):
    _stored, engine = await _current(state_svc, game["room_code"])
    week = engine.week
    for role in ("RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY"):
        await submit_order(
            game["sids_by_role"][role],
            {"room_id": game["room_code"], "week": week, "order": order},
        )


# --- failure mode 1 -------------------------------------------------------- #


async def test_order_quantities_never_appear_in_a_room_broadcast(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES", duration_weeks=8)
    game = await start_running_game(room)
    _stored, engine = await _current(state_svc, game["room_code"])
    week = engine.week
    fake_socket_manager.clear()

    probes = {"RETAILER": 7, "WHOLESALER": 13, "DISTRIBUTOR": 29, "FACTORY": 41}
    for role, qty in probes.items():
        await submit_order(
            game["sids_by_role"][role],
            {"room_id": game["room_code"], "week": week, "order": qty},
        )

    for _room_id, event, payload in fake_socket_manager.room_emits:
        if event == "game_finished":
            continue  # §3.4's single, documented exception -- not reached here
        for qty in probes.values():
            assert not _contains_leaked_value(payload, qty), (event, qty, payload)


# --- failure mode 2 -------------------------------------------------------- #


async def test_no_private_key_anywhere_in_a_room_broadcast(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    fake_socket_manager.clear()

    await _close_the_open_week(game, state_svc)  # week_closed + order_submitted x4
    await pause_game(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )
    await resume_game(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )
    await substitute_bot(
        game["host_sid"],
        {
            "room_id": game["room_code"],
            "host_secret": game["host_secret"],
            "role": "FACTORY",
        },
    )

    forbidden = (
        "inventory",
        "backlog",
        "accumulated_cost",
        "week_cost",
        "record",
        "identity",
        "session_token",
        "host_secret",
        "order",
    )
    assert fake_socket_manager.room_emits, "no room broadcast was captured"
    for _room_id, event, payload in fake_socket_manager.room_emits:
        for key in forbidden:
            assert not contains_key(payload, key), (event, key, payload)


# --- failure mode 3 -------------------------------------------------------- #


def _visibility_config(show_neighbour: bool) -> GameConfig:
    roles = {
        Role.RETAILER.value: RoleConfig(initial_inventory=101),
        Role.WHOLESALER.value: RoleConfig(initial_inventory=202),
        Role.DISTRIBUTOR.value: RoleConfig(initial_inventory=303),
        Role.FACTORY.value: FactoryConfig(initial_inventory=404),
    }
    return GameConfig(
        roles=roles,
        demand=ConstantDemand(value=4),
        duration_weeks=8,
        role_assignment_mode="PLAYER_CHOOSES",
        bot_fill_empty_roles=False,
        visibility=VisibilityConfig(show_neighbour_inventory=show_neighbour),
    )


async def test_retailer_your_state_hides_wholesaler_inventory_by_default(
    make_room_from_config, start_running_game, fake_socket_manager
):
    room = await make_room_from_config(_visibility_config(show_neighbour=False))
    game = await start_running_game(room)
    retailer_sid = game["sids_by_role"]["RETAILER"]
    fake_socket_manager.clear()

    await request_state(retailer_sid, {"room_id": game["room_code"]})

    payload = next(
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "your_state" and target == retailer_sid
    )
    assert not contains_value(
        payload, 202
    ), "the Wholesaler's inventory leaked under default visibility"


async def test_retailer_your_state_shows_wholesaler_inventory_when_enabled(
    make_room_from_config, start_running_game, fake_socket_manager
):
    room = await make_room_from_config(_visibility_config(show_neighbour=True))
    game = await start_running_game(room)
    retailer_sid = game["sids_by_role"]["RETAILER"]
    fake_socket_manager.clear()

    await request_state(retailer_sid, {"room_id": game["room_code"]})

    payload = next(
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "your_state" and target == retailer_sid
    )
    assert contains_value(
        payload, 202
    ), "show_neighbour_inventory=True must surface the Wholesaler's inventory"


# --- BE-4: order_history is host-sid only (12 §2, §3.4; 07 §3.9) ----------- #

_ORDER_PROBES = {"RETAILER": 111, "WHOLESALER": 222, "DISTRIBUTOR": 333, "FACTORY": 444}


async def _close_week_with_probe_orders(game, state_svc):
    _stored, engine = await _current(state_svc, game["room_code"])
    week = engine.week
    for role, qty in _ORDER_PROBES.items():
        await submit_order(
            game["sids_by_role"][role],
            {"room_id": game["room_code"], "week": week, "order": qty},
        )


def _host_states(fake_socket_manager, host_sid):
    return [
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "host_state" and target == host_sid
    ]


async def test_host_state_after_a_closed_week_carries_order_history_to_the_host_sid(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES", duration_weeks=8)
    game = await start_running_game(room)
    fake_socket_manager.clear()

    await _close_week_with_probe_orders(game, state_svc)

    states = _host_states(fake_socket_manager, game["host_sid"])
    assert states, "no host_state reached the host sid after the week closed"
    latest = states[-1]
    assert latest["order_history"] == {
        role: [qty] for role, qty in _ORDER_PROBES.items()
    }
    assert list(latest["order_history"].keys()) == [
        "RETAILER",
        "WHOLESALER",
        "DISTRIBUTOR",
        "FACTORY",
    ]


async def test_host_state_is_never_sent_to_a_player_sid_or_the_room(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES", duration_weeks=8)
    game = await start_running_game(room)
    fake_socket_manager.clear()

    await _close_week_with_probe_orders(game, state_svc)

    assert not [e for _t, e, _d in fake_socket_manager.room_emits if e == "host_state"]
    for target, event, _data in fake_socket_manager.sid_emits:
        if event == "host_state":
            assert target == game["host_sid"], target


async def test_order_history_appears_in_no_room_broadcast(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES", duration_weeks=8)
    game = await start_running_game(room)
    fake_socket_manager.clear()

    await _close_week_with_probe_orders(game, state_svc)
    await pause_game(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )
    await resume_game(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )

    assert fake_socket_manager.room_emits, "no room broadcast was captured"
    for _room_id, event, payload in fake_socket_manager.room_emits:
        assert not contains_key(payload, "order_history"), (event, payload)
        for qty in _ORDER_PROBES.values():
            assert not _contains_leaked_value(payload, qty), (event, qty, payload)


async def test_order_history_appears_in_no_player_your_state(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES", duration_weeks=8)
    game = await start_running_game(room)
    fake_socket_manager.clear()

    await _close_week_with_probe_orders(game, state_svc)
    for sid in game["sids_by_role"].values():
        await request_state(sid, {"room_id": game["room_code"]})

    player_sids = set(game["sids_by_role"].values())
    seen = 0
    for target, event, data in fake_socket_manager.sid_emits:
        if target in player_sids:
            seen += 1
            assert not contains_key(data, "order_history"), (target, event, data)
    assert seen, "no per-sid emit reached a player"
    your_states = [
        e for _t, e, _d in fake_socket_manager.sid_emits if e == "your_state"
    ]
    assert your_states, "no your_state was emitted"
