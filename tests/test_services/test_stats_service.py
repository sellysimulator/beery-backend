"""Black-box tests for section 14 -- ``app/services/stats_service.py``.

Covers ``14-end-of-game-persistence.md §4`` acceptance criteria 12-15 and
``§5`` failure modes 4 and (the stats half of) 12. Criteria 1-11, 20 and
failure modes 1, 2, 3, 5, 6, 7, 8, 9, 11 live in ``test_db_service.py``;
16-19 and failure mode 10 in ``test_guest_claim.py``.

Fixture data is produced through ``DbService.persist_game`` -- the only
path that writes a `weeks`/`participants` row at all -- rather than by
hand-inserting rows, so a game's numbers here are exactly what a real game
would leave behind. ``app/services/stats_service.py`` itself is never
opened.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.enums import Role
from app.models.game import UserStats
from app.services.db_service import DbService, build_snapshot
from app.services.stats_service import StatsService

from .conftest import (
    count_statements,
    insert_user,
    make_config,
    make_participant,
    make_room_document,
    run_full_game,
)

pytestmark = pytest.mark.dbschema


def _persist_game_for_user(
    db,
    *,
    user_identity: str,
    role: Role,
    duration_weeks: int = 10,
    seed: int = 0,
    holding_cost_per_unit_week: float | None = None,
):
    """One finished game with ``user_identity`` playing ``role``.

    ``duration_weeks`` defaults to a multiple of 10 -- with the default
    holding/backlog rates (multiples of 0.5), `total_cost / weeks_played`
    always lands on an exact hundredth, so `avg_cost_per_week` comparisons
    below need no tolerance.
    """
    overrides = None
    if holding_cost_per_unit_week is not None:
        overrides = {
            role.value: {"holding_cost_per_unit_week": holding_cost_per_unit_week}
        }
    config = make_config(duration_weeks=duration_weeks, role_overrides=overrides)
    engine = run_full_game(config, seed=seed)
    participants = {"P1": make_participant("P1", identity=user_identity, role=role)}
    room = make_room_document(config, engine, participants=participants)
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)
    db.commit()
    final_cost = next(
        r.cumulative_cost for r in reversed(engine.history) if r.role is role
    )
    return game_id, engine, final_cost


# ---------------------------------------------------------------------------
# Acceptance criterion 12
# ---------------------------------------------------------------------------


def test_ac12_recompute_after_one_game(db):
    user_id = insert_user(db.connection(), firebase_uid="uid-single-game")
    db.commit()
    game_id, _engine, final_cost = _persist_game_for_user(
        db,
        user_identity="uid-single-game",
        role=Role.RETAILER,
        duration_weeks=10,
        seed=101,
    )

    StatsService().recompute_user_stats(db, user_id)
    db.commit()

    row = db.get(UserStats, user_id)
    assert row is not None
    assert row.games_played == 1
    assert row.weeks_played == 10
    expected_total = Decimal(str(round(final_cost, 2)))
    expected_avg = expected_total / Decimal(10)
    assert row.total_cost == expected_total
    assert row.avg_cost_per_week == expected_avg
    assert row.best_game_id == game_id
    assert row.games_as_retailer == 1
    assert row.games_as_wholesaler == 0


# ---------------------------------------------------------------------------
# Acceptance criterion 13
# ---------------------------------------------------------------------------


def test_ac13_recompute_after_three_games_picks_the_cheapest_as_best(db):
    user_id = insert_user(db.connection(), firebase_uid="uid-three-games")
    db.commit()

    # Same duration and role for all three; only the RETAILER's holding
    # cost differs, so cost-per-week is deterministic and strictly ordered:
    # 0.25 * 12 = 3.00/week < 0.50 * 12 = 6.00/week < 1.00 * 12 = 12.00/week.
    cheap_id, _, cheap_cost = _persist_game_for_user(
        db,
        user_identity="uid-three-games",
        role=Role.RETAILER,
        seed=1,
        holding_cost_per_unit_week=0.25,
    )
    _mid_id, _, mid_cost = _persist_game_for_user(
        db,
        user_identity="uid-three-games",
        role=Role.RETAILER,
        seed=2,
        holding_cost_per_unit_week=0.50,
    )
    _expensive_id, _, expensive_cost = _persist_game_for_user(
        db,
        user_identity="uid-three-games",
        role=Role.RETAILER,
        seed=3,
        holding_cost_per_unit_week=1.00,
    )
    assert cheap_cost < mid_cost < expensive_cost  # sanity on the fixture itself

    StatsService().recompute_user_stats(db, user_id)
    db.commit()

    row = db.get(UserStats, user_id)
    assert row.games_played == 3
    assert row.weeks_played == 30
    assert row.best_game_id == cheap_id
    assert row.games_as_retailer == 3
    total = (
        Decimal(str(round(cheap_cost, 2)))
        + Decimal(str(round(mid_cost, 2)))
        + Decimal(str(round(expensive_cost, 2)))
    )
    assert row.total_cost == total
    assert row.avg_cost_per_week == total / Decimal(30)


# ---------------------------------------------------------------------------
# Acceptance criterion 14
# ---------------------------------------------------------------------------


def test_ac14_bullwhip_avg_is_null_not_zero_for_constant_demand_only(db):
    """With `CONSTANT` demand every role's `bullwhip_ratio` is NULL (D12),
    so a user whose only game used it must show `bullwhip_avg: NULL` -- a
    wrong implementation that folds a missing ratio into the mean as 0
    would instead report `0.0`."""
    user_id = insert_user(db.connection(), firebase_uid="uid-constant-demand")
    db.commit()
    _persist_game_for_user(
        db,
        user_identity="uid-constant-demand",
        role=Role.RETAILER,
        duration_weeks=10,
        seed=7,
    )

    StatsService().recompute_user_stats(db, user_id)
    db.commit()

    row = db.get(UserStats, user_id)
    assert row.games_played == 1
    assert row.bullwhip_avg is None


# ---------------------------------------------------------------------------
# Acceptance criterion 15 / failure mode 4
# ---------------------------------------------------------------------------


def test_ac15_fm4_recompute_issues_a_bounded_number_of_statements(db, schema_engine):
    user_id = insert_user(db.connection(), firebase_uid="uid-25-games")
    db.commit()
    for seed in range(25):
        _persist_game_for_user(
            db,
            user_identity="uid-25-games",
            role=Role.RETAILER,
            duration_weeks=8,
            seed=seed,
        )

    with count_statements(schema_engine) as log:
        StatsService().recompute_user_stats(db, user_id)
    db.commit()

    # Failure mode 4's own bound: "≤ 3, not 25+".
    assert log.total <= 3, (
        f"expected at most 3 statements for 25 games, ran {log.total}: "
        f"{[r.statement for r in log.records]}"
    )

    row = db.get(UserStats, user_id)
    assert row.games_played == 25


# ---------------------------------------------------------------------------
# Failure mode 12 (stats half -- the DB-cascade half is section 13's own
# `test_deleting_a_user_keeps_the_games_they_played`)
# ---------------------------------------------------------------------------


def test_fm12_recompute_for_a_deleted_user_is_a_noop(db):
    user_id = insert_user(db.connection(), firebase_uid="uid-will-be-deleted")
    db.commit()
    _persist_game_for_user(
        db,
        user_identity="uid-will-be-deleted",
        role=Role.RETAILER,
        duration_weeks=8,
        seed=301,
    )

    db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    db.commit()

    StatsService().recompute_user_stats(db, user_id)  # must not raise
    db.commit()

    assert db.get(UserStats, user_id) is None
