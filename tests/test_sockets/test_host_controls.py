"""Host controls and ``request_state`` (``12-socket-play.md §3.6``, ``§3.7``).

Covers acceptance criteria 14, 15, 16, 17, 18, 18a, 26 and 28, and failure
modes 10 and 14. Only symbols from section 12's frozen *Public surface* are
imported, plus section 11's lobby handlers to drive a room to ``RUNNING``.
``app/sockets/handlers/play.py`` and ``app/services/game_service.py`` are
never read, only imported.
"""

from __future__ import annotations

import pytest

from app.core.enums import Role
from app.sockets.handlers.play import (
    force_close_week,
    pause_game,
    request_state,
    resume_game,
    submit_order,
    substitute_bot,
)

pytestmark = pytest.mark.asyncio


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


def _host_secret_payload(game, **extra):
    return {
        "room_id": game["room_code"],
        "host_secret": game["host_secret"],
        **extra,
    }


# --- AC 14 / failure mode 10 ------------------------------------------------ #


async def test_pause_game_sets_paused_and_blocks_submission(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    await pause_game(game["host_sid"], _host_secret_payload(game))

    stored, engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "PAUSED"
    assert "game_paused" in fake_socket_manager.events_for(game["room_code"])

    fake_socket_manager.clear()
    await submit_order(
        game["sids_by_role"]["RETAILER"],
        {"room_id": game["room_code"], "week": engine.week, "order": 5},
    )

    # Failure mode 10: the order must not be recorded, not merely unacknowledged.
    _stored2, engine_after = await _current(state_svc, game["room_code"])
    assert Role.RETAILER not in engine_after.pending_orders
    error = next(
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "error" and target == game["sids_by_role"]["RETAILER"]
    )
    assert error["code"] == "GAME_PAUSED"


# --- AC 15 ------------------------------------------------------------------ #


async def test_resume_game_restores_running_and_allows_submission(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await pause_game(game["host_sid"], _host_secret_payload(game))

    await resume_game(game["host_sid"], _host_secret_payload(game))

    stored, engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "RUNNING"

    await submit_order(
        game["sids_by_role"]["RETAILER"],
        {"room_id": game["room_code"], "week": engine.week, "order": 5},
    )
    _stored2, engine_after = await _current(state_svc, game["room_code"])
    assert engine_after.pending_orders[Role.RETAILER] == 5


# --- AC 16 ------------------------------------------------------------------- #


async def test_force_close_week_records_zero_and_was_forced_for_missing_roles(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    _stored, engine = await _current(state_svc, game["room_code"])
    week = engine.week
    for role in ("RETAILER", "WHOLESALER"):
        await submit_order(
            game["sids_by_role"][role],
            {"room_id": game["room_code"], "week": week, "order": 5},
        )
    fake_socket_manager.clear()

    await force_close_week(game["host_sid"], _host_secret_payload(game))

    _stored, engine_after = await _current(state_svc, game["room_code"])
    closed = [r for r in engine_after.history if r.week == week]
    assert len(closed) == 4
    forced = {r.role for r in closed if r.was_forced}
    assert forced == {Role.DISTRIBUTOR, Role.FACTORY}
    for record in closed:
        if record.role in forced:
            assert record.order == 0
        else:
            assert record.was_forced is False

    forced_order_submitted = {
        data["role"]
        for _room_id, event, data in fake_socket_manager.room_emits
        if event == "order_submitted" and data.get("role") in {"DISTRIBUTOR", "FACTORY"}
    }
    assert forced_order_submitted == set()


# --- AC 17 -------------------------------------------------------------------- #


async def test_force_close_week_while_paused_is_rejected(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await pause_game(game["host_sid"], _host_secret_payload(game))
    _before, engine_before = await _current(state_svc, game["room_code"])
    fake_socket_manager.clear()

    await force_close_week(game["host_sid"], _host_secret_payload(game))

    stored, engine_after = await _current(state_svc, game["room_code"])
    assert stored["state"] == "PAUSED"
    assert engine_after.week == engine_before.week
    assert engine_after.history == engine_before.history
    assert "week_closed" not in fake_socket_manager.events_for(game["room_code"])
    error = next(
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "error" and target == game["host_sid"]
    )
    assert error["code"] == "GAME_PAUSED"


# --- AC 18 -------------------------------------------------------------------- #


async def test_substitute_bot_marks_role_emits_and_plays_immediately(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    fake_socket_manager.clear()

    await substitute_bot(game["host_sid"], _host_secret_payload(game, role="FACTORY"))

    stored, engine = await _current(state_svc, game["room_code"])
    factory_alias = game["alias_by_role"]["FACTORY"]
    assert stored["participants"][factory_alias]["is_bot"] is True
    assert "bot_substituted" in fake_socket_manager.events_for(game["room_code"])
    assert Role.FACTORY in engine.pending_orders  # the bot ordered immediately

    factory_submissions = [
        data
        for _room_id, event, data in fake_socket_manager.room_emits
        if event == "order_submitted" and data.get("role") == "FACTORY"
    ]
    assert factory_submissions and factory_submissions[0]["is_bot"] is True


# --- AC 18a ------------------------------------------------------------------- #


async def test_week_record_after_substitution_carries_was_bot_true_history_unchanged(
    make_room, start_running_game, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await _close_the_open_week(game, state_svc)  # week 1, all-human

    _stored, engine = await _current(state_svc, game["room_code"])
    week1_before = next(
        r for r in engine.history if r.week == 1 and r.role == Role.FACTORY
    )
    assert week1_before.was_bot is False

    await substitute_bot(game["host_sid"], _host_secret_payload(game, role="FACTORY"))
    _stored, engine = await _current(state_svc, game["room_code"])
    week2 = engine.week
    for role in ("RETAILER", "WHOLESALER", "DISTRIBUTOR"):
        await submit_order(
            game["sids_by_role"][role],
            {"room_id": game["room_code"], "week": week2, "order": 4},
        )

    _stored, engine_after = await _current(state_svc, game["room_code"])
    week2_record = next(
        r for r in engine_after.history if r.week == week2 and r.role == Role.FACTORY
    )
    assert week2_record.was_bot is True

    week1_after = next(
        r for r in engine_after.history if r.week == 1 and r.role == Role.FACTORY
    )
    assert week1_after == week1_before


async def test_substitute_bot_on_an_already_bot_role_is_a_noop(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await substitute_bot(game["host_sid"], _host_secret_payload(game, role="FACTORY"))
    fake_socket_manager.clear()

    await substitute_bot(game["host_sid"], _host_secret_payload(game, role="FACTORY"))

    # A no-op, not an error: no second bot_substituted broadcast is required
    # by the document, but nothing may error either.
    assert not any(
        event == "error"
        for target, event, _d in fake_socket_manager.sid_emits
        if target == game["host_sid"]
    )


# --- AC 26 -------------------------------------------------------------------- #


async def test_request_state_reemits_view_and_mutates_nothing(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    before, _engine = await _current(state_svc, game["room_code"])
    fake_socket_manager.clear()

    await request_state(
        game["sids_by_role"]["RETAILER"], {"room_id": game["room_code"]}
    )
    assert "your_state" in [
        event
        for target, event, _d in fake_socket_manager.sid_emits
        if target == game["sids_by_role"]["RETAILER"]
    ]

    after, _engine2 = await _current(state_svc, game["room_code"])
    assert after == before

    fake_socket_manager.clear()
    await request_state(game["host_sid"], {"room_id": game["room_code"]})
    assert "host_state" in [
        event
        for target, event, _d in fake_socket_manager.sid_emits
        if target == game["host_sid"]
    ]
    after_host, _engine3 = await _current(state_svc, game["room_code"])
    assert after_host == before


# --- AC 28 / failure mode 14 -------------------------------------------------- #


def _last_error_code(fake_socket_manager, sid: str) -> str:
    return next(
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "error" and target == sid
    )["code"]


async def test_all_host_controls_reject_a_wrong_secret(
    make_room, start_running_game, state_svc, fake_socket_manager
):
    """§3.6: a wrong or missing ``host_secret`` is ``error
    {code: "INVALID_HOST_SECRET"}`` for all four controls, with the same
    message whichever it was."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    wrong = "definitely-not-the-secret"

    # pause_game
    fake_socket_manager.clear()
    await pause_game(
        game["host_sid"], {"room_id": game["room_code"], "host_secret": wrong}
    )
    stored, _e = await _current(state_svc, game["room_code"])
    assert stored["state"] == "RUNNING"
    assert (
        _last_error_code(fake_socket_manager, game["host_sid"]) == "INVALID_HOST_SECRET"
    )

    # A missing secret is rejected the same way.
    fake_socket_manager.clear()
    await pause_game(game["host_sid"], {"room_id": game["room_code"]})
    stored, _e = await _current(state_svc, game["room_code"])
    assert stored["state"] == "RUNNING"
    assert (
        _last_error_code(fake_socket_manager, game["host_sid"]) == "INVALID_HOST_SECRET"
    )

    # Legitimately pause so resume_game's rejection has something to reject.
    await pause_game(game["host_sid"], _host_secret_payload(game))

    # resume_game
    fake_socket_manager.clear()
    await resume_game(
        game["host_sid"], {"room_id": game["room_code"], "host_secret": wrong}
    )
    stored, _e = await _current(state_svc, game["room_code"])
    assert stored["state"] == "PAUSED"
    assert (
        _last_error_code(fake_socket_manager, game["host_sid"]) == "INVALID_HOST_SECRET"
    )

    await resume_game(game["host_sid"], _host_secret_payload(game))  # back to RUNNING

    # force_close_week
    _stored, engine_before = await _current(state_svc, game["room_code"])
    fake_socket_manager.clear()
    await force_close_week(
        game["host_sid"], {"room_id": game["room_code"], "host_secret": wrong}
    )
    _stored, engine_after = await _current(state_svc, game["room_code"])
    assert engine_after.week == engine_before.week
    assert engine_after.history == engine_before.history
    assert (
        _last_error_code(fake_socket_manager, game["host_sid"]) == "INVALID_HOST_SECRET"
    )

    # substitute_bot
    fake_socket_manager.clear()
    await substitute_bot(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": wrong, "role": "FACTORY"},
    )
    stored, _e = await _current(state_svc, game["room_code"])
    factory_alias = game["alias_by_role"]["FACTORY"]
    assert stored["participants"][factory_alias]["is_bot"] is False
    assert (
        _last_error_code(fake_socket_manager, game["host_sid"]) == "INVALID_HOST_SECRET"
    )
