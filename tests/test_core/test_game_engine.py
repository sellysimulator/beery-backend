"""Black-box tests for section 07 -- ``app/core/game_engine.py``.

Covers ``07-game-engine.md §5`` acceptance criteria 1-3, 5-13, 17 and 21-26,
and ``§6`` failure modes 1-8 and 12-17.  Criteria 4, 14, 15 and 16 live in
``test_engine_determinism.py``; criteria 18-20 and failure modes 9-11 live in
``test_stats.py``.

Everything is driven through the frozen public surface of ``§2`` plus the
public surfaces of sections 03, 05 and 06.  No private name and no log
message is asserted on.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from app.core.config_models import (
    DEFAULT_LIMITS,
    ConstantDemand,
    CustomDemand,
    DemandConfig,
    FactoryConfig,
    GameConfig,
    RoleConfig,
    VisibilityConfig,
)
from app.core.enums import ROLE_ORDER, Role
from app.core.game_engine import EngineStateError, GameEngine, GamePhase, WeekRecord
from app.core.presets import get_preset

SEED = 20260919

VISIBILITY_FLAGS = (
    "show_true_customer_demand_to_all",
    "show_neighbour_inventory",
    "show_all_inventories",
    "show_supply_line_prominently",
    "show_running_cost_to_players",
    "show_leaderboard_during_game",
)


# --------------------------------------------------------------------------
# Helpers -- built only from section 03's public surface
# --------------------------------------------------------------------------


def make_config(
    role_kwargs: dict[str, Any] | None = None,
    factory_kwargs: dict[str, Any] | None = None,
    *,
    demand: DemandConfig | None = None,
    duration_weeks: int = 36,
    visibility: VisibilityConfig | None = None,
    per_role: dict[Role, dict[str, Any]] | None = None,
) -> GameConfig:
    """A ``GameConfig`` assembled through ``GameConfig.from_host_input``.

    ``from_host_input`` accepts "plain JSON or already-built config models"
    (``03-game-config.md §2``), which is what lets a test vary one parameter
    without reaching past the frozen surface.
    """
    base = dict(role_kwargs or {})
    factory_base = {**base, **dict(factory_kwargs or {})}
    roles: dict[str, RoleConfig] = {
        Role.RETAILER.value: RoleConfig(**base),
        Role.WHOLESALER.value: RoleConfig(**base),
        Role.DISTRIBUTOR.value: RoleConfig(**base),
        Role.FACTORY.value: FactoryConfig(**factory_base),
    }
    for role, overrides in (per_role or {}).items():
        if role is Role.FACTORY:
            roles[role.value] = FactoryConfig(**{**factory_base, **overrides})
        else:
            roles[role.value] = RoleConfig(**{**base, **overrides})

    payload: dict[str, Any] = {
        "roles": roles,
        "demand": demand if demand is not None else ConstantDemand(value=4),
        "duration_weeks": duration_weeks,
    }
    if visibility is not None:
        payload["visibility"] = visibility
    return GameConfig.from_host_input(payload, DEFAULT_LIMITS)


def walk_values(value: Any) -> list[Any]:
    """Every value nested anywhere inside a view payload."""
    found: list[Any] = [value]
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        for key, item in value.items():
            found.append(key)
            found.extend(walk_values(item))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            found.extend(walk_values(item))
    return found


def contains_number(payload: Any, number: int) -> bool:
    """True when ``number`` appears anywhere in ``payload``, recursively."""
    for value in walk_values(payload):
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and value == number:
            return True
    return False


def play_week(engine: GameEngine, orders: dict[Role, int]) -> list[WeekRecord]:
    for role in ROLE_ORDER:
        engine.submit_order(role, orders[role])
    return engine.close_week()


def play_flat(engine: GameEngine, quantity: int, weeks: int) -> None:
    for _ in range(weeks):
        play_week(engine, {role: quantity for role in ROLE_ORDER})


def records_for(records: list[WeekRecord], role: Role) -> list[WeekRecord]:
    return [record for record in records if record.role == role]


# --------------------------------------------------------------------------
# AC 1 -- what start() returns
# --------------------------------------------------------------------------


def test_start_opens_week_one_in_decision_phase() -> None:
    config = get_preset("CLASSIC_MIT")
    engine = GameEngine.start(config, SEED)

    assert engine.phase == GamePhase.DECISION
    assert engine.week == 1
    assert engine.seed == SEED
    assert set(engine.agents) == set(ROLE_ORDER)
    assert len(engine.agents) == 4
    assert len(engine.demand_series) == config.duration_weeks
    assert all(isinstance(value, int) for value in engine.demand_series)
    assert engine.history == []
    assert engine.weeks_played == 0
    assert engine.all_orders_in() is False


def test_start_demand_series_length_follows_duration_weeks() -> None:
    config = make_config(duration_weeks=12)
    engine = GameEngine.start(config, SEED)
    assert len(engine.demand_series) == 12


# --------------------------------------------------------------------------
# AC 2 -- start() has already run Phase A for week 1
# --------------------------------------------------------------------------


def test_start_has_already_settled_week_one() -> None:
    # Arrival 6, nothing demanded of anybody: closing inventory must be
    # 12 + 6 for all four roles, which is only true if Phase A ran.
    config = make_config(
        {
            "initial_inventory": 12,
            "initial_pipeline_quantity": 6,
            "initial_order_in_pipeline": 0,
        },
        demand=ConstantDemand(value=0),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)

    assert set(engine.settlements) == set(ROLE_ORDER)
    for role in ROLE_ORDER:
        settlement = engine.settlements[role]
        assert settlement.week == 1
        assert settlement.opening_inventory == 12
        assert settlement.arrived == 6
        assert settlement.closing_inventory == 18
        assert engine.agents[role].inventory == 18


# --------------------------------------------------------------------------
# AC 3 -- the §4 worked example, and FM 1 (supply line sampled mid-pass)
# --------------------------------------------------------------------------


def test_worked_example_week_one_settlements() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)

    assert engine.demand_series[:6] == [4, 4, 4, 4, 8, 8]
    for role in ROLE_ORDER:
        settlement = engine.settlements[role]
        assert settlement.role == role
        assert settlement.week == 1
        assert settlement.opening_inventory == 12
        assert settlement.opening_backlog == 0
        assert settlement.arrived == 4
        assert settlement.incoming_order == 4
        assert settlement.obligation == 4
        assert settlement.shipped == 4
        assert settlement.unfulfilled == 0
        assert settlement.closing_inventory == 12
        assert settlement.closing_backlog == 0
        assert settlement.holding_cost == pytest.approx(6.00)
        assert settlement.backlog_cost == pytest.approx(0.00)
        assert settlement.carrying_cost == pytest.approx(6.00)


def test_worked_example_week_one_records() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    records = play_week(engine, {role: 4 for role in ROLE_ORDER})
    by_role = {record.role: record for record in records}

    expected_supply_line = {
        Role.RETAILER: 8,
        Role.WHOLESALER: 8,
        Role.DISTRIBUTOR: 8,
        Role.FACTORY: 4,
    }
    expected_orders_in_flight = {
        Role.RETAILER: 0,
        Role.WHOLESALER: 4,
        Role.DISTRIBUTOR: 4,
        Role.FACTORY: 4,
    }
    for role in ROLE_ORDER:
        record = by_role[role]
        assert record.week == 1
        assert record.order == 4
        assert record.supply_line_after == expected_supply_line[role]
        assert record.orders_in_flight_after == expected_orders_in_flight[role]
        assert record.week_cost == pytest.approx(6.00)
        assert record.cumulative_cost == pytest.approx(6.00)
        assert record.was_bot is False
        assert record.was_forced is False

    assert by_role[Role.FACTORY].production_started == 4
    assert by_role[Role.FACTORY].production_queued == 0
    for role in (Role.RETAILER, Role.WHOLESALER, Role.DISTRIBUTOR):
        assert by_role[role].production_started is None
        assert by_role[role].production_queued is None

    assert engine.week == 2
    assert engine.phase == GamePhase.DECISION


def test_fm1_retailer_supply_line_after_is_eight_not_four() -> None:
    """FM 1 -- sampling the supply line inside settle() gives 4 here."""
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    records = play_week(engine, {role: 4 for role in ROLE_ORDER})
    retailer = records_for(records, Role.RETAILER)[0]
    assert retailer.supply_line_after == 8


# --------------------------------------------------------------------------
# FM 2 -- single-pass settlement moves the arrival week
# --------------------------------------------------------------------------


def test_fm2_retailer_week_one_order_arrives_in_week_three() -> None:
    config = make_config(
        {
            "shipping_delay_weeks": 1,
            "information_delay_weeks": 1,
            "initial_pipeline_quantity": 0,
            "initial_order_in_pipeline": 0,
            "initial_inventory": 100,
        },
        {"production_delay_weeks": 1},
        demand=CustomDemand(values=[0] * 12),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)

    assert engine.player_view(Role.RETAILER)["order_arrival_lead_weeks"] == 2

    arrivals: dict[int, int] = {}
    for week in range(1, 6):
        orders = {role: 0 for role in ROLE_ORDER}
        if week == 1:
            orders[Role.RETAILER] = 7
        records = play_week(engine, orders)
        arrivals[week] = records_for(records, Role.RETAILER)[0].arrived

    assert arrivals == {1: 0, 2: 0, 3: 7, 4: 0, 5: 0}


# --------------------------------------------------------------------------
# FM 3, FM 4 -- the Retailer's demand comes from the series, index 0 for week 1
# --------------------------------------------------------------------------


def test_fm3_retailer_ignores_its_own_order_pipeline() -> None:
    config = make_config(
        demand=ConstantDemand(value=4),
        duration_weeks=12,
        per_role={Role.RETAILER: {"initial_order_in_pipeline": 99}},
    )
    engine = GameEngine.start(config, SEED)

    assert engine.settlements[Role.RETAILER].incoming_order == 4
    assert engine.player_view(Role.RETAILER)["incoming_order"] == 4

    records = play_week(engine, {role: 4 for role in ROLE_ORDER})
    assert records_for(records, Role.RETAILER)[0].incoming_order == 4


def test_fm4_week_one_uses_demand_series_index_zero() -> None:
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120]
    config = make_config(
        {"initial_inventory": 9_000, "initial_pipeline_quantity": 0},
        demand=CustomDemand(values=values),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    assert engine.demand_series == values
    assert engine.settlements[Role.RETAILER].incoming_order == 10

    seen = []
    for _ in range(3):
        records = play_week(engine, {role: 0 for role in ROLE_ORDER})
        seen.append(records_for(records, Role.RETAILER)[0].incoming_order)
    assert seen == [10, 20, 30]


# --------------------------------------------------------------------------
# AC 5, FM 12 -- clamping
# --------------------------------------------------------------------------


def test_ac5_default_clamp_is_zero_to_nine_nine_nine_nine() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    assert engine.submit_order(Role.RETAILER, 12) == 12
    assert engine.submit_order(Role.RETAILER, 0) == 0
    assert engine.submit_order(Role.RETAILER, 9_999) == 9_999
    assert engine.submit_order(Role.RETAILER, 10_000) == 9_999
    assert engine.submit_order(Role.RETAILER, 1_000_000) == 9_999


def test_fm12_negative_order_without_the_flag_clamps_to_zero() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    assert engine.submit_order(Role.RETAILER, -5) == 0
    assert engine.submit_order(Role.WHOLESALER, -9_999_999) == 0


def test_ac5_clamps_to_configured_max_order_quantity() -> None:
    config = make_config(
        visibility=VisibilityConfig(max_order_quantity=50),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    assert engine.submit_order(Role.RETAILER, 50) == 50
    assert engine.submit_order(Role.RETAILER, 51) == 50
    assert engine.submit_order(Role.RETAILER, 10_000) == 50
    assert engine.submit_order(Role.RETAILER, -1) == 0


def test_ac5_allow_negative_orders_opens_the_floor() -> None:
    config = make_config(
        visibility=VisibilityConfig(max_order_quantity=50, allow_negative_orders=True),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    assert engine.submit_order(Role.RETAILER, -1) == -1
    assert engine.submit_order(Role.RETAILER, -50) == -50
    assert engine.submit_order(Role.RETAILER, -51) == -50
    assert engine.submit_order(Role.RETAILER, 51) == 50


def test_ac5_allow_negative_orders_without_a_configured_ceiling() -> None:
    config = make_config(
        visibility=VisibilityConfig(allow_negative_orders=True),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    assert engine.submit_order(Role.RETAILER, -9_999) == -9_999
    assert engine.submit_order(Role.RETAILER, -20_000) == -9_999


def test_submit_order_rejects_a_non_integer_and_a_non_role() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    with pytest.raises(ValueError):
        engine.submit_order(Role.RETAILER, "not-a-number")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        engine.submit_order("NOT_A_ROLE", 4)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# AC 6 -- idempotency by role
# --------------------------------------------------------------------------


def test_ac6_second_submission_replaces_the_first() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    engine.submit_order(Role.RETAILER, 4)
    assert engine.submit_order(Role.RETAILER, 9) == 9
    assert engine.all_orders_in() is False

    for role in (Role.WHOLESALER, Role.DISTRIBUTOR, Role.FACTORY):
        engine.submit_order(role, 4)
    assert engine.all_orders_in() is True

    engine.submit_order(Role.RETAILER, 11)
    assert engine.all_orders_in() is True

    records = engine.close_week()
    assert records_for(records, Role.RETAILER)[0].order == 11


def test_ac6_all_orders_in_needs_four_distinct_roles() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    for _ in range(4):
        engine.submit_order(Role.RETAILER, 4)
    assert engine.all_orders_in() is False


# --------------------------------------------------------------------------
# AC 7, AC 23, FM 14, FM 17 -- close_week and force
# --------------------------------------------------------------------------


def test_ac7_close_week_with_a_missing_order_raises() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    for role in (Role.RETAILER, Role.WHOLESALER, Role.DISTRIBUTOR):
        engine.submit_order(role, 4)
    with pytest.raises(EngineStateError):
        engine.close_week()
    assert engine.week == 1
    assert engine.history == []


def test_ac7_force_records_zero_for_the_missing_role() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    engine.submit_order(Role.RETAILER, 4)
    records = engine.close_week(force=True)
    by_role = {record.role: record for record in records}
    assert by_role[Role.RETAILER].order == 4
    assert by_role[Role.WHOLESALER].order == 0
    assert by_role[Role.DISTRIBUTOR].order == 0
    assert by_role[Role.FACTORY].order == 0


def test_fm14_force_keeps_the_three_submitted_orders() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    submitted = {Role.RETAILER: 7, Role.WHOLESALER: 13, Role.FACTORY: 29}
    for role, quantity in submitted.items():
        engine.submit_order(role, quantity)

    records = engine.close_week(force=True)
    by_role = {record.role: record for record in records}
    assert by_role[Role.RETAILER].order == 7
    assert by_role[Role.WHOLESALER].order == 13
    assert by_role[Role.FACTORY].order == 29
    assert by_role[Role.DISTRIBUTOR].order == 0

    assert by_role[Role.DISTRIBUTOR].was_forced is True
    for role in (Role.RETAILER, Role.WHOLESALER, Role.FACTORY):
        assert by_role[role].was_forced is False


def test_ac23_unforced_week_after_a_forced_one_is_all_false() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    engine.submit_order(Role.RETAILER, 4)
    forced = engine.close_week(force=True)
    assert [record.was_forced for record in forced].count(True) == 3

    later = play_week(engine, {role: 4 for role in ROLE_ORDER})
    assert all(record.was_forced is False for record in later)


def test_fm17_was_forced_is_false_when_every_order_is_in() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    unforced = play_week(engine, {role: 4 for role in ROLE_ORDER})
    assert all(record.was_forced is False for record in unforced)

    for role in ROLE_ORDER:
        engine.submit_order(role, 4)
    forced_but_complete = engine.close_week(force=True)
    assert all(record.was_forced is False for record in forced_but_complete)


# --------------------------------------------------------------------------
# AC 8 -- close_week returns four records in ROLE_ORDER
# --------------------------------------------------------------------------


def test_ac8_close_week_returns_records_in_role_order() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    for _ in range(3):
        records = play_week(engine, {role: 4 for role in ROLE_ORDER})
        assert len(records) == 4
        assert tuple(record.role for record in records) == ROLE_ORDER
    assert tuple(record.role for record in engine.history[:4]) == ROLE_ORDER
    assert tuple(record.role for record in engine.history[4:8]) == ROLE_ORDER


# --------------------------------------------------------------------------
# AC 9, AC 10, FM 5 -- cost arithmetic
# --------------------------------------------------------------------------


def test_ac9_week_cost_is_the_sum_of_its_four_components() -> None:
    config = make_config(
        {"fixed_order_cost": 1.5, "unit_purchase_cost": 0.25},
        demand=get_preset("CLASSIC_MIT").demand,
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    quantities = [4, 9, 0, 14, 6, 2, 11, 0, 7, 3, 5, 8]
    for quantity in quantities:
        for record in play_week(engine, {role: quantity for role in ROLE_ORDER}):
            assert record.week_cost == pytest.approx(
                record.holding_cost
                + record.backlog_cost
                + record.fixed_order_cost
                + record.purchase_cost,
                abs=1e-9,
            )


def test_ac10_cumulative_cost_accumulates_week_by_week() -> None:
    config = make_config(
        {"fixed_order_cost": 1.5, "unit_purchase_cost": 0.25},
        demand=get_preset("CLASSIC_MIT").demand,
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    previous = {role: 0.0 for role in ROLE_ORDER}
    quantities = [4, 9, 0, 14, 6, 2, 11, 0, 7, 3, 5, 8]
    for quantity in quantities:
        for record in play_week(engine, {role: quantity for role in ROLE_ORDER}):
            assert record.cumulative_cost == pytest.approx(
                previous[record.role] + record.week_cost, abs=1e-9
            )
            previous[record.role] = record.cumulative_cost


def test_fm5_an_order_is_charged_exactly_once_per_week() -> None:
    """Hand-computed: carrying 6.00 + one fixed order cost of 1.00 a week."""
    config = make_config(
        {"fixed_order_cost": 1.0},
        demand=ConstantDemand(value=4),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    expected_cumulative = [7.0, 14.0, 21.0]
    for week_index in range(3):
        records = play_week(engine, {role: 4 for role in ROLE_ORDER})
        for record in records:
            assert record.fixed_order_cost == pytest.approx(1.00)
            assert record.purchase_cost == pytest.approx(0.00)
            assert record.holding_cost == pytest.approx(6.00)
            assert record.backlog_cost == pytest.approx(0.00)
            assert record.week_cost == pytest.approx(7.00)
            assert record.cumulative_cost == pytest.approx(
                expected_cumulative[week_index]
            )


def test_fm5_a_zero_order_is_not_charged_the_fixed_cost() -> None:
    config = make_config(
        {"fixed_order_cost": 1.0},
        demand=ConstantDemand(value=4),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    for record in play_week(engine, {role: 0 for role in ROLE_ORDER}):
        assert record.fixed_order_cost == pytest.approx(0.00)
        assert record.week_cost == pytest.approx(6.00)


# --------------------------------------------------------------------------
# AC 11, AC 22, FM 13, FM 16 -- the end of the game
# --------------------------------------------------------------------------


def test_ac11_the_week_counter_stops_at_duration_weeks() -> None:
    config = make_config(demand=ConstantDemand(value=4), duration_weeks=12)
    engine = GameEngine.start(config, SEED)
    play_flat(engine, 4, 12)

    assert engine.phase == GamePhase.FINISHED
    assert engine.week == 12
    assert engine.weeks_played == 12
    assert len(engine.history) == 48
    with pytest.raises(EngineStateError):
        engine.close_week()


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_fm16_player_view_week_does_not_overrun(role: Role) -> None:
    config = make_config(demand=ConstantDemand(value=4), duration_weeks=12)
    engine = GameEngine.start(config, SEED)
    play_flat(engine, 4, 12)

    view = engine.player_view(role)
    assert view["week"] == 12
    assert view["duration_weeks"] == 12
    assert engine.host_view()["week"] == 12


def test_fm13_a_finished_game_accepts_nothing() -> None:
    config = make_config(demand=ConstantDemand(value=4), duration_weeks=8)
    engine = GameEngine.start(config, SEED)
    play_flat(engine, 4, 8)

    assert engine.phase == GamePhase.FINISHED
    with pytest.raises(EngineStateError):
        engine.close_week()
    with pytest.raises(EngineStateError):
        engine.close_week(force=True)
    with pytest.raises(EngineStateError):
        engine.submit_order(Role.RETAILER, 4)


def test_ac22_weeks_played_tracks_history_after_every_close() -> None:
    config = make_config(demand=ConstantDemand(value=4), duration_weeks=12)
    engine = GameEngine.start(config, SEED)
    assert engine.weeks_played == len(engine.history) // 4 == 0
    for week in range(1, 13):
        play_week(engine, {role: 4 for role in ROLE_ORDER})
        assert engine.weeks_played == len(engine.history) // 4 == week
    assert engine.phase == GamePhase.FINISHED
    assert engine.weeks_played == len(engine.history) // 4 == 12


# --------------------------------------------------------------------------
# AC 12 -- end_early()
# --------------------------------------------------------------------------


def test_ac12_end_early_abandons_the_open_week() -> None:
    config = make_config(demand=ConstantDemand(value=4), duration_weeks=36)
    engine = GameEngine.start(config, SEED)
    play_flat(engine, 4, 9)
    assert engine.week == 10

    engine.end_early()

    assert engine.phase == GamePhase.FINISHED
    assert engine.week == 10
    assert len(engine.history) == 4 * (engine.week - 1) == 36
    assert engine.weeks_played == engine.week - 1 == 9
    assert engine.weeks_played == len(engine.history) // 4
    assert all(record.week <= 9 for record in engine.history)

    with pytest.raises(EngineStateError):
        engine.end_early()


def test_ac12_end_early_immediately_after_start() -> None:
    config = make_config(demand=ConstantDemand(value=4), duration_weeks=12)
    engine = GameEngine.start(config, SEED)
    engine.end_early()
    assert engine.phase == GamePhase.FINISHED
    assert engine.week == 1
    assert engine.history == []
    assert engine.weeks_played == 0


def test_ac12_end_early_discards_pending_orders_for_the_open_week() -> None:
    config = make_config(demand=ConstantDemand(value=4), duration_weeks=12)
    engine = GameEngine.start(config, SEED)
    play_flat(engine, 4, 3)
    engine.submit_order(Role.RETAILER, 99)
    engine.end_early()
    assert engine.weeks_played == 3
    assert all(record.week <= 3 for record in engine.history)


# --------------------------------------------------------------------------
# AC 13 -- the six invariants of 06-role-agents.md §3.7, every week
# --------------------------------------------------------------------------


def test_ac13_agent_invariants_hold_over_a_full_classic_mit_game() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    script = [4, 4, 4, 4, 8, 12, 16, 12, 8, 4, 0, 0] * 3
    assert len(script) == 36
    previous_cost = {role: 0.0 for role in ROLE_ORDER}

    for week, quantity in enumerate(script, start=1):
        records = play_week(engine, {role: quantity for role in ROLE_ORDER})
        for record in records:
            # 1, 2 -- inventory and backlog never go negative
            assert record.closing_inventory >= 0
            assert record.closing_backlog >= 0
            # 3 -- never both at once, and never over-ship
            assert record.closing_inventory * record.closing_backlog == 0
            assert record.shipped <= record.obligation
            assert record.obligation == record.incoming_order + record.opening_backlog
            assert record.unfulfilled == record.obligation - record.shipped
            # 6 -- accumulated cost is monotonically non-decreasing
            assert record.cumulative_cost >= previous_cost[record.role] - 1e-9
            previous_cost[record.role] = record.cumulative_cost

        for role in ROLE_ORDER:
            agent = engine.agents[role]
            assert agent.inventory >= 0
            assert agent.backlog >= 0
            assert agent.inventory * agent.backlog == 0
            # 4 -- the Factory's production queue never goes negative
            if role is Role.FACTORY:
                assert engine.agents[Role.FACTORY].production_queue >= 0  # type: ignore[attr-defined]

        # 5 -- pipeline lengths, in the shape §3.2 leaves them in.  Mid-game
        # the engine is inside a week: Phase A pass 1 has advanced both
        # pipelines, pass 2 has refilled the three downstream shipment lines,
        # and nothing refills the order lines or the Factory's production
        # line until Phase C.
        finished = week == 36
        for role in ROLE_ORDER:
            agent = engine.agents[role]
            shipments_short = 0 if finished or role is not Role.FACTORY else 1
            assert len(agent.shipments) == agent.shipments.length - shipments_short
            if agent.orders is not None:
                orders_short = 0 if finished else 1
                assert len(agent.orders) == agent.orders.length - orders_short

    assert engine.phase == GamePhase.FINISHED


# --------------------------------------------------------------------------
# FM 6 -- pipeline length drift over a long game
# --------------------------------------------------------------------------


def test_fm6_no_pipeline_drift_after_one_hundred_and_four_weeks() -> None:
    config = make_config(
        {"shipping_delay_weeks": 3, "information_delay_weeks": 4},
        {"production_delay_weeks": 2},
        demand=ConstantDemand(value=4),
        duration_weeks=104,
    )
    engine = GameEngine.start(config, SEED)
    script = [4, 6, 2, 9, 0, 12, 5, 7]
    for week in range(104):
        quantity = script[week % len(script)]
        play_week(engine, {role: quantity for role in ROLE_ORDER})

    assert engine.phase == GamePhase.FINISHED
    assert engine.weeks_played == 104
    for role in ROLE_ORDER:
        agent = engine.agents[role]
        assert len(agent.shipments) == agent.shipments.length
        assert agent.shipments.length == config.inbound_delay_weeks(role)
        if agent.orders is not None:
            assert len(agent.orders) == agent.orders.length
            assert agent.orders.length == config.order_delay_weeks(role)


# --------------------------------------------------------------------------
# FM 7 -- no other role's order quantity in a player view
# --------------------------------------------------------------------------


def test_fm7_player_view_never_carries_another_roles_order() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    orders = {
        Role.RETAILER: 5,
        Role.WHOLESALER: 7,
        Role.DISTRIBUTOR: 13,
        Role.FACTORY: 29,
    }
    for role, quantity in orders.items():
        engine.submit_order(role, quantity)

    for role in ROLE_ORDER:
        view = engine.player_view(role)
        for other, quantity in orders.items():
            if other is role:
                continue
            assert not contains_number(
                view, quantity
            ), f"{role.value} view leaked {other.value}'s order {quantity}"


def test_fm7_awaiting_roles_carries_names_only() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    engine.submit_order(Role.RETAILER, 5)
    engine.submit_order(Role.WHOLESALER, 7)

    view = engine.player_view(Role.RETAILER)
    assert view["has_submitted"] is True
    assert set(view["awaiting_roles"]) == {Role.DISTRIBUTOR, Role.FACTORY}
    assert not contains_number(view["awaiting_roles"], 7)

    other = engine.player_view(Role.DISTRIBUTOR)
    assert other["has_submitted"] is False


# --------------------------------------------------------------------------
# FM 8 -- future demand never leaks
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_fm8_customer_demand_series_is_truncated_to_the_open_week(role: Role) -> None:
    config = make_config(
        demand=get_preset("CLASSIC_MIT").demand,
        duration_weeks=36,
        visibility=VisibilityConfig(show_true_customer_demand_to_all=True),
    )
    engine = GameEngine.start(config, SEED)
    play_flat(engine, 4, 4)
    assert engine.week == 5

    view = engine.player_view(role)
    shared = view["customer_demand_series"]
    assert len(shared) == 5
    assert list(shared) == engine.demand_series[:5]


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_fm8_customer_demand_series_absent_when_not_shared(role: Role) -> None:
    config = make_config(
        demand=get_preset("CLASSIC_MIT").demand,
        duration_weeks=36,
        visibility=VisibilityConfig(show_true_customer_demand_to_all=False),
    )
    engine = GameEngine.start(config, SEED)
    play_flat(engine, 4, 4)
    assert "customer_demand_series" not in engine.player_view(role)


# --------------------------------------------------------------------------
# AC 17 -- all 64 visibility combinations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("combination", list(product([False, True], repeat=6)))
def test_ac17_no_order_quantity_leaks_under_any_visibility(
    combination: tuple[bool, ...],
) -> None:
    flags = dict(zip(VISIBILITY_FLAGS, combination))
    config = make_config(
        demand=get_preset("CLASSIC_MIT").demand,
        duration_weeks=36,
        visibility=VisibilityConfig(**flags),
    )
    engine = GameEngine.start(config, SEED)

    week_one = {
        Role.RETAILER: 1_001,
        Role.WHOLESALER: 1_003,
        Role.DISTRIBUTOR: 1_007,
        Role.FACTORY: 1_009,
    }
    week_two = {
        Role.RETAILER: 1_013,
        Role.WHOLESALER: 1_019,
        Role.DISTRIBUTOR: 1_021,
        Role.FACTORY: 1_031,
    }
    play_week(engine, week_one)
    for role, quantity in week_two.items():
        engine.submit_order(role, quantity)

    for role in ROLE_ORDER:
        view = engine.player_view(role)
        for other in ROLE_ORDER:
            if other is role:
                continue
            for quantity in (week_one[other], week_two[other]):
                assert not contains_number(view, quantity), (
                    f"{role.value} view leaked {other.value}'s order "
                    f"{quantity} with {flags}"
                )


# --------------------------------------------------------------------------
# AC 25, FM 15 -- orders_in_flight is the SUPPLIER's order pipeline
# --------------------------------------------------------------------------


def test_fm15_player_view_reports_the_suppliers_order_pipeline() -> None:
    config = make_config(
        {"information_delay_weeks": 2},
        demand=ConstantDemand(value=4),
        duration_weeks=12,
        per_role={
            Role.RETAILER: {"initial_order_in_pipeline": 5},
            Role.WHOLESALER: {"initial_order_in_pipeline": 6},
            Role.DISTRIBUTOR: {"initial_order_in_pipeline": 7},
            Role.FACTORY: {"initial_order_in_pipeline": 8},
        },
    )
    engine = GameEngine.start(config, SEED)

    expected_total = {
        Role.RETAILER: 6,
        Role.WHOLESALER: 7,
        Role.DISTRIBUTOR: 8,
        Role.FACTORY: 0,
    }
    expected_slots = {
        Role.RETAILER: [6],
        Role.WHOLESALER: [7],
        Role.DISTRIBUTOR: [8],
        Role.FACTORY: [],
    }
    own_figure = {
        Role.RETAILER: 0,
        Role.WHOLESALER: 6,
        Role.DISTRIBUTOR: 7,
        Role.FACTORY: 8,
    }
    for role in ROLE_ORDER:
        view = engine.player_view(role)
        assert view["orders_in_flight"] == expected_total[role]
        assert list(view["orders_in_flight_slots"]) == expected_slots[role]
        # the naive wiring -- agents[role].orders_in_flight() -- differs here
        assert engine.agents[role].orders_in_flight() == own_figure[role]


def test_ac25_orders_in_flight_equals_the_suppliers_pipeline_total() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    script = [4, 9, 2, 14, 7]
    for quantity in script:
        for role in ROLE_ORDER:
            supplier = role.upstream
            view = engine.player_view(role)
            if supplier is None:
                assert view["orders_in_flight"] == 0
                assert list(view["orders_in_flight_slots"]) == []
            else:
                pipeline = engine.agents[supplier].orders
                assert pipeline is not None
                assert view["orders_in_flight"] == pipeline.total()
                assert list(view["orders_in_flight_slots"]) == pipeline.slots()
            assert view["orders_in_flight"] == sum(view["orders_in_flight_slots"])
        play_week(engine, {role: quantity for role in ROLE_ORDER})


# --------------------------------------------------------------------------
# §3.8 -- the rest of the player view's fixed keys
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_player_view_always_included_keys(role: Role) -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    view = engine.player_view(role)

    for key in (
        "role",
        "week",
        "duration_weeks",
        "phase",
        "currency_symbol",
        "inventory",
        "backlog",
        "supply_line",
        "supply_line_slots",
        "orders_in_flight",
        "orders_in_flight_slots",
        "incoming_order",
        "last_order",
        "settlement",
        "has_submitted",
        "awaiting_roles",
        "own_history",
        "max_order_quantity",
        "allow_negative_orders",
        "show_supply_line_prominently",
        "order_arrival_lead_weeks",
        "production_queue",
        "holding_cost_per_unit_week",
        "backlog_cost_per_unit_week",
    ):
        assert key in view, key

    assert view["role"] == role
    assert view["week"] == 1
    assert view["duration_weeks"] == 36
    assert view["phase"] == GamePhase.DECISION
    assert view["currency_symbol"] == "$"
    assert view["inventory"] == 12
    assert view["backlog"] == 0
    assert view["supply_line"] == (4 if role is Role.FACTORY else 8)
    assert view["supply_line"] == sum(view["supply_line_slots"])
    assert view["incoming_order"] == 4
    assert view["last_order"] is None
    assert view["has_submitted"] is False
    assert list(view["own_history"]) == []
    assert view["holding_cost_per_unit_week"] == pytest.approx(0.50)
    assert view["backlog_cost_per_unit_week"] == pytest.approx(1.00)


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_player_view_production_queue_is_factory_only(role: Role) -> None:
    config = make_config(
        {"initial_inventory": 12},
        {"production_capacity_per_week": 2},
        demand=ConstantDemand(value=4),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    play_week(
        engine,
        {
            Role.RETAILER: 4,
            Role.WHOLESALER: 4,
            Role.DISTRIBUTOR: 4,
            Role.FACTORY: 10,
        },
    )
    view = engine.player_view(role)
    if role is Role.FACTORY:
        assert view["production_queue"] == 8
    else:
        assert view["production_queue"] is None


def test_player_view_order_arrival_lead_weeks() -> None:
    config = make_config(
        {"shipping_delay_weeks": 3, "information_delay_weeks": 4},
        {"production_delay_weeks": 2},
        demand=ConstantDemand(value=4),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    for role in (Role.RETAILER, Role.WHOLESALER, Role.DISTRIBUTOR):
        supplier = role.upstream
        assert supplier is not None
        expected = config.order_delay_weeks(supplier) + config.inbound_delay_weeks(role)
        assert engine.player_view(role)["order_arrival_lead_weeks"] == expected
    factory_view = engine.player_view(Role.FACTORY)
    assert factory_view["order_arrival_lead_weeks"] == config.inbound_delay_weeks(
        Role.FACTORY
    )


def test_player_view_conditional_keys_follow_their_gates() -> None:
    demand = get_preset("CLASSIC_MIT").demand
    off = make_config(
        demand=demand,
        duration_weeks=12,
        visibility=VisibilityConfig(
            show_running_cost_to_players=False,
            show_neighbour_inventory=False,
            show_all_inventories=False,
            show_leaderboard_during_game=False,
            show_true_customer_demand_to_all=False,
        ),
    )
    view = GameEngine.start(off, SEED).player_view(Role.WHOLESALER)
    for key in (
        "accumulated_cost",
        "week_cost",
        "balance",
        "customer_demand_series",
        "neighbours",
        "chain",
        "leaderboard",
    ):
        assert key not in view, key

    on = make_config(
        demand=demand,
        duration_weeks=12,
        visibility=VisibilityConfig(
            show_running_cost_to_players=True,
            show_neighbour_inventory=True,
            show_all_inventories=True,
            show_leaderboard_during_game=True,
            show_true_customer_demand_to_all=True,
        ),
    )
    view = GameEngine.start(on, SEED).player_view(Role.WHOLESALER)
    assert "accumulated_cost" in view
    assert "week_cost" in view
    assert set(view["neighbours"]) == {Role.RETAILER, Role.DISTRIBUTOR}
    assert set(view["chain"]) == set(ROLE_ORDER)
    for entry in view["neighbours"].values():
        assert set(entry) == {"inventory", "backlog"}
    for entry in view["chain"].values():
        assert set(entry) == {"inventory", "backlog"}
    assert len(view["leaderboard"]) == 4
    for entry in view["leaderboard"]:
        assert set(entry) == {"role", "accumulated_cost"}


def test_player_view_balance_follows_starting_capital() -> None:
    without = make_config(demand=ConstantDemand(value=4), duration_weeks=12)
    engine = GameEngine.start(without, SEED)
    assert "balance" not in engine.player_view(Role.RETAILER)

    with_capital = make_config(
        {"starting_capital": 1_000.0},
        demand=ConstantDemand(value=4),
        duration_weeks=12,
    )
    engine = GameEngine.start(with_capital, SEED)
    view = engine.player_view(Role.RETAILER)
    assert view["balance"] == pytest.approx(1_000.0 - view["accumulated_cost"])


# --------------------------------------------------------------------------
# AC 26, §3.9 -- host_view()
# --------------------------------------------------------------------------


def test_ac26_host_view_production_queue_is_zero_away_from_the_factory() -> None:
    config = make_config(
        factory_kwargs={"production_capacity_per_week": 2},
        demand=ConstantDemand(value=4),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    play_week(
        engine,
        {
            Role.RETAILER: 4,
            Role.WHOLESALER: 4,
            Role.DISTRIBUTOR: 4,
            Role.FACTORY: 10,
        },
    )

    roles = engine.host_view()["roles"]
    for role in (Role.RETAILER, Role.WHOLESALER, Role.DISTRIBUTOR):
        assert roles[role]["production_queue"] == 0
        assert roles[role]["production_queue"] is not None
    assert roles[Role.FACTORY]["production_queue"] == 8
    assert engine.agents[Role.FACTORY].production_queue == 8  # type: ignore[attr-defined]


def test_host_view_keys_and_unredacted_series() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    engine.submit_order(Role.RETAILER, 5)
    view = engine.host_view()

    assert set(view) == {
        "week",
        "duration_weeks",
        "phase",
        "currency_symbol",
        "demand_series",
        "awaiting_roles",
        "chain_total_cost",
        "roles",
    }
    assert view["week"] == 1
    assert view["duration_weeks"] == 36
    assert view["phase"] == GamePhase.DECISION
    assert list(view["demand_series"]) == engine.demand_series
    assert len(view["demand_series"]) == 36
    assert set(view["awaiting_roles"]) == {
        Role.WHOLESALER,
        Role.DISTRIBUTOR,
        Role.FACTORY,
    }
    assert view["chain_total_cost"] == pytest.approx(24.0)

    assert list(view["roles"]) == list(ROLE_ORDER)
    for role in ROLE_ORDER:
        entry = view["roles"][role]
        assert set(entry) == {
            "inventory",
            "backlog",
            "supply_line",
            "orders_in_flight",
            "last_order",
            "incoming_order",
            "accumulated_cost",
            "production_queue",
            "has_submitted",
            "is_bot",
        }
        assert entry["inventory"] == 12
        assert entry["backlog"] == 0
        assert entry["is_bot"] is False
        assert entry["has_submitted"] is (role is Role.RETAILER)


def test_host_view_orders_in_flight_is_the_roles_own_pipeline() -> None:
    """§3.9 -- the host sees the unredacted figure, not player_view's."""
    config = make_config(
        {"information_delay_weeks": 2},
        demand=ConstantDemand(value=4),
        duration_weeks=12,
        per_role={
            Role.RETAILER: {"initial_order_in_pipeline": 5},
            Role.WHOLESALER: {"initial_order_in_pipeline": 6},
            Role.DISTRIBUTOR: {"initial_order_in_pipeline": 7},
            Role.FACTORY: {"initial_order_in_pipeline": 8},
        },
    )
    engine = GameEngine.start(config, SEED)
    roles = engine.host_view()["roles"]
    assert roles[Role.RETAILER]["orders_in_flight"] == 0
    assert roles[Role.WHOLESALER]["orders_in_flight"] == 6
    assert roles[Role.DISTRIBUTOR]["orders_in_flight"] == 7
    assert roles[Role.FACTORY]["orders_in_flight"] == 8


# --------------------------------------------------------------------------
# AC 24 -- set_bot
# --------------------------------------------------------------------------


def test_ac24_set_bot_marks_only_subsequent_records() -> None:
    config = make_config(demand=ConstantDemand(value=4), duration_weeks=12)
    engine = GameEngine.start(config, SEED)

    first = play_week(engine, {role: 4 for role in ROLE_ORDER})
    assert all(record.was_bot is False for record in first)

    engine.set_bot(Role.WHOLESALER)
    second = play_week(engine, {role: 4 for role in ROLE_ORDER})
    by_role = {record.role: record for record in second}
    assert by_role[Role.WHOLESALER].was_bot is True
    for role in (Role.RETAILER, Role.DISTRIBUTOR, Role.FACTORY):
        assert by_role[role].was_bot is False

    # the week-1 records are untouched
    assert all(record.was_bot is False for record in engine.history[:4])

    restored = GameEngine.from_payload(engine.to_payload(), config)
    third = play_week(restored, {role: 4 for role in ROLE_ORDER})
    assert {record.role: record.was_bot for record in third} == {
        Role.RETAILER: False,
        Role.WHOLESALER: True,
        Role.DISTRIBUTOR: False,
        Role.FACTORY: False,
    }
    assert restored.host_view()["roles"][Role.WHOLESALER]["is_bot"] is True


def test_ac24_set_bot_is_idempotent_and_reversible() -> None:
    config = make_config(demand=ConstantDemand(value=4), duration_weeks=12)
    engine = GameEngine.start(config, SEED)
    engine.set_bot(Role.FACTORY)
    engine.set_bot(Role.FACTORY)
    assert engine.agents[Role.FACTORY].is_bot is True

    engine.set_bot(Role.FACTORY, False)
    records = play_week(engine, {role: 4 for role in ROLE_ORDER})
    assert all(record.was_bot is False for record in records)


def test_set_bot_rejects_a_non_role() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    with pytest.raises(ValueError):
        engine.set_bot("NOT_A_ROLE")  # type: ignore[arg-type]


def test_start_bot_roles_are_marked_from_week_one() -> None:
    config = make_config(demand=ConstantDemand(value=4), duration_weeks=12)
    engine = GameEngine.start(config, SEED, bot_roles=frozenset({Role.FACTORY}))
    records = play_week(engine, {role: 4 for role in ROLE_ORDER})
    by_role = {record.role: record for record in records}
    assert by_role[Role.FACTORY].was_bot is True
    for role in (Role.RETAILER, Role.WHOLESALER, Role.DISTRIBUTOR):
        assert by_role[role].was_bot is False


# --------------------------------------------------------------------------
# AC 21 -- app/core/game_engine.py imports only app.core and the stdlib
# --------------------------------------------------------------------------


def imported_modules(path: Path, package: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    parts = package.split(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = ".".join(parts[: len(parts) - node.level + 1])
                found.add(f"{base}.{node.module}" if node.module else base)
            elif node.module:
                found.add(node.module)
    return found


@pytest.mark.parametrize("module_name", ["game_engine", "stats"])
def test_ac21_domain_modules_import_only_app_core_and_the_stdlib(
    module_name: str,
) -> None:
    path = Path(__file__).resolve().parents[2] / "app" / "core" / f"{module_name}.py"
    assert path.is_file(), path

    for name in imported_modules(path, "app.core"):
        root = name.split(".")[0]
        assert (
            name == "app.core"
            or name.startswith("app.core.")
            or (root in sys.stdlib_module_names)
        ), f"{module_name}.py imports {name}"
        # 00-conventions.md §4 -- neither is a domain module
        assert name != "app.core.checks"
        assert not name.startswith("app.core.checks.")
        assert name != "app.core.firebase"
