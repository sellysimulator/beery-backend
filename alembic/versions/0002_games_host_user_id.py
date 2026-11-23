"""Record a signed-in host on `games.host_user_id`; stop storing HOST rows

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-20

The cross-game host-records standard (spec D23). A finished game records who
hosted it only when the host was signed in, as `games.host_user_id`, and the
host no longer has a `participants` row.

Upgrade:

1. Add `games.host_user_id` (nullable, FK `users.id` ON DELETE SET NULL, index
   `ix_games_host_user_id`).
2. Copy the `user_id` of every HOST participant row that has one into its
   game's `host_user_id`. That includes a former guest host who claimed the
   game: that account did claim it.
3. Delete every HOST participant row, guest ones included. A guest host is
   never recorded, and the HOST rows never counted in history or stats (both
   read rows with a role), so no `user_stats` row changes.
4. Drop `games.host_display_name`: nothing read it from the database. The
   live lobby's `host_display_name` lives in the room document, not here.

`participants.role` stays nullable and `participant_type` stays a plain
string: no new row is written with a NULL role or type `HOST`, but tightening
either would make one unforeseen seat with no role fail the whole end-of-game
write, and the string column is spec §16's spectator seam.

Downgrade restores the 0001 schema. `host_display_name` comes back NOT NULL
with every existing game set to 'Host' (the names were dropped and cannot be
recovered). A HOST participant row is re-created only for a game with a
`host_user_id`; the guest-host rows (`guest_identity`) and every host's chosen
name are gone for good.
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
    op.add_column("games", sa.Column("host_user_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_games_host_user_id",
        "games",
        "users",
        ["host_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_games_host_user_id", "games", ["host_user_id"], unique=False)

    op.execute("""
        UPDATE games AS g
        SET host_user_id = p.user_id
        FROM participants AS p
        WHERE p.game_id = g.id
          AND p.participant_type = 'HOST'
          AND p.user_id IS NOT NULL
        """)
    op.execute("DELETE FROM participants WHERE participant_type = 'HOST'")

    op.drop_column("games", "host_display_name")


def downgrade() -> None:
    op.add_column(
        "games",
        sa.Column(
            "host_display_name",
            sa.String(length=24),
            nullable=False,
            server_default="Host",
        ),
    )
    # 0001 has no database default on a fact column.
    op.alter_column("games", "host_display_name", server_default=None)

    # Only a signed-in host can be restored; a guest host's identity is gone.
    op.execute("""
        INSERT INTO participants (
            game_id, alias, role, display_name, participant_type, is_bot,
            user_id, guest_identity, bullwhip_ratio
        )
        SELECT id, 'HOST', NULL, 'Host', 'HOST', false, host_user_id, NULL, NULL
        FROM games
        WHERE host_user_id IS NOT NULL
        """)

    op.drop_index("ix_games_host_user_id", table_name="games")
    op.drop_constraint("fk_games_host_user_id", "games", type_="foreignkey")
    op.drop_column("games", "host_user_id")
