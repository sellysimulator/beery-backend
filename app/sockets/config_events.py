"""What a saved config tells the room: one copy for the socket save
(`config_update`) and the REST save (`PUT /rooms/{code}/config`).

Build the events inside the room lock, before `save_room` (they mint `seq`);
send them after the lock is released.
"""

from __future__ import annotations

from typing import NamedTuple

from ..services.room_service import RoomService
from ..services.state_service import StateService
from .manager import socket_manager

_room_service = RoomService()


class ConfigSavedEvents(NamedTuple):
    """The two events of one save, and where each one goes."""

    room_code: str
    #: The claimed host tab, the only one that may see the config.
    host_sid: str | None
    config_updated: dict
    lobby_update: dict


def config_saved_events(
    room: dict, state_svc: StateService, host_sid: str | None
) -> ConfigSavedEvents:
    """The events of the save just written into `room`. A REST save of a room
    no host tab has claimed has no `host_sid`, and nobody to send the config
    to."""
    return ConfigSavedEvents(
        room_code=room["room_code"],
        host_sid=host_sid,
        config_updated={"seq": state_svc.next_seq(room), "config": room["config"]},
        lobby_update=_room_service.lobby_payload(room, state_svc.next_seq(room)),
    )


async def emit_config_saved(events: ConfigSavedEvents) -> None:
    """`config_updated` to the host alone, `lobby_update` to the room."""
    if events.host_sid is not None:
        await socket_manager.emit_to_sid(
            events.host_sid, "config_updated", events.config_updated
        )
    await socket_manager.emit_to_room(
        events.room_code, "lobby_update", events.lobby_update
    )
