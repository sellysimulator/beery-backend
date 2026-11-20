"""Results, export and guest-claim routes (spec §9.8, §9.5, §3.1).

Everything here reads from **PostgreSQL**, never Redis, except the export route's
host-secret check, which reads the *live room document* when one still
exists -- that is the one place this module touches Redis, and it
never mutates it.

`GET /results` and `GET /export` both resolve "the game for this room code"
the same way: the most recently finished `games` row for that code, because
codes are reused once a room expires. Since only *finished*
games are ever written to PostgreSQL (**D4**), any row found here is by
definition a finished game -- there is no separate status filter.

`GET /games/id/{public_id}/results` serves the same payload addressed by the
game's permanent `public_id`, for links that must outlive the room code, and
`GET /games/id/{public_id}/export` the export, open to anyone who has that
id, as the results are. The export by room code stays host-only.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.enums import ROLE_ORDER, Role
from ...core.game_engine import WeekRecord
from ...core.stats import compute_stats
from ...db.session import get_db
from ...models.game import DemandSeries, Game, GameConfigRow, Participant, Week
from ...models.user import User
from ...schemas.results import (
    ClaimRequest,
    ClaimResponse,
    ResultsResponse,
    RoleResult,
)
from ...services.claim_service import ClaimService
from ...services.export_cache import CachedExport, export_cache
from ...services.export_service import ExportService, seat_alias
from ...services.state_service import get_state_service
from ..deps import get_current_firebase_user, get_optional_firebase_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/games", tags=["games"])

_INTERNAL_ERROR_DETAIL = "Internal server error."
_NO_RESULTS_DETAIL = "No finished game found for that code."
_NO_GAME_DETAIL = "No finished game found for that id."
_EXPORT_FORBIDDEN_DETAIL = "Not authorised to export this game."

_claim_service = ClaimService()
_export_service = ExportService()

_ROLE_INDEX = {role.value: index for index, role in enumerate(ROLE_ORDER)}


# --------------------------------------------------------------------------- #
# Shared lookups, reused by `user_games.py` for the caller's own single game.
# --------------------------------------------------------------------------- #


def find_latest_finished_game(db: Session, room_code: str) -> Game | None:
    """The most recently finished game for a room code, or None.

    A room code is reused once a room expires, so "the game for this code"
    means the newest `finished_at`, never an arbitrary match.

    Case-insensitive, like the live room lookup (`state_service._room_key`):
    codes are minted and stored upper-case, and PostgreSQL compares strings
    case-sensitively, so the code from the URL is normalised here rather than
    trusting the database's collation to fold it.
    """
    return db.execute(
        select(Game)
        .where(Game.room_code == room_code.strip().upper())
        .order_by(Game.finished_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def find_game_by_public_id(db: Session, public_id: str) -> Game | None:
    """The game a stable results URL names, or None.

    Unlike a room code, a `public_id` is never reused, so there is no
    "latest" to choose between.
    """
    return db.execute(
        select(Game).where(Game.public_id == public_id)
    ).scalar_one_or_none()


def week_record_from_row(row: Week) -> WeekRecord:
    """Rebuild the `WeekRecord` `compute_stats` expects from a persisted row.

    `order_qty` is the one renamed column; every other field
    keeps its name. Money columns cross back from `Decimal` to `float` here,
    the mirror image of `db_service.round_money`.
    """
    return WeekRecord(
        role=Role(row.role),
        week=row.week,
        opening_inventory=row.opening_inventory,
        opening_backlog=row.opening_backlog,
        arrived=row.arrived,
        incoming_order=row.incoming_order,
        obligation=row.obligation,
        shipped=row.shipped,
        unfulfilled=row.unfulfilled,
        closing_inventory=row.closing_inventory,
        closing_backlog=row.closing_backlog,
        supply_line_after=row.supply_line_after,
        orders_in_flight_after=row.orders_in_flight_after,
        order=row.order_qty,
        was_bot=row.was_bot,
        was_forced=row.was_forced,
        holding_cost=float(row.holding_cost),
        backlog_cost=float(row.backlog_cost),
        fixed_order_cost=float(row.fixed_order_cost),
        purchase_cost=float(row.purchase_cost),
        total_week_cost=float(row.total_week_cost),
        cumulative_cost=float(row.cumulative_cost),
        production_started=row.production_started,
        production_queued=row.production_queued,
    )


def build_results_response(db: Session, game: Game) -> ResultsResponse:
    """The full `ResultsResponse` for one persisted game.

    Every statistic -- `total_cost`, `peak_inventory`, `peak_backlog`,
    `weeks_in_backlog`, `order_variance`, `bullwhip_ratio`, `fill_rate`,
    `average_order`, `chain_total_cost` and `demand_variance` -- comes from
    `app.core.stats.compute_stats` over the reconstructed `weeks` rows and
    `demand_series`, never from a stored column and never recomputed in SQL
    (spec §10.3). `games.chain_total_cost` exists only for cheap listing
    elsewhere; it is not read here.
    """
    game_config = db.execute(
        select(GameConfigRow).where(GameConfigRow.game_id == game.id)
    ).scalar_one()

    participants = (
        db.execute(select(Participant).where(Participant.game_id == game.id))
        .scalars()
        .all()
    )
    participants_by_role = {
        Role(participant.role): participant
        for participant in participants
        if participant.role is not None
    }

    demand_rows = db.execute(
        select(DemandSeries.week, DemandSeries.quantity)
        .where(DemandSeries.game_id == game.id)
        .order_by(DemandSeries.week)
    ).all()
    demand_series = [int(row.quantity) for row in demand_rows]

    week_rows = db.execute(select(Week).where(Week.game_id == game.id)).scalars().all()
    history = [week_record_from_row(row) for row in week_rows]
    stats = compute_stats(history, demand_series, game.weeks_played)

    per_role: list[RoleResult] = []
    for role in ROLE_ORDER:
        role_rows = sorted(
            (row for row in week_rows if Role(row.role) is role),
            key=lambda row: row.week,
        )
        role_stats = stats.per_role[role]
        participant = participants_by_role.get(role)
        per_role.append(
            RoleResult(
                role=role,
                display_name=(
                    participant.display_name if participant else role.value.title()
                ),
                is_bot=bool(participant.is_bot) if participant else True,
                total_cost=role_stats.total_cost,
                peak_inventory=role_stats.peak_inventory,
                peak_backlog=role_stats.peak_backlog,
                weeks_in_backlog=role_stats.weeks_in_backlog,
                order_variance=role_stats.order_variance,
                bullwhip_ratio=role_stats.bullwhip_ratio,
                fill_rate=role_stats.fill_rate,
                average_order=role_stats.average_order,
                orders=[row.order_qty for row in role_rows],
                inventory=[row.closing_inventory for row in role_rows],
                backlog=[row.closing_backlog for row in role_rows],
                cumulative_cost=[float(row.cumulative_cost) for row in role_rows],
            )
        )

    return ResultsResponse(
        public_id=game.public_id,
        room_code=game.room_code,
        weeks_played=game.weeks_played,
        duration_weeks=game.duration_weeks,
        ended_early=game.ended_early,
        currency_symbol=game_config.currency_symbol,
        started_at=game.started_at,
        finished_at=game.finished_at,
        demand_series=demand_series,
        chain_total_cost=stats.chain_total_cost,
        demand_variance=stats.demand_variance,
        per_role=per_role,
        preset_name=game_config.preset_name,
    )


def build_week_export_rows(db: Session, game: Game) -> list[dict]:
    """One dict per `(week, role)`, keyed exactly per
    `export_service.WEEK_EXPORT_COLUMNS`, for both export formats.

    `display_name` holds the seat's `P<N>` (`export_service.seat_alias`), not
    the player's chosen name: the export carries no name anyone typed.

    Ordered `week` ascending, then `ROLE_ORDER` within a week --
    frozen so two exports of the same finished game are byte-identical.
    """
    participants = (
        db.execute(select(Participant).where(Participant.game_id == game.id))
        .scalars()
        .all()
    )
    participants_by_role = {
        participant.role: participant
        for participant in participants
        if participant.role is not None
    }

    demand_by_week = dict(
        db.execute(
            select(DemandSeries.week, DemandSeries.quantity).where(
                DemandSeries.game_id == game.id
            )
        ).all()
    )

    week_rows = db.execute(select(Week).where(Week.game_id == game.id)).scalars().all()
    week_rows = sorted(
        week_rows,
        key=lambda row: (row.week, _ROLE_INDEX.get(row.role, len(ROLE_ORDER))),
    )

    rows: list[dict] = []
    for row in week_rows:
        participant = participants_by_role.get(row.role)
        rows.append(
            {
                "room_code": game.room_code,
                "week": row.week,
                "role": row.role,
                # The seat's `P<N>`, never the name the player chose: the
                # screens show names, the file does not.
                "display_name": seat_alias(row.role),
                "is_bot": bool(participant.is_bot) if participant else True,
                "was_bot": row.was_bot,
                "was_forced": row.was_forced,
                "customer_demand": demand_by_week.get(row.week),
                "opening_inventory": row.opening_inventory,
                "opening_backlog": row.opening_backlog,
                "arrived": row.arrived,
                "incoming_order": row.incoming_order,
                "obligation": row.obligation,
                "shipped": row.shipped,
                "unfulfilled": row.unfulfilled,
                "closing_inventory": row.closing_inventory,
                "closing_backlog": row.closing_backlog,
                "supply_line_after": row.supply_line_after,
                # Renamed at the boundary only: the column holds
                # orders coming TO the role, and the player's on-screen "orders
                # in flight" are their own orders going upstream.
                "incoming_orders_in_flight_after": row.orders_in_flight_after,
                "order_qty": row.order_qty,
                "holding_cost": float(row.holding_cost),
                "backlog_cost": float(row.backlog_cost),
                "fixed_order_cost": float(row.fixed_order_cost),
                "purchase_cost": float(row.purchase_cost),
                "total_week_cost": float(row.total_week_cost),
                "cumulative_cost": float(row.cumulative_cost),
                "production_started": row.production_started,
                "production_queued": row.production_queued,
            }
        )
    return rows


def _resolve_export_authority(
    db: Session,
    room: dict | None,
    game: Game,
    host_secret_header: str,
    authorization_header: str,
) -> None:
    """The export authority's evaluation order (spec §9.5), first match wins.

    The room-existence check has already happened (a caller reaches this
    function only once a finished `games` row was found). This function only
    decides *who* may have it: the live room's secret, or -- when there is no
    live room, or the secret is absent or wrong -- the registered host's own
    bearer token. Neither credential is used to probe the other: a wrong
    secret and a missing bearer token both fall through to the same 403.

    The registered host is the account on the game's `HOST` participant row,
    the one record of who hosted: a guest host who later claims their results
    is attributed there, and may then export with their own token.
    """
    if room is not None:
        stored = room.get("host_secret") or ""
        supplied = host_secret_header or ""
        if stored and supplied and hmac.compare_digest(stored, supplied):
            return

    host_user_id = db.execute(
        select(Participant.user_id).where(
            Participant.game_id == game.id,
            Participant.participant_type == "HOST",
        )
    ).scalar_one_or_none()
    if host_user_id is not None:
        claims = get_optional_firebase_user(authorization=authorization_header)
        if claims is not None:
            uid = claims.get("uid")
            if uid:
                user_id = db.execute(
                    select(User.id).where(User.firebase_uid == uid)
                ).scalar_one_or_none()
                if user_id is not None and user_id == host_user_id:
                    return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail=_EXPORT_FORBIDDEN_DETAIL
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/{room_code}/results", response_model=ResultsResponse)
def get_results(
    room_code: str, db: Annotated[Session, Depends(get_db)]
) -> ResultsResponse:
    """The full results payload for a finished game. No authentication --
    a results URL is shareable (spec §9.8).
    """
    try:
        game = find_latest_finished_game(db, room_code)
        if game is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=_NO_RESULTS_DETAIL
            )
        return build_results_response(db, game)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to read results for room %s.", room_code)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None


@router.get("/id/{public_id}/results", response_model=ResultsResponse)
def get_results_by_public_id(
    public_id: str, db: Annotated[Session, Depends(get_db)]
) -> ResultsResponse:
    """The same payload as `get_results`, addressed by the game's permanent
    `public_id` rather than its recyclable room code. No authentication, for
    the same reason: a results URL is shareable.
    """
    try:
        game = find_game_by_public_id(db, public_id)
        if game is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=_NO_GAME_DETAIL
            )
        return build_results_response(db, game)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to read results for game %s.", public_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None


@router.get("/{room_code}/export")
async def export_game(
    room_code: str,
    db: Annotated[Session, Depends(get_db)],
    format: Literal["csv", "json"] = "csv",
    x_host_secret: str = Header(default="", alias="X-Host-Secret"),
    authorization: str = Header(default=""),
) -> Response:
    """CSV (default) or JSON of the complete record, by room code. Host-only
    (spec §9.5): the live room's secret or the registered host's token.

    Unlike `export_game_by_public_id`, this stays closed: a room code is six
    characters and reissued once its room expires, so an open export by code
    would let anyone walk the code space and download every finished game.
    Once the caller is authorised it shares the export cache, keyed by the
    game's `public_id`.
    """
    try:
        game = find_latest_finished_game(db, room_code)
        if game is None:
            # 403, not 404: a 404 here would tell an unauthorised caller
            # that the game does not exist rather than that they may not
            # have it.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=_EXPORT_FORBIDDEN_DETAIL
            )

        room = await get_state_service().get_room(room_code)
        _resolve_export_authority(db, room, game, x_host_secret, authorization)

        cached = export_cache.get((game.public_id, format))
        if cached is None:
            cached = _render_export(db, game, format)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to export game for room %s.", room_code)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None

    return _cached_response(cached)


@router.get("/id/{public_id}/export")
def export_game_by_public_id(
    public_id: str,
    db: Annotated[Session, Depends(get_db)],
    format: Literal["csv", "json"] = "csv",
) -> Response:
    """The same export, addressed by the game's permanent `public_id`. No
    authentication, like `get_results_by_public_id`.

    Anyone who can open a finished game's results screen may download its
    raw data: a host may be a guest with no account to fall back on once the
    room and the tab are gone, so the export is not bound to the host. The
    export carries the figures the results screen shows and no name anyone
    typed: every player is their seat's `P<N>`, and there is no
    account or credential in it.

    A repeat download is served from the in-process export cache
    (`services/export_cache.py`) without touching the database: a finished
    game never changes. The guard against draining compute is `core.abuse`'s
    hourly per-address export budget (429 with `Retry-After`), charged by the
    middleware before this runs, cached or not. Only the `public_id` route is
    open: a room code is short and reissued, so `export_game` stays host-only.
    """
    try:
        cached = export_cache.get((public_id, format))
        if cached is None:
            game = find_game_by_public_id(db, public_id)
            if game is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=_NO_GAME_DETAIL
                )
            cached = _render_export(db, game, format)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to export game %s.", public_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None

    return _cached_response(cached)


def _render_export(
    db: Session, game: Game, format: Literal["csv", "json"]
) -> CachedExport:
    """Build one export from the database and keep it in the export cache.

    Only a finished, found game reaches this, so only a 200 is ever cached.
    """
    results = build_results_response(db, game)
    weeks = build_week_export_rows(db, game)
    if format == "json":
        rendered = CachedExport(
            body=JSONResponse(content=_export_service.to_json(results, weeks)).body,
            media_type="application/json",
            headers={},
        )
    else:
        filename = f"beery-{game.room_code}-{game.finished_at:%Y%m%d}.csv"
        rendered = CachedExport(
            body=_export_service.to_csv(weeks).encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    export_cache.put((game.public_id, format), rendered)
    return rendered


def _cached_response(entry: CachedExport) -> Response:
    return Response(
        content=entry.body, media_type=entry.media_type, headers=dict(entry.headers)
    )


@router.post("/claim", response_model=ClaimResponse)
def claim_games(
    payload: ClaimRequest,
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
) -> ClaimResponse:
    """Attribute a guest's finished games to the signed-in caller (spec §3.1)."""
    try:
        claimed = _claim_service.claim_guest_results(
            db, claims["uid"], payload.guest_identity
        )
    except Exception:
        logger.exception("Failed to claim guest results.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None
    return ClaimResponse(claimed=claimed)
