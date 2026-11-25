"""The room's turn: its events leave in ``seq`` order, after the lock (spec §11.2)."""

from __future__ import annotations

import asyncio

import pytest

from app.services import room_turn, state_service
from app.services.room_turn import Emit

CODE = "ABC234"
OTHER = "DEF567"
SID = "sid-1"

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("fake_redis")]


def to_room(seq: int, code: str = CODE) -> Emit:
    return Emit(code, "tick", {"seq": seq}, to_room=True)


def sent(sockets) -> list[int]:
    return [data["seq"] for _, _, data in sockets.emits]


async def test_routes_sid_and_room_events_in_order(fake_socket_manager):
    async with room_turn.room_turn(CODE) as emits:
        emits.append(Emit(SID, "config_updated", {"seq": 1}))
        emits.append(Emit(CODE, "lobby_update", {"seq": 2}, to_room=True))
        emits.append(Emit(SID, "host_state", {"seq": 3}))
    assert fake_socket_manager.emits == [
        (SID, "config_updated", {"seq": 1}),
        (CODE, "lobby_update", {"seq": 2}),
        (SID, "host_state", {"seq": 3}),
    ]
    assert fake_socket_manager.room_emits == [(CODE, "lobby_update", {"seq": 2})]


async def test_the_lock_is_free_while_the_events_are_sent(
    fake_socket_manager, monkeypatch
):
    async def under_the_lock(*args):
        async with asyncio.timeout(1):
            async with state_service.get_state_service().lock(CODE):
                fake_socket_manager.emits.append((CODE, "free", {"seq": 1}))

    monkeypatch.setattr(fake_socket_manager, "emit_to_room", under_the_lock)
    async with room_turn.room_turn(CODE) as emits:
        emits.append(to_room(1))
    assert sent(fake_socket_manager) == [1]


async def test_a_turn_taken_while_another_sends_waits_its_place(
    fake_socket_manager, monkeypatch
):
    """Fails when each turn sends its own events: the first one is slow."""
    real = fake_socket_manager.emit_to_room

    async def slow_first(code, event, data):
        await asyncio.sleep(0.02 if data["seq"] == 1 else 0)
        await real(code, event, data)

    monkeypatch.setattr(fake_socket_manager, "emit_to_room", slow_first)

    async def turn(*seqs: int) -> None:
        async with room_turn.room_turn(CODE) as emits:
            emits.extend(to_room(seq) for seq in seqs)

    await asyncio.gather(turn(1, 2), turn(3), turn(4, 5))
    assert sent(fake_socket_manager) == [1, 2, 3, 4, 5]


async def test_one_room_never_waits_for_another(fake_socket_manager, monkeypatch):
    release = asyncio.Event()
    real = fake_socket_manager.emit_to_room

    async def stuck(code, event, data):
        if code == CODE:
            await release.wait()
        await real(code, event, data)

    monkeypatch.setattr(fake_socket_manager, "emit_to_room", stuck)

    async def turn(code: str, seq: int) -> None:
        async with room_turn.room_turn(code) as emits:
            emits.append(to_room(seq, code))

    waiting = asyncio.create_task(turn(CODE, 1))
    await asyncio.sleep(0)
    async with asyncio.timeout(1):
        await turn(OTHER, 7)
    assert sent(fake_socket_manager) == [7]
    release.set()
    await waiting
    assert sent(fake_socket_manager) == [7, 1]


async def test_a_turn_taken_inside_a_send_does_not_wait_for_it(
    fake_socket_manager, monkeypatch
):
    """Socket.IO runs ``disconnect`` inside a send to a connection it finds dead. Fails
    (times out) when a turn waits for the sends ahead of it, and when it sends under
    the lock."""
    real = fake_socket_manager.emit_to_room

    async def finds_a_dead_one(code, event, data):
        if data["seq"] == 1:
            async with room_turn.room_turn(CODE) as emits:
                emits.append(to_room(3))
        await real(code, event, data)

    monkeypatch.setattr(fake_socket_manager, "emit_to_room", finds_a_dead_one)
    async with asyncio.timeout(1):
        async with room_turn.room_turn(CODE) as emits:
            emits.extend([to_room(1), to_room(2)])
    assert sent(fake_socket_manager) == [1, 2, 3]


async def test_a_failed_send_does_not_stop_the_rest(fake_socket_manager, monkeypatch):
    real = fake_socket_manager.emit_to_room

    async def fails_once(code, event, data):
        if data["seq"] == 1:
            raise ConnectionError
        await real(code, event, data)

    monkeypatch.setattr(fake_socket_manager, "emit_to_room", fails_once)
    async with room_turn.room_turn(CODE) as emits:
        emits.extend([to_room(1), to_room(2)])
    assert sent(fake_socket_manager) == [2]


async def test_a_block_that_raises_sends_nothing(fake_socket_manager):
    with pytest.raises(ValueError):
        async with room_turn.room_turn(CODE) as emits:
            emits.append(to_room(1))
            raise ValueError
    async with room_turn.room_turn(CODE) as emits:
        emits.append(to_room(2))
    assert sent(fake_socket_manager) == [2]


async def test_an_early_return_sends_what_was_collected(fake_socket_manager):
    async def refuse() -> None:
        async with room_turn.room_turn(CODE) as emits:
            emits.append(to_room(1))
            return

    await refuse()
    assert sent(fake_socket_manager) == [1]


async def test_a_code_in_lower_case_is_the_same_room(fake_socket_manager, monkeypatch):
    """The lock ignores case (REST paths keep it), so the queue must too."""
    real = fake_socket_manager.emit_to_room

    async def slow_first(code, event, data):
        await asyncio.sleep(0.02 if data["seq"] == 1 else 0)
        await real(code, event, data)

    monkeypatch.setattr(fake_socket_manager, "emit_to_room", slow_first)

    async def turn(code: str, seq: int) -> None:
        async with room_turn.room_turn(code) as emits:
            emits.append(to_room(seq))

    await asyncio.gather(turn(CODE, 1), turn(CODE.lower(), 2))
    assert sent(fake_socket_manager) == [1, 2]
