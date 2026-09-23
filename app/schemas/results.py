"""Response and request models for the results, export and profile API
(``15-results-and-export-api.md`` §2).

These shapes are FROZEN: the shipped frontend (``21 §2.0``, ``22 §2.0``)
already expects exactly these field names and this nullability. Money fields
are declared ``float`` -- never ``Decimal`` -- so pydantic coerces whatever
``Decimal`` the database hands back into a plain JSON number; a ``Decimal``
leaking into a response body breaks the frontend's charts.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from ..core.enums import Role

__all__ = [
    "ClaimRequest",
    "ClaimResponse",
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
    started_at: datetime
    finished_at: datetime
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
    best_game_id: int | None
    games_as_retailer: int
    games_as_wholesaler: int
    games_as_distributor: int
    games_as_factory: int


class MatchSummary(BaseModel):
    game_id: int
    room_code: str
    finished_at: datetime
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
