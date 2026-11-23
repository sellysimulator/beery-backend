"""Response and request models for the results, export and profile API
(spec §9.8, §9.5, §9.9).

These shapes are FROZEN: the shipped frontend
already expects exactly these field names and this nullability. Money fields
are declared ``float`` -- never ``Decimal`` -- so pydantic coerces whatever
``Decimal`` the database hands back into a plain JSON number; a ``Decimal``
leaking into a response body breaks the frontend's charts. Timestamps are
``UtcDateTime``: ISO-8601 UTC with a ``Z`` suffix.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..core.enums import Role
from .types import UtcDateTime

__all__ = [
    "ClaimRequest",
    "ClaimResponse",
    "HostedGameSummary",
    "HostedGamesResponse",
    "MatchHistoryResponse",
    "MatchSummary",
    "ResultsResponse",
    "RoleResult",
    "UserStatsResponse",
]


class RoleResult(BaseModel):
    role: Role
    display_name: str
    is_bot: bool
    total_cost: float
    peak_inventory: int
    peak_backlog: int
    weeks_in_backlog: int
    order_variance: float
    bullwhip_ratio: float | None
    fill_rate: float | None
    average_order: float
    orders: list[int]  # week 1..weeks_played
    inventory: list[int]
    backlog: list[int]
    cumulative_cost: list[float]


class ResultsResponse(BaseModel):
    # Additive: the stable id for `GET /games/id/{public_id}/results`. A room
    # code is recycled once the room expires; this is not.
    public_id: str
    room_code: str
    weeks_played: int
    duration_weeks: int
    ended_early: bool
    currency_symbol: str
    started_at: UtcDateTime
    finished_at: UtcDateTime
    demand_series: list[int]
    chain_total_cost: float
    demand_variance: float
    per_role: list[RoleResult]  # in ROLE_ORDER
    preset_name: str | None


class ClaimRequest(BaseModel):
    guest_identity: str


class ClaimResponse(BaseModel):
    claimed: int


class UserStatsResponse(BaseModel):
    games_played: int
    weeks_played: int
    total_cost: float
    avg_cost_per_week: float
    bullwhip_avg: float | None
    # The best game's `public_id` (never an internal id, D22), or None.
    best_game_public_id: str | None
    games_as_retailer: int
    games_as_wholesaler: int
    games_as_distributor: int
    games_as_factory: int


class MatchSummary(BaseModel):
    # The game's permanent id: the profile opens `/profile/games/{public_id}`.
    public_id: str
    room_code: str
    finished_at: UtcDateTime
    role: Role
    weeks_played: int
    total_cost: float
    bullwhip_ratio: float | None
    chain_total_cost: float
    preset_name: str | None


class MatchHistoryResponse(BaseModel):
    matches: list[MatchSummary]
    total: int
    page: int
    page_size: int


class HostedGameSummary(BaseModel):
    """One game the caller hosted while signed in (D23). Opened by
    `public_id` on the open results page; nothing here is the caller's own
    figure, because the host played no seat."""

    public_id: str
    room_code: str
    finished_at: UtcDateTime
    # Seats in the game (one `participants` row each, bots included).
    player_count: int
    ended_early: bool


class HostedGamesResponse(BaseModel):
    games: list[HostedGameSummary]
    total: int
    page: int
    page_size: int
