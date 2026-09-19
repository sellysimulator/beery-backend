"""Black-box tests for section 06 -- ``app/core/agents.py`` and ``records.py``.

Covers ``06-role-agents.md §5`` acceptance criteria 1-5, 7-10 and 13-16, and
``§6`` failure modes 1-4 and 8-10, plus §3.3's rejection of a negative input
and §3.8's ``from_payload`` dispatch.  The Factory-only criteria (6, 11, 12),
failure modes (5, 6, 7) and §3.6's cancellations live in
``test_factory_agent.py``, together with the Factory §4 worked example.

Everything is driven through the frozen public surface of §2 plus section 03's
and section 05's public surfaces.  No private name, no internal data structure
and no log message is asserted on.
"""

from __future__ import annotations

import ast
import inspect
import json
import random
import sys
from pathlib import Path
from typing import Any

import pytest

from app.core.agents import (
    DistributorAgent,
    FactoryAgent,
    RetailerAgent,
    RoleAgent,
    WholesalerAgent,
    agent_for,
)
from app.core.config_models import (
    DEFAULT_LIMITS,
    ConstantDemand,
    FactoryConfig,
    GameConfig,
    RoleConfig,
)
from app.core.enums import ROLE_ORDER, Role
from app.core.pipeline import Pipeline
from app.core.presets import get_preset
from app.core.records import OrderCharge, WeekSettlement

AGENT_CLASSES: dict[Role, type[RoleAgent]] = {
    Role.RETAILER: RetailerAgent,
    Role.WHOLESALER: WholesalerAgent,
    Role.DISTRIBUTOR: DistributorAgent,
    Role.FACTORY: FactoryAgent,
}


def make_config(
    role_kwargs: dict[str, Any] | None = None,
    factory_kwargs: dict[str, Any] | None = None,
) -> GameConfig:
    """A `GameConfig` built only from section 03's public surface.

    ``from_host_input`` accepts "plain JSON or already-built config models"
    (``03-game-config.md §2``), which is what lets a test vary one role
    parameter without reaching past the frozen surface.
    """
    role_kwargs = dict(role_kwargs or {})
    merged_factory = {**role_kwargs, **dict(factory_kwargs or {})}
    return GameConfig.from_host_input(
        {
            "roles": {
                Role.RETAILER.value: RoleConfig(**role_kwargs),
                Role.WHOLESALER.value: RoleConfig(**role_kwargs),
                Role.DISTRIBUTOR.value: RoleConfig(**role_kwargs),
                Role.FACTORY.value: FactoryConfig(**merged_factory),
            },
            "demand": ConstantDemand(value=4),
        },
        DEFAULT_LIMITS,
    )


def walk_values(value: Any) -> list[Any]:
    """Every value nested anywhere inside a JSON-shaped payload."""
    found: list[Any] = [value]
    if isinstance(value, dict):
        for key, item in value.items():
            found.append(key)
            found.extend(walk_values(item))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            found.extend(walk_values(item))
    return found


# --------------------------------------------------------------------------
# AC 1 -- agent_for returns the right subclass
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_agent_for_returns_the_declared_subclass(role: Role) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    assert type(agent) is AGENT_CLASSES[role]
    assert isinstance(agent, RoleAgent)
    assert agent.role == role


def test_agent_for_returns_a_fresh_agent_each_call() -> None:
    config = get_preset("CLASSIC_MIT")
    first = agent_for(Role.WHOLESALER, config)
    second = agent_for(Role.WHOLESALER, config)
    assert first is not second
    first.inventory = 99
    assert second.inventory != 99


# --------------------------------------------------------------------------
# AC 2, 3 -- construction from CLASSIC_MIT
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_classic_mit_construction(role: Role) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    assert agent.inventory == 12
    assert agent.backlog == 0
    assert agent.supply_line() == 8
    assert agent.orders_in_flight() == (0 if role == Role.RETAILER else 8)
    assert agent.accumulated_cost == 0.0
    assert agent.last_order is None
    assert agent.is_bot is False


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_only_the_retailer_has_no_order_pipeline(role: Role) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    if role == Role.RETAILER:
        assert agent.orders is None
    else:
        assert isinstance(agent.orders, Pipeline)
    assert isinstance(agent.shipments, Pipeline)


def test_construction_reads_the_role_config() -> None:
    config = make_config(
        {
            "initial_inventory": 7,
            "initial_backlog": 3,
            "shipping_delay_weeks": 3,
            "information_delay_weeks": 4,
            "initial_pipeline_quantity": 2,
            "initial_order_in_pipeline": 5,
        }
    )
    agent = agent_for(Role.DISTRIBUTOR, config)
    assert agent.inventory == 7
    assert agent.backlog == 3
    assert agent.shipments.length == 3
    assert agent.supply_line() == 6
    assert agent.orders is not None
    assert agent.orders.length == 4
    assert agent.orders_in_flight() == 20


# --------------------------------------------------------------------------
# AC 4 -- advance()
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_advance_returns_the_declared_shape(role: Role) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    arriving, incoming_order = agent.advance()
    assert isinstance(arriving, int)
    assert arriving == 4
    if role == Role.RETAILER:
        assert incoming_order is None
    else:
        assert isinstance(incoming_order, int)
        assert incoming_order == 4


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_advance_changes_nothing_but_the_pipelines(role: Role) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    agent.advance()
    assert agent.inventory == 12
    assert agent.backlog == 0
    assert agent.accumulated_cost == 0.0
    assert agent.last_order is None
    assert len(agent.shipments) == agent.shipments.length - 1
    assert agent.supply_line() == 4
    if agent.orders is not None:
        assert len(agent.orders) == agent.orders.length - 1
        assert agent.orders_in_flight() == 4


# --------------------------------------------------------------------------
# AC 5 -- worked example: the Retailer's week 6
# --------------------------------------------------------------------------


def test_worked_example_retailer_week_six() -> None:
    retailer = agent_for(Role.RETAILER, get_preset("CLASSIC_MIT"))
    retailer.inventory = 12
    retailer.backlog = 0
    retailer.shipments = Pipeline(2, fill=4)
    retailer.accumulated_cost = 17.50

    arriving, incoming = retailer.advance()
    assert (arriving, incoming) == (4, None)

    settlement = retailer.settle(week=6, arriving=4, incoming_order=8)
    assert isinstance(settlement, WeekSettlement)
    assert settlement.role == Role.RETAILER
    assert settlement.week == 6
    assert settlement.opening_inventory == 12
    assert settlement.opening_backlog == 0
    assert settlement.arrived == 4
    assert settlement.incoming_order == 8
    assert settlement.obligation == 8
    assert settlement.shipped == 8
    assert settlement.unfulfilled == 0
    assert settlement.closing_inventory == 8
    assert settlement.closing_backlog == 0
    assert settlement.holding_cost == pytest.approx(4.00)
    assert settlement.backlog_cost == pytest.approx(0.00)
    assert settlement.carrying_cost == pytest.approx(4.00)
    assert retailer.inventory == 8
    assert retailer.backlog == 0
    assert retailer.accumulated_cost == pytest.approx(21.50)
    assert retailer.supply_line() == 4

    charge = retailer.record_order(week=6, qty=10)
    assert isinstance(charge, OrderCharge)
    assert charge.role == Role.RETAILER
    assert charge.week == 6
    assert charge.order == 10
    assert charge.fixed_order_cost == pytest.approx(0.00)
    assert charge.purchase_cost == pytest.approx(0.00)
    assert charge.order_cost == pytest.approx(0.00)
    assert retailer.last_order == 10
    assert retailer.accumulated_cost == pytest.approx(21.50)


# --------------------------------------------------------------------------
# AC 5 -- worked example: a role that cannot ship in full
# --------------------------------------------------------------------------


def test_worked_example_wholesaler_short_shipment() -> None:
    wholesaler = agent_for(Role.WHOLESALER, get_preset("CLASSIC_MIT"))
    wholesaler.inventory = 3
    wholesaler.backlog = 2

    settlement = wholesaler.settle(week=9, arriving=0, incoming_order=6)
    assert settlement.role == Role.WHOLESALER
    assert settlement.week == 9
    assert settlement.opening_inventory == 3
    assert settlement.opening_backlog == 2
    assert settlement.arrived == 0
    assert settlement.incoming_order == 6
    assert settlement.obligation == 8
    assert settlement.shipped == 3
    assert settlement.unfulfilled == 5
    assert settlement.closing_inventory == 0
    assert settlement.closing_backlog == 5
    assert settlement.holding_cost == pytest.approx(0.00)
    assert settlement.backlog_cost == pytest.approx(5.00)
    assert settlement.carrying_cost == pytest.approx(5.00)
    assert wholesaler.inventory == 0
    assert wholesaler.backlog == 5
    assert wholesaler.accumulated_cost == pytest.approx(5.00)


# --------------------------------------------------------------------------
# AC 7 and FM 1 -- costs are charged on the CLOSING values
# --------------------------------------------------------------------------


def test_settle_that_ships_everything_costs_nothing() -> None:
    """AC 7: receive exactly what is owed, ship it all, pay 0."""
    agent = agent_for(Role.DISTRIBUTOR, get_preset("CLASSIC_MIT"))
    agent.inventory = 0
    agent.backlog = 0

    settlement = agent.settle(week=4, arriving=10, incoming_order=10)
    assert settlement.shipped == 10
    assert settlement.closing_inventory == 0
    assert settlement.closing_backlog == 0
    assert settlement.holding_cost == pytest.approx(0.0)
    assert settlement.backlog_cost == pytest.approx(0.0)
    assert settlement.carrying_cost == pytest.approx(0.0)
    assert agent.accumulated_cost == pytest.approx(0.0)


def test_holding_cost_is_not_charged_on_opening_inventory() -> None:
    """FM 1: opening 12, ships 12, closes at 0 -- pays 0, not 6.00.

    An implementation that charges before shipping bills ``0.50 * 12``.
    """
    agent = agent_for(Role.WHOLESALER, get_preset("CLASSIC_MIT"))
    agent.inventory = 12
    agent.backlog = 0

    settlement = agent.settle(week=2, arriving=0, incoming_order=12)
    assert settlement.opening_inventory == 12
    assert settlement.shipped == 12
    assert settlement.closing_inventory == 0
    assert settlement.holding_cost == pytest.approx(0.00)
    assert settlement.carrying_cost == pytest.approx(0.00)
    assert agent.accumulated_cost == pytest.approx(0.00)


def test_backlog_cost_is_not_charged_on_opening_backlog() -> None:
    """The mirror of FM 1: a backlog cleared this week costs nothing."""
    agent = agent_for(Role.WHOLESALER, get_preset("CLASSIC_MIT"))
    agent.inventory = 0
    agent.backlog = 6

    settlement = agent.settle(week=2, arriving=6, incoming_order=0)
    assert settlement.opening_backlog == 6
    assert settlement.obligation == 6
    assert settlement.shipped == 6
    assert settlement.closing_backlog == 0
    assert settlement.closing_inventory == 0
    assert settlement.backlog_cost == pytest.approx(0.00)
    assert settlement.carrying_cost == pytest.approx(0.00)
    assert agent.accumulated_cost == pytest.approx(0.00)


# --------------------------------------------------------------------------
# FM 2 -- the backlog carried in is part of the obligation
# --------------------------------------------------------------------------


def test_carried_backlog_is_part_of_the_obligation_and_is_cleared() -> None:
    """FM 2: backlog 2 + order 6 owes 8, and stock is enough to clear it.

    An implementation that ships ``min(inventory, incoming_order)`` and only
    *adds* the shortfall to the backlog ships 6, closes at inventory 4 and
    never clears the backlog of 2.
    """
    agent = agent_for(Role.DISTRIBUTOR, get_preset("CLASSIC_MIT"))
    agent.inventory = 10
    agent.backlog = 2

    settlement = agent.settle(week=3, arriving=0, incoming_order=6)
    assert settlement.opening_backlog == 2
    assert settlement.obligation == 8
    assert settlement.shipped == 8
    assert settlement.unfulfilled == 0
    assert settlement.closing_inventory == 2
    assert settlement.closing_backlog == 0
    assert agent.inventory == 2
    assert agent.backlog == 0
    assert settlement.holding_cost == pytest.approx(1.00)
    assert settlement.backlog_cost == pytest.approx(0.00)
    assert settlement.carrying_cost == pytest.approx(1.00)


def test_carried_backlog_is_partly_cleared_when_stock_is_short() -> None:
    """FM 2, the partial case: backlog 5 + order 5 owes 10, stock 7."""
    agent = agent_for(Role.DISTRIBUTOR, get_preset("CLASSIC_MIT"))
    agent.inventory = 4
    agent.backlog = 5

    settlement = agent.settle(week=3, arriving=3, incoming_order=5)
    assert settlement.obligation == 10
    assert settlement.shipped == 7
    assert settlement.unfulfilled == 3
    assert settlement.closing_inventory == 0
    assert settlement.closing_backlog == 3
    assert settlement.backlog_cost == pytest.approx(3.00)


# --------------------------------------------------------------------------
# FM 3 -- never stock and backlog at once
# --------------------------------------------------------------------------


def test_inventory_and_backlog_are_never_both_positive() -> None:
    closing_with_backlog = agent_for(Role.RETAILER, get_preset("CLASSIC_MIT"))
    closing_with_backlog.inventory = 2
    closing_with_backlog.backlog = 1
    settlement = closing_with_backlog.settle(week=1, arriving=0, incoming_order=9)
    assert settlement.closing_backlog == 8
    assert settlement.closing_inventory == 0
    assert settlement.closing_inventory * settlement.closing_backlog == 0
    assert closing_with_backlog.inventory * closing_with_backlog.backlog == 0

    closing_with_stock = agent_for(Role.RETAILER, get_preset("CLASSIC_MIT"))
    closing_with_stock.inventory = 12
    closing_with_stock.backlog = 3
    settlement = closing_with_stock.settle(week=1, arriving=0, incoming_order=4)
    assert settlement.closing_inventory == 5
    assert settlement.closing_backlog == 0
    assert settlement.closing_inventory * settlement.closing_backlog == 0
    assert closing_with_stock.inventory * closing_with_stock.backlog == 0


# --------------------------------------------------------------------------
# FM 4 -- a role can never ship more than it holds
# --------------------------------------------------------------------------


def test_cannot_ship_more_than_is_held() -> None:
    agent = agent_for(Role.WHOLESALER, get_preset("CLASSIC_MIT"))
    agent.inventory = 5
    agent.backlog = 0

    settlement = agent.settle(week=7, arriving=0, incoming_order=1000)
    assert settlement.shipped == 5
    assert settlement.unfulfilled == 995
    assert settlement.closing_inventory == 0
    assert settlement.closing_backlog == 995
    assert agent.inventory == 0
    assert agent.backlog == 995


# --------------------------------------------------------------------------
# §3.3 -- settle() refuses a negative input
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_settle_rejects_a_negative_arrival(role: Role) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    with pytest.raises(ValueError):
        agent.settle(week=1, arriving=-1, incoming_order=4)
    assert agent.inventory == 12
    assert agent.backlog == 0
    assert agent.accumulated_cost == pytest.approx(0.0)


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_settle_rejects_a_negative_incoming_order(role: Role) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    with pytest.raises(ValueError):
        agent.settle(week=1, arriving=4, incoming_order=-1)
    assert agent.inventory == 12
    assert agent.backlog == 0
    assert agent.accumulated_cost == pytest.approx(0.0)


def test_settle_accepts_zero_on_both_inputs() -> None:
    agent = agent_for(Role.WHOLESALER, get_preset("CLASSIC_MIT"))
    settlement = agent.settle(week=1, arriving=0, incoming_order=0)
    assert settlement.obligation == 0
    assert settlement.shipped == 0
    assert settlement.closing_inventory == 12


# --------------------------------------------------------------------------
# AC 8, 9 -- record_order charges
# --------------------------------------------------------------------------


def test_fixed_order_cost_is_charged_only_for_a_positive_order() -> None:
    config = make_config({"fixed_order_cost": 5.0})
    agent = agent_for(Role.WHOLESALER, config)

    zero = agent.record_order(week=1, qty=0)
    assert zero.order == 0
    assert zero.fixed_order_cost == pytest.approx(0.0)
    assert zero.purchase_cost == pytest.approx(0.0)
    assert zero.order_cost == pytest.approx(0.0)
    assert agent.last_order == 0
    assert agent.accumulated_cost == pytest.approx(0.0)

    one = agent.record_order(week=2, qty=1)
    assert one.order == 1
    assert one.fixed_order_cost == pytest.approx(5.0)
    assert one.purchase_cost == pytest.approx(0.0)
    assert one.order_cost == pytest.approx(5.0)
    assert agent.last_order == 1
    assert agent.accumulated_cost == pytest.approx(5.0)


def test_purchase_cost_is_unit_price_times_quantity() -> None:
    config = make_config({"unit_purchase_cost": 0.25})
    agent = agent_for(Role.DISTRIBUTOR, config)

    charge = agent.record_order(week=5, qty=8)
    assert charge.week == 5
    assert charge.order == 8
    assert charge.fixed_order_cost == pytest.approx(0.00)
    assert charge.purchase_cost == pytest.approx(2.00)
    assert charge.order_cost == pytest.approx(2.00)
    assert agent.accumulated_cost == pytest.approx(2.00)


def test_both_order_cost_terms_add_up() -> None:
    config = make_config({"fixed_order_cost": 3.0, "unit_purchase_cost": 0.5})
    agent = agent_for(Role.WHOLESALER, config)

    charge = agent.record_order(week=1, qty=10)
    assert charge.fixed_order_cost == pytest.approx(3.0)
    assert charge.purchase_cost == pytest.approx(5.0)
    assert charge.order_cost == pytest.approx(8.0)
    assert agent.accumulated_cost == pytest.approx(8.0)


def test_a_negative_order_is_not_charged_the_fixed_cost() -> None:
    """§3.4: a cancellation is not an order; its purchase cost is negative."""
    config = make_config({"fixed_order_cost": 3.0, "unit_purchase_cost": 0.5})
    agent = agent_for(Role.WHOLESALER, config)

    charge = agent.record_order(week=1, qty=-4)
    assert charge.order == -4
    assert charge.fixed_order_cost == pytest.approx(0.0)
    assert charge.purchase_cost == pytest.approx(-2.0)
    assert charge.order_cost == pytest.approx(-2.0)
    assert agent.last_order == -4
    assert agent.accumulated_cost == pytest.approx(-2.0)


def test_record_order_does_not_touch_inventory_or_pipelines() -> None:
    agent = agent_for(Role.WHOLESALER, get_preset("CLASSIC_MIT"))
    before_shipments = agent.shipments.slots()
    assert agent.orders is not None
    before_orders = agent.orders.slots()

    agent.record_order(week=1, qty=9)

    assert agent.inventory == 12
    assert agent.backlog == 0
    assert agent.shipments.slots() == before_shipments
    assert agent.orders.slots() == before_orders


# --------------------------------------------------------------------------
# AC 10 -- the Retailer has no order pipeline to receive into
# --------------------------------------------------------------------------


def test_receive_order_on_a_retailer_raises_type_error() -> None:
    retailer = agent_for(Role.RETAILER, get_preset("CLASSIC_MIT"))
    assert isinstance(retailer, RetailerAgent)
    with pytest.raises(TypeError):
        retailer.receive_order(4)
    assert retailer.orders is None
    assert retailer.orders_in_flight() == 0


@pytest.mark.parametrize("role", [Role.WHOLESALER, Role.DISTRIBUTOR, Role.FACTORY])
def test_receive_order_refills_the_order_pipeline(role: Role) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    _, incoming = agent.advance()
    assert incoming == 4
    agent.receive_order(7)
    assert agent.orders is not None
    assert len(agent.orders) == agent.orders.length
    assert agent.orders_in_flight() == 11


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_receive_shipment_refills_the_shipment_pipeline(role: Role) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    arriving, _ = agent.advance()
    assert arriving == 4
    agent.receive_shipment(6)
    assert len(agent.shipments) == agent.shipments.length
    assert agent.supply_line() == 10


# --------------------------------------------------------------------------
# AC 13 -- the six invariants of §3.7 over randomised cycles
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_invariants_hold_over_one_hundred_randomised_cycles(role: Role) -> None:
    config = make_config()
    agent = agent_for(role, config)
    rng = random.Random(f"{role.value}:invariants")
    previous_cost = agent.accumulated_cost

    for week in range(1, 101):
        arriving, incoming = agent.advance()
        if incoming is None:
            incoming = rng.randint(0, 20)
        settlement = agent.settle(week=week, arriving=arriving, incoming_order=incoming)

        # 1, 2, 3
        assert agent.inventory >= 0
        assert agent.backlog >= 0
        assert agent.inventory * agent.backlog == 0
        assert settlement.closing_inventory * settlement.closing_backlog == 0
        assert settlement.shipped <= settlement.obligation
        assert settlement.closing_inventory == agent.inventory
        assert settlement.closing_backlog == agent.backlog

        quantity = rng.randint(0, 20)
        agent.record_order(week, quantity)

        if isinstance(agent, FactoryAgent):
            agent.start_production(quantity)
            assert agent.production_queue >= 0  # 4
        else:
            agent.receive_shipment(rng.randint(0, 20))
        if agent.orders is not None:
            agent.receive_order(rng.randint(0, 20))

        # 5
        assert len(agent.shipments) == agent.shipments.length
        if agent.orders is not None:
            assert len(agent.orders) == agent.orders.length

        # 6
        assert agent.accumulated_cost >= previous_cost
        previous_cost = agent.accumulated_cost


# --------------------------------------------------------------------------
# AC 14, 15 and FM 8 -- state_payload / from_payload
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_payload_round_trip_at_rest(role: Role) -> None:
    config = get_preset("CLASSIC_MIT")
    agent = agent_for(role, config)
    restored = type(agent).from_payload(agent.state_payload(), config)
    assert restored == agent
    assert restored.role == agent.role
    assert restored.inventory == agent.inventory
    assert restored.backlog == agent.backlog
    assert restored.shipments == agent.shipments
    assert restored.orders == agent.orders


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_payload_round_trip_after_a_bare_advance(role: Role) -> None:
    config = get_preset("CLASSIC_MIT")
    agent = agent_for(role, config)
    agent.advance()
    restored = type(agent).from_payload(agent.state_payload(), config)
    assert restored == agent
    assert len(restored.shipments) == len(agent.shipments)
    assert restored.shipments.length == agent.shipments.length
    if agent.orders is not None:
        assert restored.orders is not None
        assert len(restored.orders) == len(agent.orders)


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_payload_round_trip_after_a_full_week(role: Role) -> None:
    config = get_preset("CLASSIC_MIT")
    agent = agent_for(role, config)
    arriving, incoming = agent.advance()
    agent.settle(week=1, arriving=arriving, incoming_order=incoming or 9)
    agent.record_order(week=1, qty=6)
    agent.receive_shipment(5)
    if agent.orders is not None:
        agent.receive_order(3)
    agent.is_bot = True

    payload = agent.state_payload()
    restored = type(agent).from_payload(payload, config)
    assert restored == agent
    assert restored.is_bot is True
    assert restored.last_order == 6
    assert restored.accumulated_cost == pytest.approx(agent.accumulated_cost)


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_state_payload_is_json_safe_and_holds_no_pipeline(role: Role) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    payload = agent.state_payload()

    encoded = json.dumps(payload)
    assert json.loads(encoded)

    assert not [v for v in walk_values(payload) if isinstance(v, Pipeline)]
    assert not [v for v in walk_values(payload) if isinstance(v, RoleAgent)]


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_state_payload_carries_the_declared_keys(role: Role) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    payload = agent.state_payload()

    for key in (
        "role",
        "inventory",
        "backlog",
        "accumulated_cost",
        "last_order",
        "is_bot",
        "production_queue",
    ):
        assert key in payload

    assert payload["role"] == role.value
    assert payload["inventory"] == 12
    assert payload["backlog"] == 0
    assert payload["last_order"] is None
    assert payload["is_bot"] is False
    if role == Role.RETAILER:
        assert payload["orders"] is None
    else:
        assert payload["orders"] is not None
    if role != Role.FACTORY:
        assert payload["production_queue"] == 0


def test_a_payload_round_trip_survives_json_encoding() -> None:
    config = get_preset("CLASSIC_MIT")
    agent = agent_for(Role.DISTRIBUTOR, config)
    agent.settle(week=1, arriving=0, incoming_order=20)
    agent.record_order(week=1, qty=11)

    decoded = json.loads(json.dumps(agent.state_payload()))
    restored = type(agent).from_payload(decoded, config)
    assert restored == agent


# --------------------------------------------------------------------------
# §3.8 -- from_payload dispatches on payload["role"]
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_from_payload_dispatches_on_the_payload_role(role: Role) -> None:
    """Section 07 rehydrates four agents from one document and must not
    have to pick the class itself."""
    config = get_preset("CLASSIC_MIT")
    agent = agent_for(role, config)

    restored = RoleAgent.from_payload(agent.state_payload(), config)
    assert type(restored) is AGENT_CLASSES[role]
    assert restored == agent


def test_from_payload_dispatches_even_off_the_wrong_subclass() -> None:
    config = get_preset("CLASSIC_MIT")
    factory = agent_for(Role.FACTORY, config)
    retailer = agent_for(Role.RETAILER, config)

    from_base = RoleAgent.from_payload(factory.state_payload(), config)
    assert isinstance(from_base, FactoryAgent)

    from_other = RetailerAgent.from_payload(factory.state_payload(), config)
    assert isinstance(from_other, FactoryAgent)
    assert from_other == factory

    back_again = FactoryAgent.from_payload(retailer.state_payload(), config)
    assert type(back_again) is RetailerAgent
    assert back_again == retailer


def test_from_payload_rejects_a_retailer_carrying_an_order_pipeline() -> None:
    config = get_preset("CLASSIC_MIT")
    retailer = agent_for(Role.RETAILER, config)
    wholesaler = agent_for(Role.WHOLESALER, config)

    payload = dict(retailer.state_payload())
    payload["orders"] = dict(wholesaler.state_payload())["orders"]
    with pytest.raises(ValueError):
        RoleAgent.from_payload(payload, config)


@pytest.mark.parametrize("role", [Role.WHOLESALER, Role.DISTRIBUTOR, Role.FACTORY])
def test_from_payload_rejects_a_role_that_lost_its_order_pipeline(
    role: Role,
) -> None:
    config = get_preset("CLASSIC_MIT")
    payload = dict(agent_for(role, config).state_payload())
    payload["orders"] = None
    with pytest.raises(ValueError):
        RoleAgent.from_payload(payload, config)


# --------------------------------------------------------------------------
# §3.8 -- equality
# --------------------------------------------------------------------------


def test_equality_compares_state_not_identity() -> None:
    config = get_preset("CLASSIC_MIT")
    first = agent_for(Role.WHOLESALER, config)
    second = agent_for(Role.WHOLESALER, config)
    assert first == second
    assert (first != second) is False

    second.inventory += 1
    assert first != second
    second.inventory -= 1
    assert first == second

    second.backlog += 1
    assert first != second
    second.backlog -= 1

    second.accumulated_cost += 0.5
    assert first != second
    second.accumulated_cost -= 0.5

    second.last_order = 4
    assert first != second
    second.last_order = None

    second.is_bot = True
    assert first != second
    second.is_bot = False

    second.receive_shipment(1)
    assert first != second


def test_a_different_role_is_a_different_agent() -> None:
    config = get_preset("CLASSIC_MIT")
    assert agent_for(Role.WHOLESALER, config) != agent_for(Role.DISTRIBUTOR, config)


def test_equality_against_a_foreign_object() -> None:
    agent = agent_for(Role.RETAILER, get_preset("CLASSIC_MIT"))
    assert agent.__eq__(object()) is NotImplemented
    assert agent != object()
    assert agent != "RETAILER"


def test_an_agent_is_unhashable() -> None:
    agent = agent_for(Role.RETAILER, get_preset("CLASSIC_MIT"))
    with pytest.raises(TypeError):
        hash(agent)


# --------------------------------------------------------------------------
# FM 8 -- no neighbour references
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_an_agent_takes_no_neighbour_argument(role: Role) -> None:
    parameters = list(inspect.signature(AGENT_CLASSES[role].__init__).parameters)
    assert parameters == ["self", "role", "config"]


def test_a_wired_up_chain_leaves_no_agent_inside_a_payload() -> None:
    config = get_preset("CLASSIC_MIT")
    agents = {role: agent_for(role, config) for role in ROLE_ORDER}

    # Route one week's worth of goods and orders the way section 07 will.
    for agent in agents.values():
        agent.advance()
    for role, agent in agents.items():
        settlement = agent.settle(week=1, arriving=4, incoming_order=4)
        downstream = role.downstream
        if downstream is not None:
            agents[downstream].receive_shipment(settlement.shipped)
        upstream = role.upstream
        if upstream is not None:
            agents[upstream].receive_order(4)

    for agent in agents.values():
        values = walk_values(agent.state_payload())
        assert not [v for v in values if isinstance(v, RoleAgent)]


# --------------------------------------------------------------------------
# FM 9 -- cost accumulates as a running float
# --------------------------------------------------------------------------


def test_cost_accumulation_is_not_rounded_every_week() -> None:
    """FM 9: 200 settles of ``carrying_cost == 0.125`` total 25.0.

    Rounding to 2 decimals at each step gives 26.0 or 24.0.
    """
    config = make_config(
        {
            "initial_inventory": 1,
            "initial_backlog": 0,
            "holding_cost_per_unit_week": 0.125,
        }
    )
    agent = agent_for(Role.WHOLESALER, config)
    for week in range(1, 201):
        settlement = agent.settle(week=week, arriving=0, incoming_order=0)
        assert settlement.carrying_cost == pytest.approx(0.125)
    assert agent.accumulated_cost == pytest.approx(25.0, abs=1e-9)


def test_cost_accumulation_does_not_drift_on_a_repeating_decimal() -> None:
    """The same run at 0.1, where the total must still be exact to 1e-9."""
    config = make_config(
        {
            "initial_inventory": 1,
            "initial_backlog": 0,
            "holding_cost_per_unit_week": 0.1,
        }
    )
    agent = agent_for(Role.WHOLESALER, config)
    for week in range(1, 201):
        settlement = agent.settle(week=week, arriving=0, incoming_order=0)
        assert settlement.carrying_cost == pytest.approx(0.1)
    assert agent.accumulated_cost == pytest.approx(20.0, abs=1e-9)


# --------------------------------------------------------------------------
# FM 10 -- settle() does not touch the pipelines
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_settle_leaves_the_pipelines_alone(role: Role) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    shipment_slots = agent.shipments.slots()
    order_slots = None if agent.orders is None else agent.orders.slots()

    agent.settle(week=1, arriving=6, incoming_order=3)

    assert len(agent.shipments) == agent.shipments.length
    assert agent.shipments.slots() == shipment_slots
    assert agent.supply_line() == 8
    if agent.orders is not None:
        assert len(agent.orders) == agent.orders.length
        assert agent.orders.slots() == order_slots


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_settle_after_advance_leaves_the_pipeline_one_slot_short(
    role: Role,
) -> None:
    agent = agent_for(role, get_preset("CLASSIC_MIT"))
    arriving, incoming = agent.advance()
    agent.settle(week=1, arriving=arriving, incoming_order=incoming or 4)

    assert len(agent.shipments) == agent.shipments.length - 1
    if agent.orders is not None:
        assert len(agent.orders) == agent.orders.length - 1


# --------------------------------------------------------------------------
# AC 16 -- app/core/agents.py imports only app.core and the standard library
# --------------------------------------------------------------------------


def imported_modules(source: str, package: str) -> set[str]:
    """Every absolute module name a source file imports."""
    parts = package.split(".")
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                names.add(node.module or "")
            else:
                base = ".".join(parts[: len(parts) - (node.level - 1)])
                names.add(f"{base}.{node.module}" if node.module else base)
    return names


@pytest.mark.parametrize("module", ["agents", "records"])
def test_module_imports_only_core_and_the_standard_library(
    project_root: Path, module: str
) -> None:
    source = (project_root / "app" / "core" / f"{module}.py").read_text(
        encoding="utf-8"
    )
    for name in imported_modules(source, "app.core"):
        if name == "app.core" or name.startswith("app.core."):
            continue
        assert name.split(".")[0] in sys.stdlib_module_names, name
