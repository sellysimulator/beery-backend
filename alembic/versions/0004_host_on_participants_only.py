"""games.host_user_id removed: the HOST participant row records who hosted

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22

Who hosted a game was stored twice, in `games.host_user_id` and on the game's
`HOST` row of `participants`, both resolved from the same uid at the end-of-game
write. A guest claim attributes `participants` rows only, so a guest host who
signed in and claimed their results was the host on one record and not the
other, and export, which read `games.host_user_id`, refused them. The
`participants` row is now the one record, and export reads it
(`15-results-and-export-api.md §3.2`).

Before the column goes, a game whose `host_user_id` is set while its `HOST` row
carries neither identity has the user copied onto that row, so no registered
host loses export. The two were written together, so this should find nothing.
Every stored game has a `HOST` row: the end-of-game write has always made one.

The downgrade restores the column from the `HOST` rows, claims included.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE participants p
            JOIN games g ON g.id = p.game_id
            SET p.user_id = g.host_user_id
            WHERE p.participant_type = 'HOST'
              AND p.user_id IS NULL
              AND p.guest_identity IS NULL
              AND g.host_user_id IS NOT NULL
            """
        )
    )
    op.drop_constraint("fk_games_host_user_id", "games", type_="foreignkey")
    op.drop_index("ix_games_host_user_id", table_name="games")
    op.drop_column("games", "host_user_id")


def downgrade() -> None:
    op.add_column("games", sa.Column("host_user_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_games_host_user_id",
        "games",
        "users",
        ["host_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_games_host_user_id", "games", ["host_user_id"])
    op.execute(
        sa.text(
            """
            UPDATE games g
            JOIN participants p
              ON p.game_id = g.id AND p.participant_type = 'HOST'
            SET g.host_user_id = p.user_id
            """
        )
    )
