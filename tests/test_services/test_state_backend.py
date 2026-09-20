"""The Redis on/off switch (`app/services/state_backend.py`).

The two backends must be interchangeable in *semantics*, not merely both
present: the switch is a deployment choice and never a behavioural one. A
difference here would be a bug that exists on one deployment and not the
other, which is the worst shape a bug can have.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.state_backend import InMemoryBackend, build_state_backend

pytestmark = pytest.mark.asyncio


async def test_round_trips_a_value() -> None:
    backend = InMemoryBackend()
    await backend.setex("room:AAA", 60, '{"state": "LOBBY"}')
    assert await backend.get("room:AAA") == '{"state": "LOBBY"}'
    assert await backend.exists("room:AAA") == 1


async def test_a_missing_key_is_none_not_an_error() -> None:
    backend = InMemoryBackend()
    assert await backend.get("room:NOPE") is None
    assert await backend.exists("room:NOPE") == 0


async def test_delete_reports_how_many_went() -> None:
    backend = InMemoryBackend()
    await backend.setex("a", 60, "1")
    await backend.setex("b", 60, "2")
    assert await backend.delete("a", "b", "c") == 2
    assert await backend.get("a") is None


async def test_an_expired_key_reads_as_absent(monkeypatch) -> None:
    """Lazy expiry: checked on read, so no sweeper task to own."""
    backend = InMemoryBackend()
    clock = [1_000.0]
    monkeypatch.setattr("app.services.state_backend.time.monotonic", lambda: clock[0])
    await backend.setex("room:AAA", 60, "{}")
    clock[0] += 59
    assert await backend.get("room:AAA") == "{}"
    clock[0] += 2
    assert await backend.get("room:AAA") is None
    assert await backend.exists("room:AAA") == 0


async def test_purge_expired_reclaims_the_memory(monkeypatch) -> None:
    backend = InMemoryBackend()
    clock = [1_000.0]
    monkeypatch.setattr("app.services.state_backend.time.monotonic", lambda: clock[0])
    await backend.setex("old", 10, "{}")
    await backend.setex("new", 600, "{}")
    clock[0] += 60
    assert backend.purge_expired() == 1
    assert await backend.get("new") == "{}"


async def test_the_lock_actually_serialises_two_coroutines() -> None:
    """Forces a real suspension inside the critical section.

    An uncontended `asyncio.Lock` does not suspend, so a naive version of
    this test passes with no lock at all -- the trap recorded at gate 8.
    The `sleep(0)` is what makes the interleaving reachable.
    """
    backend = InMemoryBackend()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with backend.lock("room:AAA"):
            order.append(f"{name}-in")
            await asyncio.sleep(0)
            order.append(f"{name}-out")

    await asyncio.gather(worker("a"), worker("b"))
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    ), f"the two critical sections interleaved: {order}"


async def test_two_rooms_do_not_block_each_other() -> None:
    backend = InMemoryBackend()
    async with backend.lock("room:AAA"), backend.lock("room:BBB"):
        pass


async def test_the_switch_defaults_to_memory() -> None:
    assert isinstance(build_state_backend(False, ""), InMemoryBackend)
    assert isinstance(build_state_backend(False, "redis://x:6379/0"), InMemoryBackend)


async def test_redis_on_with_no_url_fails_loudly() -> None:
    """Not with `from_url("")`'s message about URL schemes, which names no
    setting and sends the reader to the wrong place."""
    with pytest.raises(ValueError, match="REDIS_ENABLED"):
        build_state_backend(True, "")


async def test_redis_on_with_a_url_builds_a_redis_client() -> None:
    backend = build_state_backend(True, "redis://127.0.0.1:16379/0")
    assert not isinstance(backend, InMemoryBackend)
    for name in ("get", "setex", "delete", "exists", "lock"):
        assert hasattr(backend, name), f"the real client lacks {name}"
