"""Disconnection -- D7 (``12-socket-play.md §3.5``).

Covers acceptance criteria 20, 21, 22, 23, 24 and 25, and failure modes 8, 9
and 15. Only symbols from section 12's frozen *Public surface* are imported,
plus section 02's frozen ``connection.disconnect`` (the real integration
point this section's cleanup hook is wired behind) and section 11's lobby
handlers to drive a room to ``RUNNING``. ``app/sockets/handlers/play.py`` and
``app/services/game_service.py`` are never read, only imported.

Every test below calls ``app.sockets.handlers.connection.disconnect(sid)``
rather than ``play.on_participant_disconnected(sid)`` directly, because the
document's own ordering rule (``§0`` header: this hook "must be called
BEFORE ``socket_manager.disconnect(sid)``") is a property of the *wiring*
between the two sections, not of ``on_participant_disconnected`` in
isolation -- calling the hook directly would prove nothing about whether
that wiring is correct.
"""

from __future__ import annotations

import pytest

from app.core.config_models import ConstantDemand, FactoryConfig, GameConfig, RoleConfig
from app.core.enums import Role
from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.lobby import join, join_waiting
from app.sockets.handlers.play import resume_game, substitute_bot

pytestmark = pytest.mark.asyncio


async def _current(state_svc, room_code: str):
    stored = await state_svc.get_room(room_code)
    return stored, state_svc.load_engine(stored)


def _config_no_pause_on_disconnect() -> GameConfig:
    roles = {
        Role.RETAILER.value: RoleConfig(),
        Role.WHOLESALER.value: RoleConfig(),
        Role.DISTRIBUTOR.value: RoleConfig(),
        Role.FACTORY.value: FactoryConfig(),
    }
    return GameConfig(
        roles=roles,
        demand=ConstantDemand(value=4),
        duration_weeks=8,
        role_assignment_mode="PLAYER_CHOOSES",
        bot_fill_empty_roles=False,
        pause_on_disconnect=False,
    )


def _host_secret_payload(game, **extra):
    return {"room_id": game["room_code"], "host_secret": game["host_secret"], **extra}


# --- AC 20 -------------------------------------------------------------------- #


async def test_disconnect_of_seated_human_pauses_and_names_the_role(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(
        role_assignment_mode="PLAYER_CHOOSES"
    )  # pause_on_disconnect defaults True
    game = await start_running_game(room)
    fake_socket_manager.clear()

    await disconnect(game["sids_by_role"]["WHOLESALER"])

    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "PAUSED"
    payload = next(
        data
        for _room_id, event, data in fake_socket_manager.room_emits
        if event == "game_paused"
    )
    assert payload["reason"] == stored["paused_reason"]
    assert "Wholesaler" in payload["reason"]
    assert "disconnected" in payload["reason"]
    assert "WHOLESALER" in payload["reason"].upper()
    assert "participant_disconnected" in fake_socket_manager.events_for(
        game["room_code"]
    )


# --- AC 21 -------------------------------------------------------------------- #


async def test_disconnect_with_pause_on_disconnect_false_does_not_pause(
    make_room_from_config, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room_from_config(_config_no_pause_on_disconnect())
    game = await start_running_game(room)
    fake_socket_manager.clear()

    await disconnect(game["sids_by_role"]["WHOLESALER"])

    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "RUNNING"
    assert "game_paused" not in fake_socket_manager.events_for(game["room_code"])


# --- AC 22 -------------------------------------------------------------------- #


async def test_disconnect_of_a_bot_role_never_pauses(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await substitute_bot(game["host_sid"], _host_secret_payload(game, role="FACTORY"))
    fake_socket_manager.clear()

    await disconnect(game["sids_by_role"]["FACTORY"])

    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "RUNNING"
    assert "game_paused" not in fake_socket_manager.events_for(game["room_code"])


# --- AC 23 -------------------------------------------------------------------- #


async def test_disconnect_of_host_alerts_but_never_pauses_and_secret_reclaims(
    make_room, start_running_game, register_sid, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    fake_socket_manager.clear()

    await disconnect(game["host_sid"])

    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "RUNNING"
    assert stored["host_sid"] is None
    events = fake_socket_manager.events_for(game["room_code"])
    assert "game_paused" not in events
    assert "host_disconnected" in events

    fake_socket_manager.clear()
    new_host_sid = "sid-host-reclaimed"
    await register_sid(new_host_sid)
    await join_waiting(
        new_host_sid, {"room_id": game["room_code"], "host_secret": game["host_secret"]}
    )
    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["host_sid"] == new_host_sid
    assert stored["state"] == "RUNNING"
    assert "host_reconnected" in fake_socket_manager.events_for(game["room_code"])


async def test_disconnect_of_host_while_paused_keeps_the_existing_pause(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await disconnect(game["sids_by_role"]["RETAILER"])
    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "PAUSED"
    reason = stored["paused_reason"]
    fake_socket_manager.clear()

    await disconnect(game["host_sid"])

    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "PAUSED"
    assert stored["paused_reason"] == reason
    events = fake_socket_manager.events_for(game["room_code"])
    assert "game_paused" not in events
    assert "host_disconnected" in events


async def test_host_reclaim_in_the_lobby_announces_nothing(
    make_room, register_sid, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    sid = "sid-host-lobby"
    await register_sid(sid)
    fake_socket_manager.clear()

    await join_waiting(
        sid, {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    assert "host_reconnected" not in fake_socket_manager.events_for(room["room_code"])


# --- AC 24 / failure mode 15 -------------------------------------------------- #


async def test_host_secret_gone_recovers_by_identity_and_can_then_resume(
    make_room, start_running_game, connect_identity, state_svc, fake_socket_manager
):
    """D18's whole reason to exist: with the secret discarded, the *only*
    recovery path left is verified identity. Without it this test would hang
    at a room nobody can unpause (failure mode 15)."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await disconnect(game["host_sid"])
    fake_socket_manager.clear()

    recovered_sid = "sid-host-by-identity"
    await connect_identity(recovered_sid, game["host_identity"])
    await join_waiting(recovered_sid, {"room_id": game["room_code"]})  # no secret

    claimed = [
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "host_claimed" and target == recovered_sid
    ]
    assert len(claimed) == 1
    new_secret = claimed[0]["host_secret"]

    await resume_game(
        recovered_sid, {"room_id": game["room_code"], "host_secret": new_secret}
    )
    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "RUNNING"


# --- AC 25 / failure mode 8 --------------------------------------------------- #


async def test_reconnect_does_not_auto_resume_a_paused_game(
    make_room, start_running_game, connect_identity, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    wholesaler_identity = game["identities_by_role"]["WHOLESALER"]

    await disconnect(game["sids_by_role"]["WHOLESALER"])
    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "PAUSED"
    fake_socket_manager.clear()

    reconnected_sid = "sid-wholesaler-reconnected"
    await connect_identity(reconnected_sid, wholesaler_identity)
    await join(reconnected_sid, {"room_id": game["room_code"]})

    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "PAUSED"
    assert "game_resumed" not in fake_socket_manager.events_for(game["room_code"])
    assert "participant_reconnected" in fake_socket_manager.events_for(
        game["room_code"]
    )


# --- failure mode 9 ------------------------------------------------------------ #


async def test_disconnect_never_auto_substitutes_a_bot(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    await disconnect(game["sids_by_role"]["DISTRIBUTOR"])

    stored, engine = await _current(state_svc, game["room_code"])
    distributor_alias = game["alias_by_role"]["DISTRIBUTOR"]
    assert stored["participants"][distributor_alias]["is_bot"] is False
    assert engine.agents[Role.DISTRIBUTOR].is_bot is False
    assert "bot_substituted" not in fake_socket_manager.events_for(game["room_code"])
