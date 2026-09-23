"""Results, export and guest-claim routes (``15-results-and-export-api.md``).

Everything here reads from **MySQL**, never Redis, except the export route's
host-secret check, which reads the *live room document* when one still
exists (`§3.2`) -- that is the one place this module touches Redis, and it
never mutates it.

`GET /results` and `GET /export` both resolve "the game for this room code"
the same way: the most recently finished `games` row for that code, because
codes are reused once a room expires (`13 §2.2`). Since only *finished*
games are ever written to MySQL (**D4**), any row found here is by
definition a finished game -- there is no separate status filter.

`GET /games/id/{public_id}/results` serves the same payload addressed by the
game's permanent `public_id`, for links that must outlive the room code.
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
from ...services.export_service import ExportService
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

    A room code is reused once a room expires (`13-db-models-and-
    migrations.md §2.2`), so "the game for this code" means the newest
    `finished_at`, never an arbitrary match.
    """
    return db.execute(
        select(Game)
        .where(Game.room_code == room_code)
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

    `order_qty` is the one renamed column (`13 §2.8`); every other field
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
        week_cost=float(row.week_cost),
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
    (`§3.1`). `games.chain_total_cost` exists only for cheap listing
    elsewhere (`13 §2.2`); it is not read here.
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

    Ordered `week` ascending, then `ROLE_ORDER` within a week (`§3.2`) --
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
                "display_name": participant.display_name if participant else "",
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
                "orders_in_flight_after": row.orders_in_flight_after,
                "order_qty": row.order_qty,
                "holding_cost": float(row.holding_cost),
                "backlog_cost": float(row.backlog_cost),
                "fixed_order_cost": float(row.fixed_order_cost),
                "purchase_cost": float(row.purchase_cost),
                "week_cost": float(row.week_cost),
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
    """`§3.2`'s evaluation order, first match wins.

    The room-existence check has already happened (a caller reaches this
    function only once a finished `games` row was found). This function only
    decides *who* may have it: the live room's secret, or -- when there is no
    live room, or the secret is absent or wrong -- the registered host's own
    bearer token. Neither credential is used to probe the other: a wrong
    secret and a missing bearer token both fall through to the same 403.
    """
    if room is not None:
        stored = room.get("host_secret") or ""
        supplied = host_secret_header or ""
        if stored and supplied and hmac.compare_digest(stored, supplied):
            return

    if game.host_user_id is not None:
        claims = get_optional_firebase_user(authorization=authorization_header)
        if claims is not None:
            uid = claims.get("uid")
            if uid:
                user_id = db.execute(
                    select(User.id).where(User.firebase_uid == uid)
                ).scalar_one_or_none()
                if user_id is not None and user_id == game.host_user_id:
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
    a results URL is shareable (`§3.1`).
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
    """CSV (default) or JSON of the complete record. Host-only (`§3.2`)."""
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

        results = build_results_response(db, game)
        weeks = build_week_export_rows(db, game)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to export game for room %s.", room_code)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None

    filename = f"beery-{room_code}-{game.finished_at:%Y%m%d}.csv"
    if format == "json":
        return JSONResponse(content=_export_service.to_json(results, weeks))

    csv_text = _export_service.to_csv(weeks)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/claim", response_model=ClaimResponse)
def claim_games(
    payload: ClaimRequest,
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
) -> ClaimResponse:
    """Attribute a guest's finished games to the signed-in caller (`§3.3`)."""
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
