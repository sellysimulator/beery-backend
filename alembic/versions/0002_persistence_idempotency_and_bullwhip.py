"""participants bullwhip_ratio and games idempotency constraint

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-19

Two authorised late additions by section 14
(`14-end-of-game-persistence.md §3.3`, `§3.5a`). Section 13 owns
`app/models/game.py` and this migration directory; both are the kind of
authorised late edit section 12 makes to `lobby.py`.

1. `participants.bullwhip_ratio`, `DECIMAL(10, 4) NULL` -- the value is
   `stats.per_role[role].bullwhip_ratio`: NULL for a bot, for the host, and
   for any role in a CONSTANT-demand game. Stored so `user_stats.bullwhip_avg`
   can be rebuilt with one grouped SQL query instead of a second,
   SQL-language implementation of `population_variance`.

2. `UNIQUE (room_code, started_at)` on `games` -- the database-level backstop
   for `persist_game`'s idempotency guarantee. The `weeks` unique constraint
   on `(game_id, week, role)` cannot serve this: two concurrent `persist_game`
   calls for the same finished game insert two `games` rows with two
   *different* `game_id` values, so their `weeks` rows never collide. A plain
   check-then-insert with no backstop duplicates under real concurrency.
   `persist_game` now reads: look up, insert, and on an `IntegrityError` roll
   back and re-read, returning the id the winning transaction wrote.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "participants",
        sa.Column("bullwhip_ratio", sa.Numeric(10, 4), nullable=True),
    )
    op.create_unique_constraint(
        "uq_games_room_code_started_at", "games", ["room_code", "started_at"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_games_room_code_started_at", "games", type_="unique")
    op.drop_column("participants", "bullwhip_ratio")
