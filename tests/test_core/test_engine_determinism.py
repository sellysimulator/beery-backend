"""Determinism, order independence and persistence for section 07.

Covers ``07-game-engine.md §5`` acceptance criteria 4 (order independence),
14 (replay determinism), 15 (seeded demand) and 16 (``to_payload`` /
``from_payload`` round trip), plus ``§3.7``'s purity statement.

Driven entirely through the frozen public surface of ``§2``.
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from app.core.config_models import (
    DEFAULT_LIMITS,
    ConstantDemand,
    DemandConfig,
    FactoryConfig,
    GameConfig,
    RoleConfig,
    StochasticDemand,
)
from app.core.enums import ROLE_ORDER, Role
from app.core.game_engine import EngineStateError, GameEngine, GamePhase, WeekRecord
from app.core.presets import get_preset

SEED = 20260919
REVERSED_ORDER = tuple(reversed(ROLE_ORDER))


def make_config(
    role_kwargs: dict[str, Any] | None = None,
    factory_kwargs: dict[str, Any] | None = None,
    *,
    demand: DemandConfig | None = None,
    duration_weeks: int = 36,
) -> GameConfig:
    base = dict(role_kwargs or {})
    factory_base = {**base, **dict(factory_kwargs or {})}
    return GameConfig.from_host_input(
        {
            "roles": {
                Role.RETAILER.value: RoleConfig(**base),
                Role.WHOLESALER.value: RoleConfig(**base),
                Role.DISTRIBUTOR.value: RoleConfig(**base),
                Role.FACTORY.value: FactoryConfig(**factory_base),
            },
            "demand": demand if demand is not None else ConstantDemand(value=4),
            "duration_weeks": duration_weeks,
        },
        DEFAULT_LIMITS,
    )


def order_script(weeks: int, seed: int = 11) -> list[dict[Role, int]]:
    """A fixed, reproducible script of one order per role per week."""
    rng = random.Random(seed)
    return [{role: rng.randint(0, 20) for role in ROLE_ORDER} for _ in range(weeks)]


def drive(engine: GameEngine, script: list[dict[Role, int]]) -> list[WeekRecord]:
    produced: list[WeekRecord] = []
    for week in script:
        for role in ROLE_ORDER:
            engine.submit_order(role, week[role])
        produced.extend(engine.close_week())
    return produced


def record_tuple(record: WeekRecord) -> tuple[Any, ...]:
    """Every declared field of a ``WeekRecord``, in the §2 order."""
    return (
        record.role,
        record.week,
        record.opening_inventory,
        record.opening_backlog,
        record.arrived,
        record.incoming_order,
        record.obligation,
        record.shipped,
        record.unfulfilled,
        record.closing_inventory,
        record.closing_backlog,
        record.supply_line_after,
        record.orders_in_flight_after,
        record.order,
        record.was_bot,
        record.was_forced,
        record.holding_cost,
        record.backlog_cost,
        record.fixed_order_cost,
        record.purchase_cost,
        record.week_cost,
        record.cumulative_cost,
        record.production_started,
        record.production_queued,
    )


# --------------------------------------------------------------------------
# AC 4 -- order independence (D9)
# --------------------------------------------------------------------------


def test_ac4_settlement_is_independent_of_the_role_order() -> None:
    config = make_config(demand=get_preset("CLASSIC_MIT").demand, duration_weeks=12)
    script = order_script(12)

    forward = GameEngine.start(config, SEED)
    reverse = GameEngine.start(config, SEED, role_order=REVERSED_ORDER)

    forward_records = drive(forward, script)
    reverse_records = drive(reverse, script)

    assert forward.phase == reverse.phase == GamePhase.FINISHED
    # criterion 8: both histories are in ROLE_ORDER, so this compares state
    # rather than two orderings
    assert tuple(r.role for r in forward.history[:4]) == ROLE_ORDER
    assert tuple(r.role for r in reverse.history[:4]) == ROLE_ORDER

    assert [record_tuple(r) for r in forward_records] == [
        record_tuple(r) for r in reverse_records
    ]
    assert [record_tuple(r) for r in forward.history] == [
        record_tuple(r) for r in reverse.history
    ]
    assert forward.to_payload() == reverse.to_payload()
    assert forward == reverse


def test_ac4_supply_line_and_orders_in_flight_survive_the_reversal() -> None:
    """FM 1's detector: these are the two fields pass 3 exists for."""
    config = make_config(demand=get_preset("CLASSIC_MIT").demand, duration_weeks=12)
    script = order_script(12, seed=77)

    forward = drive(GameEngine.start(config, SEED), script)
    reverse = drive(GameEngine.start(config, SEED, role_order=REVERSED_ORDER), script)

    forward_pairs = [(r.role, r.week, r.supply_line_after) for r in forward]
    reverse_pairs = [(r.role, r.week, r.supply_line_after) for r in reverse]
    assert forward_pairs == reverse_pairs

    forward_flight = [(r.role, r.week, r.orders_in_flight_after) for r in forward]
    reverse_flight = [(r.role, r.week, r.orders_in_flight_after) for r in reverse]
    assert forward_flight == reverse_flight


def test_ac4_role_order_takes_no_part_in_equality_or_the_payload() -> None:
    config = make_config(demand=ConstantDemand(value=4), duration_weeks=12)
    forward = GameEngine.start(config, SEED)
    reverse = GameEngine.start(config, SEED, role_order=REVERSED_ORDER)

    assert forward.role_order == ROLE_ORDER
    assert reverse.role_order == REVERSED_ORDER
    assert forward == reverse
    assert forward.to_payload() == reverse.to_payload()


# --------------------------------------------------------------------------
# AC 14 -- replay determinism (§3.7)
# --------------------------------------------------------------------------


def test_ac14_replaying_the_recorded_orders_reproduces_history() -> None:
    config = get_preset("CLASSIC_MIT")
    script = order_script(36, seed=5)

    original = GameEngine.start(config, SEED)
    drive(original, script)
    assert original.phase == GamePhase.FINISHED

    # Only the config, the seed and the recorded orders are carried over.
    replayed_script: list[dict[Role, int]] = []
    for week in range(1, original.weeks_played + 1):
        replayed_script.append(
            {r.role: r.order for r in original.history if r.week == week}
        )

    replay = GameEngine.start(config, original.seed)
    drive(replay, replayed_script)

    assert [record_tuple(r) for r in replay.history] == [
        record_tuple(r) for r in original.history
    ]
    assert replay.to_payload() == original.to_payload()
    assert replay == original


def test_ac14_two_identically_driven_engines_are_byte_identical() -> None:
    config = get_preset("CLASSIC_MIT")
    script = order_script(36, seed=6)

    first = GameEngine.start(config, SEED, bot_roles=frozenset({Role.DISTRIBUTOR}))
    second = GameEngine.start(config, SEED, bot_roles=frozenset({Role.DISTRIBUTOR}))
    drive(first, script)
    drive(second, script)

    assert json.dumps(first.to_payload(), sort_keys=True) == json.dumps(
        second.to_payload(), sort_keys=True
    )
    assert first == second


def test_ac14_a_different_order_script_produces_a_different_history() -> None:
    """The replay assertion would be vacuous if history ignored the orders."""
    config = get_preset("CLASSIC_MIT")
    first = GameEngine.start(config, SEED)
    second = GameEngine.start(config, SEED)
    drive(first, order_script(12, seed=5)[:12])
    drive(second, order_script(12, seed=9)[:12])
    assert [record_tuple(r) for r in first.history] != [
        record_tuple(r) for r in second.history
    ]


# --------------------------------------------------------------------------
# AC 15 -- the seed drives the demand series
# --------------------------------------------------------------------------


def test_ac15_same_seed_same_demand_series() -> None:
    config = make_config(
        demand=StochasticDemand(mean=8.0, stdev=2.0, min=0, max=20),
        duration_weeks=36,
    )
    first = GameEngine.start(config, 4242)
    second = GameEngine.start(config, 4242)
    assert first.demand_series == second.demand_series
    assert first.seed == second.seed == 4242


def test_ac15_different_seeds_differ_for_a_stochastic_config() -> None:
    config = make_config(
        demand=StochasticDemand(mean=8.0, stdev=2.0, min=0, max=20),
        duration_weeks=36,
    )
    first = GameEngine.start(config, 1)
    second = GameEngine.start(config, 2)
    assert first.demand_series != second.demand_series


def test_ac15_a_deterministic_generator_ignores_the_seed() -> None:
    config = make_config(
        demand=get_preset("CLASSIC_MIT").demand,
        duration_weeks=36,
    )
    assert (
        GameEngine.start(config, 1).demand_series
        == GameEngine.start(config, 999_999).demand_series
    )


# --------------------------------------------------------------------------
# AC 16 -- to_payload() / from_payload() round trip (§3.11)
# --------------------------------------------------------------------------


def assert_round_trips(engine: GameEngine, config: GameConfig) -> None:
    payload = engine.to_payload()
    # "Full engine state as a JSON-safe dict"
    reserialised = json.loads(json.dumps(payload))
    restored = GameEngine.from_payload(reserialised, config)

    assert restored == engine
    assert restored.to_payload() == payload
    assert restored.seed == engine.seed
    assert restored.week == engine.week
    assert restored.phase == engine.phase
    assert restored.demand_series == engine.demand_series
    assert restored.weeks_played == engine.weeks_played
    assert [record_tuple(r) for r in restored.history] == [
        record_tuple(r) for r in engine.history
    ]
    assert "config" not in payload
    assert "bot_roles" not in payload
    assert "weeks_played" not in payload


def test_ac16_round_trip_immediately_after_start() -> None:
    config = make_config(demand=get_preset("CLASSIC_MIT").demand, duration_weeks=12)
    engine = GameEngine.start(config, SEED)
    assert engine.phase == GamePhase.DECISION
    assert_round_trips(engine, config)


def test_ac16_round_trip_mid_decision_with_two_of_four_orders_in() -> None:
    config = make_config(demand=get_preset("CLASSIC_MIT").demand, duration_weeks=12)
    engine = GameEngine.start(config, SEED)
    drive(engine, order_script(4, seed=3))
    engine.submit_order(Role.RETAILER, 9)
    engine.submit_order(Role.FACTORY, 17)
    assert engine.all_orders_in() is False

    assert_round_trips(engine, config)

    restored = GameEngine.from_payload(engine.to_payload(), config)
    assert restored.all_orders_in() is False
    restored.submit_order(Role.WHOLESALER, 3)
    restored.submit_order(Role.DISTRIBUTOR, 5)
    engine.submit_order(Role.WHOLESALER, 3)
    engine.submit_order(Role.DISTRIBUTOR, 5)
    assert [record_tuple(r) for r in restored.close_week()] == [
        record_tuple(r) for r in engine.close_week()
    ]


def test_ac16_round_trip_at_finished() -> None:
    config = make_config(demand=get_preset("CLASSIC_MIT").demand, duration_weeks=12)
    engine = GameEngine.start(config, SEED)
    drive(engine, order_script(12, seed=4))
    assert engine.phase == GamePhase.FINISHED

    assert_round_trips(engine, config)
    restored = GameEngine.from_payload(engine.to_payload(), config)
    with pytest.raises(EngineStateError):
        restored.close_week()


def test_ac16_round_trip_after_end_early() -> None:
    config = make_config(demand=get_preset("CLASSIC_MIT").demand, duration_weeks=36)
    engine = GameEngine.start(config, SEED)
    drive(engine, order_script(9, seed=8))
    engine.end_early()
    assert engine.phase == GamePhase.FINISHED
    assert engine.week == 10
    assert_round_trips(engine, config)


def test_ac16_equality_is_by_value_not_identity() -> None:
    """Without ``__eq__`` the round-trip criterion would be vacuous."""
    config = make_config(demand=get_preset("CLASSIC_MIT").demand, duration_weeks=12)
    first = GameEngine.start(config, SEED)
    second = GameEngine.start(config, SEED)
    assert first is not second
    assert first == second

    second.submit_order(Role.RETAILER, 7)
    assert first != second

    first.submit_order(Role.RETAILER, 7)
    assert first == second

    third = GameEngine.start(config, SEED + 1)
    assert first != third
    assert first != object()
