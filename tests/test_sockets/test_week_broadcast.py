"""Week closing, bots and game completion (``12-socket-play.md §3.2``,
``§3.3``, ``§3.8``).

Covers acceptance criteria 10, 11, 12, 13, 19 and 27, and failure modes 11
(lock held across persistence), 12 (persistence failure/hang must not break
the debrief) and 13 (bot recursion).

Only symbols from section 12's frozen *Public surface* are imported --
including the module-level ``persist_finished_game`` seam in
``app.services.game_service``, looked up by name at call time and
monkeypatched here exactly as the document prescribes -- plus section 11's
lobby handlers to drive a room to ``RUNNING`` and the frozen room-document /
``GameEngine`` attributes ``09-state-service.md §2`` and
``07-game-engine.md §2`` already freeze. ``app/sockets/handlers/play.py`` and
the rest of ``app/services/game_service.py`` are never read, only imported.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

import pytest

import app.services.game_service as game_service_module
from app.core.enums import Role
from app.sockets.handlers.lobby import claim_role, join, join_waiting, start_game
from app.sockets.handlers.play import end_game_early, submit_order
from tests.test_sockets.conftest import new_guest

pytestmark = pytest.mark.asyncio


async def _current(state_svc, room_code: str):
    stored = await state_svc.get_room(room_code)
    return stored, state_svc.load_engine(stored)


async def _close_the_open_week(game, state_svc, order: int = 4):
    """Submit ``order`` for every role, closing whatever week is open."""
    _stored, engine = await _current(state_svc, game["room_code"])
    week = engine.week
    for role in ("RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY"):
        await submit_order(
            game["sids_by_role"][role],
            {"room_id": game["room_code"], "week": week, "order": order},
        )


# --- AC 10 / AC 11 -------------------------------------------------------- #


async def test_fourth_order_closes_week_with_the_specified_emit_shape(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    _stored, engine = await _current(state_svc, game["room_code"])
    week = engine.week

    # First three orders leave the window open.
    for role in ("RETAILER", "WHOLESALER", "DISTRIBUTOR"):
        await submit_order(
            game["sids_by_role"][role],
            {"room_id": game["room_code"], "week": week, "order": 4},
        )
    fake_socket_manager.clear()

    # The closing submission.
    await submit_order(
        game["sids_by_role"]["FACTORY"],
        {"room_id": game["room_code"], "week": week, "order": 4},
    )

    events = fake_socket_manager.emits  # ordered [(target, event, data), ...]
    week_closed_idxs = [i for i, (_t, e, _d) in enumerate(events) if e == "week_closed"]
    assert len(week_closed_idxs) == 1
    week_closed_idx = week_closed_idxs[0]
    week_closed_target, _e, week_closed_payload = events[week_closed_idx]
    assert week_closed_target == game["room_code"]
    assert set(week_closed_payload.keys()) == {
        "seq",
        "week",
        "next_week",
        "awaiting_roles",
    }
    assert week_closed_payload["week"] == week
    assert week_closed_payload["next_week"] == week + 1

    ywc_idxs = [i for i, (_t, e, _d) in enumerate(events) if e == "your_week_closed"]
    assert ywc_idxs, "your_week_closed was never emitted"
    assert all(
        i > week_closed_idx for i in ywc_idxs
    ), "week_closed must precede every your_week_closed (§3.3 step 2)"
    last_ywc_idx = max(ywc_idxs)

    post_close_your_state_idxs = [
        i
        for i, (_t, e, _d) in enumerate(events)
        if e == "your_state" and i > week_closed_idx
    ]
    host_state_idxs = [i for i, (_t, e, _d) in enumerate(events) if e == "host_state"]
    assert host_state_idxs, "host_state was never emitted"
    assert all(
        i > last_ywc_idx for i in post_close_your_state_idxs + host_state_idxs
    ), "a fresh your_state/host_state must follow every your_week_closed (§3.3 step 2)"

    # AC 11 -- each player's your_week_closed carries only that role's own record.
    ywc_by_sid: dict[str, list[dict]] = {}
    for target, event, data in fake_socket_manager.sid_emits:
        if event == "your_week_closed":
            ywc_by_sid.setdefault(target, []).append(data)

    for role, sid in game["sids_by_role"].items():
        records = ywc_by_sid.get(sid)
        assert records, f"{role} never received your_week_closed"
        for payload in records:
            assert set(payload.keys()) == {"seq", "week", "record"}
            assert payload["record"]["role"] == role


# --- AC 12 ------------------------------------------------------------------ #


async def test_bots_play_automatically_when_a_window_opens(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room = await make_room(
        role_assignment_mode="PLAYER_CHOOSES", bot_fill_empty_roles=True
    )
    room_code = room["room_code"]
    for role in ("RETAILER", "WHOLESALER", "DISTRIBUTOR"):
        sid = f"sid-{role.lower()}"
        await connect_identity(sid, new_guest())
        await join(sid, {"room_id": room_code, "display_name": role.title()})
        await claim_role(sid, {"room_id": room_code, "role": role})

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room_code, "host_secret": room["host_secret"]}
    )
    fake_socket_manager.clear()

    await start_game(
        "sid-host", {"room_id": room_code, "host_secret": room["host_secret"]}
    )

    factory_submissions = [
        data
        for _room_id, event, data in fake_socket_manager.room_emits
        if event == "order_submitted" and data.get("role") == "FACTORY"
    ]
    assert factory_submissions, "the auto-filled FACTORY bot never ordered"
    assert factory_submissions[0]["is_bot"] is True

    _stored, engine = await _current(state_svc, room_code)
    assert Role.FACTORY in engine.pending_orders


# --- AC 13 -------------------------------------------------------------- #


async def test_all_bot_36_week_game_finishes_from_a_single_start_game(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room = await make_room(duration_weeks=36, bot_fill_empty_roles=True)
    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    stored, engine = await _current(state_svc, room["room_code"])
    assert stored["state"] == "FINISHED"
    assert engine.weeks_played == 36
    assert "game_finished" in fake_socket_manager.events_for(room["room_code"])


# --- AC 19 ---------------------------------------------------------------- #


async def test_end_game_early_mid_week_reports_weeks_played_as_week_minus_one(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    # Advance a couple of full weeks, then abandon the next one mid-decision.
    await _close_the_open_week(game, state_svc)
    await _close_the_open_week(game, state_svc)
    _stored, engine = await _current(state_svc, game["room_code"])
    open_week = engine.week
    await submit_order(
        game["sids_by_role"]["RETAILER"],
        {"room_id": game["room_code"], "week": open_week, "order": 3},
    )  # one of four -- the week is left mid-decision

    fake_socket_manager.clear()
    await end_game_early(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )

    stored, engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "FINISHED"
    assert engine.weeks_played == open_week - 1
    finished_payload = next(
        data
        for _room_id, event, data in fake_socket_manager.room_emits
        if event == "game_finished"
    )
    assert finished_payload["weeks_played"] == open_week - 1
    assert not any(r.week == open_week for r in engine.history)


# --- AC 27 ------------------------------------------------------------------ #


async def test_game_finished_carries_full_demand_series_and_every_roles_orders(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room = await make_room(duration_weeks=10, bot_fill_empty_roles=True)
    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    _stored, engine = await _current(state_svc, room["room_code"])
    assert engine.weeks_played == 10

    finished_payload = next(
        data
        for _room_id, event, data in fake_socket_manager.room_emits
        if event == "game_finished"
    )
    assert len(finished_payload["demand_series"]) == 10
    orders_by_role = finished_payload["orders_by_role"]
    assert set(orders_by_role.keys()) == {
        "RETAILER",
        "WHOLESALER",
        "DISTRIBUTOR",
        "FACTORY",
    }
    for role, orders in orders_by_role.items():
        assert len(orders) == 10, role


# --- failure mode 13 -------------------------------------------------------- #


async def test_all_bot_104_week_game_does_not_recurse(
    make_room, connect_identity, state_svc
):
    """A cascading, recursive ``close_week -> run_bots -> close_week -> ...``
    implementation consumes one Python call-stack frame per week. Lowering
    the recursion limit to just above the ambient call depth turns that into
    an observable ``RecursionError`` for 104 weeks, while a loop-based
    implementation's stack depth never grows with the week count."""
    room = await make_room(duration_weeks=104, bot_fill_empty_roles=True)
    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    import inspect

    ambient_depth = len(inspect.stack())
    original_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(ambient_depth + 80)
    try:
        await start_game(
            "sid-host",
            {"room_id": room["room_code"], "host_secret": room["host_secret"]},
        )
    finally:
        sys.setrecursionlimit(original_limit)

    stored, engine = await _current(state_svc, room["room_code"])
    assert stored["state"] == "FINISHED"
    assert engine.weeks_played == 104


# --- failure mode 11 -------------------------------------------------------- #


async def test_finish_game_never_acquires_the_room_lock(
    make_room, start_running_game, state_svc, monkeypatch
):
    """``finish_game`` is documented (``§2.1``) to *never* acquire
    ``state_svc.lock`` -- persistence must not run under a lock at all. A spy
    on ``state_svc.lock`` is timing-independent, unlike a bare
    ``asyncio.gather`` against ``FakeRedis``: gathered coroutines against an
    in-memory fake run to completion one at a time, so a wrong
    implementation that wraps the whole body (including a slow persistence
    call) in the lock would still finish instantly and prove nothing by
    timing alone. Counting actual lock acquisitions does not have that
    problem."""
    from app.services.game_service import GameService

    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    stored, _engine = await _current(state_svc, game["room_code"])

    lock_calls: list[str] = []
    original_lock = state_svc.lock

    def spy_lock(room_code: str):
        lock_calls.append(room_code)
        return original_lock(room_code)

    monkeypatch.setattr(state_svc, "lock", spy_lock)

    service = GameService()
    await service.finish_game(game["room_code"], stored)

    assert lock_calls == []


# --- failure mode 12 -------------------------------------------------------- #


async def test_persistence_failure_does_not_break_the_debrief(
    make_room, start_running_game, state_svc, fake_socket_manager, monkeypatch
):
    """§3.8 steps 4-5: a raising ``persist_finished_game`` must not stop
    ``game_finished`` from reaching the room, must not stop ``state`` from
    becoming ``FINISHED``, and must leave ``persisted`` ``False``."""

    async def failing_persist(room_code, room, engine, stats):
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(game_service_module, "persist_finished_game", failing_persist)

    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    await end_game_early(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )

    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["state"] == "FINISHED"
    assert stored["persisted"] is False
    assert "game_finished" in fake_socket_manager.events_for(game["room_code"])


async def test_game_finished_is_emitted_before_persistence_even_if_it_hangs(
    make_room, start_running_game, state_svc, fake_socket_manager, monkeypatch
):
    """§3.8 step 4: ``game_finished`` must be emitted *before*
    ``persist_finished_game`` is called, not after. This test isolates the
    ordering itself, independent of failure handling: it makes
    ``persist_finished_game`` hang forever (``release_persist`` is never
    set -- "longer than the test's patience") and asserts the emit has
    already happened by the time persistence has demonstrably started. That
    is only possible if the emit precedes the call; a version that emits
    after persisting would still be blocked on the hang and would never
    have reached the emit at all, so the assertion below would simply fail."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    persist_started = asyncio.Event()
    release_persist = asyncio.Event()  # never set -- "longer than the test's patience"

    async def hanging_persist(room_code, room, engine, stats):
        persist_started.set()
        await release_persist.wait()
        return True

    monkeypatch.setattr(game_service_module, "persist_finished_game", hanging_persist)

    task = asyncio.create_task(
        end_game_early(
            game["host_sid"],
            {"room_id": game["room_code"], "host_secret": game["host_secret"]},
        )
    )
    try:
        await asyncio.wait_for(persist_started.wait(), timeout=5)
        # Let anything scheduled at the same tick as `persist_started.set()`
        # settle before inspecting the recorded emits.
        await asyncio.sleep(0)

        assert "game_finished" in fake_socket_manager.events_for(game["room_code"]), (
            "game_finished must already have been emitted by the time "
            "persist_finished_game is invoked (§3.8 step 4) -- a version "
            "that emits after persisting would still be waiting here"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
