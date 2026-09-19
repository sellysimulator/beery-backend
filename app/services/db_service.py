"""Move a finished game out of Redis and into MySQL (`14-end-of-game-persistence.md`).

This is the only path that writes game data to the database (**D4**). Nothing
here runs under the Redis lock: `build_snapshot` copies everything it needs
out of the room document while the lock is still held by the caller
(`app/services/game_service.py::finish_game`), and everything below this
point in the call graph is lock-free (`§3.1`, `§2.1`).

`persist_game` is idempotent by `(room_code, started_at)` and all-or-nothing:
one transaction, so a failure part-way leaves no half-written game (`§3.3`).
Every child table with more than one row per game is written with a single
batched raw insert, never a loop (`§3.2`, `[HARD-WON]`) -- and that raw insert
never names `created_at` or `updated_at` on `participants` or `role_configs`,
the two batched tables that carry `TimestampMixin`; `weeks` and
`demand_series` have no timestamp columns at all (`13-db-models-and-
migrations.md §2.7`, §2.8`).

Money crosses from `float` to `DECIMAL(12, 2)` here, and only here, rounded
half-up at the moment of the insert (`00-conventions.md §4`, `§3.5`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.config_models import GameConfig
from ..core.enums import ROLE_ORDER, Role
from ..core.game_engine import GameEngine, WeekRecord
from ..core.stats import GameStats, compute_stats
from ..models.game import DemandConfigRow, Game, GameConfigRow
from ..models.user import User

logger = logging.getLogger(__name__)

__all__ = [
    "DbService",
    "GameSnapshot",
    "ParticipantSnapshot",
    "build_snapshot",
    "round_money",
]


# --------------------------------------------------------------------------- #
# Numeric conversion at the persistence boundary.
# --------------------------------------------------------------------------- #


def round_money(value: float | Decimal) -> Decimal:
    """Round half-up to two decimals -- the one place `float` crosses to
    `DECIMAL(12, 2)` (`00-conventions.md §4`, `§3.5`). The engine's
    accumulated values are never rounded mid-game, so `sum(week_cost)` may
    differ from `cumulative_cost` by less than a cent; that is expected and
    is not "fixed" here by re-summing.

    `stats_service.py` imports this rather than reimplementing it, so the
    rounding rule is defined exactly once."""
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


_money = round_money


def _decimal(value: float | Decimal | None) -> Decimal | None:
    """A plain, exact `float` -> `Decimal` conversion for the non-money
    fixed-point columns (`DECIMAL(6, 4)`, `DECIMAL(10, 4)`). MySQL rounds a
    value to the column's declared scale on insert; this only avoids the
    binary-float noise of passing the `float` through directly."""
    if value is None:
        return None
    return Decimal(str(value))


# --------------------------------------------------------------------------- #
# The snapshot (FROZEN shape, `§2`).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ParticipantSnapshot:
    """One row of `participants`. `identity` is copied straight out of the
    room document: `None` for a role that was always a bot, and the original
    human's identity for a role later substituted -- which is exactly what
    `§3.7` needs to keep counting that game under the human's seat."""

    alias: str
    display_name: str
    role: Role | None
    participant_type: str  # "HOST" | "PLAYER"
    is_bot: bool
    identity: str | None  # firebase uid, or guest_<uuid4>, or None for a bot


@dataclass(frozen=True)
class GameSnapshot:
    """Everything persistence needs, copied OUT of the room document so the
    Redis lock can be released before the database is touched."""

    room_code: str
    host_identity: str | None
    host_display_name: str
    seed: int
    started_at: datetime
    finished_at: datetime
    weeks_played: int
    ended_early: bool
    config: GameConfig
    demand_series: list[int]
    history: list[WeekRecord]
    participants: list[ParticipantSnapshot]
    stats: GameStats


def _parse_timestamp(value: Any) -> datetime:
    """Room document timestamps are `datetime.isoformat()` strings (`09-
    state-service.md §2`). A finished game always has both set; a missing one
    is a bug elsewhere and must not be silently invented.

    Truncated to whole seconds: `games.started_at` is a plain MySQL
    `DATETIME`, which stores no fractional seconds, so a microsecond-bearing
    Python value round-trips lossily. Truncating here, before it is ever
    written or compared, keeps the idempotency lookup in `persist_game`
    (`§3.3` -- keyed on `(room_code, started_at)`) matching what a second
    call, and what the database, actually hold.
    """
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if not isinstance(value, str) or not value:
        raise ValueError("Room document is missing a required timestamp.")
    return datetime.fromisoformat(value).replace(microsecond=0)


def build_snapshot(room: dict, config: GameConfig, engine: GameEngine) -> GameSnapshot:
    """The one place `ended_early`, `duration_weeks`'s source and the
    `order_qty` rename are decided (`§2.1`). Everything here is a pure read of
    `room` and `engine`; nothing is mutated.

    The host is not a key of `room["participants"]` -- section 11 never gives
    it an alias, so it may not appear in either sid-to-alias map
    (`00-conventions.md §2`). It is still one of the "up to five" rows
    `13-db-models-and-migrations.md §2.6` describes, so it is synthesised here
    with the fixed alias ``"HOST"`` and `role=None`.
    """
    weeks_played = engine.weeks_played
    ended_early = weeks_played < config.duration_weeks
    stats = compute_stats(engine.history, engine.demand_series, weeks_played)

    participants: list[ParticipantSnapshot] = [
        ParticipantSnapshot(
            alias="HOST",
            display_name=room.get("host_display_name") or "Host",
            role=None,
            participant_type="HOST",
            is_bot=False,
            identity=room.get("host_identity"),
        )
    ]
    for alias, participant in room["participants"].items():
        role_raw = participant.get("role")
        participants.append(
            ParticipantSnapshot(
                alias=alias,
                display_name=participant.get("display_name") or alias,
                role=Role(role_raw) if role_raw else None,
                participant_type="PLAYER",
                is_bot=bool(participant.get("is_bot")),
                identity=participant.get("identity"),
            )
        )

    return GameSnapshot(
        room_code=room["room_code"],
        host_identity=room.get("host_identity"),
        host_display_name=room.get("host_display_name") or "Host",
        seed=int(room["seed"]),
        started_at=_parse_timestamp(room.get("started_at")),
        finished_at=_parse_timestamp(room.get("finished_at")),
        weeks_played=weeks_played,
        ended_early=ended_early,
        config=config,
        demand_series=list(engine.demand_series[:weeks_played]),
        history=list(engine.history),
        participants=participants,
        stats=stats,
    )


# --------------------------------------------------------------------------- #
# Column lists for the batched raw inserts.
# --------------------------------------------------------------------------- #

_ROLE_CONFIG_COLUMNS = (
    "game_id",
    "role",
    "initial_inventory",
    "initial_backlog",
    "shipping_delay_weeks",
    "information_delay_weeks",
    "initial_pipeline_quantity",
    "initial_order_in_pipeline",
    "holding_cost_per_unit_week",
    "backlog_cost_per_unit_week",
    "fixed_order_cost",
    "unit_purchase_cost",
    "starting_capital",
    "production_delay_weeks",
    "production_capacity_per_week",
)

_PARTICIPANT_COLUMNS = (
    "game_id",
    "alias",
    "role",
    "display_name",
    "participant_type",
    "is_bot",
    "user_id",
    "guest_identity",
    "bullwhip_ratio",
)

_DEMAND_SERIES_COLUMNS = ("game_id", "week", "quantity")

_WEEK_COLUMNS = (
    "game_id",
    "week",
    "role",
    "opening_inventory",
    "opening_backlog",
    "arrived",
    "incoming_order",
    "obligation",
    "shipped",
    "unfulfilled",
    "closing_inventory",
    "closing_backlog",
    "supply_line_after",
    "orders_in_flight_after",
    "order_qty",
    "was_bot",
    "was_forced",
    "holding_cost",
    "backlog_cost",
    "fixed_order_cost",
    "purchase_cost",
    "week_cost",
    "cumulative_cost",
    "production_started",
    "production_queued",
)


def _insert_statement(table: str, columns: tuple[str, ...]) -> Any:
    column_list = ", ".join(columns)
    placeholders = ", ".join(f":{name}" for name in columns)
    return text(f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})")


_INSERT_ROLE_CONFIGS = _insert_statement("role_configs", _ROLE_CONFIG_COLUMNS)
_INSERT_PARTICIPANTS = _insert_statement("participants", _PARTICIPANT_COLUMNS)
_INSERT_DEMAND_SERIES = _insert_statement("demand_series", _DEMAND_SERIES_COLUMNS)
_INSERT_WEEKS = _insert_statement("weeks", _WEEK_COLUMNS)


class DbService:
    """Writes a finished game and everything under it (`§2`, `§3`)."""

    def persist_game(self, db: Session, snapshot: GameSnapshot) -> int:
        """Write a finished game and everything under it. Returns `games.id`.

        Idempotent by `(room_code, started_at)`: a second call for the same
        snapshot returns the existing id and writes nothing (`§3.3`). All or
        nothing: any failure part-way rolls back the whole transaction, so a
        partially written game never exists.

        The lookup below is the fast path; `games` also carries a real
        `UNIQUE (room_code, started_at)` constraint as the database-level
        backstop (`§3.3`). Two callers racing to persist the same finished
        game (a retried request, a doubly rendered results page) both pass
        the lookup, but only one `INSERT` wins the constraint -- the loser's
        `IntegrityError` is caught, its transaction rolled back, and the
        winner's id is read back and returned instead of raising (failure
        mode 6).
        """
        existing_id = self._find_existing(db, snapshot)
        if existing_id is not None:
            return existing_id

        try:
            game_id = self._insert_game(db, snapshot)
            self._insert_game_config(db, game_id, snapshot.config)
            self._insert_demand_config(db, game_id, snapshot.config)
            self._insert_role_configs(db, game_id, snapshot.config)
            self._insert_participants(db, game_id, snapshot)
            self._insert_demand_series(db, game_id, snapshot.demand_series)
            self._insert_weeks(db, game_id, snapshot.history)
            db.commit()
            return game_id
        except IntegrityError:
            db.rollback()
            # Only a genuine (room_code, started_at) collision is recoverable
            # here: an unrelated constraint violation (e.g. AC 7's forced
            # duplicate `weeks` row) leaves no `games` row to find, so
            # `_find_existing` returns `None` and the original error is
            # re-raised rather than swallowed.
            existing_id = self._find_existing(db, snapshot)
            if existing_id is not None:
                return existing_id
            raise
        except Exception:
            db.rollback()
            raise

    def _find_existing(self, db: Session, snapshot: GameSnapshot) -> int | None:
        existing_id = db.execute(
            select(Game.id).where(
                Game.room_code == snapshot.room_code,
                Game.started_at == snapshot.started_at,
            )
        ).scalar_one_or_none()
        return int(existing_id) if existing_id is not None else None

    def delete_game(self, db: Session, game_id: int) -> None:
        game = db.get(Game, game_id)
        if game is None:
            return
        db.delete(game)
        db.commit()

    # --- one row each ---------------------------------------------------- #

    def _insert_game(self, db: Session, snapshot: GameSnapshot) -> int:
        host_user_id: int | None = None
        if snapshot.host_identity is not None:
            host_user_id = db.execute(
                select(User.id).where(User.firebase_uid == snapshot.host_identity)
            ).scalar_one_or_none()

        game = Game(
            room_code=snapshot.room_code,
            host_user_id=host_user_id,
            host_display_name=snapshot.host_display_name,
            rng_seed=snapshot.seed,
            duration_weeks=snapshot.config.duration_weeks,
            weeks_played=snapshot.weeks_played,
            ended_early=snapshot.ended_early,
            started_at=snapshot.started_at,
            finished_at=snapshot.finished_at,
            chain_total_cost=_money(snapshot.stats.chain_total_cost),
        )
        db.add(game)
        db.flush()
        assert game.id is not None
        return int(game.id)

    def _insert_game_config(
        self, db: Session, game_id: int, config: GameConfig
    ) -> None:
        visibility = config.visibility
        bot = config.bot
        db.add(
            GameConfigRow(
                game_id=game_id,
                stage_count=config.stage_count,
                pause_on_disconnect=config.pause_on_disconnect,
                bot_fill_empty_roles=config.bot_fill_empty_roles,
                currency_symbol=config.currency_symbol,
                role_assignment_mode=config.role_assignment_mode.value,
                preset_name=config.preset_name,
                show_true_customer_demand_to_all=(
                    visibility.show_true_customer_demand_to_all
                ),
                show_neighbour_inventory=visibility.show_neighbour_inventory,
                show_all_inventories=visibility.show_all_inventories,
                show_supply_line_prominently=visibility.show_supply_line_prominently,
                show_running_cost_to_players=visibility.show_running_cost_to_players,
                show_leaderboard_during_game=visibility.show_leaderboard_during_game,
                max_order_quantity=visibility.max_order_quantity,
                allow_negative_orders=visibility.allow_negative_orders,
                theta=_decimal(bot.theta),
                alpha=_decimal(bot.alpha),
                beta=_decimal(bot.beta),
                target_stock_multiplier=_decimal(bot.target_stock_multiplier),
            )
        )

    def _insert_demand_config(
        self, db: Session, game_id: int, config: GameConfig
    ) -> None:
        """One row, the union of every generator's parameters (`13 §2.5`).

        Dumping through `model_dump(mode="json")` and reading every possible
        column with `.get` -- rather than branching on `kind` -- is what keeps
        every generator's *other* columns `NULL` for free (acceptance
        criterion 9): a `ConstantDemand` simply has no `step_week` key to
        find.
        """
        fields: dict[str, Any] = config.demand.model_dump(mode="json")
        kind = fields.pop("kind")
        min_value = fields.get("min")
        max_value = fields.get("max")
        db.add(
            DemandConfigRow(
                game_id=game_id,
                kind=kind,
                value=fields.get("value"),
                initial_value=fields.get("initial_value"),
                step_week=fields.get("step_week"),
                step_value=fields.get("step_value"),
                slope_per_week=_decimal(fields.get("slope_per_week")),
                start_week=fields.get("start_week"),
                cap=fields.get("cap"),
                base=fields.get("base"),
                amplitude=fields.get("amplitude"),
                period_weeks=fields.get("period_weeks"),
                phase=_decimal(fields.get("phase")),
                distribution=fields.get("distribution"),
                mean=_decimal(fields.get("mean")),
                stdev=_decimal(fields.get("stdev")),
                min_value=min_value,
                max_value=max_value,
            )
        )

    # --- batched, more than one row --------------------------------------- #

    def _insert_role_configs(
        self, db: Session, game_id: int, config: GameConfig
    ) -> None:
        rows = []
        for role in ROLE_ORDER:
            role_config = config.role_config(role)
            is_factory = role is Role.FACTORY
            rows.append(
                {
                    "game_id": game_id,
                    "role": role.value,
                    "initial_inventory": role_config.initial_inventory,
                    "initial_backlog": role_config.initial_backlog,
                    "shipping_delay_weeks": role_config.shipping_delay_weeks,
                    "information_delay_weeks": role_config.information_delay_weeks,
                    "initial_pipeline_quantity": (
                        role_config.initial_pipeline_quantity
                    ),
                    "initial_order_in_pipeline": (
                        role_config.initial_order_in_pipeline
                    ),
                    "holding_cost_per_unit_week": _money(
                        role_config.holding_cost_per_unit_week
                    ),
                    "backlog_cost_per_unit_week": _money(
                        role_config.backlog_cost_per_unit_week
                    ),
                    "fixed_order_cost": _money(role_config.fixed_order_cost),
                    "unit_purchase_cost": _money(role_config.unit_purchase_cost),
                    "starting_capital": _money(role_config.starting_capital),
                    "production_delay_weeks": (
                        role_config.production_delay_weeks if is_factory else None
                    ),
                    "production_capacity_per_week": (
                        role_config.production_capacity_per_week if is_factory else None
                    ),
                }
            )
        db.execute(_INSERT_ROLE_CONFIGS, rows)

    def _insert_participants(
        self, db: Session, game_id: int, snapshot: GameSnapshot
    ) -> None:
        rows = []
        for participant in snapshot.participants:
            user_id, guest_identity = self._resolve_attribution(db, participant)
            rows.append(
                {
                    "game_id": game_id,
                    "alias": participant.alias,
                    "role": participant.role.value if participant.role else None,
                    "display_name": participant.display_name,
                    "participant_type": participant.participant_type,
                    "is_bot": participant.is_bot,
                    "user_id": user_id,
                    "guest_identity": guest_identity,
                    "bullwhip_ratio": self._bullwhip_ratio(participant, snapshot.stats),
                }
            )
        db.execute(_INSERT_PARTICIPANTS, rows)

    def _resolve_attribution(
        self, db: Session, participant: ParticipantSnapshot
    ) -> tuple[int | None, str | None]:
        """`§3.6`. A bot is `(None, None)`. A Firebase uid with no `users` row
        -- a signed-in user who never hit `/users/upsert` -- is stored exactly
        as a guest would be: `user_id` `None` (failure mode 11)."""
        if participant.is_bot or participant.identity is None:
            return None, None
        if participant.identity.startswith("guest_"):
            return None, participant.identity
        user_id = db.execute(
            select(User.id).where(User.firebase_uid == participant.identity)
        ).scalar_one_or_none()
        return user_id, None

    @staticmethod
    def _bullwhip_ratio(
        participant: ParticipantSnapshot, stats: GameStats
    ) -> Decimal | None:
        """`§3.5a`: NULL for a bot, for the host (no role), and for any role
        whose ratio is `None` (a `CONSTANT`-demand game)."""
        if participant.is_bot or participant.role is None:
            return None
        role_stats = stats.per_role.get(participant.role)
        if role_stats is None or role_stats.bullwhip_ratio is None:
            return None
        return _decimal(role_stats.bullwhip_ratio)

    def _insert_demand_series(
        self, db: Session, game_id: int, series: list[int]
    ) -> None:
        if not series:
            return
        rows = [
            {"game_id": game_id, "week": index + 1, "quantity": quantity}
            for index, quantity in enumerate(series)
        ]
        db.execute(_INSERT_DEMAND_SERIES, rows)

    def _insert_weeks(
        self, db: Session, game_id: int, history: list[WeekRecord]
    ) -> None:
        if not history:
            return
        rows = [
            {
                "game_id": game_id,
                "week": record.week,
                "role": record.role.value,
                "opening_inventory": record.opening_inventory,
                "opening_backlog": record.opening_backlog,
                "arrived": record.arrived,
                "incoming_order": record.incoming_order,
                "obligation": record.obligation,
                "shipped": record.shipped,
                "unfulfilled": record.unfulfilled,
                "closing_inventory": record.closing_inventory,
                "closing_backlog": record.closing_backlog,
                "supply_line_after": record.supply_line_after,
                "orders_in_flight_after": record.orders_in_flight_after,
                # `order` is a MySQL reserved word -- the one rename (`§2.1`).
                "order_qty": record.order,
                "was_bot": record.was_bot,
                "was_forced": record.was_forced,
                "holding_cost": _money(record.holding_cost),
                "backlog_cost": _money(record.backlog_cost),
                "fixed_order_cost": _money(record.fixed_order_cost),
                "purchase_cost": _money(record.purchase_cost),
                "week_cost": _money(record.week_cost),
                "cumulative_cost": _money(record.cumulative_cost),
                "production_started": record.production_started,
                "production_queued": record.production_queued,
            }
            for record in history
        ]
        db.execute(_INSERT_WEEKS, rows)


_db_service: DbService | None = None


def get_db_service() -> DbService:
    """Module-level singleton, mirroring `state_service.get_state_service()`."""
    global _db_service
    if _db_service is None:
        _db_service = DbService()
    return _db_service
