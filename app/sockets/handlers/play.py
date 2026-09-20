"""The play loop's socket handlers (`12-socket-play.md`).

Registers by the ``@sio.event`` decorator firing at import (**D19**): this
module is discovered by ``app/sockets/handlers/__init__.py`` and needs no
edit anywhere upstream.

Every read-modify-write of room state is wrapped in
``async with state_svc.lock(room_code)`` (``00-conventions.md §3``), and,
following the shape ``lobby.py`` already ships and this section's own
locking contract (``§2.1``): payloads are built from values captured while
the lock is held, the block exits, and only then are they emitted -- and
only then does control pass into another ``GameService`` method. The one
exception, matching ``lobby.py``'s own style, is a same-call rejection
(``error``/``join_error``): those are cheap, terminal and emitted
immediately, under the lock, exactly as ``lobby.py``'s ``_reject`` does.

Game rules live in ``app.core``; this module and ``app.services.game_service``
only orchestrate sockets around them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ...core.bot import bot_for
from ...core.enums import Role, RoomState
from ...core.game_engine import EngineStateError, GamePhase
from ...services.game_service import get_game_service
from ...services.room_service import RoomService
from ...services.state_service import get_state_service
from ..errors import guarded
from ..manager import sio, socket_manager

logger = logging.getLogger(__name__)

_room_service = RoomService()

_INVALID_HOST_SECRET = "Invalid host secret."
_ROOM_NOT_FOUND = "Room does not exist."
_GAME_PAUSED_MSG = "The game is paused."
_GAME_NOT_RUNNING_MSG = "The game is not running."
_UNKNOWN_ROLE = "Unknown role."
_ROLE_UNFILLED = "That role is unfilled."
_GAME_NOT_STARTED = "The game has not started."

__all__ = ["on_participant_disconnected"]


def _payload(data: Any) -> dict:
    return data if isinstance(data, dict) else {}


def _room_id_of(data: dict) -> str | None:
    room_id = data.get("room_id")
    return room_id if isinstance(room_id, str) and room_id else None


async def _emit_error(sid: str, message: str, code: str) -> None:
    await socket_manager.emit_to_sid(sid, "error", {"message": message, "code": code})


# --- submit_order ------------------------------------------------------------- #


@sio.event
@guarded("error")
async def submit_order(sid: str, data: dict | None = None) -> None:
    """``§3.1``. All the behaviour lives in ``GameService.submit`` -- this
    handler only unpacks the wire payload."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        await _emit_error(sid, _ROOM_NOT_FOUND, "NOT_IN_ROOM")
        return
    await get_game_service().submit(room_id, sid, data.get("week"), data.get("order"))


# --- pause_game / resume_game ------------------------------------------------- #


@sio.event
@guarded("error")
async def pause_game(sid: str, data: dict | None = None) -> None:
    """``§3.6``. ``RUNNING`` -> ``PAUSED``. No-op if not currently running."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        return

    state_svc = get_state_service()
    room_code = room_id
    paused_payload: dict | None = None

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            return
        room_code = room["room_code"]

        if not _room_service.check_host_secret(room, data):
            await _emit_error(sid, _INVALID_HOST_SECRET, "INVALID_HOST_SECRET")
            return
        if room["state"] != RoomState.RUNNING.value:
            return

        room["state"] = RoomState.PAUSED.value
        room["paused_reason"] = "The host paused the game."
        paused_payload = {
            "seq": state_svc.next_seq(room),
            "reason": room["paused_reason"],
        }
        await state_svc.save_room(room_code, room)

    if paused_payload is not None:
        await socket_manager.emit_to_room(room_code, "game_paused", paused_payload)


@sio.event
@guarded("error")
async def resume_game(sid: str, data: dict | None = None) -> None:
    """``§3.6``. ``PAUSED`` -> ``RUNNING``, then a full state re-broadcast."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        return

    state_svc = get_state_service()
    room_code = room_id
    resumed_payload: dict | None = None
    room_snapshot: dict | None = None

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            return
        room_code = room["room_code"]

        if not _room_service.check_host_secret(room, data):
            await _emit_error(sid, _INVALID_HOST_SECRET, "INVALID_HOST_SECRET")
            return
        if room["state"] != RoomState.PAUSED.value:
            return

        room["state"] = RoomState.RUNNING.value
        room["paused_reason"] = None
        resumed_payload = {"seq": state_svc.next_seq(room)}
        await state_svc.save_room(room_code, room)
        room_snapshot = room

    if resumed_payload is not None:
        await socket_manager.emit_to_room(room_code, "game_resumed", resumed_payload)
    if room_snapshot is not None:
        await get_game_service().broadcast_state(room_code)


# --- force_close_week --------------------------------------------------------- #


@sio.event
@guarded("error")
async def force_close_week(sid: str, data: dict | None = None) -> None:
    """``§3.6``. Delegates the actual close to ``GameService`` with no lock
    held, exactly as a human submission does."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        return

    state_svc = get_state_service()
    room_code = room_id
    authorised = False

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            return
        room_code = room["room_code"]

        if not _room_service.check_host_secret(room, data):
            await _emit_error(sid, _INVALID_HOST_SECRET, "INVALID_HOST_SECRET")
            return
        if room["state"] == RoomState.PAUSED.value:
            await _emit_error(sid, _GAME_PAUSED_MSG, "GAME_PAUSED")
            return
        if room["state"] != RoomState.RUNNING.value:
            await _emit_error(sid, _GAME_NOT_RUNNING_MSG, "GAME_NOT_RUNNING")
            return
        authorised = True

    if authorised:
        await get_game_service().close_week_if_ready(room_code, force=True)


# --- substitute_bot ------------------------------------------------------------ #


@sio.event
@guarded("error")
async def substitute_bot(sid: str, data: dict | None = None) -> None:
    """``§3.6``. A no-op, not an error, when the role is already a bot.
    ``engine.set_bot(role)`` is what makes every subsequent ``WeekRecord``
    attribute the play correctly -- the participant flag alone is not
    enough (module docstring, `07-game-engine.md`'s ``set_bot``)."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        return
    role_raw = data.get("role")

    state_svc = get_state_service()
    room_code = room_id
    bot_substituted_payload: dict | None = None
    needs_bot_pass = False

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            return
        room_code = room["room_code"]

        if not _room_service.check_host_secret(room, data):
            await _emit_error(sid, _INVALID_HOST_SECRET, "INVALID_HOST_SECRET")
            return

        try:
            role = Role(role_raw)
        except ValueError:
            await _emit_error(sid, _UNKNOWN_ROLE, "UNKNOWN_ROLE")
            return

        alias = room["role_to_alias"].get(role.value)
        participant = room["participants"].get(alias) if alias else None
        if participant is None:
            await _emit_error(sid, _ROLE_UNFILLED, "UNKNOWN_ROLE")
            return
        if participant.get("is_bot"):
            return  # already a bot -- no-op (§3.6)

        engine = state_svc.load_engine(room)
        if engine is None:
            await _emit_error(sid, _GAME_NOT_STARTED, "GAME_NOT_RUNNING")
            return

        config = state_svc.load_config(room)
        bots = state_svc.load_bots(room)
        bots[role] = bot_for(role, config)
        state_svc.store_bots(room, bots)

        participant["is_bot"] = True
        engine.set_bot(role)
        state_svc.store_engine(room, engine)

        if engine.phase is GamePhase.DECISION and role not in engine.pending_orders:
            needs_bot_pass = True

        bot_substituted_payload = {
            "seq": state_svc.next_seq(room),
            "role": role.value,
            "display_name": participant.get("display_name"),
        }
        await state_svc.save_room(room_code, room)

    if bot_substituted_payload is not None:
        await socket_manager.emit_to_room(
            room_code, "bot_substituted", bot_substituted_payload
        )
    if needs_bot_pass:
        await get_game_service().run_bot_decisions(room_code)


# --- end_game_early ------------------------------------------------------------ #


@sio.event
@guarded("error")
async def end_game_early(sid: str, data: dict | None = None) -> None:
    """``§3.6``. Abandons the open week and finishes with the weeks
    actually played -- ``engine.end_early()`` never settles it."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        return

    state_svc = get_state_service()
    room_code = room_id
    room_snapshot: dict | None = None

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            return
        room_code = room["room_code"]

        if not _room_service.check_host_secret(room, data):
            await _emit_error(sid, _INVALID_HOST_SECRET, "INVALID_HOST_SECRET")
            return
        if room["state"] not in (RoomState.RUNNING.value, RoomState.PAUSED.value):
            await _emit_error(sid, _GAME_NOT_RUNNING_MSG, "GAME_NOT_RUNNING")
            return

        engine = state_svc.load_engine(room)
        if engine is None:
            await _emit_error(sid, _GAME_NOT_STARTED, "GAME_NOT_RUNNING")
            return

        try:
            engine.end_early()
        except EngineStateError:
            return

        state_svc.store_engine(room, engine)
        room["state"] = RoomState.FINISHED.value
        room["finished_at"] = datetime.now(timezone.utc).isoformat()
        await state_svc.save_room(room_code, room)
        room_snapshot = room

    if room_snapshot is not None:
        await get_game_service().finish_game(room_code, room_snapshot)


# --- request_state -------------------------------------------------------------- #


@sio.event
@guarded("error")
async def request_state(sid: str, data: dict | None = None) -> None:
    """``§3.7``. Purely a read: mutates nothing, acquires no lock."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        return

    state_svc = get_state_service()
    room = await state_svc.get_room(room_id)
    if room is None:
        return

    engine = state_svc.load_engine(room)
    if engine is None:
        return

    if sid == room.get("host_sid"):
        await socket_manager.emit_to_sid(
            sid, "host_state", {"seq": room["seq"], **engine.host_view()}
        )
        return

    alias = room["sid_to_alias"].get(sid)
    participant = room["participants"].get(alias) if alias else None
    if participant is None or participant.get("role") is None:
        return

    role = Role(participant["role"])
    await socket_manager.emit_to_sid(
        sid, "your_state", {"seq": room["seq"], **engine.player_view(role)}
    )


# --- disconnection -------------------------------------------------------------- #


async def on_participant_disconnected(sid: str) -> None:
    """**D7** (``§3.5``). Section 02's ``connection.py`` calls this BEFORE
    ``socket_manager.disconnect(sid)`` -- afterwards, ``sid_to_room`` and
    ``sid_to_alias`` have already been popped and there is no room code
    left to look this participant up by.

    Never lets an infrastructure problem (Redis unreachable, a malformed
    room document) escape: this runs on every disconnect, including a sid
    that never joined a game room at all, and the identity-map cleanup in
    ``socket_manager.disconnect(sid)`` that follows it in ``connection.py``
    must always still run. A logged, swallowed exception here is the
    correct failure mode -- the same one ``SocketManager.emit_to_room`` and
    ``emit_to_sid`` already use for the same reason (``app/sockets/manager.py``).
    """
    try:
        await _disconnect_from_room(sid)
    except Exception:
        logger.exception("on_participant_disconnected failed for sid %s.", sid)


async def _disconnect_from_room(sid: str) -> None:
    """The real behaviour of `§3.5`, split out so `on_participant_disconnected`
    can wrap it in one broad, logged try/except."""
    room_code = socket_manager.sid_to_room.get(sid)
    if room_code is None:
        candidates = [name for name in sio.rooms(sid) if name != sid]
        room_code = candidates[0] if candidates else None
    if room_code is None:
        return

    state_svc = get_state_service()
    room_code_out = room_code
    disconnected_payload: dict | None = None
    paused_payload: dict | None = None

    async with state_svc.lock(room_code):
        room = await state_svc.get_room(room_code)
        if room is None:
            return
        room_code_out = room["room_code"]

        alias = room["sid_to_alias"].get(sid)
        is_host = isinstance(room.get("host_sid"), str) and room["host_sid"] == sid

        if alias is None and not is_host:
            return

        if alias is not None:
            participant = room["participants"].get(alias)
            if participant is not None:
                participant["connected"] = False
                room["sid_to_alias"].pop(sid, None)
                disconnected_payload = {
                    "seq": state_svc.next_seq(room),
                    "alias": alias,
                    "display_name": participant.get("display_name"),
                    "role": participant.get("role"),
                }

                if (
                    room["state"] == RoomState.RUNNING.value
                    and participant.get("role") is not None
                    and not participant.get("is_bot")
                ):
                    config = state_svc.load_config(room)
                    if config.pause_on_disconnect:
                        reason = (
                            f'{participant.get("display_name")} '
                            f'({participant.get("role")}) disconnected'
                        )
                        room["state"] = RoomState.PAUSED.value
                        room["paused_reason"] = reason
                        paused_payload = {
                            "seq": state_svc.next_seq(room),
                            "reason": reason,
                        }

        if is_host:
            room["host_sid"] = None
            if room["state"] == RoomState.RUNNING.value:
                reason = "The host disconnected"
                room["state"] = RoomState.PAUSED.value
                room["paused_reason"] = reason
                paused_payload = {"seq": state_svc.next_seq(room), "reason": reason}

        await state_svc.save_room(room_code_out, room)

    if disconnected_payload is not None:
        await socket_manager.emit_to_room(
            room_code_out, "participant_disconnected", disconnected_payload
        )
    if paused_payload is not None:
        await socket_manager.emit_to_room(room_code_out, "game_paused", paused_payload)
