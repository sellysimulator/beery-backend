"""weeks.week_cost renamed total_week_cost; game_configs.allow_negative_orders dropped

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-02

Two changes from one review of how costs and orders read on screen:

- `weeks.week_cost` held holding + backlog + fixed order + purchase cost, while
  every screen showed a "week cost" that was holding + backlog only. The
  screens now show "Holding & backlog" and "Order cost" as two figures, and the
  stored sum is renamed `total_week_cost` so nobody reads it as either. A
  rename, not a copy: MySQL's `ALTER TABLE ... CHANGE` keeps every value.
- `allow_negative_orders` is gone from the game. It never worked (a negative
  order raised inside the pipeline and froze the week), orders now have a floor
  of 0, and nothing reads the column. Stored rooms and configs that still carry
  the key load and ignore it (`03-game-config.md` §2).

The downgrade renames the column back and restores `allow_negative_orders`
as `false` for every stored game, which is what every game that finished could
have had: a game with it on and a negative order never reached the end.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MONEY = sa.Numeric(12, 2)


def upgrade() -> None:
    op.alter_column(
        "weeks",
        "week_cost",
        new_column_name="total_week_cost",
        existing_type=_MONEY,
        existing_nullable=False,
    )
    op.drop_column("game_configs", "allow_negative_orders")


def downgrade() -> None:
    op.add_column(
        "game_configs",
        sa.Column(
            "allow_negative_orders",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("game_configs", "allow_negative_orders", server_default=None)
    op.alter_column(
        "weeks",
        "total_week_cost",
        new_column_name="week_cost",
        existing_type=_MONEY,
        existing_nullable=False,
    )
