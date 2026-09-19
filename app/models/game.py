"""The durable game record: the eight tables written when a game finishes.

Nothing here is written while a game is live -- Redis is authoritative for that
(**D4**). Section 14 batch-inserts these rows at game end and section 15 reads
them back.

Configuration is fully normalised (**D5**): one column per parameter, never a
JSON blob. The awkward case is `demand_configs`, which is the nullable union of
six generators' parameters; it is still normalised so that a query can filter on
`kind` together with a specific generator parameter.

`users` is section 02's model (`app/models/user.py`); it is imported here so the
two foreign keys that reference it resolve and so the whole schema is registered
on `Base.metadata` by importing this one module. The `users` **migration** lives
with this section's revision.

Column naming: `order`, `min` and `max` are MySQL reserved words, so the columns
are `order_qty`, `min_value` and `max_value`. A quoted identifier would be a
permanent tax on every raw query section 14 and 15 write.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin
from .user import User

__all__ = [
    "DemandConfigRow",
    "DemandSeries",
    "Game",
    "GameConfigRow",
    "Participant",
    "RoleConfigRow",
    "User",
    "UserStats",
    "Week",
]

# Money. The engine accumulates in float and rounds to two decimals only at this
# boundary; the models simply declare the type.
MONEY = Numeric(12, 2)
# Sterman anchor-and-adjust weights: 0.0 .. 10.0, four decimals of precision.
BOT_PARAM = Numeric(6, 4)
# Demand parameters that are not money and not counts (slope, phase, mean,
# stdev). Fixed-point rather than FLOAT so a round-trip is exact and so
# autogenerate has nothing to argue with.
DEMAND_SCALAR = Numeric(10, 4)


class Game(Base, TimestampMixin):
    """One finished game. The row every other table in this module hangs off."""

    __tablename__ = "games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Indexed but NOT unique: room codes are recycled once a room expires.
    room_code: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    # Nullable: a guest may host (**D3**), and deleting an account must not
    # destroy the record the other players are entitled to.
    host_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_games_host_user_id"),
        nullable=True,
    )
    host_display_name: Mapped[str] = mapped_column(String(24), nullable=False)
    rng_seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_weeks: Mapped[int] = mapped_column(Integer, nullable=False)
    # May be fewer than `duration_weeks`, after `end_game_early`.
    weeks_played: Mapped[int] = mapped_column(Integer, nullable=False)
    ended_early: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Denormalised so a match-history listing does not have to sum `weeks`.
    chain_total_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    __table_args__ = (Index("ix_games_host_user_id", "host_user_id"),)


class GameConfigRow(Base, TimestampMixin):
    """The game-level parameters of `GameConfig`, one column each.

    `duration_weeks` and `random_seed` live on `games` rather than here, because
    they are the two config fields a listing query actually filters on.
    """

    __tablename__ = "game_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("games.id", ondelete="CASCADE", name="fk_game_configs_game_id"),
        nullable=False,
    )

    stage_count: Mapped[int] = mapped_column(Integer, nullable=False)
    pause_on_disconnect: Mapped[bool] = mapped_column(Boolean, nullable=False)
    bot_fill_empty_roles: Mapped[bool] = mapped_column(Boolean, nullable=False)
    currency_symbol: Mapped[str] = mapped_column(String(4), nullable=False)
    role_assignment_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    preset_name: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- VisibilityConfig ---
    show_true_customer_demand_to_all: Mapped[bool] = mapped_column(
        Boolean, nullable=False
    )
    show_neighbour_inventory: Mapped[bool] = mapped_column(Boolean, nullable=False)
    show_all_inventories: Mapped[bool] = mapped_column(Boolean, nullable=False)
    show_supply_line_prominently: Mapped[bool] = mapped_column(Boolean, nullable=False)
    show_running_cost_to_players: Mapped[bool] = mapped_column(Boolean, nullable=False)
    show_leaderboard_during_game: Mapped[bool] = mapped_column(Boolean, nullable=False)
    max_order_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allow_negative_orders: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # --- BotConfig ---
    theta: Mapped[Decimal] = mapped_column(BOT_PARAM, nullable=False)
    alpha: Mapped[Decimal] = mapped_column(BOT_PARAM, nullable=False)
    beta: Mapped[Decimal] = mapped_column(BOT_PARAM, nullable=False)
    target_stock_multiplier: Mapped[Decimal] = mapped_column(BOT_PARAM, nullable=False)

    __table_args__ = (UniqueConstraint("game_id", name="uq_game_configs_game_id"),)


class RoleConfigRow(Base, TimestampMixin):
    """One row per role per game: every `RoleConfig` field, one column each."""

    __tablename__ = "role_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("games.id", ondelete="CASCADE", name="fk_role_configs_game_id"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    initial_inventory: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_backlog: Mapped[int] = mapped_column(Integer, nullable=False)
    shipping_delay_weeks: Mapped[int] = mapped_column(Integer, nullable=False)
    information_delay_weeks: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_pipeline_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_order_in_pipeline: Mapped[int] = mapped_column(Integer, nullable=False)

    holding_cost_per_unit_week: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    backlog_cost_per_unit_week: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fixed_order_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    unit_purchase_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    starting_capital: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    # Factory-only: NULL for RETAILER, WHOLESALER and DISTRIBUTOR.
    production_delay_weeks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    production_capacity_per_week: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    __table_args__ = (
        UniqueConstraint("game_id", "role", name="uq_role_configs_game_id_role"),
    )


class DemandConfigRow(Base, TimestampMixin):
    """The demand generator's parameters, as the union of all six generators.

    Every parameter column is nullable: only the ones belonging to `kind` are
    populated. `CUSTOM` stores nothing here -- its values are the
    `demand_series` rows, which every kind writes anyway.

    `min` and `max` are MySQL reserved words, hence `min_value` / `max_value`.
    """

    __tablename__ = "demand_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("games.id", ondelete="CASCADE", name="fk_demand_configs_game_id"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    # CONSTANT
    value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # STEP / RAMP
    initial_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    step_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    step_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    slope_per_week: Mapped[Decimal | None] = mapped_column(DEMAND_SCALAR, nullable=True)
    start_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # SEASONAL
    base: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amplitude: Mapped[int | None] = mapped_column(Integer, nullable=True)
    period_weeks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    phase: Mapped[Decimal | None] = mapped_column(DEMAND_SCALAR, nullable=True)
    # STOCHASTIC
    distribution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    mean: Mapped[Decimal | None] = mapped_column(DEMAND_SCALAR, nullable=True)
    stdev: Mapped[Decimal | None] = mapped_column(DEMAND_SCALAR, nullable=True)
    min_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_value: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (UniqueConstraint("game_id", name="uq_demand_configs_game_id"),)


class Participant(Base, TimestampMixin):
    """A host, player or bot that took part in a game.

    Exactly one of `user_id` and `guest_identity` is set for a human; both are
    NULL for a bot. `guest_identity` holds the server-only `guest_<uuid4>`
    string -- it is what attribution needs and what section 14's guest-claim
    flow looks the row up by. It is never returned by any endpoint.
    """

    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("games.id", ondelete="CASCADE", name="fk_participants_game_id"),
        nullable=False,
    )
    alias: Mapped[str] = mapped_column(String(4), nullable=False)
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    display_name: Mapped[str] = mapped_column(String(24), nullable=False)
    participant_type: Mapped[str] = mapped_column(String(12), nullable=False)
    is_bot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_participants_user_id"),
        nullable=True,
    )
    guest_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_participants_game_id_role", "game_id", "role"),
        Index("ix_participants_user_id", "user_id"),
    )


class DemandSeries(Base):
    """The realised customer demand, one row per played week.

    No `TimestampMixin`: this is bulk data and a timestamp per row is pure
    overhead.
    """

    __tablename__ = "demand_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("games.id", ondelete="CASCADE", name="fk_demand_series_game_id"),
        nullable=False,
    )
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("game_id", "week", name="uq_demand_series_game_id_week"),
    )


class Week(Base):
    """One `WeekRecord`: what one role did and was charged in one week.

    The source of truth for the results screen. `order` is a MySQL reserved
    word, so the decision column is `order_qty`. No `TimestampMixin`.
    """

    __tablename__ = "weeks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("games.id", ondelete="CASCADE", name="fk_weeks_game_id"),
        nullable=False,
    )
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    opening_inventory: Mapped[int] = mapped_column(Integer, nullable=False)
    opening_backlog: Mapped[int] = mapped_column(Integer, nullable=False)
    arrived: Mapped[int] = mapped_column(Integer, nullable=False)
    incoming_order: Mapped[int] = mapped_column(Integer, nullable=False)
    obligation: Mapped[int] = mapped_column(Integer, nullable=False)
    shipped: Mapped[int] = mapped_column(Integer, nullable=False)
    unfulfilled: Mapped[int] = mapped_column(Integer, nullable=False)
    closing_inventory: Mapped[int] = mapped_column(Integer, nullable=False)
    closing_backlog: Mapped[int] = mapped_column(Integer, nullable=False)
    supply_line_after: Mapped[int] = mapped_column(Integer, nullable=False)
    orders_in_flight_after: Mapped[int] = mapped_column(Integer, nullable=False)
    order_qty: Mapped[int] = mapped_column(Integer, nullable=False)

    was_bot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    was_forced: Mapped[bool] = mapped_column(Boolean, nullable=False)

    holding_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    backlog_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    fixed_order_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    purchase_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    week_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cumulative_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    # FACTORY only.
    production_started: Mapped[int | None] = mapped_column(Integer, nullable=True)
    production_queued: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("game_id", "week", "role", name="uq_weeks_game_id_week_role"),
        Index("ix_weeks_game_id_role", "game_id", "role"),
    )


class UserStats(Base, TimestampMixin):
    """Aggregate career statistics, recomputed by section 14 after each game."""

    __tablename__ = "user_stats"

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_user_stats_user_id"),
        primary_key=True,
        autoincrement=False,
    )
    games_played: Mapped[int] = mapped_column(Integer, nullable=False)
    weeks_played: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    avg_cost_per_week: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    best_game_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("games.id", ondelete="SET NULL", name="fk_user_stats_best_game_id"),
        nullable=True,
    )
    # A ratio, so precision matters more than range (**D12**); NULL when the
    # customer demand had zero variance.
    bullwhip_avg: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    games_as_retailer: Mapped[int] = mapped_column(Integer, nullable=False)
    games_as_wholesaler: Mapped[int] = mapped_column(Integer, nullable=False)
    games_as_distributor: Mapped[int] = mapped_column(Integer, nullable=False)
    games_as_factory: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("ix_user_stats_best_game_id", "best_game_id"),)
