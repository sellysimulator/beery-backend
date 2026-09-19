"""Career statistics for registered players (`14-end-of-game-persistence.md §3.7`).

`recompute_user_stats` rebuilds the whole `user_stats` row from scratch --
never incrementally, because an incremental counter that drifts is
undetectable (`§3.7`) -- using at most three SQL statements regardless of how
many games the user has played: one query for the games they occupied a role
in, one grouped query for each game's final cost, and one upsert. Ranking
happens in Python.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import bindparam, select, text
from sqlalchemy.orm import Session

from ..core.enums import ROLE_ORDER, Role
from ..models.user import User
from .db_service import round_money as _money

logger = logging.getLogger(__name__)

__all__ = ["StatsService", "get_stats_service"]


# One row per game the user occupied a role in, joined against `games` for
# `weeks_played`. `p.role IS NOT NULL` excludes the host, who never holds one.
_GAMES_FOR_USER = text("""
    SELECT p.game_id AS game_id, p.role AS role, p.bullwhip_ratio AS bullwhip_ratio,
           g.weeks_played AS weeks_played
    FROM participants p
    JOIN games g ON g.id = p.game_id
    WHERE p.user_id = :user_id AND p.role IS NOT NULL
    """)

# The ONE grouped query (`§3.7`, `[HARD-WON]`): `cumulative_cost` never
# decreases (`07-game-engine.md` acceptance criterion 13), so its MAX over a
# game and role is exactly the final week's value, without needing to know
# each game's `weeks_played` in the WHERE clause.
_FINAL_COST_BY_GAME_ROLE = text("""
    SELECT game_id, role, MAX(cumulative_cost) AS final_cost
    FROM weeks
    WHERE game_id IN :game_ids
    GROUP BY game_id, role
    """).bindparams(bindparam("game_ids", expanding=True))

_UPSERT_USER_STATS = text("""
    INSERT INTO user_stats (
        user_id, games_played, weeks_played, total_cost, avg_cost_per_week,
        best_game_id, bullwhip_avg, games_as_retailer, games_as_wholesaler,
        games_as_distributor, games_as_factory
    ) VALUES (
        :user_id, :games_played, :weeks_played, :total_cost, :avg_cost_per_week,
        :best_game_id, :bullwhip_avg, :games_as_retailer, :games_as_wholesaler,
        :games_as_distributor, :games_as_factory
    )
    ON DUPLICATE KEY UPDATE
        games_played = VALUES(games_played),
        weeks_played = VALUES(weeks_played),
        total_cost = VALUES(total_cost),
        avg_cost_per_week = VALUES(avg_cost_per_week),
        best_game_id = VALUES(best_game_id),
        bullwhip_avg = VALUES(bullwhip_avg),
        games_as_retailer = VALUES(games_as_retailer),
        games_as_wholesaler = VALUES(games_as_wholesaler),
        games_as_distributor = VALUES(games_as_distributor),
        games_as_factory = VALUES(games_as_factory),
        updated_at = CURRENT_TIMESTAMP
    """)


class StatsService:
    """Rebuilds `user_stats` (`§3.7`)."""

    def recompute_user_stats(self, db: Session, user_id: int) -> None:
        """Rebuild the `user_stats` row from scratch with ONE grouped query.

        A no-op, not a crash, for a `user_id` that no longer exists (the
        account was deleted): `participants.user_id` is `ON DELETE SET NULL`,
        so `_GAMES_FOR_USER` then matches nothing and there is nothing to
        write -- but writing a zero row regardless would violate
        `user_stats`'s foreign key, so that case is checked explicitly rather
        than assumed from an empty result.
        """
        game_rows = db.execute(_GAMES_FOR_USER, {"user_id": user_id}).mappings().all()

        if not game_rows:
            exists = db.execute(
                select(User.id).where(User.id == user_id)
            ).scalar_one_or_none()
            if exists is None:
                return
            final_cost_by_game_role: dict[tuple[int, str], Decimal] = {}
        else:
            game_ids = sorted({int(row["game_id"]) for row in game_rows})
            cost_rows = (
                db.execute(_FINAL_COST_BY_GAME_ROLE, {"game_ids": game_ids})
                .mappings()
                .all()
            )
            final_cost_by_game_role = {
                (int(row["game_id"]), row["role"]): row["final_cost"]
                for row in cost_rows
            }

        games_played = len(game_rows)
        weeks_played_total = 0
        total_cost = Decimal(0)
        best_game_id: int | None = None
        best_cost_per_week: Decimal | None = None
        role_counts: dict[Role, int] = {role: 0 for role in ROLE_ORDER}
        bullwhip_values: list[float] = []

        for row in game_rows:
            game_id = int(row["game_id"])
            role_value = row["role"]
            weeks_played = int(row["weeks_played"] or 0)
            weeks_played_total += weeks_played

            final_cost = final_cost_by_game_role.get((game_id, role_value))
            game_cost = (
                Decimal(str(final_cost)) if final_cost is not None else Decimal(0)
            )
            total_cost += game_cost

            if weeks_played > 0:
                cost_per_week = game_cost / weeks_played
                if best_cost_per_week is None or cost_per_week < best_cost_per_week:
                    best_cost_per_week = cost_per_week
                    best_game_id = game_id

            if role_value is not None:
                role_counts[Role(role_value)] += 1

            bullwhip_ratio = row["bullwhip_ratio"]
            if bullwhip_ratio is not None:
                bullwhip_values.append(float(bullwhip_ratio))

        avg_cost_per_week = (
            total_cost / weeks_played_total if weeks_played_total else Decimal(0)
        )
        bullwhip_avg = (
            Decimal(str(sum(bullwhip_values) / len(bullwhip_values)))
            if bullwhip_values
            else None
        )

        db.execute(
            _UPSERT_USER_STATS,
            {
                "user_id": user_id,
                "games_played": games_played,
                "weeks_played": weeks_played_total,
                "total_cost": _money(total_cost),
                "avg_cost_per_week": _money(avg_cost_per_week),
                "best_game_id": best_game_id,
                "bullwhip_avg": bullwhip_avg,
                "games_as_retailer": role_counts[Role.RETAILER],
                "games_as_wholesaler": role_counts[Role.WHOLESALER],
                "games_as_distributor": role_counts[Role.DISTRIBUTOR],
                "games_as_factory": role_counts[Role.FACTORY],
            },
        )
        db.commit()

    def recompute_for_game(self, db: Session, game_id: int) -> None:
        """Recompute stats for every registered participant of this game."""
        user_ids = (
            db.execute(
                text(
                    "SELECT DISTINCT user_id FROM participants "
                    "WHERE game_id = :game_id AND user_id IS NOT NULL"
                ),
                {"game_id": game_id},
            )
            .scalars()
            .all()
        )
        for user_id in user_ids:
            self.recompute_user_stats(db, int(user_id))


_stats_service: StatsService | None = None


def get_stats_service() -> StatsService:
    """Module-level singleton, mirroring `state_service.get_state_service()`."""
    global _stats_service
    if _stats_service is None:
        _stats_service = StatsService()
    return _stats_service
