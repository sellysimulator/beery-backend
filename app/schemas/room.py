"""Request and response models for the rooms REST API
(``10-rooms-rest-api.md`` §2).

``PresetSummary`` is declared before ``PresetListResponse``, which
references it -- the other order is a ``NameError`` at class-creation time.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..core.enums import RoomState
from ..core.limits import DEFAULT_LIMITS


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


class ConfigPreviewRequest(BaseModel):
    config: dict  # a whole draft config, validated exactly as a save is


class ConfigPreviewResponse(BaseModel):
    """`core.preview.ConfigPreview` on the wire (`10 §3.5b`)."""

    demand_series: list[int]  # weeks 1..duration_weeks, index 0 = week 1
    demand_is_example: bool  # STOCHASTIC with no seed yet: an example draw
    lead_times: dict[str, int]  # role -> weeks from ordering to arrival


class PresetSummary(BaseModel):
    name: str
    label: str
    # Figures written from the preset's own config, then a tagline (2026-11-03).
    description: str
    config: dict


class PresetListResponse(BaseModel):
    presets: list[PresetSummary]


# --- multi-game hosting (10-rooms-rest-api.md §3.5c, §3.5d) ---------------- #


class RoomBatchCreateRequest(BaseModel):
    """`POST /rooms/batch-create`: `count` rooms that all start alike.

    `preset` means what it means on `/create` (absent: the default preset).
    `config` is a patch over that preset, merged and validated exactly like a
    host's config save (`PUT /rooms/{code}/config`), once, before any room
    exists. Codes are always generated.
    """

    count: int = Field(
        ge=DEFAULT_LIMITS.min_batch_rooms, le=DEFAULT_LIMITS.max_batch_rooms
    )
    preset: str | None = None
    config: dict = Field(default_factory=dict)
    host_display_name: str = "Host"


class RoomBatchCreateResponse(BaseModel):
    # Every room carries its own host_secret: returned ONCE, to the creator,
    # exactly as `/create` returns one.
    rooms: list[RoomCreateResponse]


class HostedRoom(BaseModel):
    """One room the caller hosts, named with the secret that proves it.

    `room_code` is not checked for shape here: a malformed one is answered
    `exists: false` like any other room that is not there.
    """

    room_code: str = Field(max_length=64)
    host_secret: str = Field(max_length=256)


class RoomSummaryRequest(BaseModel):
    """`POST /rooms/summary`. A POST so the secrets never reach a URL or a log."""

    rooms: list[HostedRoom] = Field(
        min_length=1, max_length=DEFAULT_LIMITS.max_batch_rooms
    )


class RoomSummary(BaseModel):
    """What the multi-game dashboard shows for one room.

    `exists` is false, and every other field null, both for a room that is not
    there and for a wrong `host_secret`: the answer is no oracle for which
    codes are live. `week` is the engine's open week and is null until the
    game starts; `finished` is true once the room is FINISHED or ABANDONED.
    `can_start` and `start_blocked_reason` are `RoomService.can_start`'s
    answer, the rule `start_game` itself applies.
    """

    room_code: str
    exists: bool
    state: RoomState | None = None
    seats_taken: int | None = None
    seats_total: int | None = None
    week: int | None = None
    duration_weeks: int | None = None
    finished: bool | None = None
    can_start: bool | None = None
    start_blocked_reason: str | None = None


class RoomSummaryResponse(BaseModel):
    rooms: list[RoomSummary]
