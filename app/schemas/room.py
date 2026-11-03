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
