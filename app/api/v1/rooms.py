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

from fastapi import APIRouter, Header, HTTPException, Request, status

from ...core import abuse
from ...core.config_models import ConfigValidationError, GameConfig
from ...core.enums import ROLE_ORDER, RoomState
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
    RoomBatchCreateRequest,
    RoomBatchCreateResponse,
    RoomCreateRequest,
    RoomCreateResponse,
    RoomStatusResponse,
    RoomSummary,
    RoomSummaryRequest,
    RoomSummaryResponse,
)
from ...services.config_merge import merge_config_patch
from ...services.display_name import sanitise_display_name
from ...services.room_service import RoomService
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
_SEATS_TOTAL = len(ROLE_ORDER)
_OVER_STATES = {RoomState.FINISHED.value, RoomState.ABANDONED.value}

_room_service = RoomService()

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


def _preset_config(name: str | None) -> GameConfig:
    """The named preset, or the default one; a 400 for a name that is none.

    One reading of `preset` for `/create` and `/batch-create`, so the two
    cannot disagree about what an absent or unknown name means.
    """
    if name is None:
        return get_preset(_DEFAULT_PRESET)
    try:
        return get_preset(name)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_UNKNOWN_PRESET_DETAIL,
        ) from None


def _validated_config(stored: dict, patch: dict) -> GameConfig:
    """`patch` merged over `stored` and checked as a host's save is checked.

    The merge and the 422 naming the field are the config PUT's own, so a
    batch's config is refused exactly where a host saving it in one room's
    lobby would be.
    """
    merged = _merge_config(stored, patch)
    try:
        return GameConfig.from_host_input(merged, DEFAULT_LIMITS)
    except ConfigValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{exc.field}: {exc}",
        ) from None


def _created(room: dict) -> RoomCreateResponse:
    """The answer for a room just made, its `host_secret` included. Only the
    two create routes call this: the secret goes to the creator, once."""
    return RoomCreateResponse(
        room_code=room["room_code"],
        host_secret=room["host_secret"],
        state=RoomState(room["state"]),
        config=room["config"],
    )


@router.post("/create", response_model=RoomCreateResponse)
async def create_room(payload: RoomCreateRequest | None = None) -> RoomCreateResponse:
    """Create a room. No authentication required (D3).

    A Firebase token, valid or not, may be presented and has no effect on
    whether this succeeds -- creating a room needs no account, and a token
    that fails to verify must not become a reason to refuse a guest.
    """
    body = payload or RoomCreateRequest()

    config = _preset_config(body.preset)

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

    return _created(room)


@router.post("/batch-create", response_model=RoomBatchCreateResponse)
async def create_room_batch(
    payload: RoomBatchCreateRequest, request: Request
) -> RoomBatchCreateResponse:
    """Create `count` rooms that all start with one config (`10 §3.5c`).

    The multi-game host dashboard's create button. No authentication, like
    `/create`. In order, and nothing is created until every check has passed:

    1. `preset` resolves as on `/create` (400 for an unknown name);
    2. `config` is merged over it and validated once, as a config save is
       (422 naming the field);
    3. the room-create budget (30 rooms an hour per address, shared with
       `/create`) is charged `count` hits, all or nothing (429 with
       `Retry-After`; a refused batch is charged nothing).

    The rooms are then made one at a time. If the store fills part-way
    (`StoreFullError`), the rooms this batch already made are deleted and the
    answer is the 503 `/create` gives: a batch is whole or absent, never a
    handful of rooms whose secrets nobody received.
    """
    config = _preset_config(payload.preset)
    if payload.config:
        config = _validated_config(config.to_payload(), payload.config)

    if not abuse.room_create_allowed(request.scope, payload.count):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=abuse.RATE_LIMITED_MESSAGE,
            headers={"Retry-After": str(abuse.room_create_retry_after(request.scope))},
        )

    host_display_name = sanitise_display_name(
        payload.host_display_name, fallback="Host"
    )
    state_svc = get_state_service()
    made: list[dict] = []
    try:
        for _ in range(payload.count):
            made.append(await state_svc.create_room(config, host_display_name))
    except Exception as exc:
        for room in made:
            try:
                await state_svc.delete_room(room["room_code"])
            except Exception:
                logger.exception("Could not roll back a room of a failed batch.")
        if isinstance(exc, StoreFullError):
            logger.warning("Refusing a room batch: the in-memory store is full.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_ROOMS_FULL_DETAIL,
            ) from None
        logger.exception("Failed to create a room batch.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None

    return RoomBatchCreateResponse(rooms=[_created(room) for room in made])


def _secret_matches(room: dict, supplied: str) -> bool:
    """Timing-safe, and closed on an empty side.

    Compared as bytes: `hmac.compare_digest` raises `TypeError` on a str with
    a non-ASCII character, and a caller's typo must be a mismatch, not a 500.
    """
    stored = room.get("host_secret")
    if not isinstance(stored, str) or not stored or not supplied:
        return False
    return hmac.compare_digest(stored.encode("utf-8"), supplied.encode("utf-8"))


def _summary(room_code: str, room: dict) -> RoomSummary:
    can_start, reason = _room_service.can_start(room)
    try:
        engine = get_state_service().load_engine(room)
    except Exception:  # noqa: BLE001 - a broken engine leaves `week` unknown
        logger.warning("Room %s has an engine that will not load.", room_code)
        engine = None
    try:
        duration_weeks: int | None = GameConfig.from_payload(
            room["config"]
        ).duration_weeks
    except (ValueError, TypeError, KeyError):
        duration_weeks = None
    return RoomSummary(
        room_code=room_code,
        exists=True,
        state=RoomState(room["state"]),
        seats_taken=_room_service.seats_taken(room),
        seats_total=_SEATS_TOTAL,
        week=engine.week if engine is not None else None,
        duration_weeks=duration_weeks,
        finished=room["state"] in _OVER_STATES,
        can_start=can_start,
        start_blocked_reason=reason,
    )


@router.post("/summary", response_model=RoomSummaryResponse)
async def summarise_rooms(payload: RoomSummaryRequest) -> RoomSummaryResponse:
    """State, seats, week and `can_start` for each room the caller hosts
    (`10 §3.5d`). One answer per entry, in order.

    The multi-game dashboard polls this. Read-only: no lock, no save, so
    polling never extends a room's TTL. A room that is not there and a wrong
    secret get the same answer, `exists: false` with every other field null,
    so the route is no oracle for live codes. No secret is ever in the answer.
    A POST so the secrets travel in the body, never in a URL.
    """
    state_svc = get_state_service()
    out: list[RoomSummary] = []
    try:
        for hosted in payload.rooms:
            room = await state_svc.get_room(hosted.room_code)
            if room is None or not _secret_matches(room, hosted.host_secret):
                out.append(RoomSummary(room_code=hosted.room_code, exists=False))
            else:
                out.append(_summary(hosted.room_code, room))
    except Exception:
        logger.exception("Failed to summarise rooms.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None
    return RoomSummaryResponse(rooms=out)


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

            config = _validated_config(room["config"], payload.config)

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
            is_default=name == _DEFAULT_PRESET,
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

    An optional `patch` is merged over `config` first, by the config save's
    own `merge_config_patch`, and the answer carries the validated `config`
    (2026-11-14). That is what lets the multi-game setup form edit a draft no
    room holds: each edit is merged, defaulted and checked here, exactly as a
    lobby save would be, and the form keeps the answer as its draft.

    A plain `def`, so the CPU work runs on the threadpool and never blocks the
    event loop; `core.abuse` gives it its own per-address limit.
    """
    draft = payload.config
    if payload.patch:
        draft = _merge_config(draft, payload.patch)
    try:
        config = GameConfig.from_host_input(draft, DEFAULT_LIMITS)
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
        config=config.to_payload(),
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
