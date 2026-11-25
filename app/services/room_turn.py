"""A turn on a room: its lock, and its events sent in the order of their ``seq``
(spec §11.2). Every game event goes out through here."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from ..sockets.manager import socket_manager
from .state_service import get_state_service

logger = logging.getLogger(__name__)

__all__ = ["Emit", "flush", "queue", "room_turn"]


@dataclass(frozen=True)
class Emit:
    target: str  # a room code when ``to_room``, otherwise a sid
    event: str
    data: dict
    to_room: bool = field(default=False, kw_only=True)


# In process memory, as the in-memory rooms are: with several instances the order
# would have to be kept in Redis too.
_queues: dict[str, deque[Emit]] = {}
_sending: set[str] = set()


@asynccontextmanager
async def room_turn(room_code: str) -> AsyncIterator[list[Emit]]:
    """Hold the room's lock, then send the events put in the list.

    They are queued before the lock is released, so the queue is in the order of
    their ``seq``, and sent after it, never under it. A block that raises sends
    nothing.
    """
    emits: list[Emit] = []
    try:
        async with get_state_service().lock(room_code):
            yield emits
            queue(room_code, emits)
    finally:
        await flush(room_code)


def queue(room_code: str, emits: Iterable[Emit]) -> None:
    """Add the events to the room's queue; call it while holding the room's lock."""
    _queues.setdefault(room_code.upper(), deque()).extend(emits)


async def flush(room_code: str) -> None:
    """Send the room's queued events, one at a time. Never call it under the lock.

    A room already being sent to takes the new events in its stride, and this returns
    at once. It must not wait for them: Socket.IO runs ``disconnect`` inside a send to
    a connection it finds dead, and that send would wait for itself.
    """
    key = room_code.upper()
    if key in _sending:
        return
    _sending.add(key)
    try:
        while pending := _queues.get(key):
            emit = pending.popleft()
            try:
                await _deliver(emit)
            except Exception:
                logger.exception("Room %s: could not send %s.", key, emit.event)
    finally:
        _sending.discard(key)
        if not _queues.get(key):
            _queues.pop(key, None)


async def _deliver(emit: Emit) -> None:
    if emit.to_room:
        await socket_manager.emit_to_room(emit.target, emit.event, emit.data)
    else:
        await socket_manager.emit_to_sid(emit.target, emit.event, emit.data)
