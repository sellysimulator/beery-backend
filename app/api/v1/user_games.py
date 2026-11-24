"""The caller's own profile aggregate, match history and hosted games
(spec §9.9).

This is a second router under ``/users``: ``app/api/v1/users.py`` holds the
``/users/upsert`` and ``/users/me`` routes. FastAPI composes both routers,
discovered independently by ``app/api/v1/__init__.py`` (**D19**), because two
routers may share a path prefix.

Every route here requires a verified Firebase token and answers only for the
caller's own data -- there is no route that takes a user id, so a caller can
never enumerate or read anyone else's profile or match list.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Select, Subquery, func, select
from sqlalchemy.orm import Session

from ...core.enums import Role
from ...db.session import get_db
from ...models.game import Game, GameConfigRow, Participant, UserStats, Week
from ...schemas.results import (
    HostedGamesResponse,
    HostedGameSummary,
    MatchHistoryResponse,
    MatchSummary,
    ResultsResponse,
    UserStatsResponse,
)
from ...services.user_service import UserService
from ..deps import get_current_firebase_user
from .games import build_results_response, find_game_by_public_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])

_INTERNAL_ERROR_DETAIL = "Internal server error."
_GAME_NOT_FOUND_DETAIL = "Game not found."

_MIN_PAGE_SIZE = 1
_MAX_PAGE_SIZE = 100

_user_service = UserService()

_ZERO_STATS = UserStatsResponse(
    games_played=0,
    weeks_played=0,
    total_cost=0.0,
    avg_cost_per_week=0.0,
    bullwhip_avg=None,
    best_game_public_id=None,
    games_as_retailer=0,
    games_as_wholesaler=0,
    games_as_distributor=0,
    games_as_factory=0,
)


def _resolve_user_id(db: Session, firebase_uid: str) -> int | None:
    user = _user_service.get_by_firebase_uid(db, firebase_uid)
    return user.id if user is not None else None


def _last_week_cost_subquery() -> Subquery:
    """Each `(game_id, role)`'s final `cumulative_cost` -- the same value
    `RoleResult.total_cost` reads off the same rows in `/results` -- reached
    with `MAX(week)` rather than a per-game recompute of `compute_stats`, so
    a 25-game listing costs one join, not 25 statements
    (`[HARD-WON]`).
    """
    last_week = (
        select(
            Week.game_id.label("game_id"),
            Week.role.label("role"),
            func.max(Week.week).label("max_week"),
        )
        .group_by(Week.game_id, Week.role)
        .subquery()
    )
    return (
        select(
            Week.game_id.label("game_id"),
            Week.role.label("role"),
            Week.cumulative_cost.label("total_cost"),
        )
        .join(
            last_week,
            (Week.game_id == last_week.c.game_id)
            & (Week.role == last_week.c.role)
            & (Week.week == last_week.c.max_week),
        )
        .subquery()
    )


def _match_history_base(user_id: int) -> Select[Any]:
    """The caller's played rows, one per `(game, role)`, unordered and
    unpaginated -- shared by the count and the data query so both agree on
    exactly which rows qualify. Seats only: the host has no participant row
    (D23), so hosting never puts a game here.
    """
    last_week_cost = _last_week_cost_subquery()
    return (
        select(
            Game.public_id,
            Game.room_code,
            Game.finished_at,
            Participant.role,
            Game.weeks_played,
            Game.chain_total_cost,
            Participant.bullwhip_ratio,
            GameConfigRow.preset_name,
            last_week_cost.c.total_cost,
        )
        .select_from(Participant)
        .join(Game, Game.id == Participant.game_id)
        .outerjoin(GameConfigRow, GameConfigRow.game_id == Game.id)
        .outerjoin(
            last_week_cost,
            (last_week_cost.c.game_id == Game.id)
            & (last_week_cost.c.role == Participant.role),
        )
        .where(Participant.user_id == user_id, Participant.role.is_not(None))
    )


@router.get("/me/stats", response_model=UserStatsResponse)
def get_my_stats(
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
) -> UserStatsResponse:
    """The caller's aggregate statistics. A user with no `user_stats` row --
    including one who has never even upserted a profile -- gets a
    zero-filled response with HTTP 200, not a 404.
    """
    try:
        user_id = _resolve_user_id(db, claims["uid"])
        stats_row = db.get(UserStats, user_id) if user_id is not None else None
        # The best game goes out by `public_id`: no internal id is on the
        # wire (D22), and the profile opens it as `/profile/games/{public_id}`.
        best_game_public_id = (
            db.execute(
                select(Game.public_id).where(Game.id == stats_row.best_game_id)
            ).scalar_one_or_none()
            if stats_row is not None and stats_row.best_game_id is not None
            else None
        )
    except Exception:
        logger.exception("Failed to read stats for the caller.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None

    if stats_row is None:
        return _ZERO_STATS

    return UserStatsResponse(
        games_played=stats_row.games_played,
        weeks_played=stats_row.weeks_played,
        total_cost=float(stats_row.total_cost),
        avg_cost_per_week=float(stats_row.avg_cost_per_week),
        bullwhip_avg=(
            float(stats_row.bullwhip_avg)
            if stats_row.bullwhip_avg is not None
            else None
        ),
        best_game_public_id=best_game_public_id,
        games_as_retailer=stats_row.games_as_retailer,
        games_as_wholesaler=stats_row.games_as_wholesaler,
        games_as_distributor=stats_row.games_as_distributor,
        games_as_factory=stats_row.games_as_factory,
    )


@router.get("/me/games", response_model=MatchHistoryResponse)
def get_my_games(
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
    page: int = 1,
    page_size: int = 20,
) -> MatchHistoryResponse:
    """The caller's match history, newest first, paginated.

    `page_size` is clamped to `[1, 100]` rather than rejected. Exactly three
    statements regardless of how many games the caller has played: resolve
    the caller's `users.id`, one `COUNT`, and one joined, grouped data query
    -- never a query per game (`[HARD-WON]`).
    """
    page = max(page, 1)
    page_size = min(max(page_size, _MIN_PAGE_SIZE), _MAX_PAGE_SIZE)

    try:
        user_id = _resolve_user_id(db, claims["uid"])
        if user_id is None:
            return MatchHistoryResponse(
                matches=[], total=0, page=page, page_size=page_size
            )

        base = _match_history_base(user_id)
        total = db.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar_one()
        rows = db.execute(
            base.order_by(Game.finished_at.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        ).all()
    except Exception:
        logger.exception("Failed to read match history for the caller.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None

    matches = [
        MatchSummary(
            public_id=row.public_id,
            room_code=row.room_code,
            finished_at=row.finished_at,
            role=Role(row.role),
            weeks_played=row.weeks_played,
            total_cost=float(row.total_cost) if row.total_cost is not None else 0.0,
            bullwhip_ratio=(
                float(row.bullwhip_ratio) if row.bullwhip_ratio is not None else None
            ),
            chain_total_cost=float(row.chain_total_cost),
            preset_name=row.preset_name,
        )
        for row in rows
    ]
    return MatchHistoryResponse(
        matches=matches, total=total, page=page, page_size=page_size
    )


@router.get("/me/hosted-games", response_model=HostedGamesResponse)
def get_my_hosted_games(
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
    page: int = 1,
    page_size: int = 20,
) -> HostedGamesResponse:
    """The games the caller hosted while signed in, newest first, paginated
    exactly as `/me/games` is (D23).

    The only reader of `games.host_user_id`. It lists; it grants nothing:
    each entry opens on the open results page by `public_id`, and no export,
    claim or authority follows from being on it. Three statements whatever
    the count: the caller's `users.id`, one `COUNT` and one data query whose
    seat count is a correlated subquery, never a query per game.
    """
    page = max(page, 1)
    page_size = min(max(page_size, _MIN_PAGE_SIZE), _MAX_PAGE_SIZE)

    try:
        user_id = _resolve_user_id(db, claims["uid"])
        if user_id is None:
            return HostedGamesResponse(
                games=[], total=0, page=page, page_size=page_size
            )

        total = db.execute(
            select(func.count()).select_from(Game).where(Game.host_user_id == user_id)
        ).scalar_one()
        # Correlated, so it counts only the page's games, through
        # `ix_participants_game_id_role`.
        seats = (
            select(func.count())
            .where(Participant.game_id == Game.id)
            .correlate(Game)
            .scalar_subquery()
        )
        rows = db.execute(
            select(
                Game.public_id,
                Game.room_code,
                Game.finished_at,
                Game.ended_early,
                seats.label("player_count"),
            )
            .where(Game.host_user_id == user_id)
            .order_by(Game.finished_at.desc(), Game.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        ).all()
    except Exception:
        logger.exception("Failed to read hosted games for the caller.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None

    games = [
        HostedGameSummary(
            public_id=row.public_id,
            room_code=row.room_code,
            finished_at=row.finished_at,
            player_count=int(row.player_count),
            ended_early=row.ended_early,
        )
        for row in rows
    ]
    return HostedGamesResponse(games=games, total=total, page=page, page_size=page_size)


@router.get("/me/games/{public_id}", response_model=ResultsResponse)
def get_my_game(
    public_id: str,
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
) -> ResultsResponse:
    """One of the caller's own games, in full, by its `public_id`: a game they
    played a seat in. Never by an internal id (D22).

    404 -- never 403 -- when the caller did not play it, identical to a
    non-existent id, so this route is never a game-existence oracle. Hosting
    a game does not open it here (D23): a hosted game is on the open results
    page by `public_id`.

    One account may hold two seats of one game (a guest seat in one tab, a
    signed-in one in another, then a claim), so the check is an existence
    test, never a single-row read.
    """
    try:
        user_id = _resolve_user_id(db, claims["uid"])
        game = find_game_by_public_id(db, public_id) if user_id is not None else None
        played = game is not None and (
            db.execute(
                select(Participant.id)
                .where(
                    Participant.game_id == game.id,
                    Participant.user_id == user_id,
                    Participant.role.is_not(None),
                )
                .limit(1)
            ).first()
            is not None
        )
        if game is None or not played:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=_GAME_NOT_FOUND_DETAIL
            )
        return build_results_response(db, game)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to read game %s for the caller.", public_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None
