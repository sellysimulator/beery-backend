"""Black-box tests for section 03 -- ``app/core/enums.py`` and
``app/core/config_models.py``.

Scope is exactly the *Public surface* chapter of ``03-game-config.md``: the
enums, the frozen Pydantic models, ``GameConfig``'s accessors and construction
helpers, ``Limits`` / ``DEFAULT_LIMITS`` and ``ConfigValidationError``.  The
presets module is imported only to obtain a known-valid host-input payload --
``§3.8`` guarantees every preset survives ``from_host_input`` unchanged, which
makes ``get_preset("CLASSIC_MIT").to_payload()`` the one payload shape the
document itself certifies as valid input.  Presets are covered on their own in
``test_presets.py``.

Nothing here reaches into the implementation: no private name, no internal data
structure and no log message is asserted (``00-conventions.md §5``).  The single
exception is ``test_config_models_imports_nothing_from_other_app_layers``, which
``§5`` item 16 explicitly asks to be proved "by inspecting the module's
imports".

``§3.1``'s ``.field`` table is part of the contract, so every rejection test
asserts the exact string rather than merely "some field".  ``§3.1`` also fixes
the evaluation order (``duration_weeks`` -> ``currency_symbol`` -> ``roles`` ->
``demand``), which makes a doubly-invalid payload's ``.field`` deterministic and
therefore testable.
"""

from __future__ import annotations

import ast
import copy
import json
import math
import random
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config_models import (
    DEFAULT_LIMITS,
    BotConfig,
    ConfigValidationError,
    ConstantDemand,
    CustomDemand,
    FactoryConfig,
    GameConfig,
    Limits,
    RampDemand,
    RoleConfig,
    SeasonalDemand,
    StepDemand,
    StochasticDemand,
    VisibilityConfig,
)
from app.core.enums import (
    ROLE_ORDER,
    DemandKind,
    Distribution,
    Role,
    RoleAssignmentMode,
    RoomState,
)
from app.core.presets import get_preset

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ROLE_NAMES = ("RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY")


def base_payload(**overrides: Any) -> dict:
    """A fresh, known-valid host-input payload.

    ``§3.8``: "Every preset must pass ``from_host_input`` unchanged", and
    ``§3.7`` fixes ``to_payload()`` as the plain JSON-safe dict form.  Deriving
    the base from the preset therefore uses the only payload shape the document
    certifies, rather than one invented by the test.
    """
    payload = copy.deepcopy(get_preset("CLASSIC_MIT").to_payload())
    payload.update(overrides)
    return payload


def payload_for_role(role_name: str, **fields: Any) -> dict:
    payload = base_payload()
    payload["roles"][role_name].update(fields)
    return payload


def payload_for_all_roles(**fields: Any) -> dict:
    payload = base_payload()
    for role_name in ROLE_NAMES:
        payload["roles"][role_name].update(fields)
    return payload


def payload_with_visibility(**fields: Any) -> dict:
    payload = base_payload()
    payload["visibility"].update(fields)
    return payload


def payload_with_demand(**demand: Any) -> dict:
    return base_payload(demand=demand)


def build(payload: dict, limits: Limits | None = None) -> GameConfig:
    return GameConfig.from_host_input(payload, limits or DEFAULT_LIMITS)


def assert_rejected(
    payload: dict, *, field: str | None = None
) -> ConfigValidationError:
    """Assert ``from_host_input`` rejects ``payload`` and returns the error."""
    with pytest.raises(ConfigValidationError) as excinfo:
        build(payload)
    err = excinfo.value
    assert isinstance(err, ValueError), "ConfigValidationError must subclass ValueError"
    assert isinstance(err.field, str) and err.field, ".field must be a non-empty string"
    assert str(err), "the error must carry a human-readable message"
    if field is not None:
        assert err.field == field
    return err


def assert_rejected_custom(
    values: list[Any], *, field: str | None = None, weeks: int = 8
) -> ConfigValidationError:
    payload = base_payload(duration_weeks=weeks)
    payload["demand"] = {"kind": "CUSTOM", "values": values}
    return assert_rejected(payload, field=field)


def hand_built_config() -> GameConfig:
    """A config in which **every** field is set away from its default.

    The per-role delays are deliberately all different so that ``§3.2``'s
    accessors cannot pass by accident.
    """
    return GameConfig(
        duration_weeks=52,
        stage_count=4,
        pause_on_disconnect=False,
        bot_fill_empty_roles=True,
        random_seed=987_654,
        currency_symbol="€",
        role_assignment_mode=RoleAssignmentMode.RANDOM,
        preset_name="HAND_BUILT",
        roles={
            Role.RETAILER: RoleConfig(
                initial_inventory=3,
                initial_backlog=1,
                shipping_delay_weeks=1,
                information_delay_weeks=3,
                initial_pipeline_quantity=2,
                initial_order_in_pipeline=5,
                holding_cost_per_unit_week=0.25,
                backlog_cost_per_unit_week=2.5,
                fixed_order_cost=1.5,
                unit_purchase_cost=0.75,
                starting_capital=100.0,
            ),
            Role.WHOLESALER: RoleConfig(
                initial_inventory=4,
                initial_backlog=2,
                shipping_delay_weeks=2,
                information_delay_weeks=4,
                initial_pipeline_quantity=3,
                initial_order_in_pipeline=6,
                holding_cost_per_unit_week=0.75,
                backlog_cost_per_unit_week=3.0,
                fixed_order_cost=2.0,
                unit_purchase_cost=1.25,
                starting_capital=200.0,
            ),
            Role.DISTRIBUTOR: RoleConfig(
                initial_inventory=5,
                initial_backlog=3,
                shipping_delay_weeks=3,
                information_delay_weeks=5,
                initial_pipeline_quantity=6,
                initial_order_in_pipeline=7,
                holding_cost_per_unit_week=1.25,
                backlog_cost_per_unit_week=4.0,
                fixed_order_cost=3.0,
                unit_purchase_cost=1.75,
                starting_capital=300.0,
            ),
            Role.FACTORY: FactoryConfig(
                initial_inventory=6,
                initial_backlog=4,
                shipping_delay_weeks=6,
                information_delay_weeks=7,
                initial_pipeline_quantity=8,
                initial_order_in_pipeline=9,
                holding_cost_per_unit_week=1.75,
                backlog_cost_per_unit_week=5.0,
                fixed_order_cost=4.0,
                unit_purchase_cost=2.25,
                starting_capital=400.0,
                production_delay_weeks=5,
                production_capacity_per_week=40,
            ),
        },
        demand=SeasonalDemand(base=10, amplitude=6, period_weeks=8, phase=1.5),
        visibility=VisibilityConfig(
            show_true_customer_demand_to_all=True,
            show_neighbour_inventory=True,
            show_all_inventories=True,
            show_supply_line_prominently=False,
            show_running_cost_to_players=False,
            show_leaderboard_during_game=True,
            max_order_quantity=500,
            allow_negative_orders=True,
        ),
        bot=BotConfig(theta=0.9, alpha=0.1, beta=0.75, target_stock_multiplier=5.5),
    )


# ---------------------------------------------------------------------------
# Sanity: the base payload really is accepted, so a failure below is about the
# rule under test rather than about the payload shape.
# ---------------------------------------------------------------------------


def test_base_payload_is_accepted_unchanged() -> None:
    preset = get_preset("CLASSIC_MIT")
    assert build(base_payload()) == preset


# ---------------------------------------------------------------------------
# §2 enums / §3.3 chain topology -- AC 7
# ---------------------------------------------------------------------------


def test_role_members_and_values() -> None:
    assert [r.value for r in Role] == list(ROLE_NAMES)
    assert Role.RETAILER == "RETAILER"


def test_role_order_is_retailer_to_factory() -> None:
    assert ROLE_ORDER == (
        Role.RETAILER,
        Role.WHOLESALER,
        Role.DISTRIBUTOR,
        Role.FACTORY,
    )


@pytest.mark.parametrize(
    ("role", "index"),
    [
        (Role.RETAILER, 0),
        (Role.WHOLESALER, 1),
        (Role.DISTRIBUTOR, 2),
        (Role.FACTORY, 3),
    ],
)
def test_role_index(role: Role, index: int) -> None:
    assert role.index == index


@pytest.mark.parametrize(
    ("role", "downstream"),
    [
        (Role.RETAILER, None),
        (Role.WHOLESALER, Role.RETAILER),
        (Role.DISTRIBUTOR, Role.WHOLESALER),
        (Role.FACTORY, Role.DISTRIBUTOR),
    ],
)
def test_role_downstream(role: Role, downstream: Role | None) -> None:
    assert role.downstream is downstream


@pytest.mark.parametrize(
    ("role", "upstream"),
    [
        (Role.RETAILER, Role.WHOLESALER),
        (Role.WHOLESALER, Role.DISTRIBUTOR),
        (Role.DISTRIBUTOR, Role.FACTORY),
        (Role.FACTORY, None),
    ],
)
def test_role_upstream(role: Role, upstream: Role | None) -> None:
    assert role.upstream is upstream


def test_role_index_agrees_with_role_order() -> None:
    assert tuple(sorted(Role, key=lambda r: r.index)) == ROLE_ORDER


def test_supporting_enum_members() -> None:
    assert [m.value for m in RoleAssignmentMode] == [
        "HOST_ASSIGNS",
        "PLAYER_CHOOSES",
        "RANDOM",
    ]
    assert [m.value for m in DemandKind] == [
        "CONSTANT",
        "STEP",
        "RAMP",
        "SEASONAL",
        "STOCHASTIC",
        "CUSTOM",
    ]
    assert [m.value for m in Distribution] == ["NORMAL", "POISSON", "UNIFORM"]
    assert [m.value for m in RoomState] == [
        "LOBBY",
        "CONFIGURING",
        "READY",
        "RUNNING",
        "PAUSED",
        "FINISHED",
        "ABANDONED",
    ]


# ---------------------------------------------------------------------------
# §4 Worked examples (normative) -- asserted literally, value for value
# ---------------------------------------------------------------------------


def test_worked_example_classic_mit_accessors() -> None:
    cfg = get_preset("CLASSIC_MIT")

    assert cfg.duration_weeks == 36
    assert cfg.inbound_delay_weeks(Role.RETAILER) == 2
    assert cfg.inbound_delay_weeks(Role.FACTORY) == 2  # production_delay_weeks
    assert cfg.order_delay_weeks(Role.FACTORY) == 2  # information_delay_weeks
    assert cfg.role_config(Role.WHOLESALER).holding_cost_per_unit_week == 0.50
    assert Role.WHOLESALER.downstream == Role.RETAILER
    assert Role.RETAILER.downstream is None
    assert Role.FACTORY.upstream is None


def test_worked_example_duration_weeks_clamped_up_and_down() -> None:
    assert build(base_payload(duration_weeks=500)).duration_weeks == 104
    assert build(base_payload(duration_weeks=2)).duration_weeks == 8


def test_worked_example_shipping_delay_zero_becomes_one() -> None:
    cfg = build(payload_for_all_roles(shipping_delay_weeks=0))
    for role in ROLE_ORDER:
        assert cfg.role_config(role).shipping_delay_weeks == 1


def test_worked_example_huge_holding_cost_clamped() -> None:
    cfg = build(payload_for_role("RETAILER", holding_cost_per_unit_week=1e12))
    assert cfg.role_config(Role.RETAILER).holding_cost_per_unit_week == 1_000_000.0


def test_worked_example_negative_initial_inventory_becomes_zero() -> None:
    cfg = build(payload_for_role("RETAILER", initial_inventory=-7))
    assert cfg.role_config(Role.RETAILER).initial_inventory == 0


def test_worked_example_short_custom_series_rejected_with_field() -> None:
    payload = base_payload(duration_weeks=36)
    payload["demand"] = {"kind": "CUSTOM", "values": [4] * 20}
    assert_rejected(payload, field="demand.values")


# ---------------------------------------------------------------------------
# §3.2 delay semantics -- AC 6; §2 accessors -- AC 5
# ---------------------------------------------------------------------------


def test_role_config_returns_the_per_role_object() -> None:
    cfg = hand_built_config()
    assert cfg.role_config(Role.RETAILER).initial_inventory == 3
    assert cfg.role_config(Role.WHOLESALER).initial_inventory == 4
    assert cfg.role_config(Role.DISTRIBUTOR).initial_inventory == 5
    assert cfg.role_config(Role.FACTORY).initial_inventory == 6


def test_factory_config_is_a_factory_config() -> None:
    cfg = hand_built_config()
    factory = cfg.factory_config()
    assert isinstance(factory, FactoryConfig)
    assert factory.production_delay_weeks == 5
    assert factory.production_capacity_per_week == 40
    assert cfg.role_config(Role.FACTORY) == factory


def test_preset_factory_entry_is_a_factory_config() -> None:
    for name in ("CLASSIC_MIT", "FAST_GAME", "CHAOS"):
        cfg = get_preset(name)
        assert isinstance(cfg.roles[Role.FACTORY], FactoryConfig)
        assert isinstance(cfg.factory_config(), FactoryConfig)


def test_inbound_delay_weeks_uses_shipping_except_for_the_factory() -> None:
    cfg = hand_built_config()
    assert cfg.inbound_delay_weeks(Role.RETAILER) == 1
    assert cfg.inbound_delay_weeks(Role.WHOLESALER) == 2
    assert cfg.inbound_delay_weeks(Role.DISTRIBUTOR) == 3
    # FACTORY has shipping_delay_weeks=6 but must use production_delay_weeks=5.
    assert cfg.inbound_delay_weeks(Role.FACTORY) == 5


def test_order_delay_weeks_uses_information_delay_for_all_four_roles() -> None:
    cfg = hand_built_config()
    assert cfg.order_delay_weeks(Role.RETAILER) == 3
    assert cfg.order_delay_weeks(Role.WHOLESALER) == 4
    assert cfg.order_delay_weeks(Role.DISTRIBUTOR) == 5
    assert cfg.order_delay_weeks(Role.FACTORY) == 7


# ---------------------------------------------------------------------------
# §5 AC 1 immutability, and §6 failure mode 8
# ---------------------------------------------------------------------------


def test_assigning_to_a_game_config_field_raises() -> None:
    cfg = get_preset("CLASSIC_MIT")
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
        cfg.duration_weeks = 10
    assert cfg.duration_weeks == 36


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        (RoleConfig(), "initial_inventory", 99),
        (FactoryConfig(), "production_delay_weeks", 9),
        (BotConfig(), "theta", 0.9),
        (VisibilityConfig(), "allow_negative_orders", True),
        (ConstantDemand(), "value", 11),
        (StepDemand(), "step_week", 9),
        (RampDemand(), "start_week", 9),
        (SeasonalDemand(), "base", 9),
        (StochasticDemand(), "mean", 9.0),
        (CustomDemand(values=[1, 2]), "values", [3]),
        (Limits(), "max_weeks", 9),
    ],
)
def test_every_config_model_is_frozen(model: Any, field: str, value: Any) -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
        setattr(model, field, value)


def test_a_roles_entry_cannot_be_replaced_after_freeze() -> None:
    """``§6`` failure mode 8: ``cfg.roles[Role.RETAILER]`` cannot be replaced.

    A frozen model only blocks *attribute* assignment, so a plain mutable dict
    would let a caller swap a role's whole configuration out from under a
    running game.  The mapping itself has to refuse the write.
    """
    cfg = get_preset("CLASSIC_MIT")
    original = cfg.role_config(Role.RETAILER)

    with pytest.raises(TypeError):
        cfg.roles[Role.RETAILER] = RoleConfig(initial_inventory=999)

    assert cfg.role_config(Role.RETAILER) == original
    assert cfg.role_config(Role.RETAILER).initial_inventory == 12


def test_a_nested_role_config_field_cannot_be_mutated() -> None:
    cfg = get_preset("CLASSIC_MIT")
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
        cfg.roles[Role.RETAILER].initial_inventory = 999
    assert cfg.role_config(Role.RETAILER).initial_inventory == 12


# ---------------------------------------------------------------------------
# §3.7 serialisation -- AC 2, AC 3
# ---------------------------------------------------------------------------


def _assert_json_primitive(node: Any, path: str) -> None:
    assert not isinstance(node, Enum), f"{path} is an enum object, not a string"
    if isinstance(node, dict):
        for key, value in node.items():
            assert not isinstance(key, Enum), f"{path} has an enum key {key!r}"
            assert type(key) is str, f"{path} has a non-str key {key!r}"
            _assert_json_primitive(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _assert_json_primitive(value, f"{path}[{i}]")
    else:
        assert type(node) in (
            str,
            int,
            float,
            bool,
            type(None),
        ), f"{path} is {type(node)!r}, which is not a JSON primitive"


@pytest.mark.parametrize("name", ["CLASSIC_MIT", "FAST_GAME", "CHAOS"])
def test_to_payload_is_json_safe_and_enum_free(name: str) -> None:
    payload = get_preset(name).to_payload()
    _assert_json_primitive(payload, "payload")
    json.dumps(payload)  # must not raise


def test_hand_built_to_payload_is_json_safe_and_enum_free() -> None:
    payload = hand_built_config().to_payload()
    _assert_json_primitive(payload, "payload")
    assert set(payload["roles"]) == set(ROLE_NAMES)
    assert payload["demand"]["kind"] == "SEASONAL"
    assert payload["role_assignment_mode"] == "RANDOM"
    json.dumps(payload)


@pytest.mark.parametrize("name", ["CLASSIC_MIT", "FAST_GAME", "CHAOS"])
def test_round_trip_through_payload_is_lossless_for_presets(name: str) -> None:
    cfg = get_preset(name)
    assert GameConfig.from_payload(cfg.to_payload()) == cfg


def test_round_trip_through_payload_is_lossless_for_every_non_default_value() -> None:
    cfg = hand_built_config()
    assert GameConfig.from_payload(cfg.to_payload()) == cfg


def test_round_trip_preserves_the_factory_config_type() -> None:
    cfg = GameConfig.from_payload(hand_built_config().to_payload())
    assert isinstance(cfg.factory_config(), FactoryConfig)
    assert cfg.factory_config().production_delay_weeks == 5


def test_round_trip_survives_a_json_text_hop() -> None:
    """The point of ``§3.7``: the config has to live in Redis as text."""
    cfg = hand_built_config()
    assert GameConfig.from_payload(json.loads(json.dumps(cfg.to_payload()))) == cfg


def test_from_payload_raises_on_a_payload_missing_roles_and_demand() -> None:
    with pytest.raises(ValidationError):
        GameConfig.from_payload({"duration_weeks": 36})


def test_from_payload_does_not_clamp() -> None:
    """``§3.1``: ``from_payload`` assumes an already-validated value."""
    payload = get_preset("CLASSIC_MIT").to_payload()
    payload["duration_weeks"] = 500
    assert GameConfig.from_payload(payload).duration_weeks == 500


# ---------------------------------------------------------------------------
# §3.1 rejection rules 1-9 -- AC 8
# ---------------------------------------------------------------------------


def test_reject_1_custom_demand_shorter_than_duration_weeks() -> None:
    payload = base_payload(duration_weeks=12)
    payload["demand"] = {"kind": "CUSTOM", "values": [4] * 11}
    assert_rejected(payload, field="demand.values")


def test_reject_2_custom_demand_containing_a_negative_value() -> None:
    payload = base_payload(duration_weeks=12)
    values = [4] * 12
    values[7] = -1
    payload["demand"] = {"kind": "CUSTOM", "values": values}
    assert_rejected(payload, field="demand.values")


@pytest.mark.parametrize("step_week", [1, 0, -3, 37, 100])
def test_reject_3_step_week_outside_two_to_duration_weeks(step_week: int) -> None:
    payload = payload_with_demand(
        kind="STEP", initial_value=4, step_week=step_week, step_value=8
    )
    payload["duration_weeks"] = 36
    assert_rejected(payload, field="demand.step_week")


@pytest.mark.parametrize("start_week", [0, -1, 37, 500])
def test_reject_4_ramp_start_week_outside_one_to_duration_weeks(
    start_week: int,
) -> None:
    payload = payload_with_demand(
        kind="RAMP",
        initial_value=4,
        slope_per_week=1.0,
        start_week=start_week,
        cap=None,
    )
    payload["duration_weeks"] = 36
    assert_rejected(payload, field="demand.start_week")


@pytest.mark.parametrize("period_weeks", [1, 0, -4])
def test_reject_5_seasonal_period_below_two(period_weeks: int) -> None:
    payload = payload_with_demand(
        kind="SEASONAL",
        base=8,
        amplitude=4,
        period_weeks=period_weeks,
        phase=0.0,
    )
    assert_rejected(payload, field="demand.period_weeks")


@pytest.mark.parametrize("missing", ROLE_NAMES)
def test_reject_6_roles_missing_any_member(missing: str) -> None:
    payload = base_payload()
    del payload["roles"][missing]
    assert_rejected(payload, field="roles")


def test_reject_6_empty_roles_mapping() -> None:
    payload = base_payload()
    payload["roles"] = {}
    assert_rejected(payload, field="roles")


def test_reject_7_factory_entry_that_is_not_a_factory_config() -> None:
    """``§3.1``: rule 7 is reachable only when the caller passes model
    *instances* -- any mapping under ``"FACTORY"`` parses as a ``FactoryConfig``
    with defaults, so a pure-JSON payload can never trigger it."""
    payload = base_payload()
    payload["roles"]["FACTORY"] = RoleConfig()
    assert_rejected(payload, field="roles.FACTORY")


def test_a_factory_config_instance_under_factory_is_accepted() -> None:
    """The mirror of rule 7: the model-instance input shape itself is legal."""
    payload = base_payload()
    payload["roles"]["FACTORY"] = FactoryConfig(production_capacity_per_week=25)
    cfg = build(payload)
    assert cfg.factory_config().production_capacity_per_week == 25


def test_role_config_instances_are_accepted_for_the_other_three_roles() -> None:
    payload = base_payload()
    payload["roles"]["RETAILER"] = RoleConfig(initial_inventory=17)
    payload["roles"]["WHOLESALER"] = RoleConfig(shipping_delay_weeks=3)
    cfg = build(payload)
    assert cfg.role_config(Role.RETAILER).initial_inventory == 17
    assert cfg.role_config(Role.WHOLESALER).shipping_delay_weeks == 3


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_reject_8_non_finite_cost(bad: float) -> None:
    assert not math.isfinite(bad)
    assert_rejected(
        payload_for_role("RETAILER", holding_cost_per_unit_week=bad),
        field="roles.RETAILER.holding_cost_per_unit_week",
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_reject_8_non_finite_starting_capital(bad: float) -> None:
    assert_rejected(
        payload_for_role("FACTORY", starting_capital=bad),
        field="roles.FACTORY.starting_capital",
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_reject_8_non_finite_bot_parameter(bad: float) -> None:
    payload = base_payload()
    payload["bot"]["theta"] = bad
    assert_rejected(payload, field="bot.theta")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_reject_8_non_finite_demand_parameter(bad: float) -> None:
    assert_rejected(
        payload_with_demand(
            kind="RAMP",
            initial_value=4,
            slope_per_week=bad,
            start_week=5,
            cap=None,
        ),
        field="demand.slope_per_week",
    )


@pytest.mark.parametrize("symbol", ["$$$$$", "US$$$", "€€€€€"])
def test_reject_9_currency_symbol_longer_than_four_characters(symbol: str) -> None:
    assert len(symbol) == 5
    assert_rejected(base_payload(currency_symbol=symbol), field="currency_symbol")


def test_config_validation_error_is_a_value_error_with_a_field() -> None:
    err = assert_rejected(base_payload(currency_symbol="toolong"))
    assert isinstance(err, ValueError)
    assert err.field == "currency_symbol"


# ---------------------------------------------------------------------------
# §3.1: a violation that is NOT one of the nine must be repaired, not raised
# ---------------------------------------------------------------------------


def _accepted(payload: dict) -> GameConfig:
    try:
        return build(payload)
    except ConfigValidationError as err:  # pragma: no cover - failure path
        pytest.fail(
            f"from_host_input rejected a value that §3.1 does not list as a "
            f"rejection rule (.field={err.field!r}): {err}"
        )


def test_out_of_range_numbers_are_repaired_not_rejected() -> None:
    _accepted(base_payload(duration_weeks=10_000))
    _accepted(base_payload(duration_weeks=-5))
    _accepted(payload_for_all_roles(shipping_delay_weeks=0))
    _accepted(payload_for_all_roles(information_delay_weeks=99))
    _accepted(payload_for_role("FACTORY", production_delay_weeks=0))
    _accepted(payload_for_role("RETAILER", initial_inventory=-1))
    _accepted(payload_for_role("RETAILER", initial_backlog=50_000))
    _accepted(payload_for_role("RETAILER", initial_pipeline_quantity=-3))
    _accepted(payload_for_role("RETAILER", initial_order_in_pipeline=99_999))
    _accepted(payload_for_role("RETAILER", holding_cost_per_unit_week=-2.0))
    _accepted(payload_for_role("RETAILER", backlog_cost_per_unit_week=1e30))
    _accepted(payload_for_role("RETAILER", starting_capital=-10.0))
    _accepted(payload_with_visibility(max_order_quantity=-5))
    _accepted(payload_for_role("FACTORY", production_capacity_per_week=0))


def test_bot_parameters_out_of_range_are_clamped_not_rejected() -> None:
    payload = base_payload()
    payload["bot"] = {
        "theta": 5.0,
        "alpha": -1.0,
        "beta": 99.0,
        "target_stock_multiplier": -3.0,
    }
    _accepted(payload)


def test_boundary_values_on_the_rejection_rules_are_accepted() -> None:
    """The nine rules reject *outside* their range; the edges stay legal."""
    # Rule 3: step_week in 2..duration_weeks inclusive.
    payload = payload_with_demand(
        kind="STEP", initial_value=4, step_week=2, step_value=8
    )
    payload["duration_weeks"] = 36
    assert _accepted(payload).demand.step_week == 2

    payload = payload_with_demand(
        kind="STEP", initial_value=4, step_week=36, step_value=8
    )
    payload["duration_weeks"] = 36
    assert _accepted(payload).demand.step_week == 36

    # Rule 4: start_week in 1..duration_weeks inclusive.
    for start_week in (1, 36):
        payload = payload_with_demand(
            kind="RAMP",
            initial_value=4,
            slope_per_week=1.0,
            start_week=start_week,
            cap=None,
        )
        payload["duration_weeks"] = 36
        assert _accepted(payload).demand.start_week == start_week

    # Rule 5: period_weeks == 2 is the smallest legal period.
    payload = payload_with_demand(
        kind="SEASONAL", base=8, amplitude=4, period_weeks=2, phase=0.0
    )
    assert _accepted(payload).demand.period_weeks == 2

    # Rule 2: zero is not negative.
    payload = base_payload(duration_weeks=8)
    payload["demand"] = {"kind": "CUSTOM", "values": [0] * 8}
    assert _accepted(payload).demand.values == [0] * 8

    # Rule 9: exactly four characters is not "longer than 4".
    assert _accepted(base_payload(currency_symbol="US$ ")).currency_symbol == "US$ "


# ---------------------------------------------------------------------------
# §3.1 clamping -- AC 9, and §6 failure modes 2 and 3
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (500, 104),
        (10_000, 104),
        (105, 104),
        (104, 104),
        (8, 8),
        (7, 8),
        (2, 8),
        (0, 8),
        (-9, 8),
    ],
)
def test_clamp_duration_weeks(given: int, expected: int) -> None:
    """Failure mode 2: an uncapped week count is an unbounded-work lever."""
    assert build(base_payload(duration_weeks=given)).duration_weeks == expected


@pytest.mark.parametrize(
    ("given", "expected"), [(0, 1), (-4, 1), (1, 1), (8, 8), (9, 8), (1000, 8)]
)
def test_clamp_shipping_delay_weeks(given: int, expected: int) -> None:
    """Failure mode 3: a zero delay collapses the pipeline."""
    cfg = build(payload_for_all_roles(shipping_delay_weeks=given))
    for role in ROLE_ORDER:
        assert cfg.role_config(role).shipping_delay_weeks == expected


@pytest.mark.parametrize(("given", "expected"), [(0, 1), (-4, 1), (8, 8), (12, 8)])
def test_clamp_information_delay_weeks(given: int, expected: int) -> None:
    cfg = build(payload_for_all_roles(information_delay_weeks=given))
    for role in ROLE_ORDER:
        assert cfg.order_delay_weeks(role) == expected


@pytest.mark.parametrize(("given", "expected"), [(0, 1), (-2, 1), (8, 8), (40, 8)])
def test_clamp_production_delay_weeks(given: int, expected: int) -> None:
    cfg = build(payload_for_role("FACTORY", production_delay_weeks=given))
    assert cfg.factory_config().production_delay_weeks == expected
    assert cfg.inbound_delay_weeks(Role.FACTORY) == expected


@pytest.mark.parametrize(
    "field",
    [
        "initial_inventory",
        "initial_backlog",
        "initial_pipeline_quantity",
        "initial_order_in_pipeline",
    ],
)
@pytest.mark.parametrize(
    ("given", "expected"),
    [(-1, 0), (-9_999, 0), (10_000, 9_999), (1_000_000, 9_999), (12, 12)],
)
def test_clamp_initial_quantities(field: str, given: int, expected: int) -> None:
    cfg = build(payload_for_role("WHOLESALER", **{field: given}))
    assert getattr(cfg.role_config(Role.WHOLESALER), field) == expected


@pytest.mark.parametrize(
    "field",
    [
        "holding_cost_per_unit_week",
        "backlog_cost_per_unit_week",
        "fixed_order_cost",
        "unit_purchase_cost",
    ],
)
@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (-1.0, 0.0),
        (1e12, 1_000_000.0),
        (1_000_001.0, 1_000_000.0),
        (1_000_000.0, 1_000_000.0),
        (0.5, 0.5),
    ],
)
def test_clamp_costs(field: str, given: float, expected: float) -> None:
    cfg = build(payload_for_role("DISTRIBUTOR", **{field: given}))
    assert getattr(cfg.role_config(Role.DISTRIBUTOR), field) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [(-50.0, 0.0), (0.0, 0.0), (1e12, 1_000_000.0), (2_500.0, 2_500.0)],
)
def test_clamp_starting_capital(given: float, expected: float) -> None:
    """``§3.4``: clamped to ``[0, max_unit_value]``."""
    cfg = build(payload_for_role("RETAILER", starting_capital=given))
    assert cfg.role_config(Role.RETAILER).starting_capital == expected


@pytest.mark.parametrize(
    ("given", "expected"), [(50_000, 9_999), (10_000, 9_999), (9_999, 9_999), (25, 25)]
)
def test_clamp_max_order_quantity_upper_bound(given: int, expected: int) -> None:
    cfg = build(payload_with_visibility(max_order_quantity=given))
    assert cfg.visibility.max_order_quantity == expected


def test_clamping_uses_the_injected_limits_not_a_hardcoded_table() -> None:
    """``Limits`` exists so ``app/core`` stays free of ``app.config``."""
    tight = Limits(
        max_order_quantity=50,
        max_weeks=20,
        min_weeks=10,
        max_delay_weeks=3,
        min_delay_weeks=2,
        max_initial_quantity=15,
        max_unit_value=9.0,
    )
    payload = payload_for_all_roles(
        shipping_delay_weeks=8,
        initial_inventory=500,
        holding_cost_per_unit_week=100.0,
    )
    payload["duration_weeks"] = 104
    payload["visibility"]["max_order_quantity"] = 9_999
    cfg = GameConfig.from_host_input(payload, tight)

    assert cfg.duration_weeks == 20
    assert cfg.role_config(Role.RETAILER).shipping_delay_weeks == 3
    assert cfg.role_config(Role.RETAILER).initial_inventory == 15
    assert cfg.role_config(Role.RETAILER).holding_cost_per_unit_week == 9.0
    assert cfg.visibility.max_order_quantity == 50

    payload = base_payload(duration_weeks=8)
    assert GameConfig.from_host_input(payload, tight).duration_weeks == 10


# ---------------------------------------------------------------------------
# §3.1 nonsensical optional override -- §6 failure mode 9
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given", [-5, 0, -1, -9_999])
def test_nonsensical_max_order_quantity_becomes_none(given: int) -> None:
    cfg = build(payload_with_visibility(max_order_quantity=given))
    assert cfg.visibility.max_order_quantity is None


@pytest.mark.parametrize("given", [-5, 0, -1, -9_999])
def test_nonsensical_production_capacity_becomes_none(given: int) -> None:
    cfg = build(payload_for_role("FACTORY", production_capacity_per_week=given))
    assert cfg.factory_config().production_capacity_per_week is None


def test_a_sensible_optional_override_is_kept() -> None:
    cfg = build(payload_with_visibility(max_order_quantity=1))
    assert cfg.visibility.max_order_quantity == 1
    cfg = build(payload_for_role("FACTORY", production_capacity_per_week=30))
    assert cfg.factory_config().production_capacity_per_week == 30


def test_an_absent_optional_override_stays_none() -> None:
    cfg = build(payload_with_visibility(max_order_quantity=None))
    assert cfg.visibility.max_order_quantity is None
    cfg = build(payload_for_role("FACTORY", production_capacity_per_week=None))
    assert cfg.factory_config().production_capacity_per_week is None


# ---------------------------------------------------------------------------
# §3.1 swap -- AC 10, and §6 failure mode 1
# ---------------------------------------------------------------------------


def test_inverted_stochastic_range_is_swapped() -> None:
    cfg = build(
        payload_with_demand(
            kind="STOCHASTIC",
            distribution="NORMAL",
            mean=8.0,
            stdev=2.0,
            min=20,
            max=5,
        )
    )
    assert cfg.demand.min == 5
    assert cfg.demand.max == 20


def test_a_non_inverted_stochastic_range_is_left_alone() -> None:
    cfg = build(
        payload_with_demand(
            kind="STOCHASTIC",
            distribution="UNIFORM",
            mean=8.0,
            stdev=2.0,
            min=3,
            max=17,
        )
    )
    assert (cfg.demand.min, cfg.demand.max) == (3, 17)
    assert cfg.demand.distribution == Distribution.UNIFORM


def test_a_swapped_stochastic_range_is_safe_to_draw_from() -> None:
    """Failure mode 1.

    An inverted range makes ``random.randint(min, max)`` raise on *every*
    subsequent week; the exception fires before state is persisted, so the room
    is wedged permanently.  Drawing the series is section 04's job, so this
    asserts the property the generator depends on: the bounds the config hands
    out are ordered and usable.
    """
    cfg = build(
        payload_with_demand(
            kind="STOCHASTIC",
            distribution="NORMAL",
            mean=8.0,
            stdev=2.0,
            min=20,
            max=5,
        )
    )
    assert cfg.demand.min <= cfg.demand.max
    rng = random.Random(1234)
    for _ in range(50):
        draw = rng.randint(cfg.demand.min, cfg.demand.max)
        assert cfg.demand.min <= draw <= cfg.demand.max


def test_an_equal_stochastic_range_is_accepted() -> None:
    cfg = build(
        payload_with_demand(
            kind="STOCHASTIC",
            distribution="POISSON",
            mean=8.0,
            stdev=2.0,
            min=7,
            max=7,
        )
    )
    assert (cfg.demand.min, cfg.demand.max) == (7, 7)


# ---------------------------------------------------------------------------
# §6 failure mode 5 -- custom series length boundaries
# ---------------------------------------------------------------------------


def test_custom_series_one_short_raises_exact_length_and_longer_pass() -> None:
    weeks = 12

    payload = base_payload(duration_weeks=weeks)
    payload["demand"] = {"kind": "CUSTOM", "values": [4] * (weeks - 1)}
    assert_rejected(payload, field="demand.values")

    payload = base_payload(duration_weeks=weeks)
    payload["demand"] = {"kind": "CUSTOM", "values": [4] * weeks}
    assert build(payload).demand.values == [4] * weeks

    payload = base_payload(duration_weeks=weeks)
    payload["demand"] = {"kind": "CUSTOM", "values": [4] * (weeks + 10)}
    assert build(payload).demand.values == [4] * (weeks + 10)


def test_custom_series_length_is_measured_against_the_clamped_duration() -> None:
    """``duration_weeks`` below ``min_weeks`` is clamped up to 8 first, so a
    6-entry series is short for the game that will actually be played."""
    payload = base_payload(duration_weeks=6)
    payload["demand"] = {"kind": "CUSTOM", "values": [4] * 6}
    assert_rejected(payload, field="demand.values")


# ---------------------------------------------------------------------------
# §6 failure mode 10 -- currency symbol
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("symbol", ["€", "$", "¥", "£", "R$", "US$", "kr.", "CHF."])
def test_short_currency_symbols_are_accepted(symbol: str) -> None:
    assert len(symbol) <= 4
    assert build(base_payload(currency_symbol=symbol)).currency_symbol == symbol


def test_a_five_character_currency_symbol_raises() -> None:
    assert_rejected(base_payload(currency_symbol="ABCDE"), field="currency_symbol")


# ---------------------------------------------------------------------------
# §3.5 bot parameters -- AC 15
# ---------------------------------------------------------------------------


def test_bot_config_defaults() -> None:
    bot = BotConfig()
    assert bot.theta == 0.25
    assert bot.alpha == 0.30
    assert bot.beta == 0.25
    assert bot.target_stock_multiplier == 3.0


def test_game_config_carries_the_bot_defaults() -> None:
    assert get_preset("CLASSIC_MIT").bot == BotConfig()


@pytest.mark.parametrize("field", ["theta", "alpha", "beta"])
@pytest.mark.parametrize(
    ("given", "expected"),
    [(-0.5, 0.0), (0.0, 0.0), (1.0, 1.0), (1.5, 1.0), (99.0, 1.0), (0.4, 0.4)],
)
def test_bot_weights_clamp_to_zero_one(
    field: str, given: float, expected: float
) -> None:
    payload = base_payload()
    payload["bot"][field] = given
    cfg = build(payload)
    assert getattr(cfg.bot, field) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [(-1.0, 0.0), (0.0, 0.0), (10.0, 10.0), (10.5, 10.0), (1e6, 10.0), (4.5, 4.5)],
)
def test_target_stock_multiplier_clamps_to_zero_ten(
    given: float, expected: float
) -> None:
    payload = base_payload()
    payload["bot"]["target_stock_multiplier"] = given
    assert build(payload).bot.target_stock_multiplier == expected


# ---------------------------------------------------------------------------
# §3.6 allow_negative_orders -- this section only carries the flag
# ---------------------------------------------------------------------------


def test_allow_negative_orders_defaults_false_and_is_carried() -> None:
    assert VisibilityConfig().allow_negative_orders is False
    cfg = build(payload_with_visibility(allow_negative_orders=True))
    assert cfg.visibility.allow_negative_orders is True
    assert GameConfig.from_payload(cfg.to_payload()).visibility.allow_negative_orders


# ---------------------------------------------------------------------------
# §2 Limits -- AC 14
# ---------------------------------------------------------------------------


def test_default_limits_match_the_decisions_table() -> None:
    """``00-decisions.md §5``."""
    assert DEFAULT_LIMITS.max_order_quantity == 9_999
    assert DEFAULT_LIMITS.max_weeks == 104
    assert DEFAULT_LIMITS.min_weeks == 8
    assert DEFAULT_LIMITS.max_delay_weeks == 8
    assert DEFAULT_LIMITS.min_delay_weeks == 1
    assert DEFAULT_LIMITS.max_initial_quantity == 9_999
    assert DEFAULT_LIMITS.max_unit_value == 1_000_000.0


def test_default_limits_is_a_limits_instance_with_the_model_defaults() -> None:
    assert isinstance(DEFAULT_LIMITS, Limits)
    assert DEFAULT_LIMITS == Limits()


# ---------------------------------------------------------------------------
# D6 / D14 -- the superseded parameters must not exist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["round_timer_seconds", "timeout_policy", "allow_chat"]
)
def test_superseded_spec_parameters_are_absent(field: str) -> None:
    """``D6`` (untimed v1) and ``D14`` (no chat)."""
    assert field not in GameConfig.model_fields
    assert field not in VisibilityConfig.model_fields
    assert not hasattr(get_preset("CLASSIC_MIT"), field)


def test_stage_count_is_fixed_at_four() -> None:
    assert get_preset("CLASSIC_MIT").stage_count == 4
    assert len(ROLE_ORDER) == 4


# ---------------------------------------------------------------------------
# §5 AC 16 -- app/core purity, proved by inspecting the module's imports
# ---------------------------------------------------------------------------

FORBIDDEN_PACKAGES = ("app.config", "app.services", "app.db", "app.sockets", "app.api")


def _imported_module_names(module: Any) -> list[str]:
    """Every dotted module name ``module``'s source imports.

    Relative imports are resolved against the module's package so that
    ``from ..config import settings`` is seen as ``app.config``.
    """
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    package = module.__package__ or ""
    names: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                parts = package.split(".") if package else []
                trimmed = (
                    parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                )
                base = ".".join(trimmed)
                if node.module:
                    base = f"{base}.{node.module}" if base else node.module
            if base:
                names.append(base)
            for alias in node.names:
                names.append(f"{base}.{alias.name}" if base else alias.name)

    return names


def _offending_imports(module: Any) -> list[str]:
    return [
        name
        for name in _imported_module_names(module)
        if any(
            name == forbidden or name.startswith(forbidden + ".")
            for forbidden in FORBIDDEN_PACKAGES
        )
    ]


def test_config_models_imports_nothing_from_other_app_layers() -> None:
    """``§5`` item 16 / ``00-conventions.md §4``: ``app/core`` is pure."""
    from app.core import config_models

    offenders = _offending_imports(config_models)
    assert offenders == [], (
        f"app/core/config_models.py must not import from "
        f"{', '.join(FORBIDDEN_PACKAGES)}; found {offenders}"
    )


def test_the_import_inspector_would_catch_a_violation(tmp_path: Path) -> None:
    """Guard the guard: a module that *does* import a forbidden package is
    detected, so a green AC-16 test means something."""

    class _Stub:
        __package__ = "app.core"

    offending = tmp_path / "offender.py"
    offending.write_text(
        "from __future__ import annotations\n"
        "import app.services.state_service\n"
        "from ..config import settings\n"
        "from app.db import base\n",
        encoding="utf-8",
    )
    stub = _Stub()
    stub.__file__ = str(offending)  # type: ignore[attr-defined]

    offenders = _offending_imports(stub)
    assert "app.services.state_service" in offenders
    assert "app.config" in offenders
    assert "app.db" in offenders


def test_enums_module_is_also_pure() -> None:
    from app.core import enums

    assert _offending_imports(enums) == []


# ---------------------------------------------------------------------------
# Every demand generator, end to end -- §2, §3.7
# ---------------------------------------------------------------------------

DEMAND_PAYLOADS: dict[str, dict] = {
    "CONSTANT": {"kind": "CONSTANT", "value": 7},
    "STEP": {"kind": "STEP", "initial_value": 3, "step_week": 6, "step_value": 11},
    "RAMP": {
        "kind": "RAMP",
        "initial_value": 5,
        "slope_per_week": 0.75,
        "start_week": 4,
        "cap": 15,
    },
    "SEASONAL": {
        "kind": "SEASONAL",
        "base": 9,
        "amplitude": 5,
        "period_weeks": 13,
        "phase": 0.25,
    },
    "STOCHASTIC": {
        "kind": "STOCHASTIC",
        "distribution": "UNIFORM",
        "mean": 9.5,
        "stdev": 3.5,
        "min": 2,
        "max": 18,
    },
    "CUSTOM": {"kind": "CUSTOM", "values": list(range(20))},
}


@pytest.mark.parametrize("kind", list(DEMAND_PAYLOADS))
def test_every_demand_kind_is_accepted_and_round_trips(kind: str) -> None:
    payload = base_payload(duration_weeks=20)
    payload["demand"] = copy.deepcopy(DEMAND_PAYLOADS[kind])

    cfg = build(payload)
    assert cfg.demand.kind == DemandKind(kind)

    dumped = cfg.to_payload()
    _assert_json_primitive(dumped, "payload")
    assert dumped["demand"]["kind"] == kind
    assert GameConfig.from_payload(json.loads(json.dumps(dumped))) == cfg


def test_constant_demand_value_is_carried() -> None:
    cfg = build(payload_with_demand(kind="CONSTANT", value=7))
    assert isinstance(cfg.demand, ConstantDemand)
    assert cfg.demand.value == 7


def test_ramp_cap_is_carried_and_may_be_absent() -> None:
    capped = build(
        payload_with_demand(
            kind="RAMP", initial_value=4, slope_per_week=1.5, start_week=3, cap=15
        )
    )
    assert isinstance(capped.demand, RampDemand)
    assert capped.demand.cap == 15
    assert capped.demand.slope_per_week == 1.5

    uncapped = build(
        payload_with_demand(
            kind="RAMP", initial_value=4, slope_per_week=1.0, start_week=3, cap=None
        )
    )
    assert uncapped.demand.cap is None


@pytest.mark.parametrize("distribution", ["NORMAL", "POISSON", "UNIFORM"])
def test_every_stochastic_distribution_is_accepted(distribution: str) -> None:
    cfg = build(
        payload_with_demand(
            kind="STOCHASTIC",
            distribution=distribution,
            mean=8.0,
            stdev=2.0,
            min=1,
            max=15,
        )
    )
    assert isinstance(cfg.demand, StochasticDemand)
    assert cfg.demand.distribution == Distribution(distribution)
    assert cfg.to_payload()["demand"]["distribution"] == distribution


def test_seasonal_parameters_are_carried() -> None:
    cfg = build(
        payload_with_demand(
            kind="SEASONAL", base=9, amplitude=5, period_weeks=13, phase=0.25
        )
    )
    assert isinstance(cfg.demand, SeasonalDemand)
    assert (cfg.demand.base, cfg.demand.amplitude) == (9, 5)
    assert (cfg.demand.period_weeks, cfg.demand.phase) == (13, 0.25)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize(
    ("demand", "field"),
    [
        (
            {"kind": "SEASONAL", "base": 8, "amplitude": 4, "period_weeks": 12},
            "demand.phase",
        ),
        (
            {
                "kind": "STOCHASTIC",
                "distribution": "NORMAL",
                "stdev": 2.0,
                "min": 0,
                "max": 20,
            },
            "demand.mean",
        ),
        (
            {
                "kind": "STOCHASTIC",
                "distribution": "NORMAL",
                "mean": 8.0,
                "min": 0,
                "max": 20,
            },
            "demand.stdev",
        ),
    ],
    ids=["seasonal.phase", "stochastic.mean", "stochastic.stdev"],
)
def test_reject_8_non_finite_in_any_demand_float_field(
    bad: float, demand: dict, field: str
) -> None:
    payload_demand = dict(demand)
    payload_demand[field.split(".", 1)[1]] = bad
    assert_rejected(payload_with_demand(**payload_demand), field=field)


# ---------------------------------------------------------------------------
# Clamping is per role, not global -- §3.1 "every initial quantity, every cost"
# ---------------------------------------------------------------------------


def test_each_role_is_clamped_independently() -> None:
    payload = base_payload()
    payload["roles"]["RETAILER"].update(
        initial_inventory=-10, shipping_delay_weeks=0, holding_cost_per_unit_week=-1.0
    )
    payload["roles"]["WHOLESALER"].update(
        initial_inventory=50_000,
        shipping_delay_weeks=99,
        backlog_cost_per_unit_week=1e12,
    )
    payload["roles"]["DISTRIBUTOR"].update(
        initial_backlog=-3, information_delay_weeks=0, fixed_order_cost=-4.0
    )
    payload["roles"]["FACTORY"].update(
        initial_pipeline_quantity=99_999,
        information_delay_weeks=42,
        production_delay_weeks=0,
        unit_purchase_cost=1e30,
        starting_capital=-1.0,
    )

    cfg = build(payload)

    retailer = cfg.role_config(Role.RETAILER)
    assert retailer.initial_inventory == 0
    assert retailer.shipping_delay_weeks == 1
    assert retailer.holding_cost_per_unit_week == 0.0

    wholesaler = cfg.role_config(Role.WHOLESALER)
    assert wholesaler.initial_inventory == 9_999
    assert wholesaler.shipping_delay_weeks == 8
    assert wholesaler.backlog_cost_per_unit_week == 1_000_000.0

    distributor = cfg.role_config(Role.DISTRIBUTOR)
    assert distributor.initial_backlog == 0
    assert distributor.information_delay_weeks == 1
    assert distributor.fixed_order_cost == 0.0

    factory = cfg.factory_config()
    assert factory.initial_pipeline_quantity == 9_999
    assert factory.information_delay_weeks == 8
    assert factory.production_delay_weeks == 1
    assert factory.unit_purchase_cost == 1_000_000.0
    assert factory.starting_capital == 0.0

    # Roles the host did not break are untouched.
    assert cfg.role_config(Role.RETAILER).initial_backlog == 0
    assert cfg.role_config(Role.WHOLESALER).holding_cost_per_unit_week == 0.50


@pytest.mark.parametrize("role_name", list(ROLE_NAMES))
def test_clamping_covers_every_numeric_field_of_every_role(role_name: str) -> None:
    payload = payload_for_role(
        role_name,
        initial_inventory=-1,
        initial_backlog=-1,
        initial_pipeline_quantity=-1,
        initial_order_in_pipeline=-1,
        shipping_delay_weeks=0,
        information_delay_weeks=0,
        holding_cost_per_unit_week=-1.0,
        backlog_cost_per_unit_week=-1.0,
        fixed_order_cost=-1.0,
        unit_purchase_cost=-1.0,
        starting_capital=-1.0,
    )
    rc = build(payload).role_config(Role(role_name))

    assert rc.initial_inventory == 0
    assert rc.initial_backlog == 0
    assert rc.initial_pipeline_quantity == 0
    assert rc.initial_order_in_pipeline == 0
    assert rc.shipping_delay_weeks == 1
    assert rc.information_delay_weeks == 1
    assert rc.holding_cost_per_unit_week == 0.0
    assert rc.backlog_cost_per_unit_week == 0.0
    assert rc.fixed_order_cost == 0.0
    assert rc.unit_purchase_cost == 0.0
    assert rc.starting_capital == 0.0


# ---------------------------------------------------------------------------
# Defaulted fields -- §2 declares a default for everything but `roles` and
# `demand`, so a minimal host payload must still produce a complete config.
# ---------------------------------------------------------------------------


def minimal_payload() -> dict:
    payload = base_payload()
    return {"roles": payload["roles"], "demand": payload["demand"]}


def test_a_minimal_host_payload_takes_every_declared_default() -> None:
    cfg = build(minimal_payload())

    assert cfg.duration_weeks == 36
    assert cfg.stage_count == 4
    assert cfg.pause_on_disconnect is True
    assert cfg.bot_fill_empty_roles is False
    assert cfg.random_seed is None
    assert cfg.currency_symbol == "$"
    assert cfg.role_assignment_mode == RoleAssignmentMode.HOST_ASSIGNS
    assert cfg.preset_name is None
    assert cfg.visibility == VisibilityConfig()
    assert cfg.bot == BotConfig()


def test_a_minimal_host_payload_still_round_trips() -> None:
    cfg = build(minimal_payload())
    assert GameConfig.from_payload(json.loads(json.dumps(cfg.to_payload()))) == cfg


def test_omitted_role_fields_take_their_model_defaults() -> None:
    payload = minimal_payload()
    payload["roles"] = {
        "RETAILER": {"initial_inventory": 20},
        "WHOLESALER": {},
        "DISTRIBUTOR": {"holding_cost_per_unit_week": 0.75},
        "FACTORY": {"production_capacity_per_week": 30},
    }
    cfg = build(payload)

    assert cfg.role_config(Role.RETAILER).initial_inventory == 20
    assert cfg.role_config(Role.RETAILER).shipping_delay_weeks == 2
    assert cfg.role_config(Role.WHOLESALER) == RoleConfig()
    assert cfg.role_config(Role.DISTRIBUTOR).holding_cost_per_unit_week == 0.75
    assert cfg.factory_config().production_delay_weeks == 2
    assert cfg.factory_config().production_capacity_per_week == 30


@pytest.mark.parametrize("seed", [0, 1, 987_654_321, -17])
def test_random_seed_is_carried_verbatim(seed: int) -> None:
    """``D11``: the host's seed is the game's seed. It is not a clamped knob."""
    cfg = build(base_payload(random_seed=seed))
    assert cfg.random_seed == seed
    assert GameConfig.from_payload(cfg.to_payload()).random_seed == seed


@pytest.mark.parametrize("mode", ["HOST_ASSIGNS", "PLAYER_CHOOSES", "RANDOM"])
def test_role_assignment_mode_accepts_every_member(mode: str) -> None:
    cfg = build(base_payload(role_assignment_mode=mode))
    assert cfg.role_assignment_mode == RoleAssignmentMode(mode)
    assert cfg.to_payload()["role_assignment_mode"] == mode


@pytest.mark.parametrize("flag", ["pause_on_disconnect", "bot_fill_empty_roles"])
@pytest.mark.parametrize("value", [True, False])
def test_boolean_game_flags_are_carried(flag: str, value: bool) -> None:
    cfg = build(base_payload(**{flag: value}))
    assert getattr(cfg, flag) is value
    assert GameConfig.from_payload(cfg.to_payload()) == cfg


@pytest.mark.parametrize(
    "flag",
    [
        "show_true_customer_demand_to_all",
        "show_neighbour_inventory",
        "show_all_inventories",
        "show_supply_line_prominently",
        "show_running_cost_to_players",
        "show_leaderboard_during_game",
    ],
)
@pytest.mark.parametrize("value", [True, False])
def test_visibility_flags_are_carried(flag: str, value: bool) -> None:
    cfg = build(payload_with_visibility(**{flag: value}))
    assert getattr(cfg.visibility, flag) is value


def test_preset_name_is_carried_and_optional() -> None:
    assert build(base_payload(preset_name="CHAOS")).preset_name == "CHAOS"
    assert build(base_payload(preset_name=None)).preset_name is None


# ---------------------------------------------------------------------------
# §3.1: "from_payload ... raises on a structurally invalid payload."
# ---------------------------------------------------------------------------


def _broken(mutate) -> dict:
    payload = get_preset("CLASSIC_MIT").to_payload()
    mutate(payload)
    return payload


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("roles"),
        lambda p: p.pop("demand"),
        lambda p: p["roles"].pop("FACTORY"),
        lambda p: p.__setitem__("roles", []),
        lambda p: p.__setitem__("demand", {"kind": "NOT_A_KIND", "value": 4}),
        lambda p: p["demand"].pop("kind"),
        lambda p: p.__setitem__("duration_weeks", "thirty-six"),
        lambda p: p.__setitem__("stage_count", 6),
        lambda p: p["roles"]["RETAILER"].__setitem__("initial_inventory", "twelve"),
        lambda p: p.__setitem__("role_assignment_mode", "NOBODY_ASSIGNS"),
        lambda p: p.__setitem__("visibility", "yes please"),
    ],
    ids=[
        "no-roles",
        "no-demand",
        "missing-factory",
        "roles-not-a-mapping",
        "unknown-demand-kind",
        "demand-without-discriminator",
        "duration-not-an-int",
        "stage-count-not-four",
        "quantity-not-an-int",
        "unknown-assignment-mode",
        "visibility-not-a-mapping",
    ],
)
def test_from_payload_raises_on_a_structurally_invalid_payload(mutate) -> None:
    """``§3.1``: ``from_payload``'s input is the server's own serialised state,
    so a structural failure there is a bug, not a user error -- it raises
    pydantic's ``ValidationError``, *not* ``ConfigValidationError``."""
    with pytest.raises(ValidationError) as excinfo:
        GameConfig.from_payload(_broken(mutate))
    assert isinstance(excinfo.value, ValueError)
    assert not isinstance(excinfo.value, ConfigValidationError)


def test_from_payload_accepts_exactly_what_to_payload_produced() -> None:
    """The negative cases above only mean something if the untouched payload
    is accepted."""
    payload = get_preset("CLASSIC_MIT").to_payload()
    assert GameConfig.from_payload(payload) == get_preset("CLASSIC_MIT")


# ---------------------------------------------------------------------------
# §5 AC 8b -- from_host_input raises NOTHING but ConfigValidationError
# ---------------------------------------------------------------------------


def assert_raises_only_config_validation_error(payload: dict) -> ConfigValidationError:
    """``§3.1``: ``from_host_input`` takes untrusted input straight from a host
    over the wire, so it must have exactly one failure mode.  Letting pydantic's
    own ``ValidationError`` escape turns a fat-fingered host into a 500 at the
    section 10 boundary.
    """
    try:
        GameConfig.from_host_input(payload, DEFAULT_LIMITS)
    except ConfigValidationError as err:
        assert isinstance(err.field, str) and err.field
        assert str(err)
        return err
    except Exception as err:  # noqa: BLE001 - catching broadly IS the test
        pytest.fail(
            f"from_host_input raised {type(err).__name__}, which callers do not "
            f"catch; §3.1 requires ConfigValidationError: {err}"
        )
    pytest.fail("from_host_input accepted a payload the test expected it to reject")


def test_8b_malformed_demand_is_a_config_validation_error() -> None:
    for demand in (None, "STEP", 7, [], ["kind", "STEP"]):
        payload = base_payload()
        payload["demand"] = demand
        err = assert_raises_only_config_validation_error(payload)
        assert err.field == "demand"


def test_8b_missing_demand_is_a_config_validation_error() -> None:
    payload = base_payload()
    del payload["demand"]
    err = assert_raises_only_config_validation_error(payload)
    assert err.field == "demand"


def test_8b_unknown_demand_kind_is_a_config_validation_error() -> None:
    for kind in ("NOT_A_KIND", "", "step", 3):
        err = assert_raises_only_config_validation_error(
            payload_with_demand(kind=kind, value=4)
        )
        assert err.field == "demand.kind"


def test_8b_missing_demand_kind_is_a_config_validation_error() -> None:
    payload = base_payload()
    payload["demand"] = {"initial_value": 4, "step_week": 5, "step_value": 8}
    err = assert_raises_only_config_validation_error(payload)
    assert err.field in {"demand", "demand.kind"}


@pytest.mark.parametrize("roles", [None, [], "RETAILER", 4, ["RETAILER"]])
def test_8b_roles_that_is_not_a_mapping_is_a_config_validation_error(
    roles: Any,
) -> None:
    payload = base_payload()
    payload["roles"] = roles
    err = assert_raises_only_config_validation_error(payload)
    assert err.field == "roles"


@pytest.mark.parametrize("role_name", list(ROLE_NAMES))
def test_8b_a_role_entry_that_is_not_a_mapping_is_a_config_validation_error(
    role_name: str,
) -> None:
    payload = base_payload()
    payload["roles"][role_name] = "twelve units please"
    err = assert_raises_only_config_validation_error(payload)
    assert err.field == f"roles.{role_name}"


@pytest.mark.parametrize("mode", ["NOBODY_ASSIGNS", "", 7, None, "host_assigns"])
def test_8b_an_unrecognised_role_assignment_mode_falls_back_to_its_default(
    mode: Any,
) -> None:
    """``§3.1``: an unrecognised value for an enum that *has* a default falls
    back to that default rather than raising."""
    cfg = build(base_payload(role_assignment_mode=mode))
    assert cfg.role_assignment_mode == RoleAssignmentMode.HOST_ASSIGNS


@pytest.mark.parametrize("distribution", ["GAUSSIAN", "", 7, None])
def test_8b_an_unrecognised_distribution_falls_back_to_its_default(
    distribution: Any,
) -> None:
    cfg = build(
        payload_with_demand(
            kind="STOCHASTIC",
            distribution=distribution,
            mean=8.0,
            stdev=2.0,
            min=0,
            max=20,
        )
    )
    assert cfg.demand.distribution == Distribution.NORMAL


@pytest.mark.parametrize("stage_count", [5, 1, 0, -2, 6, "four", None])
def test_8b_stage_count_is_forced_to_four_rather_than_rejected(
    stage_count: Any,
) -> None:
    """``D14``: v1 is a four-stage chain, and rejection is not among the nine."""
    assert build(base_payload(stage_count=stage_count)).stage_count == 4


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("duration_weeks", "thirty-six", 36),
        ("duration_weeks", None, 36),
        ("duration_weeks", [], 36),
        ("currency_symbol", 7, "$"),
        ("currency_symbol", None, "$"),
        ("pause_on_disconnect", "maybe", True),
        ("bot_fill_empty_roles", "maybe", False),
        ("random_seed", "not-a-seed", None),
        ("preset_name", 7, None),
    ],
)
def test_8b_an_unusable_scalar_falls_back_to_its_declared_default(
    key: str, value: Any, expected: Any
) -> None:
    """``§3.1``: "an unusable scalar falls back to its declared default and is
    then clamped"."""
    assert getattr(build(base_payload(**{key: value})), key) == expected


@pytest.mark.parametrize(
    "field",
    [
        "initial_inventory",
        "shipping_delay_weeks",
        "holding_cost_per_unit_week",
        "starting_capital",
    ],
)
def test_8b_an_unusable_role_scalar_falls_back_to_its_declared_default(
    field: str,
) -> None:
    cfg = build(payload_for_role("RETAILER", **{field: "not a number"}))
    assert getattr(cfg.role_config(Role.RETAILER), field) == getattr(
        RoleConfig(), field
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.__setitem__("visibility", "yes please"),
        lambda p: p.__setitem__("bot", 7),
        lambda p: p.__setitem__("visibility", None),
        lambda p: p.__setitem__("bot", None),
        lambda p: p["visibility"].__setitem__("max_order_quantity", "lots"),
        lambda p: p["bot"].__setitem__("theta", "a quarter"),
        lambda p: p["demand"].__setitem__("step_value", "eight"),
        lambda p: p["roles"]["FACTORY"].__setitem__("production_delay_weeks", {}),
        lambda p: p.__setitem__("stage_count", object()),
        lambda p: p.__setitem__("roles", {"RETAILER": None}),
        lambda p: p.__setitem__("demand", {"kind": "CUSTOM", "values": "4444"}),
        lambda p: p.__setitem__("demand", {"kind": "CUSTOM"}),
    ],
    ids=[
        "visibility-not-a-mapping",
        "bot-not-a-mapping",
        "visibility-none",
        "bot-none",
        "visibility-scalar-garbage",
        "bot-scalar-garbage",
        "demand-scalar-garbage",
        "role-scalar-garbage",
        "stage-count-garbage",
        "roles-with-a-none-entry",
        "custom-values-not-a-list",
        "custom-values-missing",
    ],
)
def test_8b_no_malformed_payload_escapes_as_anything_else(mutate) -> None:
    """Whatever the host sends, the only exception a caller must handle is
    ``ConfigValidationError`` -- and several of these are repaired instead,
    which is also fine.  What is *not* fine is a pydantic ``ValidationError``
    or a ``TypeError`` reaching section 10."""
    payload = base_payload()
    mutate(payload)
    try:
        cfg = GameConfig.from_host_input(payload, DEFAULT_LIMITS)
    except ConfigValidationError as err:
        assert isinstance(err.field, str) and err.field
    except Exception as err:  # noqa: BLE001 - catching broadly IS the test
        pytest.fail(
            f"from_host_input raised {type(err).__name__} rather than "
            f"ConfigValidationError: {err}"
        )
    else:
        assert isinstance(cfg, GameConfig)


def test_8b_a_pydantic_validation_error_is_not_a_config_validation_error() -> None:
    """Guard the guard: the two exception types really are distinguishable, so
    the AC-8b tests above are not vacuous."""
    assert not issubclass(ValidationError, ConfigValidationError)
    assert issubclass(ConfigValidationError, ValueError)


# ---------------------------------------------------------------------------
# §3.1 rejection evaluation order:
# duration_weeks -> currency_symbol -> roles -> demand
# ---------------------------------------------------------------------------


def test_currency_symbol_is_reported_before_roles_and_demand() -> None:
    payload = base_payload(currency_symbol="FIVE!")
    del payload["roles"]["FACTORY"]
    payload["demand"] = {
        "kind": "SEASONAL",
        "base": 8,
        "amplitude": 4,
        "period_weeks": 1,
    }
    assert_rejected(payload, field="currency_symbol")


def test_roles_is_reported_before_demand() -> None:
    payload = base_payload()
    del payload["roles"]["WHOLESALER"]
    payload["demand"] = {
        "kind": "SEASONAL",
        "base": 8,
        "amplitude": 4,
        "period_weeks": 1,
    }
    assert_rejected(payload, field="roles")


def test_currency_symbol_is_reported_before_demand() -> None:
    payload = base_payload(currency_symbol="FIVE!", duration_weeks=12)
    payload["demand"] = {"kind": "CUSTOM", "values": [4] * 3}
    assert_rejected(payload, field="currency_symbol")


def test_duration_weeks_is_clamped_before_the_demand_rules_are_applied() -> None:
    """``duration_weeks`` leads the order because rules 1, 3 and 4 are all
    measured against it.

    A host who asks for 500 weeks gets 104, so a 104-entry ``CUSTOM`` series is
    long enough.  Measured against the *raw* 500 it would be 396 short, so this
    is the case that tells the two orderings apart.
    """
    payload = base_payload(duration_weeks=500)
    payload["demand"] = {"kind": "CUSTOM", "values": [4] * 104}
    cfg = build(payload)
    assert cfg.duration_weeks == 104
    assert len(cfg.demand.values) == 104

    # One short of the clamped duration still raises.
    payload = base_payload(duration_weeks=500)
    payload["demand"] = {"kind": "CUSTOM", "values": [4] * 103}
    assert_rejected(payload, field="demand.values")


def test_duration_weeks_is_clamped_before_the_step_week_rule() -> None:
    payload = base_payload(duration_weeks=500)
    payload["demand"] = {
        "kind": "STEP",
        "initial_value": 4,
        "step_week": 200,
        "step_value": 8,
    }
    assert_rejected(payload, field="demand.step_week")


def test_a_valid_payload_is_unaffected_by_the_ordering() -> None:
    assert build(base_payload()) == get_preset("CLASSIC_MIT")


# ---------------------------------------------------------------------------
# §3.1 demand magnitudes are clamped to [0, limits.max_order_quantity]
# ---------------------------------------------------------------------------

MAX_Q = DEFAULT_LIMITS.max_order_quantity


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (-5, 0),
        (-1, 0),
        (0, 0),
        (50_000, MAX_Q),
        (MAX_Q + 1, MAX_Q),
        (MAX_Q, MAX_Q),
        (7, 7),
    ],
)
def test_clamp_constant_demand_value(given: int, expected: int) -> None:
    cfg = build(payload_with_demand(kind="CONSTANT", value=given))
    assert cfg.demand.value == expected


@pytest.mark.parametrize("field", ["initial_value", "step_value"])
@pytest.mark.parametrize(("given", "expected"), [(-5, 0), (50_000, MAX_Q), (6, 6)])
def test_clamp_step_demand_magnitudes(field: str, given: int, expected: int) -> None:
    demand = {"kind": "STEP", "initial_value": 4, "step_week": 5, "step_value": 8}
    demand[field] = given
    cfg = build(payload_with_demand(**demand))
    assert getattr(cfg.demand, field) == expected


@pytest.mark.parametrize(("given", "expected"), [(-5, 0), (50_000, MAX_Q), (9, 9)])
def test_clamp_ramp_initial_value(given: int, expected: int) -> None:
    cfg = build(
        payload_with_demand(
            kind="RAMP",
            initial_value=given,
            slope_per_week=1.0,
            start_week=5,
            cap=None,
        )
    )
    assert cfg.demand.initial_value == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (-1e9, -float(MAX_Q)),
        (-float(MAX_Q) - 1, -float(MAX_Q)),
        (1e9, float(MAX_Q)),
        (-2.5, -2.5),
        (2.5, 2.5),
    ],
)
def test_clamp_ramp_slope_to_plus_or_minus_max_order_quantity(
    given: float, expected: float
) -> None:
    """``§3.1``: ``slope_per_week`` clamps to ``±max_order_quantity`` -- a
    negative slope is a legitimate declining-demand scenario, so it is not
    floored at 0 the way a magnitude is."""
    cfg = build(
        payload_with_demand(
            kind="RAMP",
            initial_value=4,
            slope_per_week=given,
            start_week=5,
            cap=None,
        )
    )
    assert cfg.demand.slope_per_week == expected


@pytest.mark.parametrize(
    ("given", "expected"), [(-5, 0), (0, 0), (50_000, MAX_Q), (15, 15)]
)
def test_ramp_cap_is_clamped_not_reduced_to_none(given: int, expected: int) -> None:
    """``§3.1``: ``cap`` is clamped, **not** reduced to ``None`` -- ``cap = 0``
    is a coherent if dull scenario, not a wedged game, so it is not one of the
    two optional overrides that collapse to ``None``."""
    cfg = build(
        payload_with_demand(
            kind="RAMP",
            initial_value=4,
            slope_per_week=1.0,
            start_week=5,
            cap=given,
        )
    )
    assert cfg.demand.cap == expected
    assert cfg.demand.cap is not None


def test_an_absent_ramp_cap_stays_none() -> None:
    cfg = build(
        payload_with_demand(
            kind="RAMP", initial_value=4, slope_per_week=1.0, start_week=5, cap=None
        )
    )
    assert cfg.demand.cap is None


@pytest.mark.parametrize("field", ["base", "amplitude"])
@pytest.mark.parametrize(("given", "expected"), [(-5, 0), (50_000, MAX_Q), (11, 11)])
def test_clamp_seasonal_magnitudes(field: str, given: int, expected: int) -> None:
    demand = {
        "kind": "SEASONAL",
        "base": 8,
        "amplitude": 4,
        "period_weeks": 12,
        "phase": 0.0,
    }
    demand[field] = given
    cfg = build(payload_with_demand(**demand))
    assert getattr(cfg.demand, field) == expected


@pytest.mark.parametrize("period_weeks", [2, 12, 104, 5_000, 1_000_000])
def test_seasonal_period_weeks_is_not_upper_clamped(period_weeks: int) -> None:
    """``§3.1``: "``period_weeks`` and ``phase`` are not upper-clamped; a long
    period is harmless."""
    cfg = build(
        payload_with_demand(
            kind="SEASONAL",
            base=8,
            amplitude=4,
            period_weeks=period_weeks,
            phase=0.0,
        )
    )
    assert cfg.demand.period_weeks == period_weeks


@pytest.mark.parametrize("phase", [0.0, 1.5, -3.25, 1e6, -1e6])
def test_seasonal_phase_is_not_clamped(phase: float) -> None:
    cfg = build(
        payload_with_demand(
            kind="SEASONAL", base=8, amplitude=4, period_weeks=12, phase=phase
        )
    )
    assert cfg.demand.phase == phase


@pytest.mark.parametrize("field", ["mean", "stdev"])
@pytest.mark.parametrize(
    ("given", "expected"), [(-5.0, 0.0), (1e9, float(MAX_Q)), (6.5, 6.5)]
)
def test_clamp_stochastic_float_magnitudes(
    field: str, given: float, expected: float
) -> None:
    demand = {
        "kind": "STOCHASTIC",
        "distribution": "NORMAL",
        "mean": 8.0,
        "stdev": 2.0,
        "min": 0,
        "max": 20,
    }
    demand[field] = given
    cfg = build(payload_with_demand(**demand))
    assert getattr(cfg.demand, field) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ((-5, 17), (0, 17)),
        ((3, -5), (0, 3)),
        ((50_000, 9_999), (9_999, 9_999)),
        ((3, 50_000), (3, 9_999)),
        ((-5, -50), (0, 0)),
        ((50_000, 100_000), (9_999, 9_999)),
        ((3, 17), (3, 17)),
    ],
)
def test_clamp_stochastic_bounds(
    given: tuple[int, int], expected: tuple[int, int]
) -> None:
    """Both bounds clamp to ``[0, max_order_quantity]``.

    Asserted as a *pair*, because clamping and the ``§3.1`` swap compose: a
    bound that clamps past its partner inverts the range, and the swap then
    reorders it.  Asserting one bound in isolation would be asserting an
    intermediate value that no caller ever sees.
    """
    cfg = build(
        payload_with_demand(
            kind="STOCHASTIC",
            distribution="NORMAL",
            mean=8.0,
            stdev=2.0,
            min=given[0],
            max=given[1],
        )
    )
    assert (cfg.demand.min, cfg.demand.max) == expected
    assert 0 <= cfg.demand.min <= cfg.demand.max <= MAX_Q


def test_clamping_a_stochastic_bound_never_leaves_it_inverted() -> None:
    """Clamp and swap have to compose: whichever runs first, the bounds that
    come out must still be safe for ``random.randint``."""
    for low, high in ((50_000, 5), (-5, -50), (20, -3), (99_999, 0)):
        cfg = build(
            payload_with_demand(
                kind="STOCHASTIC",
                distribution="NORMAL",
                mean=8.0,
                stdev=2.0,
                min=low,
                max=high,
            )
        )
        assert 0 <= cfg.demand.min <= cfg.demand.max <= MAX_Q
        random.Random(7).randint(cfg.demand.min, cfg.demand.max)


@pytest.mark.parametrize(
    ("given", "expected"), [(50_000, MAX_Q), (MAX_Q + 1, MAX_Q), (0, 0), (4, 4)]
)
def test_clamp_custom_demand_entries(given: int, expected: int) -> None:
    payload = base_payload(duration_weeks=8)
    values = [4] * 8
    values[3] = given
    payload["demand"] = {"kind": "CUSTOM", "values": values}
    cfg = build(payload)
    assert cfg.demand.values[3] == expected
    assert all(0 <= v <= MAX_Q for v in cfg.demand.values)


def test_custom_rules_are_evaluated_before_clamping() -> None:
    """``§3.1``: "rules 1 and 2 are evaluated *before* clamping, so a short or
    negative ``CUSTOM`` series still raises rather than being silently repaired
    into validity"."""
    payload = base_payload(duration_weeks=8)
    payload["demand"] = {"kind": "CUSTOM", "values": [4, 4, -1, 4, 4, 4, 4, 4]}
    assert_rejected(payload, field="demand.values")

    payload = base_payload(duration_weeks=8)
    payload["demand"] = {"kind": "CUSTOM", "values": [4, 4, -100_000, 4, 4, 4, 4, 4]}
    assert_rejected(payload, field="demand.values")

    payload = base_payload(duration_weeks=8)
    payload["demand"] = {"kind": "CUSTOM", "values": [4] * 7}
    assert_rejected(payload, field="demand.values")


# ---------------------------------------------------------------------------
# §3.2 production_capacity_per_week's upper bound is limits.max_order_quantity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [(50_000, MAX_Q), (MAX_Q + 1, MAX_Q), (MAX_Q, MAX_Q), (20, 20), (1, 1)],
)
def test_clamp_production_capacity_upper_bound(given: int, expected: int) -> None:
    cfg = build(payload_for_role("FACTORY", production_capacity_per_week=given))
    assert cfg.factory_config().production_capacity_per_week == expected


def test_production_capacity_upper_bound_follows_the_injected_limits() -> None:
    tight = Limits(max_order_quantity=50)
    payload = payload_for_role("FACTORY", production_capacity_per_week=9_999)
    cfg = GameConfig.from_host_input(payload, tight)
    assert cfg.factory_config().production_capacity_per_week == 50


def test_demand_magnitudes_follow_the_injected_limits() -> None:
    tight = Limits(max_order_quantity=50)
    payload = payload_with_demand(kind="CONSTANT", value=9_999)
    assert GameConfig.from_host_input(payload, tight).demand.value == 50


# ---------------------------------------------------------------------------
# §2: `roles` is a Mapping, not a plain dict
# ---------------------------------------------------------------------------


def test_roles_is_an_immutable_mapping() -> None:
    cfg = get_preset("CLASSIC_MIT")
    assert isinstance(cfg.roles, Mapping)
    assert set(cfg.roles) == set(ROLE_ORDER)
    assert cfg.roles[Role.FACTORY] == cfg.factory_config()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.__setitem__(Role.RETAILER, RoleConfig(initial_inventory=1)),
        lambda m: m.__delitem__(Role.RETAILER),
    ],
    ids=["setitem", "delitem"],
)
def test_the_roles_mapping_refuses_every_write(mutate) -> None:
    cfg = get_preset("CLASSIC_MIT")
    with pytest.raises(TypeError):
        mutate(cfg.roles)
    assert set(cfg.roles) == set(ROLE_ORDER)
    assert cfg.role_config(Role.RETAILER).initial_inventory == 12


# ---------------------------------------------------------------------------
# §4 coercion block (normative) and §5 AC 9b
# ---------------------------------------------------------------------------


def test_worked_example_coercion_of_duration_weeks() -> None:
    """``§4``, asserted literally. A host's form submits strings, and a JSON
    payload can carry anything."""
    assert build(base_payload(duration_weeks="40")).duration_weeks == 40
    assert build(base_payload(duration_weeks="banana")).duration_weeks == 36
    assert build(base_payload(duration_weeks=[1, 2])).duration_weeks == 36
    assert build(base_payload(duration_weeks=None)).duration_weeks == 36


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("40", 40),
        (" 40 ", 40),
        ("104", 104),
        ("8", 8),
        ("500", 104),  # coerced, then clamped
        ("2", 8),  # coerced, then clamped
        (40.0, 40),
        (40.7, 40),
        ("banana", 36),
        ("", 36),
        ([1, 2], 36),
        ({}, 36),
        (None, 36),
        (object(), 36),
    ],
)
def test_9b_a_mistyped_duration_weeks_is_coerced_or_defaulted_then_clamped(
    given: Any, expected: int
) -> None:
    assert build(base_payload(duration_weeks=given)).duration_weeks == expected


@pytest.mark.parametrize(
    ("field", "given", "expected"),
    [
        ("initial_inventory", "20", 20),
        ("initial_inventory", 20.0, 20),
        ("initial_inventory", True, 1),  # a bool reads as a number
        ("initial_inventory", False, 0),
        ("initial_inventory", "-3", 0),  # coerced, then clamped
        ("initial_inventory", "50000", 9_999),
        ("initial_inventory", "twelve", 12),  # declared default
        ("shipping_delay_weeks", "3", 3),
        ("shipping_delay_weeks", "0", 1),  # coerced, then clamped
        ("shipping_delay_weeks", "soon", 2),
        ("holding_cost_per_unit_week", "1.25", 1.25),
        ("holding_cost_per_unit_week", "-1", 0.0),
        ("holding_cost_per_unit_week", 2, 2.0),
        ("holding_cost_per_unit_week", "free", 0.50),
        ("starting_capital", "250.5", 250.5),
        ("starting_capital", "lots", 0.0),
    ],
)
def test_9b_mistyped_role_scalars_are_coerced_or_defaulted(
    field: str, given: Any, expected: Any
) -> None:
    cfg = build(payload_for_role("RETAILER", **{field: given}))
    assert getattr(cfg.role_config(Role.RETAILER), field) == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("yes", True),
        ("true", True),
        ("True", True),
        ("1", True),
        (1, True),
        ("no", False),
        ("false", False),
        ("False", False),
        ("0", False),
        (0, False),
    ],
)
def test_9b_bool_fields_read_yes_false_and_zero(given: Any, expected: bool) -> None:
    """``§4``: "``yes``/``false``/``0`` read as bools for a bool field"."""
    assert (
        build(base_payload(pause_on_disconnect=given)).pause_on_disconnect is expected
    )
    assert (
        build(base_payload(bot_fill_empty_roles=given)).bot_fill_empty_roles is expected
    )
    cfg = build(payload_with_visibility(show_all_inventories=given))
    assert cfg.visibility.show_all_inventories is expected


@pytest.mark.parametrize("given", ["maybe", "", [], {}, None, object()])
def test_9b_an_unusable_bool_falls_back_to_its_declared_default(given: Any) -> None:
    assert build(base_payload(pause_on_disconnect=given)).pause_on_disconnect is True
    assert build(base_payload(bot_fill_empty_roles=given)).bot_fill_empty_roles is False


@pytest.mark.parametrize(
    ("given", "expected"), [("123", 123), (123.0, 123), ("-4", -4), ("seedy", None)]
)
def test_9b_mistyped_random_seed_is_coerced_or_defaulted(
    given: Any, expected: int | None
) -> None:
    assert build(base_payload(random_seed=given)).random_seed == expected


@pytest.mark.parametrize(
    ("field", "given", "expected"),
    [
        ("step_week", "6", 6),
        ("step_week", 6.0, 6),
        ("initial_value", "3", 3),
        ("step_value", "11", 11),
        ("step_value", "eleven", 8),  # declared default
    ],
)
def test_9b_mistyped_demand_scalars_are_coerced_or_defaulted(
    field: str, given: Any, expected: Any
) -> None:
    demand = {"kind": "STEP", "initial_value": 4, "step_week": 5, "step_value": 8}
    demand[field] = given
    cfg = build(payload_with_demand(**demand))
    assert getattr(cfg.demand, field) == expected


@pytest.mark.parametrize(
    ("given", "expected"), [("50", 50), (50.0, 50), ("-5", None), ("lots", None)]
)
def test_9b_mistyped_optional_overrides_are_coerced_then_repaired(
    given: Any, expected: int | None
) -> None:
    assert (
        build(
            payload_with_visibility(max_order_quantity=given)
        ).visibility.max_order_quantity
        == expected
    )
    cfg = build(payload_for_role("FACTORY", production_capacity_per_week=given))
    assert cfg.factory_config().production_capacity_per_week == expected


@pytest.mark.parametrize(
    ("given", "expected"), [("0.4", 0.4), (1, 1.0), ("2", 1.0), ("hot", 0.25)]
)
def test_9b_mistyped_bot_scalars_are_coerced_then_clamped(
    given: Any, expected: float
) -> None:
    payload = base_payload()
    payload["bot"]["theta"] = given
    assert build(payload).bot.theta == expected


def test_9b_coercion_never_raises_for_any_scalar_field() -> None:
    """``§4``: "an unusable value ... never raises, because none of the nine
    rejections covers a mistyped scalar".

    ``currency_symbol`` is deliberately not in this sweep: it is the one scalar
    with a rejection rule of its own, and a garbage value that coerces to a
    valid-but-too-long *string* is rule 9's business rather than AC 9b's.  Its
    non-string inputs are covered by
    ``test_9b_a_mistyped_currency_symbol_falls_back_to_the_default``.
    """
    garbage = ["banana", "", [1, 2], {}, None, object(), True, -1, 10**9]
    for value in garbage:
        for key in ("duration_weeks", "random_seed", "preset_name"):
            assert isinstance(build(base_payload(**{key: value})), GameConfig)
        for field in (
            "initial_inventory",
            "shipping_delay_weeks",
            "fixed_order_cost",
            "starting_capital",
            "initial_order_in_pipeline",
        ):
            assert isinstance(
                build(payload_for_role("RETAILER", **{field: value})), GameConfig
            )
        payload = base_payload()
        payload["bot"]["beta"] = value
        assert isinstance(build(payload), GameConfig)


@pytest.mark.parametrize("given", [[1, 2], {}, None, True, -1, 10**9, 7.5])
def test_9b_a_mistyped_currency_symbol_falls_back_to_the_default(given: Any) -> None:
    assert build(base_payload(currency_symbol=given)).currency_symbol == "$"


def test_9b_a_coerced_config_still_round_trips() -> None:
    cfg = build(
        base_payload(duration_weeks="40", pause_on_disconnect="no", random_seed="77")
    )
    assert cfg.duration_weeks == 40
    assert cfg.pause_on_disconnect is False
    assert cfg.random_seed == 77
    assert GameConfig.from_payload(json.loads(json.dumps(cfg.to_payload()))) == cfg


# ---------------------------------------------------------------------------
# §3.1: the four remaining `.field` strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [None, [], "a config", 7, 3.5, object(), True])
def test_a_payload_that_is_not_a_mapping_reports_field_config(payload: Any) -> None:
    assert_rejected(payload, field="config")


def test_an_unknown_roles_key_is_skipped_and_rule_6_then_fires() -> None:
    """``§3.1``: a `roles` key that is not a ``Role`` name is **skipped**, so
    rule 6 fires for whichever member is now missing."""
    payload = base_payload()
    payload["roles"]["GOBLIN"] = payload["roles"].pop("WHOLESALER")
    assert_rejected(payload, field="roles")


def test_an_unknown_roles_key_alongside_all_four_members_is_simply_ignored() -> None:
    """The other half of the rule: skipping means skipping, not rejecting."""
    payload = base_payload()
    payload["roles"]["GOBLIN"] = {"initial_inventory": 99}
    payload["roles"]["retailer"] = {"initial_inventory": 98}
    payload["roles"][""] = {}

    cfg = build(payload)
    assert set(cfg.roles) == set(ROLE_ORDER)
    assert cfg.role_config(Role.RETAILER).initial_inventory == 12
    assert cfg == get_preset("CLASSIC_MIT")


@pytest.mark.parametrize(
    "values",
    ["4444", 4, None, {}, {"0": 4}, 7.5],
    ids=["str", "int", "none", "dict", "mapping", "float"],
)
def test_custom_values_that_is_not_a_list_reports_demand_values(values: Any) -> None:
    payload = base_payload(duration_weeks=8)
    payload["demand"] = {"kind": "CUSTOM", "values": values}
    assert_rejected(payload, field="demand.values")


def test_custom_values_missing_entirely_reports_demand_values() -> None:
    payload = base_payload(duration_weeks=8)
    payload["demand"] = {"kind": "CUSTOM"}
    assert_rejected(payload, field="demand.values")


@pytest.mark.parametrize(
    "bad",
    ["four", "", None, [], {}, object()],
    ids=["str", "empty", "none", "list", "dict", "object"],
)
def test_custom_values_containing_a_non_number_reports_demand_values(bad: Any) -> None:
    payload = base_payload(duration_weeks=8)
    values: list[Any] = [4] * 8
    values[5] = bad
    payload["demand"] = {"kind": "CUSTOM", "values": values}
    assert_rejected(payload, field="demand.values")


def build_custom(values: list[Any], weeks: int = 8) -> GameConfig:
    payload = base_payload(duration_weeks=weeks)
    payload["demand"] = {"kind": "CUSTOM", "values": values}
    return build(payload)


def test_custom_values_of_numbers_are_accepted_and_are_integers() -> None:
    """``§3.1`` rejects a `CUSTOM` series "that contains a non-number", so a
    series of numbers must be accepted -- and ``00-conventions.md §4`` requires
    quantities to be ``int`` everywhere, never a float."""
    cfg = build_custom([4, 4.0, 5.0, 4, 4, 4, 4, 4])
    assert cfg.demand.values == [4, 4, 5, 4, 4, 4, 4, 4]
    assert all(type(v) is int for v in cfg.demand.values)


def test_custom_values_read_numeric_strings_as_numbers() -> None:
    """``§3.1``: "A numeric string is a number here".

    ``beer-game-spec.md §6.4`` names paste and CSV upload as the input path for
    a custom series, so every value arrives as a string.  Rejecting ``"4"``
    would fail a host pasting ``4,4,8,8`` out of a spreadsheet, with a message
    telling them their values must be integers.
    """
    cfg = build_custom(["4", "4", "8", "8", "8", "8", "8", "8"])
    assert cfg.demand.values == [4, 4, 8, 8, 8, 8, 8, 8]
    assert all(type(v) is int for v in cfg.demand.values)


def test_custom_values_accept_a_mixed_series_of_spellings() -> None:
    cfg = build_custom(["4", 4, 4.0, "5", 6.0, "7", 8, "9"])
    assert cfg.demand.values == [4, 4, 4, 5, 6, 7, 8, 9]
    assert all(type(v) is int for v in cfg.demand.values)


@pytest.mark.parametrize("value", [4.7, "4.7", 4.2, "4.0", " 4 ", 4.999])
def test_custom_values_truncate_a_fractional_entry(value: Any) -> None:
    """``§3.1``: "``4.7`` truncates to ``4``", consistent with ``§4``'s
    coercion rule for scalars.

    Truncation applies only to an entry that has already cleared rule 2 -- see
    ``test_a_negative_custom_entry_rejects_however_it_is_spelled``.
    """
    cfg = build_custom([value] + [4] * 7)
    assert cfg.demand.values[0] == 4
    assert type(cfg.demand.values[0]) is int


def test_custom_values_read_a_bool_as_zero_or_one() -> None:
    """``§3.1``: "a bool reads as ``0``/``1``"."""
    cfg = build_custom([True, False, 4, 4, 4, 4, 4, 4])
    assert cfg.demand.values[:2] == [1, 0]
    assert all(type(v) is int for v in cfg.demand.values)


@pytest.mark.parametrize(
    "negative",
    [
        -1,
        "-1",
        -8,
        "-8",
        -2.0,
        "-2.0",
        -9_999,
        "-9999",
        -50_000,
        -0.5,
        "-0.5",
        -0.9,
        "-0.1",
        "-0.0001",
    ],
    ids=[
        "int",
        "int-str",
        "int-8",
        "int-str-8",
        "float",
        "float-str",
        "at-limit",
        "at-limit-str",
        "past-limit",
        "fraction-half",
        "fraction-half-str",
        "fraction-point-nine",
        "fraction-point-one-str",
        "fraction-tiny-str",
    ],
)
def test_a_negative_custom_entry_rejects_however_it_is_spelled(negative: Any) -> None:
    """``§3.1``: rule 2 is evaluated on the **parsed number, before
    truncation**.

    A negative entry is a negative entry however the wire spelled it, so it
    rejects rather than being clamped up to 0 or truncated toward zero into
    valid demand.  The fractional cases are the ones that matter: truncating
    ``-0.5`` toward zero would repair the host's value into something they did
    not ask for, which is the same failure the "rules 1 and 2 before clamping"
    ordering exists to prevent, one step further in.
    """
    assert_rejected_custom([negative] + [4] * 7, field="demand.values")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), ("0", 0), (0.0, 0), (0.4, 0), (0.9, 0), ("0.9", 0), ("0.0001", 0)],
)
def test_a_non_negative_fraction_below_one_truncates_to_zero(
    value: Any, expected: int
) -> None:
    """The other side of the same frontier: rule 2 rejects everything below
    ``0``, and everything in ``[0, 1)`` truncates to ``0`` and is kept.  Pinning
    both sides is what stops the boundary drifting."""
    cfg = build_custom([value] + [4] * 7)
    assert cfg.demand.values[0] == expected
    assert type(cfg.demand.values[0]) is int


def test_the_negative_frontier_is_exactly_zero() -> None:
    """Stated as one assertion so a future edit cannot move the boundary by
    half a unit without a test naming it."""
    assert build_custom([0] + [4] * 7).demand.values[0] == 0
    assert_rejected_custom([-0.0001] + [4] * 7, field="demand.values")


def test_a_numeric_string_entry_is_still_clamped() -> None:
    cfg = build_custom(["50000"] + [4] * 7)
    assert cfg.demand.values[0] == MAX_Q


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_custom_entry_reports_demand_values_unindexed(
    bad: float,
) -> None:
    """``§3.1``: rule 8 and the ``CUSTOM`` bullet overlap for a non-finite entry
    inside a series, and the ``CUSTOM`` bullet wins -- the field is
    ``demand.values``, **not** ``demand.values[3]``.  An index-bearing field
    string is not something a black-box test can predict.
    """
    err = assert_rejected_custom([4, 4, 4, bad, 4, 4, 4, 4], field="demand.values")
    assert "[" not in err.field
    assert "3" not in err.field


def test_custom_values_are_integers_after_a_round_trip() -> None:
    payload = base_payload(duration_weeks=8)
    payload["demand"] = {"kind": "CUSTOM", "values": [0, 1, 2, 3, 4, 5, 6, 7]}
    cfg = build(payload)
    dumped = cfg.to_payload()
    _assert_json_primitive(dumped, "payload")
    assert GameConfig.from_payload(json.loads(json.dumps(dumped))) == cfg


# ---------------------------------------------------------------------------
# §6 failure mode 8 -- EVERY mutating entry point on `roles` must raise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("setitem", lambda m: m.__setitem__(Role.RETAILER, RoleConfig())),
        ("delitem", lambda m: m.__delitem__(Role.RETAILER)),
        ("update", lambda m: m.update({Role.RETAILER: RoleConfig()})),
        ("pop", lambda m: m.pop(Role.RETAILER)),
        ("popitem", lambda m: m.popitem()),
        ("setdefault", lambda m: m.setdefault(Role.RETAILER, RoleConfig())),
        ("clear", lambda m: m.clear()),
    ],
)
def test_every_mutating_entry_point_on_the_roles_mapping_raises(
    name: str, mutate
) -> None:
    """``§6`` failure mode 8.

    "Every mutating entry point must raise, or the freeze is theatre -- a later
    edit swapping the mapping for a plain ``dict`` would otherwise pass every
    test while leaving a 'frozen' config writable."  A read-only mapping refuses
    ``__setitem__``/``__delitem__`` with ``TypeError`` and simply does not
    provide the named methods, so either exception type is a correct refusal;
    what matters is that the write does not land.
    """
    cfg = get_preset("CLASSIC_MIT")
    before = dict(cfg.roles)

    with pytest.raises((TypeError, AttributeError)):
        mutate(cfg.roles)

    assert dict(cfg.roles) == before
    assert set(cfg.roles) == set(ROLE_ORDER)
    assert cfg.role_config(Role.RETAILER).initial_inventory == 12
    assert cfg == get_preset("CLASSIC_MIT")


def test_the_roles_mapping_reads_like_a_mapping_while_refusing_writes() -> None:
    """``§2``: "An immutable mapping, NOT a plain dict".

    The guarantee is behavioural, not nominal -- a read-only ``dict`` subclass
    is a perfectly good immutable mapping -- so this asserts that every *read*
    still works while the writes above are refused.
    """
    cfg = get_preset("CLASSIC_MIT")
    assert isinstance(cfg.roles, Mapping)
    assert len(cfg.roles) == 4
    assert Role.RETAILER in cfg.roles
    assert cfg.roles[Role.RETAILER] == cfg.role_config(Role.RETAILER)
    assert cfg.roles.get(Role.FACTORY) == cfg.factory_config()
    assert sorted(cfg.roles, key=lambda r: r.index) == list(ROLE_ORDER)
    assert dict(cfg.roles) == {r: cfg.role_config(r) for r in ROLE_ORDER}


def test_a_hand_built_config_gets_the_same_immutable_mapping() -> None:
    """Immutability is a property of the model, not of the preset registry."""
    cfg = hand_built_config()
    with pytest.raises((TypeError, AttributeError)):
        cfg.roles[Role.RETAILER] = RoleConfig()
    with pytest.raises((TypeError, AttributeError)):
        cfg.roles.clear()
    assert cfg.role_config(Role.RETAILER).initial_inventory == 3


def test_a_round_tripped_config_gets_the_same_immutable_mapping() -> None:
    cfg = GameConfig.from_payload(get_preset("CLASSIC_MIT").to_payload())
    with pytest.raises((TypeError, AttributeError)):
        cfg.roles.clear()
    assert set(cfg.roles) == set(ROLE_ORDER)


def test_the_dict_passed_to_from_host_input_is_not_aliased_into_the_config() -> None:
    """A caller who keeps their input dict must not have a back door into the
    frozen config."""
    payload = base_payload()
    roles_in = payload["roles"]
    cfg = build(payload)

    roles_in["RETAILER"] = {"initial_inventory": 999}
    roles_in.pop("FACTORY", None)

    assert cfg.role_config(Role.RETAILER).initial_inventory == 12
    assert set(cfg.roles) == set(ROLE_ORDER)


# ---------------------------------------------------------------------------
# §5 AC 8c -- from_payload given a non-mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [[], [1, 2, 3], "a payload", "", 7, 0, 3.5, None, True, (1, 2), {1, 2}, object()],
    ids=[
        "empty-list",
        "list",
        "str",
        "empty-str",
        "int",
        "zero",
        "float",
        "none",
        "bool",
        "tuple",
        "set",
        "object",
    ],
)
def test_8c_from_payload_given_a_non_mapping_raises_validation_error(
    payload: Any,
) -> None:
    """``§5`` AC 8c.

    ``from_payload``'s input is the server's own serialised state, so a
    structural failure there is a bug rather than a user error -- and letting a
    non-mapping reach ``data.get(...)`` would turn it into an ``AttributeError``
    and a 500.
    """
    with pytest.raises(ValidationError) as excinfo:
        GameConfig.from_payload(payload)

    err = excinfo.value
    assert isinstance(err, ValueError)
    assert not isinstance(err, ConfigValidationError)
    assert not isinstance(err, AttributeError)
    assert not isinstance(err, TypeError)


def test_8c_from_host_input_and_from_payload_differ_on_the_same_bad_input() -> None:
    """The two entry points are deliberately asymmetric, and that asymmetry is
    the whole point of AC 8c: untrusted host input gets one catchable error,
    the server's own state gets pydantic's."""
    for payload in ([], "a payload", 7, None):
        with pytest.raises(ConfigValidationError) as host:
            GameConfig.from_host_input(payload, DEFAULT_LIMITS)
        assert host.value.field == "config"

        with pytest.raises(ValidationError) as stored:
            GameConfig.from_payload(payload)
        assert not isinstance(stored.value, ConfigValidationError)
