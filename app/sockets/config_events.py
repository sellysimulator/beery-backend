"""What a saved config tells the room: one copy for the socket save
(`config_update`) and the REST save (`PUT /rooms/{code}/config`).

Build the events inside the room's turn, before `save_room` (they mint `seq`).
"""

from __future__ import annotations

from ..services.room_service import RoomService
from ..services.room_turn import Emit
from ..services.state_service import StateService

_room_service = RoomService()


def config_saved_emits(
    room: dict, state_svc: StateService, host_sid: str | None
) -> list[Emit]:
    """The events of the save just written into `room`: `config_updated` to the
    claimed host tab alone, the only one that may see the config, and
    `lobby_update` to the room. A REST save of a room no host tab has claimed
    has no `host_sid`, and nobody to send the config to."""
    config_updated = {"seq": state_svc.next_seq(room), "config": room["config"]}
    lobby_update = _room_service.lobby_payload(room, state_svc.next_seq(room))
    emits: list[Emit] = []
    if host_sid is not None:
        emits.append(Emit(host_sid, "config_updated", config_updated))
    emits.append(Emit(room["room_code"], "lobby_update", lobby_update, to_room=True))
    return emits
