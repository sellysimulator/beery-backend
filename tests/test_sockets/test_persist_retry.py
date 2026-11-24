"""A failed end-of-game write is retried (spec §10.2).

Three more attempts in the background after the first, never holding the
room lock; and a host or player arriving at a FINISHED room whose game is
still unsaved, with no write in flight, starts the write again. The write is
idempotent on (``room_code``, ``started_at``), so a retry saves the game
exactly once. The delays are zeroed here; their production values are
``PERSIST_RETRY_DELAYS_SECONDS``.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

import app.services.game_service as game_service_module
from app.sockets.handlers.lobby import join, join_waiting
from app.sockets.handlers.play import end_game_early, request_state

pytestmark = pytest.mark.asyncio

PUBLIC_ID = "d" * 32


class FlakyDatabase:
    """Stands in for ``persist_finished_game``: fails the first ``failures``
    calls, then writes idempotently by (room code, ``started_at``), as the
    real unique key does."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.games: dict[tuple[str, str], str] = {}

    async def persist(self, room_code, room, engine, stats):
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError("connection refused")
        key = (room_code, room["started_at"])
        return self.games.setdefault(key, PUBLIC_ID)


async def _drain_retries() -> None:
    while game_service_module._retry_tasks:
        await asyncio.gather(*list(game_service_module._retry_tasks))


async def _finish_with(game, database, monkeypatch) -> None:
    monkeypatch.setattr(game_service_module, "persist_finished_game", database.persist)
    await end_game_early(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )


def _persisted_events(fake_socket_manager, room_code: str) -> list[dict]:
    return [
        data
        for event, data in fake_socket_manager.emits_for(room_code)
        if event == "game_persisted"
    ]


async def test_a_retry_after_a_failure_saves_the_game_once_and_announces_it(
    make_room, start_running_game, state_svc, fake_socket_manager, monkeypatch
):
    monkeypatch.setattr(
        game_service_module, "PERSIST_RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0)
    )
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    database = FlakyDatabase(failures=1)

    await _finish_with(game, database, monkeypatch)
    assert _persisted_events(fake_socket_manager, game["room_code"]) == []
    await _drain_retries()

    assert database.calls == 2
    assert list(database.games.values()) == [PUBLIC_ID]
    [persisted] = _persisted_events(fake_socket_manager, game["room_code"])
    assert persisted["game_id"] == PUBLIC_ID
    stored = await state_svc.get_room(game["room_code"])
    assert stored["persisted"] is True
    assert stored["seq"] == persisted["seq"]
    assert game["room_code"] not in game_service_module._persisting


async def test_the_retries_stop_after_three_and_leave_the_room_unsaved(
    make_room, start_running_game, state_svc, fake_socket_manager, monkeypatch
):
    monkeypatch.setattr(
        game_service_module, "PERSIST_RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0)
    )
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    database = FlakyDatabase(failures=99)

    await _finish_with(game, database, monkeypatch)
    await _drain_retries()

    assert database.calls == 4  # the first attempt and three retries
    assert _persisted_events(fake_socket_manager, game["room_code"]) == []
    stored = await state_svc.get_room(game["room_code"])
    assert stored["persisted"] is False
    assert game["room_code"] not in game_service_module._persisting


async def test_no_retry_holds_the_room_lock_while_writing_or_sleeping(
    make_room, start_running_game, state_svc, monkeypatch
):
    """Every write, the first and each retry, runs with the lock free, and so
    does every sleep between them: a ``request_state`` gets through while a
    retry is waiting."""
    gate = asyncio.Event()

    async def gated_sleep(_delay: float) -> None:
        await gate.wait()

    monkeypatch.setattr(
        game_service_module, "PERSIST_RETRY_DELAYS_SECONDS", (2.0, 10.0, 30.0)
    )
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    held = 0
    original_lock = state_svc.lock

    @contextlib.asynccontextmanager
    async def spy_lock(room_code: str):
        nonlocal held
        async with original_lock(room_code):
            held += 1
            try:
                yield
            finally:
                held -= 1

    monkeypatch.setattr(state_svc, "lock", spy_lock)
    database = FlakyDatabase(failures=1)
    held_at_write: list[int] = []

    async def spy_persist(room_code, room_doc, engine, stats):
        held_at_write.append(held)
        return await database.persist(room_code, room_doc, engine, stats)

    real_sleep = asyncio.sleep
    await _finish_with(game, database, monkeypatch)
    monkeypatch.setattr(game_service_module, "persist_finished_game", spy_persist)
    monkeypatch.setattr(game_service_module.asyncio, "sleep", gated_sleep)
    await real_sleep(0)  # let the retry start, and wait at its first delay

    # The retry is asleep: the room still answers.
    await asyncio.wait_for(
        request_state(game["host_sid"], {"room_id": game["room_code"]}), timeout=5
    )
    gate.set()
    await asyncio.wait_for(_drain_retries(), timeout=5)

    assert held_at_write == [0]
    stored = await state_svc.get_room(game["room_code"])
    assert stored["persisted"] is True


async def _unsaved_finished_game(make_room, start_running_game, monkeypatch):
    """A FINISHED room whose only write failed, with no retries configured."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await _finish_with(game, FlakyDatabase(failures=1), monkeypatch)
    assert game["room_code"] not in game_service_module._persisting
    return game


async def test_the_host_reclaiming_an_unsaved_finished_room_saves_it(
    make_room, start_running_game, state_svc, fake_socket_manager, monkeypatch
):
    game = await _unsaved_finished_game(make_room, start_running_game, monkeypatch)
    database = FlakyDatabase(failures=0)
    monkeypatch.setattr(game_service_module, "persist_finished_game", database.persist)

    await join_waiting(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )

    assert database.calls == 1
    [persisted] = _persisted_events(fake_socket_manager, game["room_code"])
    assert persisted["game_id"] == PUBLIC_ID
    assert (await state_svc.get_room(game["room_code"]))["persisted"] is True


async def test_a_player_rejoining_an_unsaved_finished_room_saves_it(
    make_room, start_running_game, state_svc, fake_socket_manager, monkeypatch
):
    game = await _unsaved_finished_game(make_room, start_running_game, monkeypatch)
    database = FlakyDatabase(failures=0)
    monkeypatch.setattr(game_service_module, "persist_finished_game", database.persist)

    await join(game["sids_by_role"]["RETAILER"], {"room_id": game["room_code"]})

    assert database.calls == 1
    assert len(_persisted_events(fake_socket_manager, game["room_code"])) == 1


async def test_request_state_in_an_unsaved_finished_room_saves_it(
    make_room, start_running_game, state_svc, fake_socket_manager, monkeypatch
):
    game = await _unsaved_finished_game(make_room, start_running_game, monkeypatch)
    database = FlakyDatabase(failures=0)
    monkeypatch.setattr(game_service_module, "persist_finished_game", database.persist)

    await request_state(game["sids_by_role"]["FACTORY"], {"room_id": game["room_code"]})

    assert database.calls == 1
    assert len(_persisted_events(fake_socket_manager, game["room_code"])) == 1


async def test_a_rejoin_starts_no_second_write_beside_one_in_flight(
    make_room, start_running_game, fake_socket_manager, monkeypatch
):
    game = await _unsaved_finished_game(make_room, start_running_game, monkeypatch)
    database = FlakyDatabase(failures=0)
    monkeypatch.setattr(game_service_module, "persist_finished_game", database.persist)
    game_service_module._persisting.add(game["room_code"])  # a retry is waiting

    await join_waiting(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )
    await request_state(game["host_sid"], {"room_id": game["room_code"]})

    assert database.calls == 0


async def test_a_rejoin_of_a_saved_room_writes_nothing(
    make_room, start_running_game, monkeypatch
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    database = FlakyDatabase(failures=0)
    await _finish_with(game, database, monkeypatch)
    assert database.calls == 1

    await join_waiting(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )
    await join(game["sids_by_role"]["RETAILER"], {"room_id": game["room_code"]})

    assert database.calls == 1


# --- the in-flight guard's two edges -------------------------------------------


async def test_the_game_is_in_flight_from_before_its_first_write(
    make_room, start_running_game, monkeypatch
):
    """A rejoin while the first write is still running starts no second one."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    database = FlakyDatabase(failures=0)
    writing = asyncio.Event()
    release = asyncio.Event()

    async def slow_persist(room_code, room_doc, engine, stats):
        writing.set()
        await release.wait()
        return await database.persist(room_code, room_doc, engine, stats)

    monkeypatch.setattr(game_service_module, "persist_finished_game", slow_persist)
    ending = asyncio.create_task(
        end_game_early(
            game["host_sid"],
            {"room_id": game["room_code"], "host_secret": game["host_secret"]},
        )
    )
    await asyncio.wait_for(writing.wait(), timeout=5)
    assert game["room_code"] in game_service_module._persisting

    await join(game["sids_by_role"]["RETAILER"], {"room_id": game["room_code"]})
    release.set()
    await asyncio.wait_for(ending, timeout=5)

    assert database.calls == 1


async def test_the_game_leaves_the_in_flight_set_before_it_is_announced_saved(
    make_room, start_running_game, fake_socket_manager, monkeypatch
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    in_flight_at_announcement: list[bool] = []
    record = fake_socket_manager.emit_to_room

    async def emit_to_room(room_id, event, data):
        if event == "game_persisted":
            in_flight_at_announcement.append(room_id in game_service_module._persisting)
        await record(room_id, event, data)

    fake_socket_manager.emit_to_room = emit_to_room

    await _finish_with(game, FlakyDatabase(failures=0), monkeypatch)

    assert in_flight_at_announcement == [False]


async def test_the_game_leaves_the_in_flight_set_before_giving_up_is_announced(
    make_room, start_running_game, monkeypatch
):
    """After the last failed retry a rejoin must be able to start a new
    attempt at once, not be turned away as a duplicate while the failure is
    still being reported."""
    monkeypatch.setattr(game_service_module, "PERSIST_RETRY_DELAYS_SECONDS", (0.0,))
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    in_flight_at_give_up: list[bool] = []
    log_error = game_service_module.logger.error

    def spy_error(message, *args, **kwargs):
        if message.startswith("Gave up"):
            in_flight_at_give_up.append(
                game["room_code"] in game_service_module._persisting
            )
        log_error(message, *args, **kwargs)

    monkeypatch.setattr(game_service_module.logger, "error", spy_error)

    await _finish_with(game, FlakyDatabase(failures=99), monkeypatch)
    await _drain_retries()

    assert in_flight_at_give_up == [False]
