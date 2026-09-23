"""Black-box tests for section 14 -- ``app/services/db_service.py``.

Covers ``14-end-of-game-persistence.md §4`` acceptance criteria 1-11 and 20,
and ``§5`` failure modes 1, 2, 3, 5, 6, 7, 8, 9 and 11. Criteria 12-15 live
in ``test_stats_service.py`` and 16-19 in ``test_guest_claim.py``, together
with failure modes 4, 10 and 12.

Driven entirely through: ``DbService``, ``GameSnapshot``, ``ParticipantSnapshot``
and ``build_snapshot`` (this section's own frozen surface); the frozen
surfaces of ``GameEngine``/``WeekRecord`` (07), the room document (09 §2),
``app/models/game.py`` and ``app/models/user.py`` (13); and
``app.services.game_service.persist_finished_game``, the seam section 12
freezes and this section fills the body of. ``app/services/db_service.py``
itself is never opened.

Needs a real MySQL (Testcontainers, shared with section 13's suite) because
the behaviour under test -- batched raw inserts, an idempotency lookup, an
all-or-nothing transaction and DECIMAL rounding -- is either unenforced or
silently "correct for the wrong reason" against a fake.
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.config_models import StepDemand
from app.core.enums import ROLE_ORDER, DemandKind, Role
from app.core.game_engine import WeekRecord
from app.core.stats import compute_stats
from app.models.game import (
    DemandConfigRow,
    DemandSeries,
    Game,
    GameConfigRow,
    Participant,
    RoleConfigRow,
    Week,
)
from app.services.db_service import (
    DbService,
    GameSnapshot,
    ParticipantSnapshot,
    build_snapshot,
)
from app.services.game_service import persist_finished_game
from app.services.state_service import get_state_service

from .conftest import (
    count_statements,
    insert_user,
    make_config,
    make_participant,
    make_room_document,
    run_full_game,
    unique_room_code,
)

pytestmark = pytest.mark.dbschema


# ---------------------------------------------------------------------------
# Acceptance criterion 1
# ---------------------------------------------------------------------------


def test_ac1_persist_writes_full_36_week_game(db):
    """A finished 36-week Classic MIT game writes every row §4.1 names."""
    config = make_config(duration_weeks=36)
    engine = run_full_game(config, seed=42)
    assert engine.weeks_played == 36

    participants = {
        "P1": make_participant("P1", identity="uid-retailer", role=Role.RETAILER),
        "P2": make_participant(
            "P2",
            identity="guest_22222222-2222-2222-2222-222222222222",
            role=Role.WHOLESALER,
        ),
        "P3": make_participant(
            "P3",
            identity="guest_33333333-3333-3333-3333-333333333333",
            role=Role.DISTRIBUTOR,
        ),
        "P4": make_participant("P4", identity=None, role=Role.FACTORY, is_bot=True),
    }
    room = make_room_document(
        config,
        engine,
        host_identity=None,
        host_display_name="Host Guest",
        participants=participants,
    )
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)

    assert db.query(Game).filter_by(id=game_id).count() == 1
    assert db.query(GameConfigRow).filter_by(game_id=game_id).count() == 1
    assert db.query(DemandConfigRow).filter_by(game_id=game_id).count() == 1
    assert db.query(RoleConfigRow).filter_by(game_id=game_id).count() == 4
    assert db.query(DemandSeries).filter_by(game_id=game_id).count() == 36
    assert db.query(Week).filter_by(game_id=game_id).count() == 144
    # 4 role players + the host = up to 5, per 13-db-models-and-migrations.md §2.6.
    assert db.query(Participant).filter_by(game_id=game_id).count() == 5


# ---------------------------------------------------------------------------
# Acceptance criterion 2
# ---------------------------------------------------------------------------


def test_ac2_weeks_round_trip_to_equal_week_records(db):
    """Every persisted `weeks` row reconstructs the engine's `WeekRecord`."""
    config = make_config(duration_weeks=8)
    engine = run_full_game(config, seed=7)
    room = make_room_document(config, engine)
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)

    rows = db.query(Week).filter_by(game_id=game_id).all()
    by_key = {(row.week, row.role): row for row in rows}
    assert len(by_key) == len(engine.history)

    int_fields = (
        "opening_inventory",
        "opening_backlog",
        "arrived",
        "incoming_order",
        "obligation",
        "shipped",
        "unfulfilled",
        "closing_inventory",
        "closing_backlog",
        "supply_line_after",
        "orders_in_flight_after",
    )
    money_fields = (
        "holding_cost",
        "backlog_cost",
        "fixed_order_cost",
        "purchase_cost",
        "week_cost",
        "cumulative_cost",
    )
    for record in engine.history:
        row = by_key[(record.week, record.role.value)]
        for field in int_fields:
            assert getattr(row, field) == getattr(record, field), field
        assert row.order_qty == record.order
        assert bool(row.was_bot) == record.was_bot
        assert bool(row.was_forced) == record.was_forced
        assert row.production_started == record.production_started
        assert row.production_queued == record.production_queued
        for field in money_fields:
            expected = Decimal(str(round(getattr(record, field), 2)))
            assert getattr(row, field) == expected, field


# ---------------------------------------------------------------------------
# Acceptance criterion 3
# ---------------------------------------------------------------------------


def test_ac3_weeks_played_matches_distinct_weeks_in_weeks_table(db):
    config = make_config(duration_weeks=10)
    engine = run_full_game(config, seed=3, stop_after=6)
    assert engine.weeks_played == 6
    room = make_room_document(config, engine)
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)

    game = db.get(Game, game_id)
    distinct_weeks = {
        row.week for row in db.query(Week).filter_by(game_id=game_id).all()
    }
    assert game.weeks_played == len(distinct_weeks) == 6


# ---------------------------------------------------------------------------
# Acceptance criterion 4
# ---------------------------------------------------------------------------


def test_ac4_chain_total_cost_matches_sum_of_final_cumulative_costs(db):
    config = make_config(duration_weeks=12)
    engine = run_full_game(config, seed=5)
    room = make_room_document(config, engine)
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)

    final_by_role: dict[Role, float] = {}
    for record in engine.history:  # chronological: last write per role wins
        final_by_role[record.role] = record.cumulative_cost
    expected = sum(
        (Decimal(str(round(v, 2))) for v in final_by_role.values()), Decimal("0.00")
    )

    game = db.get(Game, game_id)
    assert game.chain_total_cost == expected


# ---------------------------------------------------------------------------
# Acceptance criterion 5
# ---------------------------------------------------------------------------


def test_ac5_ended_early_writes_partial_weeks_and_flags(db):
    config = make_config(duration_weeks=36)
    engine = run_full_game(config, seed=11, stop_after=9)
    assert engine.weeks_played == 9
    room = make_room_document(config, engine)
    snapshot = build_snapshot(room, config, engine)
    assert snapshot.ended_early is True

    game_id = DbService().persist_game(db, snapshot)
    game = db.get(Game, game_id)
    assert game.weeks_played == 9
    assert game.duration_weeks == 36
    assert game.ended_early is True
    assert db.query(Week).filter_by(game_id=game_id).count() == 36  # 9 * 4


def test_ac5_forcing_every_week_to_the_end_is_not_ended_early(db):
    """Force-closing a week is not the same as ending early (§2.1)."""
    config = make_config(duration_weeks=8)
    engine = run_full_game(
        config,
        seed=13,
        force_missing_role=Role.FACTORY,
        force_missing_at_week=4,
    )
    assert engine.weeks_played == 8
    forced = [r for r in engine.history if r.was_forced]
    assert forced, "the scenario must actually produce a forced week"

    room = make_room_document(config, engine)
    snapshot = build_snapshot(room, config, engine)
    assert snapshot.ended_early is False

    game_id = DbService().persist_game(db, snapshot)
    game = db.get(Game, game_id)
    assert game.ended_early is False
    assert game.weeks_played == 8
    assert game.duration_weeks == 8


# ---------------------------------------------------------------------------
# Acceptance criterion 5a
# ---------------------------------------------------------------------------


def test_ac5a_bullwhip_null_for_every_role_in_a_constant_demand_game(db):
    config = make_config(duration_weeks=10)  # ConstantDemand by default
    engine = run_full_game(config, seed=17)
    room = make_room_document(
        config,
        engine,
        participants={
            "P1": make_participant("P1", identity="uid-1", role=Role.RETAILER),
        },
    )
    snapshot = build_snapshot(room, config, engine)
    assert all(s.bullwhip_ratio is None for s in snapshot.stats.per_role.values())

    game_id = DbService().persist_game(db, snapshot)
    rows = db.query(Participant).filter_by(game_id=game_id).all()
    assert rows
    assert all(row.bullwhip_ratio is None for row in rows)


def test_ac5a_bullwhip_null_for_bot_and_host_even_with_a_defined_ratio(db):
    """A demand shock gives every role a *defined* ratio, so a NULL here can
    only come from the bot/host special-casing, not from D12's CONSTANT rule."""
    config = make_config(
        duration_weeks=20,
        demand=StepDemand(initial_value=4, step_week=5, step_value=12),
    )
    engine = run_full_game(config, seed=19, bot_roles=frozenset({Role.WHOLESALER}))

    participants = {
        "P1": make_participant("P1", identity="uid-retailer", role=Role.RETAILER),
        "P2": make_participant("P2", identity=None, role=Role.WHOLESALER, is_bot=True),
        "P3": make_participant("P3", identity="guest_abc", role=Role.DISTRIBUTOR),
        "P4": make_participant("P4", identity="guest_def", role=Role.FACTORY),
    }
    room = make_room_document(
        config, engine, host_identity="uid-host", participants=participants
    )
    snapshot = build_snapshot(room, config, engine)
    assert snapshot.stats.per_role[Role.WHOLESALER].bullwhip_ratio is not None

    game_id = DbService().persist_game(db, snapshot)
    rows = db.query(Participant).filter_by(game_id=game_id).all()
    by_alias = {r.alias: r for r in rows if r.participant_type == "PLAYER"}
    host_rows = [r for r in rows if r.participant_type == "HOST"]
    assert len(host_rows) == 1

    assert by_alias["P1"].bullwhip_ratio is not None
    assert by_alias["P3"].bullwhip_ratio is not None
    assert by_alias["P4"].bullwhip_ratio is not None
    assert by_alias["P2"].bullwhip_ratio is None  # bot-played WHOLESALER
    assert host_rows[0].bullwhip_ratio is None  # the host has no role
    assert host_rows[0].role is None


# ---------------------------------------------------------------------------
# Acceptance criterion 5b
# ---------------------------------------------------------------------------


def test_ac5b_reading_weeks_back_reproduces_the_engines_game_stats(db):
    # Default role costs (0.50 / 1.00 / 0 / 0) are exact multiples of a cent,
    # so the DECIMAL(12,2) round trip introduces no rounding drift here.
    config = make_config(duration_weeks=16)
    engine = run_full_game(config, seed=23)
    expected_stats = compute_stats(
        engine.history, engine.demand_series, engine.weeks_played
    )

    room = make_room_document(config, engine)
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)

    week_rows = db.query(Week).filter_by(game_id=game_id).all()
    week_rows.sort(key=lambda r: (r.week, ROLE_ORDER.index(Role(r.role))))
    rebuilt_history = [
        WeekRecord(
            role=Role(r.role),
            week=r.week,
            opening_inventory=r.opening_inventory,
            opening_backlog=r.opening_backlog,
            arrived=r.arrived,
            incoming_order=r.incoming_order,
            obligation=r.obligation,
            shipped=r.shipped,
            unfulfilled=r.unfulfilled,
            closing_inventory=r.closing_inventory,
            closing_backlog=r.closing_backlog,
            supply_line_after=r.supply_line_after,
            orders_in_flight_after=r.orders_in_flight_after,
            order=r.order_qty,
            was_bot=bool(r.was_bot),
            was_forced=bool(r.was_forced),
            holding_cost=float(r.holding_cost),
            backlog_cost=float(r.backlog_cost),
            fixed_order_cost=float(r.fixed_order_cost),
            purchase_cost=float(r.purchase_cost),
            week_cost=float(r.week_cost),
            cumulative_cost=float(r.cumulative_cost),
            production_started=r.production_started,
            production_queued=r.production_queued,
        )
        for r in week_rows
    ]
    demand_rows = (
        db.query(DemandSeries)
        .filter_by(game_id=game_id)
        .order_by(DemandSeries.week)
        .all()
    )
    rebuilt_demand = [row.quantity for row in demand_rows]

    rebuilt_stats = compute_stats(rebuilt_history, rebuilt_demand, engine.weeks_played)
    assert rebuilt_stats == expected_stats


# ---------------------------------------------------------------------------
# Acceptance criterion 6 / failure mode 6
# ---------------------------------------------------------------------------


def test_ac6_persist_game_called_twice_writes_once_and_returns_same_id(session_factory):
    config = make_config(duration_weeks=8)
    engine = run_full_game(config, seed=29)
    room = make_room_document(config, engine)
    snapshot = build_snapshot(room, config, engine)

    game_id_1 = DbService().persist_game(session_factory(), snapshot)
    game_id_2 = DbService().persist_game(session_factory(), snapshot)

    assert game_id_1 == game_id_2
    verify_db = session_factory()
    assert verify_db.query(Game).filter_by(room_code=room["room_code"]).count() == 1


def test_fm6_concurrent_persist_calls_write_one_game_row(session_factory):
    """Best-effort concurrency probe.

    `games` carries no unique constraint on `(room_code, started_at)` --
    only the explicit lookup `persist_game` performs guards it (§3.3) --
    so true simultaneous overlap cannot be forced from a black-box test
    without a synchronisation hook into the implementation. Five threads
    racing to persist the same snapshot at least catches an implementation
    with no idempotency guard at all.
    """
    import threading

    config = make_config(duration_weeks=8)
    engine = run_full_game(config, seed=31)
    room = make_room_document(config, engine)
    snapshot = build_snapshot(room, config, engine)

    results: list[int] = []
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            results.append(DbService().persist_game(session_factory(), snapshot))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(set(results)) == 1
    verify_db = session_factory()
    assert verify_db.query(Game).filter_by(room_code=room["room_code"]).count() == 1


# ---------------------------------------------------------------------------
# Acceptance criterion 7 / failure modes 2 and 3
# ---------------------------------------------------------------------------


def test_ac7_failure_partway_leaves_zero_rows(db):
    """Forcing the `weeks` insert to fail leaves no `games` row at all."""
    config = make_config(duration_weeks=8)
    engine = run_full_game(config, seed=37)
    room = make_room_document(config, engine)
    snapshot = build_snapshot(room, config, engine)
    # A duplicate (week, role) violates `uq_weeks_game_id_week_role` --
    # exactly the insert AC 7 names.
    corrupted = dataclasses.replace(
        snapshot, history=[*snapshot.history, snapshot.history[0]]
    )

    with pytest.raises(IntegrityError):
        DbService().persist_game(db, corrupted)
    db.rollback()

    assert db.query(Game).filter_by(room_code=room["room_code"]).count() == 0


@pytest.mark.asyncio
async def test_fm2_fm3_persist_finished_game_logs_error_and_does_not_raise(
    caplog,
    session_factory,
    app_db_pointed_at_container,
):
    """A raising `persist_game` must be logged at ERROR naming the room code
    (not a bare `except: pass`) and must not break the caller (`finish_game`
    already emitted `game_finished`; this must return `False`, not raise)."""
    config = make_config(duration_weeks=6)
    engine = run_full_game(config, seed=41)
    # Public field, deliberately corrupted the same way as AC 7: a duplicate
    # (week, role) pair forces a real, deterministic DB failure.
    engine.history.append(engine.history[0])
    room = make_room_document(config, engine)
    room_code = room["room_code"]
    stats = compute_stats(engine.history, engine.demand_series, engine.weeks_played)

    with caplog.at_level(logging.ERROR):
        result = await persist_finished_game(room_code, room, engine, stats)

    assert result is None, "a failed persist must return None, not raise"

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected an ERROR-level log record"
    assert any(
        room_code in r.getMessage() for r in error_records
    ), "the ERROR log must name the room code"

    verify_db = session_factory()
    assert verify_db.query(Game).filter_by(room_code=room_code).count() == 0


# ---------------------------------------------------------------------------
# Failure mode 1
# ---------------------------------------------------------------------------


def test_fm1_batched_participants_and_role_configs_get_server_side_timestamps(db):
    """`participants` and `role_configs` carry `TimestampMixin`; `weeks` and
    `demand_series` deliberately do not (§2.7/§2.8), so only these two
    tables can catch a raw insert that relies on an ORM-side default that
    a batched raw insert bypasses [HARD-WON].

    The room's own timestamps are set to a date far in the past, so a bug
    that (wrongly) sources `created_at` from the room document rather than
    letting `server_default=func.now()` supply it is distinguishable from a
    merely-present-but-wrong value, not just a NULL.
    """
    config = make_config(duration_weeks=8)
    engine = run_full_game(config, seed=47)
    ancient = datetime(2000, 1, 1, tzinfo=timezone.utc)
    room = make_room_document(
        config,
        engine,
        participants={
            "P1": make_participant("P1", identity="uid-x", role=Role.RETAILER)
        },
        started_at=ancient,
        finished_at=ancient,
    )
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)

    now = datetime.now(timezone.utc)
    role_config_rows = db.query(RoleConfigRow).filter_by(game_id=game_id).all()
    participant_rows = db.query(Participant).filter_by(game_id=game_id).all()
    assert role_config_rows and participant_rows
    for row in (*role_config_rows, *participant_rows):
        assert row.created_at is not None
        assert row.updated_at is not None
        assert (
            now - row.created_at.astimezone(timezone.utc)
        ).total_seconds() < 300, (
            "created_at looks sourced from the room document, not the DB clock"
        )


# ---------------------------------------------------------------------------
# Failure mode 5
# ---------------------------------------------------------------------------


def test_fm5_child_tables_are_inserted_with_one_executemany_each(db, schema_engine):
    config = make_config(duration_weeks=36)
    engine = run_full_game(config, seed=89)
    participants = {
        "P1": make_participant("P1", identity="uid-1", role=Role.RETAILER),
        "P2": make_participant("P2", identity="uid-2", role=Role.WHOLESALER),
        "P3": make_participant("P3", identity="uid-3", role=Role.DISTRIBUTOR),
        "P4": make_participant("P4", identity="uid-4", role=Role.FACTORY),
    }
    room = make_room_document(config, engine, participants=participants)
    snapshot = build_snapshot(room, config, engine)

    with count_statements(schema_engine) as log:
        DbService().persist_game(db, snapshot)

    expectations = (
        ("into weeks", 4 * engine.weeks_played),
        ("into demand_series", engine.weeks_played),
        ("into role_configs", 4),
        ("into participants", 5),
    )
    for needle, expected_count in expectations:
        matches = log.matching(needle)
        assert len(matches) == 1, (
            f"expected exactly one statement touching {needle!r}, "
            f"got {len(matches)}: {[m.statement for m in matches]}"
        )
        assert matches[0].executemany is True, f"{needle} was not batched"
        assert matches[0].batch_size == expected_count, (
            f"{needle}: expected a batch of {expected_count}, "
            f"got {matches[0].batch_size}"
        )


# ---------------------------------------------------------------------------
# Failure mode 7
# ---------------------------------------------------------------------------


def test_fm7_money_rounds_half_up_to_two_decimals_at_the_boundary(db):
    config = make_config(duration_weeks=8)
    shared = {
        "week": 1,
        "opening_inventory": 12,
        "opening_backlog": 0,
        "arrived": 4,
        "incoming_order": 4,
        "obligation": 4,
        "shipped": 4,
        "unfulfilled": 0,
        "closing_inventory": 12,
        "closing_backlog": 0,
        "supply_line_after": 4,
        "orders_in_flight_after": 4,
        "order": 4,
        "was_bot": False,
        "was_forced": False,
        "holding_cost": 0.50,
        "backlog_cost": 0.0,
        "fixed_order_cost": 0.0,
        "purchase_cost": 0.0,
        "week_cost": 0.50,
        "cumulative_cost": 1234.567,
        "production_started": None,
        "production_queued": None,
    }
    history = [WeekRecord(role=role, **shared) for role in ROLE_ORDER]
    demand_series = [4]
    stats = compute_stats(history, demand_series, weeks_played=1)
    participants = [
        ParticipantSnapshot(
            alias="P1",
            display_name="Ana",
            role=Role.RETAILER,
            participant_type="PLAYER",
            is_bot=False,
            identity="uid-money",
        ),
    ]
    snapshot = GameSnapshot(
        room_code=unique_room_code(),
        host_identity=None,
        host_display_name="Host",
        seed=1,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        finished_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        weeks_played=1,
        ended_early=True,
        config=config,
        demand_series=demand_series,
        history=history,
        participants=participants,
        stats=stats,
    )

    game_id = DbService().persist_game(db, snapshot)
    row = db.query(Week).filter_by(game_id=game_id, role=Role.RETAILER.value).one()
    assert row.week_cost == Decimal("0.50")
    assert row.cumulative_cost == Decimal("1234.57")


# ---------------------------------------------------------------------------
# Failure mode 8
# ---------------------------------------------------------------------------


def test_fm8_persist_game_signature_takes_no_room_dict_or_lock_handle():
    """`persist_game`'s frozen signature is `(self, db, snapshot)` -- a plain
    `Session` and a `GameSnapshot`, never a live room `dict` or a lock."""
    params = list(inspect.signature(DbService.persist_game).parameters)
    assert params == ["self", "db", "snapshot"]


@pytest.mark.asyncio
async def test_ac20_persistence_runs_with_the_room_lock_released(
    fake_redis, monkeypatch, app_db_pointed_at_container
):
    """`persist_finished_game` must never call `state_svc.lock` at all --
    the same spy-on-`lock` technique section 12's own suite uses for
    `finish_game` (``tests/test_sockets/test_week_broadcast.py::
    test_finish_game_never_acquires_the_room_lock``), applied directly to
    the function this section's body fills in. Counting real lock
    acquisitions proves the claim regardless of timing, unlike racing a
    concurrent operation against an in-memory fake."""
    state_svc = get_state_service()
    lock_calls: list[str] = []
    original_lock = state_svc.lock

    def spy_lock(room_code: str):
        lock_calls.append(room_code)
        return original_lock(room_code)

    monkeypatch.setattr(state_svc, "lock", spy_lock)

    config = make_config(duration_weeks=6)
    engine = run_full_game(config, seed=83)
    room = make_room_document(config, engine)
    stats = compute_stats(engine.history, engine.demand_series, engine.weeks_played)

    result = await persist_finished_game(room["room_code"], room, engine, stats)

    assert isinstance(result, str) and len(result) == 32
    assert lock_calls == []


# ---------------------------------------------------------------------------
# Failure mode 9
# ---------------------------------------------------------------------------


def test_fm9_no_guest_identity_in_return_value_or_logs(db, caplog):
    config = make_config(duration_weeks=6)
    engine = run_full_game(config, seed=53)
    participants = {
        "P1": make_participant(
            "P1",
            identity="guest_ffffffff-ffff-ffff-ffff-ffffffffffff",
            role=Role.RETAILER,
        ),
    }
    room = make_room_document(
        config,
        engine,
        host_identity="guest_00000000-0000-0000-0000-000000000000",
        participants=participants,
    )
    snapshot = build_snapshot(room, config, engine)

    with caplog.at_level(logging.DEBUG):
        game_id = DbService().persist_game(db, snapshot)

    assert isinstance(game_id, int)
    for record in caplog.records:
        assert "guest_" not in record.getMessage()


# ---------------------------------------------------------------------------
# Acceptance criteria 8, 9, 10, 11
# ---------------------------------------------------------------------------


def test_ac8_role_configs_factory_only_columns(db):
    config = make_config(duration_weeks=6)
    engine = run_full_game(config, seed=73)
    room = make_room_document(config, engine)
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)

    rows = {
        row.role: row
        for row in db.query(RoleConfigRow).filter_by(game_id=game_id).all()
    }
    assert rows[Role.FACTORY.value].production_delay_weeks is not None
    for role in (Role.RETAILER, Role.WHOLESALER, Role.DISTRIBUTOR):
        assert rows[role.value].production_delay_weeks is None
        assert rows[role.value].production_capacity_per_week is None


def test_ac9_demand_configs_step_only_columns(db):
    config = make_config(
        duration_weeks=10, demand=StepDemand(initial_value=4, step_week=5, step_value=9)
    )
    engine = run_full_game(config, seed=79)
    room = make_room_document(config, engine)
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)

    row = db.query(DemandConfigRow).filter_by(game_id=game_id).one()
    assert row.kind == DemandKind.STEP.value
    assert row.initial_value == 4
    assert row.step_week == 5
    assert row.step_value == 9
    other_columns = (
        "value",
        "slope_per_week",
        "start_week",
        "cap",
        "base",
        "amplitude",
        "period_weeks",
        "phase",
        "distribution",
        "mean",
        "stdev",
        "min_value",
        "max_value",
    )
    for column in other_columns:
        assert getattr(row, column) is None, column


def test_ac10_guest_hosted_game_persists_with_null_host_user_id(db):
    config = make_config(duration_weeks=6)
    engine = run_full_game(config, seed=61)
    room = make_room_document(
        config,
        engine,
        host_identity=None,
        host_display_name="Guest Host",
    )
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)

    game = db.get(Game, game_id)
    assert game.host_user_id is None
    assert game.host_display_name == "Guest Host"


def test_registered_host_resolves_to_the_matching_users_row(db):
    """The positive case AC 10 needs a contrast against: a bug that always
    nulls `host_user_id` would pass AC 10 alone but fails here."""
    firebase_uid = "uid-registered-host"
    user_id = insert_user(
        db.connection(), firebase_uid=firebase_uid, display_name="Prof"
    )
    db.commit()

    config = make_config(duration_weeks=6)
    engine = run_full_game(config, seed=67)
    room = make_room_document(
        config,
        engine,
        host_identity=firebase_uid,
        host_display_name="Prof",
    )
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)

    game = db.get(Game, game_id)
    assert game.host_user_id == user_id


# ---------------------------------------------------------------------------
# Failure mode 11
# ---------------------------------------------------------------------------


def test_fm11_unregistered_firebase_uid_persists_with_null_user_id(db):
    """A Firebase uid with no `users` row (a signed-in user who never called
    `/users/upsert`) must not raise a foreign-key error; it persists exactly
    like a guest-shaped attribution (§3.6)."""
    config = make_config(duration_weeks=6)
    engine = run_full_game(config, seed=59)
    participants = {
        "P1": make_participant(
            "P1", identity="uid-never-registered", role=Role.RETAILER
        ),
    }
    room = make_room_document(
        config,
        engine,
        host_identity="uid-never-registered-host",
        participants=participants,
    )
    snapshot = build_snapshot(room, config, engine)

    game_id = DbService().persist_game(db, snapshot)  # must not raise

    game = db.get(Game, game_id)
    assert game.host_user_id is None
    player_row = db.query(Participant).filter_by(game_id=game_id, alias="P1").one()
    assert player_row.user_id is None


def test_ac11_bot_participant_has_null_identity_columns(db):
    config = make_config(duration_weeks=6)
    engine = run_full_game(config, seed=71, bot_roles=frozenset({Role.RETAILER}))
    participants = {
        "P1": make_participant("P1", identity=None, role=Role.RETAILER, is_bot=True),
    }
    room = make_room_document(config, engine, participants=participants)
    snapshot = build_snapshot(room, config, engine)
    game_id = DbService().persist_game(db, snapshot)

    row = db.query(Participant).filter_by(game_id=game_id, alias="P1").one()
    assert row.user_id is None
    assert row.guest_identity is None
    assert bool(row.is_bot) is True
