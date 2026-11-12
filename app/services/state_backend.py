"""The room-state backend, and the switch between Redis and process memory.

`00-decisions.md D4` makes the room document authoritative and writes MySQL
only at the end of a game. It does not say *where* that document lives, and
for a single-instance deployment it does not need to be Redis: the state
service uses six operations, all of which a dict implements exactly.

`REDIS_ENABLED` is that switch, and it is **off by default**. With it off the
room documents live in this process and the app has no Redis dependency at
all. With it on, `REDIS_URL` must be set and the behaviour is unchanged from
the original build.

**The two backends are deliberately identical in semantics, not merely
similar.** Both store JSON strings, both require an explicit `save_room`, both
expire on the same TTL. It is tempting to have the in-memory backend hold live
`GameEngine` objects and skip serialisation -- worth about 3.4 ms of CPU per
handler at week 36 -- and it must not, because then a handler's mutations
would take effect without saving, which works here and breaks the moment
anyone sets `REDIS_ENABLED=true`. The switch is a deployment choice and never
a behavioural one.

**The constraint the switch carries:** with the in-memory backend, this
process must be the only one serving the app. Two instances means two players
in one room land on different processes and never see each other -- no error,
no log, just a lobby that will not fill. Nothing inside a process can detect
that, so `app/core/checks/` logs it at startup and `README.md` states it.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from typing import Protocol, Self

__all__ = [
    "MAX_ROOMS",
    "InMemoryBackend",
    "StateBackend",
    "StoreFullError",
    "build_state_backend",
]

#: Every key the in-memory backend holds is a room document. Past this many
#: live rooms a new one is refused: room creation is unauthenticated (D3), and
#: without a ceiling it grows the process until the platform kills it.
#: A lobby is about 2.6 KB, a finished 36-week game about 77 KB.
MAX_ROOMS = 500
#: Expired rooms are swept from the write path at most this often.
PURGE_INTERVAL_SECONDS = 60.0


class StoreFullError(RuntimeError):
    """The in-memory backend already holds `MAX_ROOMS` live rooms."""


class StateBackend(Protocol):
    """The six operations `StateService` uses, and nothing else.

    Deliberately a subset of `redis.asyncio.Redis` so the real client
    satisfies it without an adapter.
    """

    async def get(self, key: str) -> str | bytes | None: ...
    async def setex(self, key: str, ttl: int, value: str) -> object: ...
    async def delete(self, *keys: str) -> int: ...
    async def exists(self, key: str) -> int: ...
    def lock(self, key: str, timeout: int = 10) -> object: ...


class _MemoryLock:
    """An `asyncio.Lock` wearing `redis.asyncio.Lock`'s context-manager shape.

    `timeout` is accepted and ignored. On Redis it is a deadlock escape
    hatch for a lock whose owner died mid-operation; within one event loop
    an owner cannot die mid-operation without unwinding through this
    `finally`, so there is nothing for it to rescue. Accepting and ignoring
    it keeps the 25 `async with state_svc.lock(...)` call sites identical
    across both backends.
    """

    def __init__(self, lock: asyncio.Lock, timeout: int) -> None:
        self._lock = lock
        self.timeout = timeout

    async def __aenter__(self) -> Self:
        await self._lock.acquire()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


class InMemoryBackend:
    """Room documents in this process's memory, with lazy TTL expiry.

    Expiry is checked on read rather than swept by a background task: a
    sweeper is a task lifecycle to own and a source of surprise in tests.
    Instead, writing a *new* key runs `purge_expired()` when the last sweep
    is `PURGE_INTERVAL_SECONDS` old or the store is at `MAX_ROOMS`, and a
    new key past `MAX_ROOMS` live rooms raises `StoreFullError`. Rewriting
    an existing room is never refused.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[float | None, str]] = {}
        # Weak values: a lock lives exactly as long as some `_MemoryLock`
        # (a holder or a waiter) references it, then drops out on its own.
        # A plain dict kept one entry per key ever locked -- including any
        # string a client sent as a room id -- for the life of the process.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._purged_at = float("-inf")

    def _live(self, key: str) -> str | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at is not None and expires_at <= time.monotonic():
            del self._store[key]
            return None
        return value

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        if key not in self._store:
            now = time.monotonic()
            due = now - self._purged_at >= PURGE_INTERVAL_SECONDS
            if due or len(self._store) >= MAX_ROOMS:
                self.purge_expired()
            if len(self._store) >= MAX_ROOMS:
                raise StoreFullError(f"{MAX_ROOMS} live rooms already held.")
        self._store[key] = (time.monotonic() + ttl if ttl else None, value)
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._store.pop(key, None) is not None:
                removed += 1
        return removed

    async def exists(self, key: str) -> int:
        return 1 if self._live(key) is not None else 0

    def lock(self, key: str, timeout: int = 10) -> _MemoryLock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return _MemoryLock(lock, timeout)

    def purge_expired(self) -> int:
        """Drop every expired entry. Returns how many went."""
        now = time.monotonic()
        self._purged_at = now
        dead = [
            key
            for key, (expires_at, _) in self._store.items()
            if expires_at is not None and expires_at <= now
        ]
        for key in dead:
            del self._store[key]
        return len(dead)


def build_state_backend(enabled: bool, url: str) -> StateBackend:
    """Return the Redis client when `enabled`, else an `InMemoryBackend`.

    Raises `ValueError` when Redis is switched on with no URL, rather than
    letting `from_url("")` fail with a message about URL schemes that says
    nothing about which setting is wrong.
    """
    if not enabled:
        return InMemoryBackend()
    if not url:
        raise ValueError(
            "REDIS_ENABLED is true but REDIS_URL is empty. Set REDIS_URL, or "
            "set REDIS_ENABLED=false to keep room state in this process."
        )
    import redis.asyncio as aioredis

    return aioredis.from_url(url)  # type: ignore[no-any-return]
