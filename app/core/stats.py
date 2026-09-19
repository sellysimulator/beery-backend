"""End-of-game statistics (`07-game-engine.md` §3.10, `00-decisions.md` D12).

Pure: this module imports only from `app.core` and the standard library. It
performs no I/O, reads no clock and holds no RNG.

`compute_stats` reads a list of `WeekRecord`s and the demand series and never
touches live agent state, so it works identically on a finished in-memory engine
and on rows loaded back out of MySQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .enums import ROLE_ORDER, Role
from .game_engine import WeekRecord

__all__ = ["GameStats", "RoleStats", "compute_stats", "population_variance"]


@dataclass(frozen=True)
class RoleStats:
    """One role's summary over the weeks actually played."""

    role: Role
    total_cost: float
    peak_inventory: int
    peak_backlog: int
    weeks_in_backlog: int
    order_variance: float
    bullwhip_ratio: float | None  # None when Var(demand) == 0
    fill_rate: float | None  # None when total obligation == 0
    average_order: float


@dataclass(frozen=True)
class GameStats:
    """The whole chain's summary."""

    weeks_played: int
    demand_variance: float
    chain_total_cost: float
    per_role: dict[Role, RoleStats]


def population_variance(values: Sequence[float]) -> float:
    """Population variance, `ddof = 0`.

    Returns 0.0 for a sequence of length 0 or 1. A *sample* variance
    (`ddof = 1`) would make every bullwhip ratio subtly wrong:
    `population_variance([2, 4])` is `1.0`, not `2.0`.
    """
    count = len(values)
    if count < 2:
        return 0.0
    mean = sum(values) / count
    return sum((value - mean) ** 2 for value in values) / count


def compute_stats(
    history: list[WeekRecord],
    demand_series: list[int],
    weeks_played: int,
) -> GameStats:
    """Summarise a played game.

    `weeks_played` comes from `GameEngine.weeks_played` and is the number of
    *complete* weeks: a week abandoned by `end_early()` has no records and must
    not be counted, or its absent orders would distort the bullwhip ratio.

    `total_cost` reads the final `cumulative_cost` rather than re-summing
    `week_cost`, so a discrepancy between the two is detectable.
    """
    demand_variance = population_variance(demand_series[:weeks_played])

    per_role: dict[Role, RoleStats] = {}
    for role in ROLE_ORDER:
        records = [record for record in history if record.role is role]
        orders = [record.order for record in records]
        order_variance = population_variance(orders)

        # Exactly the CONSTANT generator: an undefined ratio, reported as
        # `None` rather than as infinity, zero or an exception (D12).
        bullwhip_ratio = (
            None if demand_variance == 0 else order_variance / demand_variance
        )

        total_obligation = sum(record.obligation for record in records)
        total_shipped = sum(record.shipped for record in records)
        fill_rate = None if total_obligation == 0 else total_shipped / total_obligation

        per_role[role] = RoleStats(
            role=role,
            total_cost=records[-1].cumulative_cost if records else 0.0,
            peak_inventory=max(
                (record.closing_inventory for record in records), default=0
            ),
            peak_backlog=max((record.closing_backlog for record in records), default=0),
            weeks_in_backlog=sum(1 for record in records if record.closing_backlog > 0),
            order_variance=order_variance,
            bullwhip_ratio=bullwhip_ratio,
            fill_rate=fill_rate,
            average_order=(sum(orders) / len(orders)) if orders else 0.0,
        )

    return GameStats(
        weeks_played=weeks_played,
        demand_variance=demand_variance,
        chain_total_cost=sum(stats.total_cost for stats in per_role.values()),
        per_role=per_role,
    )
