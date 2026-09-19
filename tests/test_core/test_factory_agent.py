"""Black-box tests for the Factory half of section 06 -- ``06-role-agents.md``.

Covers acceptance criteria 6, 11 and 12, the Factory worked example of §4
(the third of the three), failure modes 5, 6 and 7, and §3.6's legal
cancellations.  The rest of §5 and §6 is in ``test_agents.py``.

Only the frozen public surfaces of sections 03, 05 and 06 are imported.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.agents import FactoryAgent, RoleAgent, agent_for
from app.core.config_models import (
    DEFAULT_LIMITS,
    ConstantDemand,
    FactoryConfig,
    GameConfig,
    RoleConfig,
)
from app.core.enums import Role
from app.core.pipeline import Pipeline
from app.core.presets import get_preset
from app.core.records import ProductionOutcome


def make_config(**factory_kwargs: Any) -> GameConfig:
    """A `GameConfig` whose FACTORY carries ``factory_kwargs``.

    Built through section 03's public ``from_host_input``, which accepts
    "plain JSON or already-built config models" (``03-game-config.md §2``).
    """
    return GameConfig.from_host_input(
        {
            "roles": {
                Role.RETAILER.value: RoleConfig(),
                Role.WHOLESALER.value: RoleConfig(),
                Role.DISTRIBUTOR.value: RoleConfig(),
                Role.FACTORY.value: FactoryConfig(**factory_kwargs),
            },
            "demand": ConstantDemand(value=4),
        },
        DEFAULT_LIMITS,
    )


def classic_mit_with(**factory_overrides: Any) -> GameConfig:
    """`CLASSIC_MIT` with the FACTORY's parameters overridden.

    Uses only section 03's public `to_payload()` / `from_payload()`, whose
    payload keys the FACTORY's `roles` entry by the role name.
    """
    payload = get_preset("CLASSIC_MIT").to_payload()
    payload["roles"][Role.FACTORY.value].update(factory_overrides)
    return GameConfig.from_payload(payload)


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def test_agent_for_builds_a_factory_agent() -> None:
    factory = agent_for(Role.FACTORY, get_preset("CLASSIC_MIT"))
    assert type(factory) is FactoryAgent
    assert isinstance(factory, RoleAgent)
    assert factory.role == Role.FACTORY
    assert factory.production_queue == 0
    assert isinstance(factory.orders, Pipeline)


# --------------------------------------------------------------------------
# AC 6 and FM 5 -- the Factory's pipeline is the PRODUCTION pipeline
# --------------------------------------------------------------------------


def test_factory_pipeline_uses_production_delay_not_shipping_delay() -> None:
    config = make_config(
        shipping_delay_weeks=2,
        production_delay_weeks=5,
        information_delay_weeks=3,
        initial_pipeline_quantity=4,
    )
    factory = agent_for(Role.FACTORY, config)

    assert factory.shipments.length == 5
    assert len(factory.shipments) == 5
    assert factory.supply_line() == 20
    assert factory.orders is not None
    assert factory.orders.length == 3


def test_a_non_factory_role_still_uses_the_shipping_delay() -> None:
    config = make_config(shipping_delay_weeks=2, production_delay_weeks=5)
    wholesaler = agent_for(Role.WHOLESALER, config)
    assert wholesaler.shipments.length == 2


# --------------------------------------------------------------------------
# AC 5 (third worked example), AC 12 -- capacity and queue
# --------------------------------------------------------------------------


def test_worked_example_factory_capacity_and_queue() -> None:
    """§4: `cfg` is CLASSIC_MIT with `production_capacity_per_week` set to 10."""
    config = classic_mit_with(production_capacity_per_week=10)
    assert config.factory_config().production_capacity_per_week == 10
    factory = FactoryAgent(Role.FACTORY, config)
    assert factory.production_queue == 0

    outcome = factory.start_production(14)
    assert isinstance(outcome, ProductionOutcome)
    assert outcome.requested == 14
    assert outcome.started == 10
    assert outcome.queued == 4
    assert factory.production_queue == 4

    outcome = factory.start_production(3)
    assert outcome.requested == 7
    assert outcome.started == 7
    assert outcome.queued == 0
    assert factory.production_queue == 0


def test_started_units_enter_the_production_pipeline() -> None:
    config = make_config(
        production_capacity_per_week=10,
        production_delay_weeks=3,
        initial_pipeline_quantity=0,
    )
    factory = agent_for(Role.FACTORY, config)
    assert factory.supply_line() == 0

    arriving, _ = factory.advance()
    assert arriving == 0
    factory.start_production(14)

    assert factory.supply_line() == 10
    assert len(factory.shipments) == factory.shipments.length


def test_queued_units_are_produced_in_later_weeks() -> None:
    """AC 12: the queue drains first-in-first-out across weeks."""
    config = make_config(
        production_capacity_per_week=10,
        production_delay_weeks=2,
        initial_pipeline_quantity=0,
    )
    factory = agent_for(Role.FACTORY, config)

    started = []
    for request in (25, 0, 0, 0):
        factory.advance()
        factory.receive_order(0)
        outcome = factory.start_production(request)
        started.append(outcome.started)
        assert len(factory.shipments) == factory.shipments.length

    assert started == [10, 10, 5, 0]
    assert factory.production_queue == 0


# --------------------------------------------------------------------------
# AC 11 -- unlimited capacity
# --------------------------------------------------------------------------


def test_unlimited_capacity_starts_everything_and_queues_nothing() -> None:
    config = make_config(
        production_capacity_per_week=None,
        production_delay_weeks=2,
        initial_pipeline_quantity=0,
    )
    factory = agent_for(Role.FACTORY, config)

    for request in (0, 1, 500, 9_999):
        factory.advance()
        factory.receive_order(0)
        outcome = factory.start_production(request)
        assert outcome.requested == request
        assert outcome.started == request
        assert outcome.queued == 0
        assert factory.production_queue == 0

    assert factory.supply_line() == 9_999 + 500


# --------------------------------------------------------------------------
# FM 6 -- excess production is queued, never lost
# --------------------------------------------------------------------------


def test_excess_production_is_queued_not_lost() -> None:
    config = make_config(
        production_capacity_per_week=10,
        production_delay_weeks=2,
        initial_pipeline_quantity=0,
    )
    factory = agent_for(Role.FACTORY, config)

    total_started = 0
    outcome = factory.start_production(14)
    total_started += outcome.started
    assert outcome.started == 10
    assert factory.production_queue == 4

    for _ in range(3):
        outcome = factory.start_production(0)
        total_started += outcome.started

    assert total_started == 14
    assert factory.production_queue == 0


def test_a_long_overload_conserves_every_requested_unit() -> None:
    config = make_config(
        production_capacity_per_week=7,
        production_delay_weeks=2,
        initial_pipeline_quantity=0,
    )
    factory = agent_for(Role.FACTORY, config)

    requests = [20, 3, 0, 11, 0, 0, 0, 0, 0, 0]
    total_started = 0
    for request in requests:
        outcome = factory.start_production(request)
        assert outcome.started <= 7
        assert outcome.queued >= 0
        total_started += outcome.started

    assert total_started == sum(requests)
    assert factory.production_queue == 0


# --------------------------------------------------------------------------
# §3.6 -- a negative qty cancels against the queue
# --------------------------------------------------------------------------


def test_a_cancellation_draws_against_the_production_queue() -> None:
    config = make_config(
        production_capacity_per_week=10,
        production_delay_weeks=2,
        initial_pipeline_quantity=0,
    )
    factory = agent_for(Role.FACTORY, config)
    factory.advance()
    factory.receive_order(0)
    factory.start_production(14)
    assert factory.production_queue == 4

    factory.advance()
    factory.receive_order(0)
    outcome = factory.start_production(-3)

    assert outcome.requested == 1
    assert outcome.started == 1
    assert outcome.queued == 0
    assert factory.production_queue == 0
    # the week proceeds normally: the started unit entered the pipeline
    assert len(factory.shipments) == factory.shipments.length
    assert factory.supply_line() == 10 + 1


def test_a_cancellation_that_exactly_empties_the_queue_is_legal() -> None:
    config = make_config(
        production_capacity_per_week=10,
        production_delay_weeks=2,
        initial_pipeline_quantity=0,
    )
    factory = agent_for(Role.FACTORY, config)
    factory.start_production(14)
    assert factory.production_queue == 4

    outcome = factory.start_production(-4)
    assert outcome.requested == 0
    assert outcome.started == 0
    assert outcome.queued == 0
    assert factory.production_queue == 0


def test_a_cancellation_larger_than_the_queue_raises_and_changes_nothing() -> None:
    """§3.6: `requested` may not go negative, and the failed call must not
    leave a negative queue behind for a caller that catches the error."""
    config = make_config(
        production_capacity_per_week=10,
        production_delay_weeks=2,
        initial_pipeline_quantity=0,
    )
    factory = agent_for(Role.FACTORY, config)
    supply_line_before = factory.supply_line()

    with pytest.raises(ValueError):
        factory.start_production(-10)

    assert factory.production_queue == 0
    assert not factory.production_queue < 0
    assert factory.supply_line() == supply_line_before
    assert len(factory.shipments) == factory.shipments.length

    # and with a queue that is merely too small
    factory.start_production(14)
    assert factory.production_queue == 4
    with pytest.raises(ValueError):
        factory.start_production(-10)
    assert factory.production_queue == 4
    assert not factory.production_queue < 0

    # the agent is still usable afterwards
    outcome = factory.start_production(0)
    assert outcome.requested == 4
    assert outcome.started == 4
    assert factory.production_queue == 0


def test_a_cancellation_is_rejected_under_unlimited_capacity_too() -> None:
    config = make_config(
        production_capacity_per_week=None,
        production_delay_weeks=2,
        initial_pipeline_quantity=0,
    )
    factory = agent_for(Role.FACTORY, config)
    with pytest.raises(ValueError):
        factory.start_production(-1)
    assert factory.production_queue == 0


# --------------------------------------------------------------------------
# FM 7 -- the queue never goes negative
# --------------------------------------------------------------------------


def test_starting_nothing_with_an_empty_queue_leaves_the_queue_at_zero() -> None:
    config = make_config(
        production_capacity_per_week=10,
        production_delay_weeks=2,
        initial_pipeline_quantity=0,
    )
    factory = agent_for(Role.FACTORY, config)

    for _ in range(3):
        outcome = factory.start_production(0)
        assert outcome.requested == 0
        assert outcome.started == 0
        assert outcome.queued == 0
        assert isinstance(outcome.queued, int)
        assert isinstance(factory.production_queue, int)
        assert factory.production_queue == 0
        assert not factory.production_queue < 0


def test_the_queue_never_goes_negative_under_unlimited_capacity() -> None:
    config = make_config(
        production_capacity_per_week=None,
        production_delay_weeks=2,
        initial_pipeline_quantity=0,
    )
    factory = agent_for(Role.FACTORY, config)

    for request in (0, 0, 5, 0, 0):
        outcome = factory.start_production(request)
        assert outcome.queued == 0
        assert factory.production_queue == 0


# --------------------------------------------------------------------------
# The Factory settles like every other role
# --------------------------------------------------------------------------


def test_factory_settles_from_its_production_pipeline() -> None:
    config = make_config(
        production_capacity_per_week=10,
        production_delay_weeks=2,
        initial_pipeline_quantity=4,
        initial_inventory=12,
    )
    factory = agent_for(Role.FACTORY, config)

    arriving, incoming = factory.advance()
    assert (arriving, incoming) == (4, 4)

    settlement = factory.settle(week=1, arriving=arriving, incoming_order=incoming)
    assert settlement.role == Role.FACTORY
    assert settlement.opening_inventory == 12
    assert settlement.arrived == 4
    assert settlement.obligation == 4
    assert settlement.shipped == 4
    assert settlement.closing_inventory == 12
    assert settlement.closing_backlog == 0
    assert settlement.holding_cost == pytest.approx(6.0)
    assert factory.inventory == 12


def test_the_factory_receives_orders_from_its_downstream_neighbour() -> None:
    factory = agent_for(Role.FACTORY, get_preset("CLASSIC_MIT"))
    _, incoming = factory.advance()
    assert incoming == 4
    factory.receive_order(9)
    assert factory.orders is not None
    assert len(factory.orders) == factory.orders.length
    assert factory.orders_in_flight() == 13


# --------------------------------------------------------------------------
# AC 14, 15 for the Factory -- the queue round-trips
# --------------------------------------------------------------------------


def test_production_queue_round_trips_through_the_payload() -> None:
    config = make_config(production_capacity_per_week=10)
    factory = agent_for(Role.FACTORY, config)
    factory.advance()
    factory.start_production(14)
    factory.record_order(week=1, qty=14)
    assert factory.production_queue == 4

    payload = factory.state_payload()
    assert payload["production_queue"] == 4
    decoded = json.loads(json.dumps(payload))

    restored = type(factory).from_payload(decoded, config)
    assert isinstance(restored, FactoryAgent)
    assert restored.production_queue == 4
    assert restored == factory

    restored_outcome = restored.start_production(3)
    assert restored_outcome.requested == 7
    assert restored_outcome.started == 7


def test_two_factories_differing_only_in_their_queue_are_not_equal() -> None:
    config = make_config(production_capacity_per_week=10)
    first = agent_for(Role.FACTORY, config)
    second = agent_for(Role.FACTORY, config)
    assert first == second

    second.start_production(14)
    assert second.production_queue == 4
    assert first != second
