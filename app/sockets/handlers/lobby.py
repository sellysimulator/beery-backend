"""The lobby: hosts claiming a room, players arriving, seating, and start
(``11-socket-lobby.md``).

Registers by the ``@sio.event`` decorator firing at import (**D19**): this
module is discovered by ``app/sockets/handlers/__init__.py`` and needs no
edit anywhere upstream.

Every read-modify-write of room state is wrapped in
``async with state_svc.lock(room_code)`` (``00-conventions.md §3``). Emits
happen after the lock is released, from values captured while it was held,
so a slow transport write never extends how long the room is locked.

``room["sid_to_alias"]`` is authoritative; ``socket_manager.sid_to_alias`` is
per-process bookkeeping written by ``socket_manager.join_room`` as a side
effect (``§2.3``). Every handler here reads the room's own map.

Every rejection in this section, per its own explicit text throughout
``11-socket-lobby.md §3``, is a ``join_error`` -- never the generic ``error``
event ``00-conventions.md §3`` describes for other sections.
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any

from ...core.bot import bot_for
from ...core.config_models import DEFAULT_LIMITS, ConfigValidationError, GameConfig
from ...core.enums import ROLE_ORDER, Role, RoleAssignmentMode, RoomState
from ...core.game_engine import GameEngine
from ...services.game_service import get_game_service
from ...services.room_service import RoomService, merge_config_patch
from ...services.state_service import get_state_service, next_free_alias
from ..errors import guarded
from ..manager import sio, socket_manager

logger = logging.getLogger(__name__)

_room_service = RoomService()

_PRE_START_STATES = frozenset(
    {RoomState.LOBBY.value, RoomState.CONFIGURING.value, RoomState.READY.value}
)
_RUNNING_STATES = frozenset({RoomState.RUNNING.value, RoomState.PAUSED.value})

_ROOM_NOT_FOUND = "Room does not exist."
_INVALID_HOST_SECRET = "Invalid host secret."
_GAME_ALREADY_STARTED_JOIN = "Game has already started."
_ROOM_FULL = "Room is full."
_CANNOT_LEAVE_STARTED = "Cannot leave once the game has started."
_CONFIG_STARTED = "Cannot change configuration after the game has started."
_ROLE_ACTION_STARTED = "The game has already started."
_UNKNOWN_PARTICIPANT = "Unknown participant."
_UNKNOWN_ROLE = "Unknown role."
_ROLE_TAKEN = "That role has already been taken."
_HOST_ASSIGNING = "The host is assigning roles."
_RANDOM_ROLES_DEALT = "Roles are dealt at random when the game starts."


def _payload(data: Any) -> dict:
    return data if isinstance(data, dict) else {}


def _room_id_of(data: dict) -> str | None:
    room_id = data.get("room_id")
    return room_id if isinstance(room_id, str) and room_id else None


async def _reject(sid: str, message: str) -> None:
    await socket_manager.emit_to_sid(sid, "join_error", {"message": message})


def _host_target(room: dict, sid: str) -> str:
    """The most recently claimed host sid, or the caller if none is on
    record yet -- host-only events always go to whichever tab most recently
    proved the secret (``11-socket-lobby.md §3.1``)."""
    host_sid = room.get("host_sid")
    return host_sid if isinstance(host_sid, str) and host_sid else sid


def _game_started_payload(room: dict, engine: GameEngine, seq: int) -> dict:
    return {
        "seq": seq,
        "week": engine.week,
        "duration_weeks": engine.config.duration_weeks,
        "role_to_alias": dict(room["role_to_alias"]),
        "bots": sorted(room["bots"].keys()),
        "config_public": _room_service.public_config(engine.config, role=None),
    }


# --- join_waiting ----------------------------------------------------------- #


@sio.event
@guarded("join_error")
async def join_waiting(sid: str, data: dict | None = None) -> None:
    """The host claims or re-claims the room (``§3.1``, **D18**)."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        await _reject(sid, _ROOM_NOT_FOUND)
        return

    state_svc = get_state_service()
    room_code: str
    host_secret_value: str
    lobby_event: dict
    config_event: dict

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            await _reject(sid, _ROOM_NOT_FOUND)
            return
        room_code = room["room_code"]

        identity = socket_manager.sid_to_identity.get(sid)
        by_secret = _room_service.check_host_secret(room, data)
        by_identity = _room_service.check_host_identity(room, identity)
        if not (by_secret or by_identity):
            await _reject(sid, _INVALID_HOST_SECRET)
            return

        if by_secret and identity and not room.get("host_identity"):
            room["host_identity"] = identity

        room["host_sid"] = sid
        # Real sio, deliberately -- not socket_manager.join_room, which
        # requires an alias. The host holds no alias and must never appear
        # in either sid-to-alias map (§2.3).
        await sio.enter_room(sid, room_code)

        host_secret_value = room["host_secret"]
        lobby_event = _room_service.lobby_payload(room, state_svc.next_seq(room))
        config_event = {"seq": state_svc.next_seq(room), "config": room["config"]}

        await state_svc.save_room(room_code, room)

    await socket_manager.emit_to_sid(
        sid, "host_claimed", {"room_id": room_code, "host_secret": host_secret_value}
    )
    await socket_manager.emit_to_room(room_code, "lobby_update", lobby_event)
    await socket_manager.emit_to_sid(sid, "config_updated", config_event)


# --- join --------------------------------------------------------------------- #


@sio.event
@guarded("join_error")
async def join(sid: str, data: dict | None = None) -> None:
    """A player arrives or returns (``§3.2``). Idempotent by identity."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        await _reject(sid, _ROOM_NOT_FOUND)
        return

    identity = socket_manager.sid_to_identity.get(sid)
    session_token = data.get("session_token")
    if not isinstance(session_token, str) or not session_token:
        session_token = None
    display_name_raw = data.get("display_name")

    state_svc = get_state_service()
    room_code: str
    lobby_event: dict
    joined_event: dict
    reconnected_event: dict | None = None
    resync_events: list[tuple[str, dict]] = []

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            await _reject(sid, _ROOM_NOT_FOUND)
            return
        room_code = room["room_code"]

        alias = _room_service.find_alias_for_reconnect(room, session_token, identity)
        if alias is None:
            alias = _room_service.find_alias_for_identity(room, identity)

        if alias is not None:
            participant = room["participants"][alias]
            for stale_sid in [s for s, a in room["sid_to_alias"].items() if a == alias]:
                room["sid_to_alias"].pop(stale_sid, None)
            room["sid_to_alias"][sid] = alias
            participant["connected"] = True

            await socket_manager.join_room(sid, room_code, alias)

            lobby_event = _room_service.lobby_payload(room, state_svc.next_seq(room))
            joined_event = {
                "alias": alias,
                "session_token": participant["session_token"],
                "role": participant["role"],
                "is_host": False,
            }
            # This is section 12's event (`12-socket-play.md §3.5`): without
            # it the host is never told a dropped player has come back, and
            # a pause-on-disconnect looks permanent even though resuming
            # would work.
            reconnected_event = {
                "seq": state_svc.next_seq(room),
                "alias": alias,
                "display_name": participant.get("display_name"),
                "role": participant.get("role"),
            }

            if room["state"] in _RUNNING_STATES and participant.get("role"):
                engine = state_svc.load_engine(room)
                if engine is not None:
                    role = Role(participant["role"])
                    resync_events.append(
                        (
                            "game_started",
                            _game_started_payload(
                                room, engine, state_svc.next_seq(room)
                            ),
                        )
                    )
                    resync_events.append(
                        (
                            "your_state",
                            {
                                "seq": state_svc.next_seq(room),
                                **engine.player_view(role),
                            },
                        )
                    )

            await state_svc.save_room(room_code, room)
        else:
            if room["state"] in _RUNNING_STATES or room["state"] in (
                RoomState.FINISHED.value,
                RoomState.ABANDONED.value,
            ):
                await _reject(sid, _GAME_ALREADY_STARTED_JOIN)
                return
            if _room_service.seats_taken(room) >= 4:
                await _reject(sid, _ROOM_FULL)
                return

            alias = next_free_alias(room)
            new_token = uuid.uuid4().hex
            display_name = _room_service.sanitise_display_name(
                display_name_raw, fallback=alias
            )

            room["participants"][alias] = {
                "alias": alias,
                "identity": identity,
                "session_token": new_token,
                "display_name": display_name,
                "role": None,
                "is_bot": False,
                "connected": True,
            }
            room["sid_to_alias"][sid] = alias

            await socket_manager.join_room(sid, room_code, alias)

            lobby_event = _room_service.lobby_payload(room, state_svc.next_seq(room))
            joined_event = {
                "alias": alias,
                "session_token": new_token,
                "role": None,
                "is_host": False,
            }

            await state_svc.save_room(room_code, room)

    await socket_manager.emit_to_room(room_code, "lobby_update", lobby_event)
    await socket_manager.emit_to_sid(sid, "joined", joined_event)
    if reconnected_event is not None:
        await socket_manager.emit_to_room(
            room_code, "participant_reconnected", reconnected_event
        )
    for event_name, event_payload in resync_events:
        await socket_manager.emit_to_sid(sid, event_name, event_payload)


# --- leave ---------------------------------------------------------------------- #


@sio.event
@guarded("join_error")
async def leave(sid: str, data: dict | None = None) -> None:
    """Permitted only before the game starts (``§3.3``)."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        await _reject(sid, _ROOM_NOT_FOUND)
        return

    state_svc = get_state_service()
    room_code: str
    lobby_event: dict | None = None

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            await _reject(sid, _ROOM_NOT_FOUND)
            return
        room_code = room["room_code"]

        if room["state"] not in _PRE_START_STATES:
            await _reject(sid, _CANNOT_LEAVE_STARTED)
            return

        # The alias comes ONLY from the authoritative map -- a client may
        # never supply an alias to remove; that would let anyone kick
        # anyone.
        alias = room["sid_to_alias"].get(sid)
        if alias is not None:
            participant = room["participants"].pop(alias, None)
            if participant is not None:
                role = participant.get("role")
                if role is not None and room["role_to_alias"].get(role) == alias:
                    room["role_to_alias"][role] = None
            for stale_sid in [s for s, a in room["sid_to_alias"].items() if a == alias]:
                room["sid_to_alias"].pop(stale_sid, None)

            lobby_event = _room_service.lobby_payload(room, state_svc.next_seq(room))

        await state_svc.save_room(room_code, room)

    socket_manager.sid_to_alias.pop(sid, None)
    socket_manager.sid_to_room.pop(sid, None)

    await socket_manager.emit_to_sid(sid, "leave_ack", {"room_id": room_code})
    if lobby_event is not None:
        await socket_manager.emit_to_room(room_code, "lobby_update", lobby_event)


# --- config_update ---------------------------------------------------------------- #


@sio.event
@guarded("join_error")
async def config_update(sid: str, data: dict | None = None) -> None:
    """The real-time twin of ``PUT /rooms/{code}/config`` (``§3.4``)."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        await _reject(sid, _ROOM_NOT_FOUND)
        return

    patch = data.get("config")
    state_svc = get_state_service()
    room_code: str
    host_target: str
    config_event: dict
    lobby_event: dict

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            await _reject(sid, _ROOM_NOT_FOUND)
            return
        room_code = room["room_code"]

        if not _room_service.check_host_secret(room, data):
            await _reject(sid, _INVALID_HOST_SECRET)
            return

        if room["state"] not in _PRE_START_STATES:
            await _reject(sid, _CONFIG_STARTED)
            return

        merged = merge_config_patch(
            room["config"], patch if isinstance(patch, dict) else {}
        )
        try:
            config = GameConfig.from_host_input(merged, DEFAULT_LIMITS)
        except ConfigValidationError as exc:
            await _reject(sid, f"{exc.field}: {exc}")
            return

        room["config"] = config.to_payload()
        if room["state"] == RoomState.LOBBY.value:
            room["state"] = RoomState.CONFIGURING.value

        host_target = _host_target(room, sid)
        config_event = {"seq": state_svc.next_seq(room), "config": room["config"]}
        lobby_event = _room_service.lobby_payload(room, state_svc.next_seq(room))

        await state_svc.save_room(room_code, room)

    await socket_manager.emit_to_sid(host_target, "config_updated", config_event)
    await socket_manager.emit_to_room(room_code, "lobby_update", lobby_event)


# --- set_role_mode ------------------------------------------------------------------ #


@sio.event
@guarded("join_error")
async def set_role_mode(sid: str, data: dict | None = None) -> None:
    """Host only. Clears every assignment, even if the mode is unchanged
    (``§3.5``)."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        await _reject(sid, _ROOM_NOT_FOUND)
        return

    mode = data.get("mode")
    state_svc = get_state_service()
    room_code: str
    host_target: str
    roles_event: dict
    lobby_event: dict
    config_event: dict

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            await _reject(sid, _ROOM_NOT_FOUND)
            return
        room_code = room["room_code"]

        if not _room_service.check_host_secret(room, data):
            await _reject(sid, _INVALID_HOST_SECRET)
            return

        if room["state"] not in _PRE_START_STATES:
            await _reject(sid, _ROLE_ACTION_STARTED)
            return

        merged = merge_config_patch(room["config"], {"role_assignment_mode": mode})
        try:
            config = GameConfig.from_host_input(merged, DEFAULT_LIMITS)
        except ConfigValidationError as exc:
            await _reject(sid, f"{exc.field}: {exc}")
            return

        room["config"] = config.to_payload()

        for role in ROLE_ORDER:
            room["role_to_alias"][role.value] = None
        for participant in room["participants"].values():
            participant["role"] = None

        host_target = _host_target(room, sid)
        roles_event = {
            "seq": state_svc.next_seq(room),
            "role_to_alias": dict(room["role_to_alias"]),
        }
        lobby_event = _room_service.lobby_payload(room, state_svc.next_seq(room))
        config_event = {"seq": state_svc.next_seq(room), "config": room["config"]}

        await state_svc.save_room(room_code, room)

    await socket_manager.emit_to_room(room_code, "roles_assigned", roles_event)
    await socket_manager.emit_to_room(room_code, "lobby_update", lobby_event)
    await socket_manager.emit_to_sid(host_target, "config_updated", config_event)


# --- assign_role ---------------------------------------------------------------------- #


@sio.event
@guarded("join_error")
async def assign_role(sid: str, data: dict | None = None) -> None:
    """Host authority. ``role: null`` unseats (``§3.5``)."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        await _reject(sid, _ROOM_NOT_FOUND)
        return

    alias = data.get("alias")
    role_raw = data.get("role")

    state_svc = get_state_service()
    room_code: str
    roles_event: dict
    lobby_event: dict

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            await _reject(sid, _ROOM_NOT_FOUND)
            return
        room_code = room["room_code"]

        if not _room_service.check_host_secret(room, data):
            await _reject(sid, _INVALID_HOST_SECRET)
            return

        if room["state"] not in _PRE_START_STATES:
            await _reject(sid, _ROLE_ACTION_STARTED)
            return

        config = state_svc.load_config(room)
        if config.role_assignment_mode is RoleAssignmentMode.RANDOM:
            await _reject(sid, _RANDOM_ROLES_DEALT)
            return

        if not isinstance(alias, str) or alias not in room["participants"]:
            await _reject(sid, _UNKNOWN_PARTICIPANT)
            return

        if role_raw is None:
            new_role: Role | None = None
        else:
            try:
                new_role = Role(role_raw)
            except ValueError:
                await _reject(sid, _UNKNOWN_ROLE)
                return

        current_role = room["participants"][alias].get("role")
        if (
            current_role is not None
            and room["role_to_alias"].get(current_role) == alias
        ):
            room["role_to_alias"][current_role] = None

        if new_role is not None:
            previous_holder = room["role_to_alias"].get(new_role.value)
            if previous_holder is not None and previous_holder != alias:
                room["participants"][previous_holder]["role"] = None
            room["role_to_alias"][new_role.value] = alias

        room["participants"][alias]["role"] = (
            new_role.value if new_role is not None else None
        )

        roles_event = {
            "seq": state_svc.next_seq(room),
            "role_to_alias": dict(room["role_to_alias"]),
        }
        lobby_event = _room_service.lobby_payload(room, state_svc.next_seq(room))

        await state_svc.save_room(room_code, room)

    await socket_manager.emit_to_room(room_code, "roles_assigned", roles_event)
    await socket_manager.emit_to_room(room_code, "lobby_update", lobby_event)


# --- claim_role ------------------------------------------------------------------------- #


@sio.event
@guarded("join_error")
async def claim_role(sid: str, data: dict | None = None) -> None:
    """First come, first served in ``PLAYER_CHOOSES`` (``§3.5``). Atomic:
    the check and the write happen inside one lock acquisition."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        await _reject(sid, _ROOM_NOT_FOUND)
        return

    role_raw = data.get("role")

    state_svc = get_state_service()
    room_code: str
    roles_event: dict
    lobby_event: dict

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            await _reject(sid, _ROOM_NOT_FOUND)
            return
        room_code = room["room_code"]

        if room["state"] not in _PRE_START_STATES:
            await _reject(sid, _ROLE_ACTION_STARTED)
            return

        config = state_svc.load_config(room)
        mode = config.role_assignment_mode
        if mode is RoleAssignmentMode.HOST_ASSIGNS:
            await _reject(sid, _HOST_ASSIGNING)
            return
        if mode is RoleAssignmentMode.RANDOM:
            await _reject(sid, _RANDOM_ROLES_DEALT)
            return

        alias = room["sid_to_alias"].get(sid)
        if alias is None or alias not in room["participants"]:
            await _reject(sid, _UNKNOWN_PARTICIPANT)
            return

        try:
            role = Role(role_raw)
        except ValueError:
            await _reject(sid, _UNKNOWN_ROLE)
            return

        holder = room["role_to_alias"].get(role.value)
        if holder is not None and holder != alias:
            await _reject(sid, _ROLE_TAKEN)
            return

        current_role = room["participants"][alias].get("role")
        if (
            current_role is not None
            and current_role != role.value
            and room["role_to_alias"].get(current_role) == alias
        ):
            room["role_to_alias"][current_role] = None

        room["role_to_alias"][role.value] = alias
        room["participants"][alias]["role"] = role.value

        roles_event = {
            "seq": state_svc.next_seq(room),
            "role_to_alias": dict(room["role_to_alias"]),
        }
        lobby_event = _room_service.lobby_payload(room, state_svc.next_seq(room))

        await state_svc.save_room(room_code, room)

    await socket_manager.emit_to_room(room_code, "roles_assigned", roles_event)
    await socket_manager.emit_to_room(room_code, "lobby_update", lobby_event)


# --- release_role --------------------------------------------------------------------------- #


@sio.event
@guarded("join_error")
async def release_role(sid: str, data: dict | None = None) -> None:
    """The caller gives up their role. Pre-start only (``§3.5``)."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        await _reject(sid, _ROOM_NOT_FOUND)
        return

    state_svc = get_state_service()
    room_code: str
    roles_event: dict | None = None
    lobby_event: dict | None = None

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            await _reject(sid, _ROOM_NOT_FOUND)
            return
        room_code = room["room_code"]

        if room["state"] not in _PRE_START_STATES:
            await _reject(sid, _ROLE_ACTION_STARTED)
            return

        alias = room["sid_to_alias"].get(sid)
        if alias is None or alias not in room["participants"]:
            return

        current_role = room["participants"][alias].get("role")
        if current_role is None:
            return

        if room["role_to_alias"].get(current_role) == alias:
            room["role_to_alias"][current_role] = None
        room["participants"][alias]["role"] = None

        roles_event = {
            "seq": state_svc.next_seq(room),
            "role_to_alias": dict(room["role_to_alias"]),
        }
        lobby_event = _room_service.lobby_payload(room, state_svc.next_seq(room))

        await state_svc.save_room(room_code, room)

    if roles_event is not None and lobby_event is not None:
        await socket_manager.emit_to_room(room_code, "roles_assigned", roles_event)
        await socket_manager.emit_to_room(room_code, "lobby_update", lobby_event)


# --- start_game ------------------------------------------------------------------------------- #


@sio.event
@guarded("join_error")
async def start_game(sid: str, data: dict | None = None) -> None:
    """Host authority. ``can_start`` is the whole gate (``§2.2``, ``§3.6``):
    the invalid-config case, the already-running case and the
    unfilled-roles case are all inside it, and none of them is restated
    here."""
    data = _payload(data)
    room_id = _room_id_of(data)
    if room_id is None:
        await _reject(sid, _ROOM_NOT_FOUND)
        return

    state_svc = get_state_service()
    room_code: str
    host_target: str
    game_started_event: dict
    host_state_event: dict
    your_state_events: list[tuple[str, dict]] = []

    async with state_svc.lock(room_id):
        room = await state_svc.get_room(room_id)
        if room is None:
            await _reject(sid, _ROOM_NOT_FOUND)
            return
        room_code = room["room_code"]
        host_target = _host_target(room, sid)

        if not _room_service.check_host_secret(room, data):
            await _reject(sid, _INVALID_HOST_SECRET)
            return

        try:
            config: GameConfig | None = state_svc.load_config(room)
        except Exception:  # noqa: BLE001 - can_start below reports this to the host
            logger.warning(
                "Room %s has a stored config that will not build a GameConfig.",
                room_code,
            )
            config = None

        if config is not None:
            seed = (
                config.random_seed
                if config.random_seed is not None
                else random.randrange(2**31)
            )
            room["seed"] = seed
            if config.role_assignment_mode is RoleAssignmentMode.RANDOM:
                _room_service.assign_roles_randomly(
                    room, random.Random(f"{seed}:roles")
                )

        ok, reason = _room_service.can_start(room)
        if not ok:
            if room["state"] not in _PRE_START_STATES:
                # A double-click on a room that is already running is not an
                # error -- the first click succeeded, and a toast reading "the
                # game is already running" on the happy path is noise. What is
                # suppressed here is the *notification*, not the decision:
                # `can_start` remains the only rule, and nothing is mutated or
                # saved either way (§3.6 step 4, AC 31).
                return
            await _reject(sid, reason or _INVALID_HOST_SECRET)
            return

        assert config is not None  # can_start already parsed it successfully

        empty_roles = [
            role for role in ROLE_ORDER if room["role_to_alias"].get(role.value) is None
        ]
        bot_roles = frozenset(empty_roles)
        for role in empty_roles:
            bot_alias = next_free_alias(room)
            room["participants"][bot_alias] = {
                "alias": bot_alias,
                "identity": None,
                "session_token": None,
                "display_name": f"{role.value.title()} (bot)",
                "role": role.value,
                "is_bot": True,
                "connected": True,
            }
            room["role_to_alias"][role.value] = bot_alias

        bots = {role: bot_for(role, config) for role in bot_roles}
        engine = GameEngine.start(config, room["seed"], bot_roles)

        room["state"] = RoomState.RUNNING.value
        room["started_at"] = datetime.now(timezone.utc).isoformat()
        state_svc.store_engine(room, engine)
        state_svc.store_bots(room, bots)

        game_started_event = _game_started_payload(
            room, engine, state_svc.next_seq(room)
        )

        for role in ROLE_ORDER:
            alias = room["role_to_alias"].get(role.value)
            participant = room["participants"].get(alias) if alias else None
            if participant is None or participant.get("is_bot"):
                continue
            for player_sid, mapped_alias in room["sid_to_alias"].items():
                if mapped_alias == alias:
                    your_state_events.append(
                        (
                            player_sid,
                            {
                                "seq": state_svc.next_seq(room),
                                **engine.player_view(role),
                            },
                        )
                    )

        host_state_event = {"seq": state_svc.next_seq(room), **engine.host_view()}

        await state_svc.save_room(room_code, room)

    await socket_manager.emit_to_room(room_code, "game_started", game_started_event)
    for target_sid, payload in your_state_events:
        await socket_manager.emit_to_sid(target_sid, "your_state", payload)
    await socket_manager.emit_to_sid(host_target, "host_state", host_state_event)

    # Section 12 (`12-socket-play.md §3.2`): play every bot role immediately,
    # so a bot-filled room does not sit idle in week 1. This runs after the
    # lock above has been released -- `run_bot_decisions` acquires its own,
    # and appending it inside that block would deadlock every bot game for
    # `LOCK_TIMEOUT_SECONDS` on the very first week.
    await get_game_service().run_bot_decisions(room_code)
