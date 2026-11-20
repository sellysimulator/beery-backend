"""Week closing, bots and game completion (spec §7, §11.2).

Also: no lock held across persistence, a persistence failure or hang must not
break the debrief, and no bot recursion.

Only symbols from the play handlers' public surface are imported --
including the module-level ``persist_finished_game`` seam in
``app.services.game_service``, looked up by name at call time and
monkeypatched here -- plus the lobby handlers to drive a room to ``RUNNING``
and the room-document / ``GameEngine`` attributes already frozen (spec
§11.3, §8.6). ``app/sockets/handlers/play.py`` and the rest of
``app/services/game_service.py`` are never read, only imported.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

import pytest

import app.services.game_service as game_service_module
from app.core.enums import Role
from app.sockets.handlers.lobby import claim_role, join, join_waiting, start_game
from app.sockets.handlers.play import end_game_early, request_state, submit_order
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


# --- per-round state carries the new record, not the whole history -------- #


def _sid_events(fake_socket_manager, sid: str, name: str) -> list[dict]:
    return [
        data
        for target, event, data in fake_socket_manager.sid_emits
        if target == sid and event == name
    ]


async def test_per_round_your_state_leaves_the_history_to_your_week_closed(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    """A role's history grows by one record a week, so resending all of it in
    every ``your_state`` grows the traffic with the square of the game's
    length. The week's one new record travels in ``your_week_closed`` and the
    client appends it; the ``your_state`` after a submission and after a
    close carries no history at all."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await _close_the_open_week(game, state_svc)
    fake_socket_manager.clear()

    await _close_the_open_week(game, state_svc)

    for role, sid in game["sids_by_role"].items():
        states = _sid_events(fake_socket_manager, sid, "your_state")
        # One after the role's own submission, one after the close.
        assert len(states) == 2, role
        assert all("own_history" not in state for state in states), role
        records = [
            data["record"]
            for data in _sid_events(fake_socket_manager, sid, "your_week_closed")
        ]
        assert [(r["role"], r["week"]) for r in records] == [(role, 2)]


async def test_request_state_resends_the_whole_history(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    """The full history still goes out wherever a client may have missed a
    record: on an explicit state request (and on reconnect, through
    ``join``)."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await _close_the_open_week(game, state_svc)
    await _close_the_open_week(game, state_svc)
    sid = game["sids_by_role"]["WHOLESALER"]
    fake_socket_manager.clear()

    await request_state(sid, {"room_id": game["room_code"]})

    (state,) = _sid_events(fake_socket_manager, sid, "your_state")
    assert [(r["role"], r["week"]) for r in state["own_history"]] == [
        ("WHOLESALER", 1),
        ("WHOLESALER", 2),
    ]


# --- the fourth order closes the week ------------------------------------- #


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
    ), "week_closed must precede every your_week_closed"
    last_ywc_idx = max(ywc_idxs)

    post_close_your_state_idxs = [
        i
        for i, (_t, e, _d) in enumerate(events)
        if e == "your_state" and i > week_closed_idx
    ]
    # The closing submission's own host_state goes out before the close; the
    # one for the new week is what must follow the records.
    host_state_idxs = [
        i
        for i, (_t, e, _d) in enumerate(events)
        if e == "host_state" and i > week_closed_idx
    ]
    assert host_state_idxs, "host_state was never emitted after the close"
    assert all(
        i > last_ywc_idx for i in post_close_your_state_idxs + host_state_idxs
    ), "a fresh your_state/host_state must follow every your_week_closed"

    # Each player's your_week_closed carries only that role's own record.
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


# --- bots play when a window opens ------------------------------------------ #


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


async def test_the_host_sees_the_bots_decided_while_humans_still_decide(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    """Bots play the moment a week opens, after the host's state for that week
    has gone out. The host gets a fresh one once they have played, or the
    console shows the bot seats as undecided until the week closes."""
    room = await make_room(
        role_assignment_mode="PLAYER_CHOOSES", bot_fill_empty_roles=True
    )
    room_code = room["room_code"]
    for role in ("RETAILER", "WHOLESALER"):
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

    host_states = _sid_events(fake_socket_manager, "sid-host", "host_state")
    latest = host_states[-1]
    assert latest["roles"]["DISTRIBUTOR"]["has_submitted"] is True
    assert latest["roles"]["FACTORY"]["has_submitted"] is True
    assert latest["roles"]["RETAILER"]["has_submitted"] is False
    assert latest["awaiting_roles"] == ["RETAILER", "WHOLESALER"]


async def test_the_host_sees_every_seat_decided_when_the_last_order_ends_the_game(
    make_room, connect_identity, fake_socket_manager, state_svc, monkeypatch
):
    """Two humans and two bots, played to the end. The last human's order
    closes the final week and finishes the game, and no state for a new week
    follows it -- so unless the host is sent a state *for that submission*,
    the console ends the game with that player still "Still deciding"."""

    async def no_persist(room_code, room, engine, stats):
        return None

    monkeypatch.setattr(game_service_module, "persist_finished_game", no_persist)

    room = await make_room(
        role_assignment_mode="PLAYER_CHOOSES",
        bot_fill_empty_roles=True,
        duration_weeks=8,
    )
    room_code = room["room_code"]
    humans = {"RETAILER": "sid-retailer", "WHOLESALER": "sid-wholesaler"}
    for role, sid in humans.items():
        await connect_identity(sid, new_guest())
        await join(sid, {"room_id": room_code, "display_name": role.title()})
        await claim_role(sid, {"room_id": room_code, "role": role})
    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room_code, "host_secret": room["host_secret"]}
    )
    await start_game(
        "sid-host", {"room_id": room_code, "host_secret": room["host_secret"]}
    )

    for _ in range(8):
        _stored, engine = await _current(state_svc, room_code)
        week = engine.week
        fake_socket_manager.clear()
        for sid in humans.values():
            await submit_order(sid, {"room_id": room_code, "week": week, "order": 4})

    stored, _engine = await _current(state_svc, room_code)
    assert stored["state"] == "FINISHED"
    latest = _sid_events(fake_socket_manager, "sid-host", "host_state")[-1]
    assert {role: view["has_submitted"] for role, view in latest["roles"].items()} == {
        "RETAILER": True,
        "WHOLESALER": True,
        "DISTRIBUTOR": True,
        "FACTORY": True,
    }


# --- an all-bot game ---------------------------------------------------- #


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


# --- end_game_early ------------------------------------------------------- #


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


async def test_end_game_early_leaves_every_live_total_equal_to_the_results(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    """The results leave the abandoned week's holding and backlog out ($164),
    so the live totals must too, not show $180 on the host console. The host
    and every seated player get a fresh state, before ``game_finished``, whose
    totals are the rolled-back ones."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await _close_the_open_week(game, state_svc)
    await _close_the_open_week(game, state_svc)
    _stored, engine = await _current(state_svc, game["room_code"])
    live_before = engine.host_view()["chain_total_cost"]

    fake_socket_manager.clear()
    await end_game_early(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )

    finished = next(
        data
        for _room_id, event, data in fake_socket_manager.room_emits
        if event == "game_finished"
    )
    host_state = next(
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "host_state" and target == game["host_sid"]
    )
    assert host_state["phase"] == "FINISHED"
    assert host_state["seq"] < finished["seq"]
    assert host_state["chain_total_cost"] == finished["stats"]["chain_total_cost"]
    assert host_state["chain_total_cost"] < live_before
    for role, sid in game["sids_by_role"].items():
        your_state = next(
            data
            for target, event, data in fake_socket_manager.sid_emits
            if event == "your_state" and target == sid
        )
        assert your_state["accumulated_cost"] == (
            finished["stats"]["per_role"][role]["total_cost"]
        )


# --- game_finished ---------------------------------------------------------- #


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


# --- no bot recursion ------------------------------------------------------- #


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


# --- finish_game: saves under the lock, persistence outside it -------------- #


async def test_finish_game_saves_under_the_room_lock_and_persists_outside_it(
    make_room, start_running_game, state_svc, monkeypatch
):
    """Every ``save_room`` ``finish_game`` makes -- the ``game_finished``
    ``seq`` and the ``persisted`` flag -- happens while it holds
    ``state_svc.lock``, and ``persist_finished_game`` runs with the lock
    released. A spy that tracks whether the lock is held is
    timing-independent, unlike a bare ``asyncio.gather`` against
    ``FakeRedis``: gathered coroutines against an in-memory fake run to
    completion one at a time, so neither a lock-free save nor a lock held
    across a slow persistence call would show up by timing alone. The lock is
    not reentrant, so the call is bounded: a re-acquisition would hang."""
    from app.services.game_service import GameService

    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    stored, _engine = await _current(state_svc, game["room_code"])

    held = 0
    acquisitions = 0
    original_lock = state_svc.lock

    @contextlib.asynccontextmanager
    async def spy_lock(room_code: str):
        nonlocal held, acquisitions
        async with original_lock(room_code):
            held += 1
            acquisitions += 1
            try:
                yield
            finally:
                held -= 1

    monkeypatch.setattr(state_svc, "lock", spy_lock)

    saves_held: list[int] = []
    original_save = state_svc.save_room

    async def spy_save(room_code: str, room_doc: dict) -> None:
        saves_held.append(held)
        await original_save(room_code, room_doc)

    monkeypatch.setattr(state_svc, "save_room", spy_save)

    persist_held: list[int] = []

    async def spy_persist(room_code, room_doc, engine, stats):
        persist_held.append(held)
        return "b" * 32

    monkeypatch.setattr(game_service_module, "persist_finished_game", spy_persist)

    await asyncio.wait_for(
        GameService().finish_game(game["room_code"], stored), timeout=5
    )

    assert persist_held == [0]
    assert saves_held == [1, 1]  # game_finished's seq, then the persisted flag
    assert acquisitions == 2
    after, _engine = await _current(state_svc, game["room_code"])
    assert after["state"] == "FINISHED"
    assert after["persisted"] is True


async def test_a_seq_minted_during_persistence_survives_finish_game(
    make_room, start_running_game, state_svc, fake_socket_manager, monkeypatch
):
    """A ``request_state`` that lands while the database write is running
    mints its ``seq`` under the lock and saves it. ``finish_game`` must not
    then overwrite the room with the copy it held before persisting: the
    reply must sit strictly between ``game_finished`` and ``game_persisted``,
    and the stored counter must end at ``game_persisted``'s number, so no two
    events ever share a ``seq``."""
    public_id = "c" * 32
    persist_started = asyncio.Event()
    release_persist = asyncio.Event()

    async def gated_persist(room_code, room_doc, engine, stats):
        persist_started.set()
        await release_persist.wait()
        return public_id

    monkeypatch.setattr(game_service_module, "persist_finished_game", gated_persist)

    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    task = asyncio.create_task(
        end_game_early(
            game["host_sid"],
            {"room_id": game["room_code"], "host_secret": game["host_secret"]},
        )
    )
    try:
        await asyncio.wait_for(persist_started.wait(), timeout=5)
        emitted_before = len(fake_socket_manager.sid_emits)
        await asyncio.wait_for(
            request_state(game["host_sid"], {"room_id": game["room_code"]}),
            timeout=5,
        )
        release_persist.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    reply_seq = next(
        data["seq"]
        for target, event, data in fake_socket_manager.sid_emits[emitted_before:]
        if target == game["host_sid"] and event == "host_state"
    )
    emits = fake_socket_manager.emits_for(game["room_code"])
    finished_seq = next(
        data["seq"] for event, data in emits if event == "game_finished"
    )
    persisted_seq = next(
        data["seq"] for event, data in emits if event == "game_persisted"
    )
    assert finished_seq < reply_seq < persisted_seq

    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["seq"] == persisted_seq
    assert stored["persisted"] is True
    assert stored["state"] == "FINISHED"


# --- a persistence failure -------------------------------------------------- #


async def test_persistence_failure_does_not_break_the_debrief(
    make_room, start_running_game, state_svc, fake_socket_manager, monkeypatch
):
    """A raising ``persist_finished_game`` must not stop
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


async def test_game_persisted_carries_the_public_id_after_a_successful_persist(
    make_room, start_running_game, state_svc, fake_socket_manager, monkeypatch
):
    """Once persistence succeeds, the room hears ``game_persisted`` with the
    game's permanent ``public_id`` -- after ``game_finished``, never before,
    since the permanent results URL does not exist until the write lands."""
    public_id = "a" * 32

    async def succeeding_persist(room_code, room, engine, stats):
        return public_id

    monkeypatch.setattr(
        game_service_module, "persist_finished_game", succeeding_persist
    )

    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    await end_game_early(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )

    stored, _engine = await _current(state_svc, game["room_code"])
    assert stored["persisted"] is True

    emits = fake_socket_manager.emits_for(game["room_code"])
    events = [event for event, _data in emits]
    assert events.index("game_finished") < events.index("game_persisted")
    finished_seq = next(
        data["seq"] for event, data in emits if event == "game_finished"
    )
    persisted = next(data for event, data in emits if event == "game_persisted")
    assert persisted["game_id"] == public_id
    assert persisted["seq"] > finished_seq


async def test_no_game_persisted_when_persistence_fails(
    make_room, start_running_game, state_svc, fake_socket_manager, monkeypatch
):
    """A failed write has no permanent URL to announce."""

    async def failing_persist(room_code, room, engine, stats):
        return None

    monkeypatch.setattr(game_service_module, "persist_finished_game", failing_persist)

    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    await end_game_early(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )

    events = fake_socket_manager.events_for(game["room_code"])
    assert "game_finished" in events
    assert "game_persisted" not in events


async def test_game_finished_is_emitted_before_persistence_even_if_it_hangs(
    make_room, start_running_game, state_svc, fake_socket_manager, monkeypatch
):
    """``game_finished`` must be emitted *before*
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
            "persist_finished_game is invoked -- a version "
            "that emits after persisting would still be waiting here"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
