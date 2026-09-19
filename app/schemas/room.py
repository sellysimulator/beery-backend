"""Request and response models for the rooms REST API
(``10-rooms-rest-api.md`` §2).

``PresetSummary`` is declared before ``PresetListResponse``, which
references it -- the other order is a ``NameError`` at class-creation time.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..core.enums import RoomState


class RoomCreateRequest(BaseModel):
    host_display_name: str = "Host"
    preset: str | None = None  # "CLASSIC_MIT" | "FAST_GAME" | "CHAOS"


class RoomCreateResponse(BaseModel):
    room_code: str
    host_secret: str  # returned ONCE, to the creator only
    state: RoomState
    config: dict


class RoomStatusResponse(BaseModel):
    ok: bool
    room_code: str | None = None
    reason: str | None = None
    state: RoomState | None = None
    host_display_name: str | None = None
    participant_count: int | None = None
    seats_taken: int | None = None
    seats_total: int | None = None


class ConfigUpdateRequest(BaseModel):
    config: dict  # partial or whole; merged then validated


class ConfigResponse(BaseModel):
    room_code: str
    state: RoomState
    config: dict


class PresetSummary(BaseModel):
    name: str
    label: str
    description: str
    config: dict


class PresetListResponse(BaseModel):
    presets: list[PresetSummary]
