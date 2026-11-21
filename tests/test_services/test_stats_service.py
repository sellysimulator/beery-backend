"""Black-box tests for ``app/services/stats_service.py`` (spec §10.5).

Writing a finished game is tested in ``test_db_service.py``, and the guest
claim in ``test_guest_claim.py``.

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
from app.models.game import Game, UserStats
from app.models.user import User
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
# Recompute after one game
# ---------------------------------------------------------------------------


def test_recompute_after_one_game(db):
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


def test_recompute_twice_updates_the_one_row_in_place(db):
    """The upsert's second run takes the `ON CONFLICT (user_id) DO UPDATE`
    branch: still one `user_stats` row, now carrying the new totals."""
    user_id = insert_user(db.connection(), firebase_uid="uid-upsert-twice")
    db.commit()
    _persist_game_for_user(
        db, user_identity="uid-upsert-twice", role=Role.FACTORY, seed=111
    )
    StatsService().recompute_user_stats(db, user_id)
    db.commit()
    first = db.get(UserStats, user_id)
    assert first is not None and first.games_played == 1
    first_created = first.created_at

    _persist_game_for_user(
        db, user_identity="uid-upsert-twice", role=Role.FACTORY, seed=112
    )
    StatsService().recompute_user_stats(db, user_id)
    db.commit()
    db.expire_all()

    rows = db.query(UserStats).filter_by(user_id=user_id).all()
    assert len(rows) == 1
    assert rows[0].games_played == 2
    assert rows[0].weeks_played == 20
    assert rows[0].games_as_factory == 2
    assert rows[0].created_at == first_created


# ---------------------------------------------------------------------------
# The best game
# ---------------------------------------------------------------------------


def test_recompute_after_three_games_picks_the_cheapest_as_best(db):
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
# The bullwhip average
# ---------------------------------------------------------------------------


def test_bullwhip_avg_is_null_not_zero_for_constant_demand_only(db):
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
# A bounded number of statements
# ---------------------------------------------------------------------------


def test_recompute_issues_a_bounded_number_of_statements(db, schema_engine):
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

    # The bound: "≤ 3, not 25+".
    assert log.total <= 3, (
        f"expected at most 3 statements for 25 games, ran {log.total}: "
        f"{[r.statement for r in log.records]}"
    )

    row = db.get(UserStats, user_id)
    assert row.games_played == 25


# ---------------------------------------------------------------------------
# A deleted user (the stats half -- the DB-cascade half is
# `tests/test_models`' `test_deleting_a_user_keeps_the_games_they_played`)
# ---------------------------------------------------------------------------


def test_recompute_for_a_deleted_user_is_a_noop(db):
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


# ---------------------------------------------------------------------------
# Hosting is not playing (D23)
# ---------------------------------------------------------------------------


def test_recompute_for_game_creates_no_stats_for_the_host(db):
    """A signed-in host's game recomputes its players' stats and never the
    host's: the host has no participant row, and `host_user_id` is not
    read."""
    host_user_id = insert_user(db.connection(), firebase_uid="uid-stats-host")
    db.commit()

    config = make_config(duration_weeks=10)
    engine = run_full_game(config, seed=7)
    room = make_room_document(
        config,
        engine,
        host_uid="uid-stats-host",
        participants={
            "P1": make_participant(
                "P1", identity="uid-stats-player", role=Role.RETAILER
            )
        },
    )
    game_id = DbService().persist_game(db, build_snapshot(room, config, engine))
    db.commit()
    assert db.get(Game, game_id).host_user_id == host_user_id

    StatsService().recompute_for_game(db, game_id)

    assert db.get(UserStats, host_user_id) is None
    player = db.query(User).filter_by(firebase_uid="uid-stats-player").one()
    assert db.get(UserStats, player.id).games_played == 1
