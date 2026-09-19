"""Black-box tests for section 14 -- ``app/services/claim_service.py``.

Covers ``14-end-of-game-persistence.md §4`` acceptance criteria 16-19 and
``§5`` failure mode 10. Criteria 1-11, 20 and failure modes 1, 2, 3, 5, 6,
7, 8, 9, 11 live in ``test_db_service.py``; 12-15 and the stats half of 12
in ``test_stats_service.py``. ``app/services/claim_service.py`` itself is
never opened.

Guest-owned fixture rows are produced through ``DbService.persist_game``,
exactly as a real finished game would leave them, rather than by hand.
Failure mode 10's edge case is the one exception: a participant row with
*both* a real `user_id` and a matching `guest_identity` is not a shape any
public call can produce (the claim itself clears `guest_identity` on
success), so it is built with a raw insert purely to prove
``claim_guest_results``'s guard is `user_id IS NULL`, not "guest_identity
matches".
"""

from __future__ import annotations

import pytest

from app.core.enums import Role
from app.models.game import Participant, UserStats
from app.models.user import User
from app.services.claim_service import ClaimService
from app.services.db_service import DbService, build_snapshot

from .conftest import (
    insert_game,
    insert_row,
    insert_user,
    make_config,
    make_participant,
    make_room_document,
    run_full_game,
)

pytestmark = pytest.mark.dbschema


def _persist_game_for_guest(
    db,
    *,
    guest_identity: str,
    role: Role,
    duration_weeks: int = 8,
    seed: int = 0,
):
    config = make_config(duration_weeks=duration_weeks)
    engine = run_full_game(config, seed=seed)
    participants = {"P1": make_participant("P1", identity=guest_identity, role=role)}
    room = make_room_document(config, engine, participants=participants)
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)
    db.commit()
    return game_id


# ---------------------------------------------------------------------------
# Acceptance criteria 16 and 17
# ---------------------------------------------------------------------------


def test_ac16_ac17_claim_attributes_every_matching_row_and_is_idempotent(db):
    guest_identity = "guest_11111111-2222-3333-4444-555555555555"
    game1_id = _persist_game_for_guest(
        db, guest_identity=guest_identity, role=Role.RETAILER, seed=1
    )
    game2_id = _persist_game_for_guest(
        db, guest_identity=guest_identity, role=Role.WHOLESALER, seed=2
    )
    # An unrelated guest row must not be touched by this claim.
    other_game_id = _persist_game_for_guest(
        db,
        guest_identity="guest_unrelated-0000-0000-0000-000000000000",
        role=Role.RETAILER,
        seed=3,
    )

    firebase_uid = "uid-claimer"
    count = ClaimService().claim_guest_results(db, firebase_uid, guest_identity)
    db.commit()

    assert count == 2  # AC 16: every matching row, and only those rows

    user = db.query(User).filter_by(firebase_uid=firebase_uid).one()
    # Each persisted game also carries its own synthesised HOST participant
    # row (alias unrelated to "P1"), so the claimed *player* rows are
    # identified by alias, not by "every row for these two games".
    claimed_rows = (
        db.query(Participant)
        .filter(
            Participant.game_id.in_([game1_id, game2_id]), Participant.alias == "P1"
        )
        .all()
    )
    assert len(claimed_rows) == 2
    assert all(row.user_id == user.id for row in claimed_rows)
    assert all(row.guest_identity is None for row in claimed_rows)

    other_row = db.query(Participant).filter_by(game_id=other_game_id, alias="P1").one()
    assert other_row.user_id is None
    assert other_row.guest_identity == "guest_unrelated-0000-0000-0000-000000000000"

    # AC 17: a second claim of the same, now-fully-claimed guest id is a no-op.
    second_count = ClaimService().claim_guest_results(
        db, "uid-second-claimer", guest_identity
    )
    db.commit()
    assert second_count == 0
    unchanged_rows = (
        db.query(Participant)
        .filter(
            Participant.game_id.in_([game1_id, game2_id]), Participant.alias == "P1"
        )
        .all()
    )
    assert all(
        row.user_id == user.id for row in unchanged_rows
    )  # still the first claimer


# ---------------------------------------------------------------------------
# Acceptance criterion 18
# ---------------------------------------------------------------------------


def test_ac18_unknown_guest_id_returns_zero_and_creates_no_rows(db):
    """AC 18, read together with §2's frozen docstring for
    `claim_guest_results` -- "Returns 0 and writes nothing when the guest
    identity is unknown" -- which is unambiguous that "no rows" covers a
    `users` row too, not just participant attribution. (§3.8's own numbered
    steps list "resolve `users.id`... create it if absent" unconditionally,
    *before* step 2 looks for a matching `guest_identity`; read literally
    that would create a `users` row even here, in tension with both AC 18
    and §2's own docstring. The frozen §2 docstring governs.)
    """
    firebase_uid = "uid-never-seen-before"
    count = ClaimService().claim_guest_results(
        db, firebase_uid, "guest_00000000-aaaa-bbbb-cccc-dddddddddddd"
    )
    db.commit()

    assert count == 0
    assert db.query(User).filter_by(firebase_uid=firebase_uid).count() == 0


# ---------------------------------------------------------------------------
# Acceptance criterion 19
# ---------------------------------------------------------------------------


def test_ac19_claim_recomputes_the_claiming_users_stats(db):
    guest_identity = "guest_stats-check-0000-0000-000000000000"
    _persist_game_for_guest(
        db, guest_identity=guest_identity, role=Role.RETAILER, seed=11
    )

    firebase_uid = "uid-stats-claimer"
    ClaimService().claim_guest_results(db, firebase_uid, guest_identity)
    db.commit()

    user = db.query(User).filter_by(firebase_uid=firebase_uid).one()
    stats_row = db.get(UserStats, user.id)
    assert stats_row is not None
    assert stats_row.games_played == 1


# ---------------------------------------------------------------------------
# Failure mode 10
# ---------------------------------------------------------------------------


def test_fm10_a_row_with_a_non_null_user_id_is_not_re_attributed(db):
    original_owner_id = insert_user(db.connection(), firebase_uid="uid-original-owner")
    game_id = insert_game(db.connection())
    guest_identity = "guest_edge-case-0000-0000-0000-000000000000"
    insert_row(
        db.connection(),
        "participants",
        game_id=game_id,
        alias="P1",
        role=Role.RETAILER.value,
        display_name="Ana",
        participant_type="PLAYER",
        is_bot=False,
        user_id=original_owner_id,
        guest_identity=guest_identity,
    )
    db.commit()

    count = ClaimService().claim_guest_results(db, "uid-second-claimer", guest_identity)
    db.commit()

    assert count == 0
    row = db.query(Participant).filter_by(game_id=game_id).one()
    assert row.user_id == original_owner_id
    assert row.guest_identity == guest_identity  # untouched, not just re-set
