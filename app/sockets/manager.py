"""Socket.IO server setup and the per-process connection manager."""

import logging
from typing import Any

from socketio import (  # type: ignore[import-untyped]
    AsyncRedisManager,
    AsyncServer,
)

from ..config import settings
from ..core.abuse import MAX_BODY_BYTES

logger = logging.getLogger(__name__)

sio = AsyncServer(
    async_mode="asgi",
    # `None` selects Socket.IO's in-process manager. Guard on the setting,
    # never on a try/except around the constructor: `AsyncRedisManager("")`
    # constructs happily and only fails later, at connect time, on the hot
    # path. Cross-instance fan-out is the one thing the in-memory state
    # backend cannot replace, so this and `REDIS_ENABLED` move together.
    client_manager=(
        AsyncRedisManager(settings.REDIS_URL) if settings.REDIS_ENABLED else None
    ),
    cors_allowed_origins=settings.CORS_ORIGINS,
    # Never hardcoded to True: engine.io logs every packet's full payload at
    # INFO, which is CPU on the hot path and writes wire data into the logs.
    logger=settings.DEBUG,
    engineio_logger=settings.DEBUG,
    ping_timeout=120,
    ping_interval=25,
    # The largest legitimate client message is a whole config patch with a
    # 104-week CUSTOM demand series, a few KB. 5 MB per message was 5 MB of
    # parsing any anonymous socket could ask for. The same cap as a REST body.
    max_http_buffer_size=MAX_BODY_BYTES,
)


class SocketManager:
    """Tracks what each connected sid is, and fans events out to sids or rooms."""

    def __init__(self) -> None:
        self.sid_to_room: dict[str, str] = {}
        # Per-process bookkeeping of the alias a sid last joined as. The
        # authoritative mapping lives in the room document in Redis.
        self.sid_to_alias: dict[str, str] = {}
        # Verified identity (Firebase uid or guest_<uuid4>), established once at
        # the handshake and never afterwards accepted from the client.
        self.sid_to_identity: dict[str, str] = {}
        # The Firebase uid of a sid whose handshake presented an ID token that
        # verified -- and only those. Never inferred from `sid_to_identity`
        # (a missing `guest_` prefix proves nothing): `start_game` records the
        # host's account from here (spec D23), so a guest has no entry.
        self.sid_to_verified_uid: dict[str, str] = {}

    async def connect(self, sid: str, environ: dict) -> None:
        logger.info("Client connected: %s", sid)

    async def disconnect(self, sid: str) -> None:
        self.sid_to_room.pop(sid, None)
        self.sid_to_alias.pop(sid, None)
        self.sid_to_identity.pop(sid, None)
        self.sid_to_verified_uid.pop(sid, None)
        logger.info("Client disconnected: %s", sid)

    async def join_room(self, sid: str, room_id: str, alias: str) -> None:
        self.sid_to_room[sid] = room_id
        self.sid_to_alias[sid] = alias
        await sio.enter_room(sid, room_id)

    async def leave_room(self, sid: str, room_id: str) -> None:
        await sio.leave_room(sid, room_id)
        self.sid_to_room.pop(sid, None)

    async def emit_to_room(self, room_id: str, event: str, data: Any) -> None:
        """Broadcast to a room. Carries only what every participant may see."""
        try:
            await sio.emit(event, data, room=room_id)
        except Exception:
            logger.exception("Failed to emit %r to room %s.", event, room_id)

    async def emit_to_sid(self, sid: str, event: str, data: Any) -> None:
        """Send to one connection. Everything private goes out this way."""
        try:
            await sio.emit(event, data, room=sid)
        except Exception:
            logger.exception("Failed to emit %r to sid %s.", event, sid)


socket_manager = SocketManager()
