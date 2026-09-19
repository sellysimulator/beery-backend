"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-19

Creates the nine tables of `13-db-models-and-migrations.md §2`: `users` (whose
model belongs to section 02) plus the eight durable game-record tables in
`app/models/game.py`.

There is no `Base.metadata.create_all()` anywhere in the application -- this
revision is the only thing that builds the schema. `downgrade()` is real and
drops the tables in reverse dependency order, because a revision with a `pass`
downgrade cannot be tested.

Every `TimestampMixin` column carries `server_default=func.now()`: section 14
writes with batched raw inserts that bypass ORM-side defaults, and a NOT NULL
column without a database-side default fails under `sql_mode=STRICT_ALL_TABLES`
with "Field 'created_at' doesn't have a default value".
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Money. Section 14 rounds to two decimals at this boundary and nowhere else.
MONEY = sa.Numeric(12, 2)
# Sterman anchor-and-adjust weights.
BOT_PARAM = sa.Numeric(6, 4)
# Demand parameters that are neither money nor counts.
DEMAND_SCALAR = sa.Numeric(10, 4)


def _timestamps() -> list[sa.Column]:
    """The two `TimestampMixin` columns, database-side defaults included."""
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    ]


def upgrade() -> None:
    """Create the nine tables, their indexes and their constraints."""
    # --- 2.1 users (model owned by section 02) ---------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("firebase_uid", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(24), nullable=True),
        sa.Column("email", sa.String(200), nullable=True),
        sa.Column("photo_url", sa.String(500), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_users_firebase_uid", "users", ["firebase_uid"], unique=True)

    # --- 2.2 games -------------------------------------------------------
    op.create_table(
        "games",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # Indexed, not unique: codes are reused after a room expires.
        sa.Column("room_code", sa.String(8), nullable=False),
        # Nullable: a guest may host (D3).
        sa.Column("host_user_id", sa.Integer(), nullable=True),
        sa.Column("host_display_name", sa.String(24), nullable=False),
        sa.Column("rng_seed", sa.BigInteger(), nullable=False),
        sa.Column("duration_weeks", sa.Integer(), nullable=False),
        sa.Column("weeks_played", sa.Integer(), nullable=False),
        sa.Column(
            "ended_early", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chain_total_cost", MONEY, nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["host_user_id"],
            ["users.id"],
            name="fk_games_host_user_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_games_room_code", "games", ["room_code"])
    op.create_index("ix_games_host_user_id", "games", ["host_user_id"])

    # --- 2.3 game_configs ------------------------------------------------
    op.create_table(
        "game_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("stage_count", sa.Integer(), nullable=False),
        sa.Column("pause_on_disconnect", sa.Boolean(), nullable=False),
        sa.Column("bot_fill_empty_roles", sa.Boolean(), nullable=False),
        sa.Column("currency_symbol", sa.String(4), nullable=False),
        sa.Column("role_assignment_mode", sa.String(20), nullable=False),
        sa.Column("preset_name", sa.String(32), nullable=True),
        # VisibilityConfig
        sa.Column("show_true_customer_demand_to_all", sa.Boolean(), nullable=False),
        sa.Column("show_neighbour_inventory", sa.Boolean(), nullable=False),
        sa.Column("show_all_inventories", sa.Boolean(), nullable=False),
        sa.Column("show_supply_line_prominently", sa.Boolean(), nullable=False),
        sa.Column("show_running_cost_to_players", sa.Boolean(), nullable=False),
        sa.Column("show_leaderboard_during_game", sa.Boolean(), nullable=False),
        sa.Column("max_order_quantity", sa.Integer(), nullable=True),
        sa.Column("allow_negative_orders", sa.Boolean(), nullable=False),
        # BotConfig
        sa.Column("theta", BOT_PARAM, nullable=False),
        sa.Column("alpha", BOT_PARAM, nullable=False),
        sa.Column("beta", BOT_PARAM, nullable=False),
        sa.Column("target_stock_multiplier", BOT_PARAM, nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_game_configs_game_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("game_id", name="uq_game_configs_game_id"),
    )

    # --- 2.4 role_configs ------------------------------------------------
    op.create_table(
        "role_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("initial_inventory", sa.Integer(), nullable=False),
        sa.Column("initial_backlog", sa.Integer(), nullable=False),
        sa.Column("shipping_delay_weeks", sa.Integer(), nullable=False),
        sa.Column("information_delay_weeks", sa.Integer(), nullable=False),
        sa.Column("initial_pipeline_quantity", sa.Integer(), nullable=False),
        sa.Column("initial_order_in_pipeline", sa.Integer(), nullable=False),
        sa.Column("holding_cost_per_unit_week", MONEY, nullable=False),
        sa.Column("backlog_cost_per_unit_week", MONEY, nullable=False),
        sa.Column("fixed_order_cost", MONEY, nullable=False),
        sa.Column("unit_purchase_cost", MONEY, nullable=False),
        sa.Column("starting_capital", MONEY, nullable=False),
        # Factory-only, NULL for the other three roles.
        sa.Column("production_delay_weeks", sa.Integer(), nullable=True),
        sa.Column("production_capacity_per_week", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_role_configs_game_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("game_id", "role", name="uq_role_configs_game_id_role"),
    )

    # --- 2.5 demand_configs ----------------------------------------------
    op.create_table(
        "demand_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.Integer(), nullable=True),
        sa.Column("initial_value", sa.Integer(), nullable=True),
        sa.Column("step_week", sa.Integer(), nullable=True),
        sa.Column("step_value", sa.Integer(), nullable=True),
        sa.Column("slope_per_week", DEMAND_SCALAR, nullable=True),
        sa.Column("start_week", sa.Integer(), nullable=True),
        sa.Column("cap", sa.Integer(), nullable=True),
        sa.Column("base", sa.Integer(), nullable=True),
        sa.Column("amplitude", sa.Integer(), nullable=True),
        sa.Column("period_weeks", sa.Integer(), nullable=True),
        sa.Column("phase", DEMAND_SCALAR, nullable=True),
        sa.Column("distribution", sa.String(16), nullable=True),
        sa.Column("mean", DEMAND_SCALAR, nullable=True),
        sa.Column("stdev", DEMAND_SCALAR, nullable=True),
        # `min` and `max` are MySQL reserved words.
        sa.Column("min_value", sa.Integer(), nullable=True),
        sa.Column("max_value", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_demand_configs_game_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("game_id", name="uq_demand_configs_game_id"),
    )

    # --- 2.6 participants -------------------------------------------------
    op.create_table(
        "participants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(4), nullable=False),
        sa.Column("role", sa.String(16), nullable=True),
        sa.Column("display_name", sa.String(24), nullable=False),
        sa.Column("participant_type", sa.String(12), nullable=False),
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("guest_identity", sa.String(64), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_participants_game_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_participants_user_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_participants_game_id_role", "participants", ["game_id", "role"])
    op.create_index("ix_participants_user_id", "participants", ["user_id"])

    # --- 2.7 demand_series -------------------------------------------------
    op.create_table(
        "demand_series",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("week", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_demand_series_game_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("game_id", "week", name="uq_demand_series_game_id_week"),
    )

    # --- 2.8 weeks ---------------------------------------------------------
    op.create_table(
        "weeks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("week", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("opening_inventory", sa.Integer(), nullable=False),
        sa.Column("opening_backlog", sa.Integer(), nullable=False),
        sa.Column("arrived", sa.Integer(), nullable=False),
        sa.Column("incoming_order", sa.Integer(), nullable=False),
        sa.Column("obligation", sa.Integer(), nullable=False),
        sa.Column("shipped", sa.Integer(), nullable=False),
        sa.Column("unfulfilled", sa.Integer(), nullable=False),
        sa.Column("closing_inventory", sa.Integer(), nullable=False),
        sa.Column("closing_backlog", sa.Integer(), nullable=False),
        sa.Column("supply_line_after", sa.Integer(), nullable=False),
        sa.Column("orders_in_flight_after", sa.Integer(), nullable=False),
        # `order` is a MySQL reserved word.
        sa.Column("order_qty", sa.Integer(), nullable=False),
        sa.Column("was_bot", sa.Boolean(), nullable=False),
        sa.Column("was_forced", sa.Boolean(), nullable=False),
        sa.Column("holding_cost", MONEY, nullable=False),
        sa.Column("backlog_cost", MONEY, nullable=False),
        sa.Column("fixed_order_cost", MONEY, nullable=False),
        sa.Column("purchase_cost", MONEY, nullable=False),
        sa.Column("week_cost", MONEY, nullable=False),
        sa.Column("cumulative_cost", MONEY, nullable=False),
        sa.Column("production_started", sa.Integer(), nullable=True),
        sa.Column("production_queued", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["game_id"], ["games.id"], name="fk_weeks_game_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "game_id", "week", "role", name="uq_weeks_game_id_week_role"
        ),
    )
    op.create_index("ix_weeks_game_id_role", "weeks", ["game_id", "role"])

    # --- 2.9 user_stats ----------------------------------------------------
    op.create_table(
        "user_stats",
        sa.Column("user_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("games_played", sa.Integer(), nullable=False),
        sa.Column("weeks_played", sa.Integer(), nullable=False),
        sa.Column("total_cost", MONEY, nullable=False),
        sa.Column("avg_cost_per_week", MONEY, nullable=False),
        sa.Column("best_game_id", sa.Integer(), nullable=True),
        sa.Column("bullwhip_avg", sa.Numeric(10, 4), nullable=True),
        sa.Column("games_as_retailer", sa.Integer(), nullable=False),
        sa.Column("games_as_wholesaler", sa.Integer(), nullable=False),
        sa.Column("games_as_distributor", sa.Integer(), nullable=False),
        sa.Column("games_as_factory", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_stats_user_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["best_game_id"],
            ["games.id"],
            name="fk_user_stats_best_game_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_user_stats_best_game_id", "user_stats", ["best_game_id"])


def downgrade() -> None:
    """Drop the nine tables in reverse dependency order.

    Indexes are not dropped separately: `DROP TABLE` takes them with it, and
    MySQL refuses to drop an index that a foreign key still needs
    ("Cannot drop index ...: needed in a foreign key constraint"), which is
    exactly what an index on an FK column is.
    """
    op.drop_table("user_stats")
    op.drop_table("weeks")
    op.drop_table("demand_series")
    op.drop_table("participants")
    op.drop_table("demand_configs")
    op.drop_table("role_configs")
    op.drop_table("game_configs")
    op.drop_table("games")
    op.drop_table("users")
