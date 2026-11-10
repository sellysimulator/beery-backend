"""Room creation and configuration routes (``10-rooms-rest-api.md``).

Creating a room requires no authentication (``00-decisions.md`` D3):
authority over it is the ``host_secret`` this module mints, once, and checks
with ``hmac.compare_digest`` on every privileged call. There is no REST
endpoint that re-issues a lost secret -- that recovery path is socket-only
(D18, section 11) because it depends on the handshake-established identity
that a REST call cannot prove for a guest.
"""

from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, status

from ...core.config_models import ConfigValidationError, GameConfig
from ...core.enums import RoomState
from ...core.limits import DEFAULT_LIMITS, Limits
from ...core.presets import get_preset, preset_names, scenario_summary
from ...core.preview import preview_config
from ...schemas.room import (
    ConfigPreviewRequest,
    ConfigPreviewResponse,
    ConfigResponse,
    ConfigUpdateRequest,
    PresetListResponse,
    PresetSummary,
    RoomCreateRequest,
    RoomCreateResponse,
    RoomStatusResponse,
)
from ...services.config_merge import merge_config_patch
from ...services.display_name import sanitise_display_name
from ...services.state_backend import StoreFullError
from ...services.state_service import get_state_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rooms", tags=["rooms"])

_INTERNAL_ERROR_DETAIL = "Internal server error."
_UNKNOWN_PRESET_DETAIL = "Unknown preset."
_ROOM_NOT_FOUND_DETAIL = "Room does not exist."
_FORBIDDEN_DETAIL = "Invalid host secret."
_STARTED_DETAIL = "Cannot change configuration after the game has started."
_ROOMS_FULL_DETAIL = "The server is at its room limit. Please try again later."
_DEFAULT_PRESET = "CLASSIC_MIT"

# Human copy for the host UI's preset picker (10-rooms-rest-api.md §3.5): a
# label and a tagline, and no figure. The figures are written out of each
# preset's own config by `scenario_summary` (2026-11-03), so a card cannot
# promise a preset the engine does not play.
_PRESET_META: dict[str, tuple[str, str]] = {
    "CLASSIC_MIT": (
        "Classic MIT",
        "The standard scenario, and the one that produces the textbook result.",
    ),
    "FAST_GAME": (
        "Fast Game",
        "The classic scenario on a tight schedule.",
    ),
    "CHAOS": (
        "Chaos",
        "Expect spectacular failure, which is the point.",
    ),
}

# A room may be reconfigured in any of these states; anything else (RUNNING,
# PAUSED, FINISHED, ABANDONED) is frozen (10-rooms-rest-api.md §3.3 step 3).
_CONFIGURABLE_STATES = {
    RoomState.LOBBY.value,
    RoomState.CONFIGURING.value,
    RoomState.READY.value,
}


def _check_host_secret(room: dict, provided: str | None) -> None:
    """Raise 403 unless `provided` matches the room's secret, timing-safely.

    Both sides are checked for emptiness first, so a room whose secret is
    somehow `""` can never be authorised by an equally-empty header --
    `hmac.compare_digest("", "")` is `True`, and that would otherwise be a
    real bypass rather than a hypothetical one (§5 failure mode 3).
    """
    stored = room.get("host_secret") or ""
    supplied = provided or ""
    if not stored or not supplied or not hmac.compare_digest(stored, supplied):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_FORBIDDEN_DETAIL
        )


# The merge itself lives in `app/services/config_merge.py`, so that section
# 11's `config_update` -- which must merge identically (`11 §3.4`) -- can call
# the same function instead of carrying a second copy of it.
_merge_config = merge_config_patch


@router.post("/create", response_model=RoomCreateResponse)
async def create_room(payload: RoomCreateRequest | None = None) -> RoomCreateResponse:
    """Create a room. No authentication required (D3).

    A Firebase token, valid or not, may be presented and has no effect on
    whether this succeeds -- creating a room needs no account, and a token
    that fails to verify must not become a reason to refuse a guest.
    """
    body = payload or RoomCreateRequest()

    if body.preset is not None:
        try:
            config = get_preset(body.preset)
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=_UNKNOWN_PRESET_DETAIL,
            ) from None
    else:
        config = get_preset(_DEFAULT_PRESET)

    host_display_name = sanitise_display_name(body.host_display_name, fallback="Host")

    try:
        room = await get_state_service().create_room(config, host_display_name)
    except HTTPException:
        raise
    except StoreFullError:
        logger.warning("Refusing a new room: the in-memory store is full.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_ROOMS_FULL_DETAIL,
        ) from None
    except Exception:
        logger.exception("Failed to create a room.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None

    return RoomCreateResponse(
        room_code=room["room_code"],
        host_secret=room["host_secret"],
        state=RoomState(room["state"]),
        config=room["config"],
    )


@router.get("/{room_code}/status", response_model=RoomStatusResponse)
async def room_status(room_code: str) -> RoomStatusResponse:
    """Is this room joinable? Public, and deliberately thin (§3.2).

    Always HTTP 200: a 404 for "does not exist" would be a room-code oracle,
    and evaluating the conditions in the document's stated order keeps a
    full room and a finished game from being confused with each other.
    """
    try:
        room = await get_state_service().get_room(room_code)
    except Exception:
        logger.exception("Failed to read status for room %s.", room_code)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None

    if room is None:
        return RoomStatusResponse(ok=False, reason="Room does not exist.")

    state = room["state"]
    if state in (RoomState.FINISHED.value, RoomState.ABANDONED.value):
        return RoomStatusResponse(ok=False, reason="This game has already finished.")
    if state in (RoomState.RUNNING.value, RoomState.PAUSED.value):
        return RoomStatusResponse(ok=False, reason="This game has already started.")

    seats_taken = len(room["participants"])
    if seats_taken >= 4:
        return RoomStatusResponse(ok=False, reason="Room is full.")

    # `host_display_name` stays in the shape but is never filled: this route
    # is unauthenticated, so a name here was readable by anyone guessing
    # codes, and the frontend never reads it from a joinable room.
    return RoomStatusResponse(
        ok=True,
        room_code=room["room_code"],
        state=RoomState(state),
        host_display_name=None,
        participant_count=seats_taken,
        seats_taken=seats_taken,
        seats_total=4,
    )


@router.put("/{room_code}/config", response_model=ConfigResponse)
async def update_config(
    room_code: str,
    payload: ConfigUpdateRequest,
    x_host_secret: str = Header(default="", alias="X-Host-Secret"),
) -> ConfigResponse:
    """Set parameters while configuring (§3.3). Requires `X-Host-Secret`."""
    state_svc = get_state_service()
    try:
        async with state_svc.lock(room_code):
            room = await state_svc.get_room(room_code)
            if room is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=_ROOM_NOT_FOUND_DETAIL,
                )
            _check_host_secret(room, x_host_secret)

            if room["state"] not in _CONFIGURABLE_STATES:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=_STARTED_DETAIL
                )

            merged = _merge_config(room["config"], payload.config)
            try:
                config = GameConfig.from_host_input(merged, DEFAULT_LIMITS)
            except ConfigValidationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"{exc.field}: {exc}",
                ) from None

            room["config"] = config.to_payload()
            if room["state"] == RoomState.LOBBY.value:
                room["state"] = RoomState.CONFIGURING.value
            await state_svc.save_room(room_code, room)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to update config for room %s.", room_code)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None

    return ConfigResponse(
        room_code=room["room_code"],
        state=RoomState(room["state"]),
        config=room["config"],
    )


@router.get("/{room_code}/config", response_model=ConfigResponse)
async def get_config(
    room_code: str,
    x_host_secret: str = Header(default="", alias="X-Host-Secret"),
) -> ConfigResponse:
    """Read the current config back (§3.4). Requires `X-Host-Secret`."""
    try:
        room = await get_state_service().get_room(room_code)
    except Exception:
        logger.exception("Failed to read config for room %s.", room_code)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None

    if room is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_ROOM_NOT_FOUND_DETAIL
        )
    _check_host_secret(room, x_host_secret)

    return ConfigResponse(
        room_code=room["room_code"],
        state=RoomState(room["state"]),
        config=room["config"],
    )


@router.get("/presets", response_model=PresetListResponse)
async def list_presets() -> PresetListResponse:
    """The three presets, for the host UI (§3.5). No authentication."""
    summaries = [
        PresetSummary(
            name=name,
            label=_PRESET_META[name][0],
            description=(
                f"{scenario_summary(get_preset(name))} {_PRESET_META[name][1]}"
            ),
            config=get_preset(name).to_payload(),
        )
        for name in preset_names()
    ]
    return PresetListResponse(presets=summaries)


@router.post("/preview", response_model=ConfigPreviewResponse)
def preview_room_config(payload: ConfigPreviewRequest) -> ConfigPreviewResponse:
    """What a draft config will play, for the host form. No authentication.

    The draft is checked by `GameConfig.from_host_input` exactly as a save is,
    and a refusal is the same 422 naming the field and the range. A valid
    draft returns the demand series and every role's lead time from
    `core.preview`, which calls the generator and the lead-time function the
    engine itself calls -- the host form keeps no copy of either (2026-11-03).
    Nothing is stored and no room is read, so the call needs no room code and
    no host secret: it is a pure function of the body, like `/limits`.

    A plain `def`, so the CPU work runs on the threadpool and never blocks the
    event loop; `core.abuse` gives it its own per-address limit.
    """
    try:
        config = GameConfig.from_host_input(payload.config, DEFAULT_LIMITS)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{exc.field}: {exc}",
        ) from None
    except Exception:
        logger.exception("Failed to preview a config.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None

    preview = preview_config(config)
    return ConfigPreviewResponse(
        demand_series=preview.demand_series,
        demand_is_example=preview.demand_is_example,
        lead_times={role.value: weeks for role, weeks in preview.lead_times.items()},
    )


@router.get("/limits", response_model=Limits)
async def list_limits() -> Limits:
    """Every range the host form and the order form check, for the host UI.
    No authentication.

    The backend is the only copy of these numbers (2026-11-02). The form builds
    its checks from this response instead of carrying its own table, so the
    form and `GameConfig.from_host_input` cannot disagree about a bound -- they
    did, on four fields, while there were two copies. The order bounds a player
    sees travel separately, per game, in `your_state` (`min_order_quantity`,
    `max_order_quantity`), because a host may lower the ceiling per game.
    """
    return DEFAULT_LIMITS
