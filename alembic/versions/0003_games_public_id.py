"""games public_id

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23

`games.public_id`, `VARCHAR(32) NOT NULL UNIQUE` -- the id a public results
URL carries (`/results/g/:gameId`). A room code cannot serve: codes are
recycled once a room expires, so an old link would silently resolve to a newer
game. `games.id` cannot serve either: it is sequential, so exposing it would
let anyone walk every game ever played. `public_id` is a uuid4 hex string,
unguessable and permanent.

Existing rows are backfilled from Python's `uuid4`, not MySQL's `UUID()`,
which is a time-based v1 UUID and therefore partly guessable. The column is
added nullable, filled, then tightened, so the upgrade works on a populated
table.
"""

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("games", sa.Column("public_id", sa.String(32), nullable=True))

    connection = op.get_bind()
    game_ids = connection.execute(sa.text("SELECT id FROM games")).scalars().all()
    for game_id in game_ids:
        connection.execute(
            sa.text("UPDATE games SET public_id = :public_id WHERE id = :id"),
            {"public_id": uuid4().hex, "id": game_id},
        )

    op.alter_column(
        "games", "public_id", existing_type=sa.String(32), nullable=False
    )
    op.create_unique_constraint("uq_games_public_id", "games", ["public_id"])


def downgrade() -> None:
    op.drop_constraint("uq_games_public_id", "games", type_="unique")
    op.drop_column("games", "public_id")
