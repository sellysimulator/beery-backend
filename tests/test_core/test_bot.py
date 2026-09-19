"""Black-box tests for section 08 -- ``app/core/bot.py``.

Covers ``08-bot-agent.md §5`` acceptance criteria 1-17 and ``§6`` failure
modes 1-10.

Driven entirely through the frozen public surface of ``§2`` plus the public
surfaces of sections 03 (``config_models``, ``presets``), 04/05 (folded into
``GameEngine``), 06 (``agents`` -- only to prove ``BotAgent`` is not a
``RoleAgent``) and 07 (``game_engine``, ``stats``). No private name and no log
message is asserted on.
"""

from __future__ import annotations

import ast
import itertools
import sys
from pathlib import Path
from typing import Any

import pytest

from app.core.agents import RoleAgent
from app.core.bot import BotAgent, BotMemory, bot_for
from app.core.config_models import (
    DEFAULT_LIMITS,
    ConstantDemand,
    DemandConfig,
    FactoryConfig,
    GameConfig,
    RoleConfig,
)
from app.core.enums import ROLE_ORDER, Role
from app.core.game_engine import GameEngine, GamePhase
from app.core.presets import get_preset
from app.core.stats import compute_stats

SEED = 20260919


# --------------------------------------------------------------------------
# Helpers -- built only from sections 03/07's public surface
# --------------------------------------------------------------------------


def make_config(
    role_kwargs: dict[str, Any] | None = None,
    factory_kwargs: dict[str, Any] | None = None,
    *,
    demand: DemandConfig | None = None,
    duration_weeks: int = 36,
    per_role: dict[Role, dict[str, Any]] | None = None,
    bot_kwargs: dict[str, Any] | None = None,
    visibility_kwargs: dict[str, Any] | None = None,
) -> GameConfig:
    """A ``GameConfig`` assembled entirely through ``from_host_input``."""
    base = dict(role_kwargs or {})
    factory_base = {**base, **dict(factory_kwargs or {})}
    roles: dict[str, Any] = {
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
    if bot_kwargs:
        payload["bot"] = bot_kwargs
    if visibility_kwargs:
        payload["visibility"] = visibility_kwargs
    return GameConfig.from_host_input(payload, DEFAULT_LIMITS)


def with_bot(config: GameConfig, **bot_fields: Any) -> GameConfig:
    """``config`` with its ``bot`` section overridden, through the public
    ``to_payload`` / ``from_host_input`` round trip only."""
    payload = config.to_payload()
    payload["bot"] = {**payload["bot"], **bot_fields}
    return GameConfig.from_host_input(payload, DEFAULT_LIMITS)


def climb_to_expected_demand(
    bot: BotAgent, target: float, theta: float, anchor: float
) -> None:
    """Reach ``expected_demand == target`` using two ``observe()`` calls only.

    The first call leaves the anchor untouched and records ``x``; the second
    applies the smoothing: ``theta * x + (1 - theta) * anchor == target``, so
    ``x = (target - (1 - theta) * anchor) / theta``. Used only to reach the
    §4 backlogged states, which the document requires be reached by calling
    ``observe()``/``decide()``, never by driving an engine.
    """
    x = (target - (1 - theta) * anchor) / theta
    assert x == int(
        x
    ), "the worked example must be reachable with an integer observation"
    bot.observe(int(x))
    bot.observe(0)  # second call's own value is irrelevant to decide()


# --------------------------------------------------------------------------
# AC 1 -- the five worked examples of §4 reproduce exactly
# --------------------------------------------------------------------------


def test_ac1_worked_example_week1() -> None:
    config = get_preset("CLASSIC_MIT")
    bot = bot_for(Role.WHOLESALER, config)
    assert bot.target_stock == pytest.approx(36.0)
    assert bot.memory.expected_demand == pytest.approx(4.0)

    bot.observe(4)
    assert bot.memory.expected_demand == pytest.approx(4.0)
    assert bot.memory.last_observed_demand == 4

    order = bot.decide(inventory=12, backlog=0, supply_line=8)
    assert order == 11


def test_ac1_worked_example_week2() -> None:
    config = get_preset("CLASSIC_MIT")
    bot = bot_for(Role.WHOLESALER, config)
    bot.observe(4)
    bot.decide(inventory=12, backlog=0, supply_line=8)  # week 1, discarded

    bot.observe(4)
    assert bot.memory.expected_demand == pytest.approx(4.0)

    order = bot.decide(inventory=12, backlog=0, supply_line=15)
    assert order == 10


def test_ac1_worked_example_backlogged_default_beta() -> None:
    config = get_preset("CLASSIC_MIT")
    bot = bot_for(Role.WHOLESALER, config)
    climb_to_expected_demand(bot, target=8.0, theta=config.bot.theta, anchor=4.0)
    assert bot.memory.expected_demand == pytest.approx(8.0)

    order = bot.decide(inventory=0, backlog=14, supply_line=22)
    assert order == 21


def test_ac1_worked_example_backlogged_beta_1() -> None:
    config = with_bot(get_preset("CLASSIC_MIT"), beta=1.0)
    bot = bot_for(Role.WHOLESALER, config)
    climb_to_expected_demand(bot, target=8.0, theta=config.bot.theta, anchor=4.0)

    order = bot.decide(inventory=0, backlog=14, supply_line=22)
    assert order == 16


def test_ac1_worked_example_half_up_rounding() -> None:
    """Also the failure mode 5 assertion: a raw of exactly 10.5 gives 11."""
    config = make_config(
        role_kwargs={"initial_order_in_pipeline": 0},
        bot_kwargs={"theta": 0.5, "alpha": 0.0},
    )
    bot = bot_for(Role.RETAILER, config)
    assert bot.memory.expected_demand == pytest.approx(0.0)

    bot.observe(21)
    bot.observe(0)
    assert bot.memory.expected_demand == pytest.approx(10.5)

    order = bot.decide(inventory=0, backlog=0, supply_line=0)
    assert order == 11


# --------------------------------------------------------------------------
# AC 2 -- BotAgent is not a RoleAgent
# --------------------------------------------------------------------------


def test_ac2_bot_agent_is_not_a_role_agent_and_holds_no_inventory() -> None:
    assert not issubclass(BotAgent, RoleAgent)
    bot = bot_for(Role.RETAILER, get_preset("CLASSIC_MIT"))
    assert not isinstance(bot, RoleAgent)
    assert not hasattr(bot, "inventory")
    assert not hasattr(bot, "backlog")


# --------------------------------------------------------------------------
# AC 3 -- initial expected_demand == initial_order_in_pipeline, per role
# --------------------------------------------------------------------------


def test_ac3_initial_expected_demand_is_the_preloaded_pipeline_per_role() -> None:
    config = make_config(
        per_role={
            Role.RETAILER: {"initial_order_in_pipeline": 1},
            Role.WHOLESALER: {"initial_order_in_pipeline": 2},
            Role.DISTRIBUTOR: {"initial_order_in_pipeline": 3},
            Role.FACTORY: {"initial_order_in_pipeline": 4},
        }
    )
    for role, expected in (
        (Role.RETAILER, 1.0),
        (Role.WHOLESALER, 2.0),
        (Role.DISTRIBUTOR, 3.0),
        (Role.FACTORY, 4.0),
    ):
        bot = bot_for(role, config)
        assert bot.memory.expected_demand == pytest.approx(expected)
        assert isinstance(bot.memory.expected_demand, float)
        assert bot.memory.last_observed_demand is None


# --------------------------------------------------------------------------
# AC 4 / AC 5 -- observe() semantics
# --------------------------------------------------------------------------


def test_ac4_first_observe_leaves_expected_demand_unchanged() -> None:
    config = get_preset("CLASSIC_MIT")
    bot = bot_for(Role.RETAILER, config)
    initial = bot.memory.expected_demand

    bot.observe(999)

    assert bot.memory.expected_demand == pytest.approx(initial)
    assert bot.memory.last_observed_demand == 999


def test_ac5_later_observe_smooths_against_the_previous_observation() -> None:
    config = make_config(bot_kwargs={"theta": 0.4})
    bot = bot_for(Role.RETAILER, config)
    initial = bot.memory.expected_demand

    bot.observe(10)  # first call: no smoothing yet
    bot.observe(999)  # second call: smooths against 10, NOT 999

    expected = 0.4 * 10 + 0.6 * initial
    assert bot.memory.expected_demand == pytest.approx(expected)
    assert bot.memory.last_observed_demand == 999


# --------------------------------------------------------------------------
# AC 6 -- decide() is pure
# --------------------------------------------------------------------------


def test_ac6_decide_is_pure_and_does_not_mutate_memory() -> None:
    config = get_preset("CLASSIC_MIT")
    bot = bot_for(Role.WHOLESALER, config)
    bot.observe(4)

    before = (bot.memory.expected_demand, bot.memory.last_observed_demand)
    first = bot.decide(inventory=12, backlog=0, supply_line=8)
    middle = (bot.memory.expected_demand, bot.memory.last_observed_demand)
    second = bot.decide(inventory=12, backlog=0, supply_line=8)
    after = (bot.memory.expected_demand, bot.memory.last_observed_demand)

    assert first == second
    assert before == middle == after


# --------------------------------------------------------------------------
# AC 7 -- target_stock is per role
# --------------------------------------------------------------------------


def test_ac7_target_stock_is_derived_per_role() -> None:
    config = make_config(
        per_role={
            Role.RETAILER: {"initial_inventory": 10},
            Role.WHOLESALER: {"initial_inventory": 20},
            Role.DISTRIBUTOR: {"initial_inventory": 30},
            Role.FACTORY: {"initial_inventory": 40},
        },
        bot_kwargs={"target_stock_multiplier": 2.0},
    )
    targets = {role: bot_for(role, config).target_stock for role in ROLE_ORDER}

    assert targets[Role.RETAILER] == pytest.approx(20.0)
    assert targets[Role.WHOLESALER] == pytest.approx(40.0)
    assert targets[Role.DISTRIBUTOR] == pytest.approx(60.0)
    assert targets[Role.FACTORY] == pytest.approx(80.0)
    assert len(set(targets.values())) == 4


# --------------------------------------------------------------------------
# AC 8 / AC 9 -- theta extremes
# --------------------------------------------------------------------------


def test_ac8_theta_1_tracks_the_previous_observation_exactly() -> None:
    config = make_config(bot_kwargs={"theta": 1.0})
    bot = bot_for(Role.RETAILER, config)

    bot.observe(7)
    bot.observe(20)
    assert bot.memory.expected_demand == pytest.approx(7.0)

    bot.observe(3)
    assert bot.memory.expected_demand == pytest.approx(20.0)


def test_ac9_theta_0_freezes_expected_demand_forever() -> None:
    config = make_config(bot_kwargs={"theta": 0.0})
    bot = bot_for(Role.RETAILER, config)
    initial = bot.memory.expected_demand

    for value in (10, 200, 0, 5000, 1):
        bot.observe(value)
        assert bot.memory.expected_demand == pytest.approx(initial)


# --------------------------------------------------------------------------
# AC 10 -- alpha 0 ignores stock position entirely
# --------------------------------------------------------------------------


def test_ac10_alpha_0_orders_exactly_half_up_expected_demand() -> None:
    config = make_config(bot_kwargs={"alpha": 0.0})
    bot = bot_for(Role.WHOLESALER, config)
    bot.observe(11)  # first call: expected_demand stays at initial (4.0)

    expected_order = 4  # half_up_round(4.0)
    for inventory, backlog, supply_line in (
        (12, 0, 8),
        (0, 14, 22),
        (200, 0, 100),
        (0, 0, 0),
    ):
        order = bot.decide(
            inventory=inventory, backlog=backlog, supply_line=supply_line
        )
        assert order == expected_order


# --------------------------------------------------------------------------
# AC 11 -- beta damps the supply-line panic, pinned to the §4 backlogged state
# --------------------------------------------------------------------------


def test_ac11_beta_1_is_strictly_lower_than_beta_default_for_the_backlogged_state() -> (
    None
):
    base = get_preset("CLASSIC_MIT")

    bot_lo = bot_for(Role.WHOLESALER, base)
    climb_to_expected_demand(bot_lo, target=8.0, theta=base.bot.theta, anchor=4.0)
    order_beta_25 = bot_lo.decide(inventory=0, backlog=14, supply_line=22)

    hi_config = with_bot(base, beta=1.0)
    bot_hi = bot_for(Role.WHOLESALER, hi_config)
    climb_to_expected_demand(bot_hi, target=8.0, theta=hi_config.bot.theta, anchor=4.0)
    order_beta_1 = bot_hi.decide(inventory=0, backlog=14, supply_line=22)

    assert order_beta_25 == 21
    assert order_beta_1 == 16
    assert order_beta_1 < order_beta_25


# --------------------------------------------------------------------------
# AC 12 -- output is always int and always >= 0
# --------------------------------------------------------------------------


def test_ac12_output_is_always_a_nonnegative_int() -> None:
    config = get_preset("CLASSIC_MIT")
    bot = bot_for(Role.WHOLESALER, config)
    bot.observe(4)

    order = bot.decide(inventory=12, backlog=0, supply_line=8)
    assert isinstance(order, int) and not isinstance(order, bool)
    assert order >= 0

    surplus_order = bot.decide(inventory=200, backlog=0, supply_line=100)
    assert isinstance(surplus_order, int) and not isinstance(surplus_order, bool)
    assert surplus_order == 0


# --------------------------------------------------------------------------
# AC 13 -- payload round trip
# --------------------------------------------------------------------------


def test_ac13_from_payload_of_to_payload_equals_the_original() -> None:
    config = get_preset("CLASSIC_MIT")
    bot = bot_for(Role.WHOLESALER, config)
    bot.observe(4)
    bot.decide(inventory=12, backlog=0, supply_line=8)
    bot.observe(4)

    payload = bot.to_payload()
    assert set(payload) == {"role", "memory"}
    assert payload["role"] == Role.WHOLESALER.value

    restored = BotAgent.from_payload(payload, config)
    assert restored == bot
    assert restored is not bot


def test_ac13_declares_value_equality_so_the_round_trip_means_something() -> None:
    """Guards against a vacuous __eq__: two bots with different memory must
    compare unequal, or the round-trip criterion asserts nothing."""
    config = get_preset("CLASSIC_MIT")
    a = bot_for(Role.WHOLESALER, config)
    b = bot_for(Role.WHOLESALER, config)
    assert a == b

    b.observe(4)
    b.observe(999)
    assert a != b


# --------------------------------------------------------------------------
# AC 14, 15, 16 -- a real 36-week all-bot game against the gated GameEngine
# --------------------------------------------------------------------------


def run_all_bot_game(config: GameConfig, seed: int) -> GameEngine:
    """Exactly the §3.4 driving loop: observe() then decide(), once each, per
    week, for every bot role, as soon as the decision window opens."""
    bots = {role: bot_for(role, config) for role in ROLE_ORDER}
    engine = GameEngine.start(config, seed, bot_roles=frozenset(ROLE_ORDER))
    while engine.phase is GamePhase.DECISION:
        for role in ROLE_ORDER:
            view = engine.player_view(role)
            bots[role].observe(view["incoming_order"])
            qty = bots[role].decide(
                inventory=view["inventory"],
                backlog=view["backlog"],
                supply_line=view["supply_line"],
            )
            engine.submit_order(role, qty)
        engine.close_week()
    return engine


def test_ac14_a_full_36_week_all_bot_game_completes_without_violating_invariants() -> (
    None
):
    config = get_preset("CLASSIC_MIT")
    engine = run_all_bot_game(config, SEED)

    assert engine.phase is GamePhase.FINISHED
    assert engine.weeks_played == 36

    # 06-role-agents.md §3.7's invariants, enforced by RoleAgent._check_invariants
    # on every settle()/record_order() and re-asserted here from the WeekRecords:
    # stock never negative, and inventory/backlog are never both held at once.
    for record in engine.history:
        assert record.closing_inventory >= 0
        assert record.closing_backlog >= 0
        assert record.closing_inventory * record.closing_backlog == 0

    # Invariant 6: accumulated cost is monotonically non-decreasing per role.
    for role in ROLE_ORDER:
        costs = [r.cumulative_cost for r in engine.history if r.role is role]
        assert all(x <= y + 1e-9 for x, y in itertools.pairwise(costs))


def test_ac15_the_bot_reproduces_bullwhip_amplification() -> None:
    config = get_preset("CLASSIC_MIT")
    assert config.bot.beta == pytest.approx(0.25)
    engine = run_all_bot_game(config, SEED)

    stats = compute_stats(engine.history, engine.demand_series, engine.weeks_played)
    factory_ratio = stats.per_role[Role.FACTORY].bullwhip_ratio
    retailer_ratio = stats.per_role[Role.RETAILER].bullwhip_ratio

    assert factory_ratio is not None
    assert retailer_ratio is not None
    assert factory_ratio > retailer_ratio


def test_ac16_beta_1_damps_the_bullwhip_ratio_for_every_role() -> None:
    low_beta_config = get_preset("CLASSIC_MIT")
    high_beta_config = with_bot(low_beta_config, beta=1.0)

    low_beta_engine = run_all_bot_game(low_beta_config, SEED)
    high_beta_engine = run_all_bot_game(high_beta_config, SEED)

    low_stats = compute_stats(
        low_beta_engine.history,
        low_beta_engine.demand_series,
        low_beta_engine.weeks_played,
    )
    high_stats = compute_stats(
        high_beta_engine.history,
        high_beta_engine.demand_series,
        high_beta_engine.weeks_played,
    )

    for role in ROLE_ORDER:
        low_ratio = low_stats.per_role[role].bullwhip_ratio
        high_ratio = high_stats.per_role[role].bullwhip_ratio
        assert low_ratio is not None
        assert high_ratio is not None
        assert high_ratio < low_ratio


# --------------------------------------------------------------------------
# AC 17 -- app/core/bot.py imports only app.core and the stdlib, never random
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


def test_ac17_bot_module_imports_only_app_core_and_the_stdlib_never_random() -> None:
    path = Path(__file__).resolve().parents[2] / "app" / "core" / "bot.py"
    assert path.is_file(), path

    names = imported_modules(path, "app.core")
    assert "random" not in names, "bot.py must never import random"
    for name in names:
        root = name.split(".")[0]
        assert (
            name == "app.core"
            or name.startswith("app.core.")
            or (root in sys.stdlib_module_names)
        ), f"bot.py imports {name}"
        assert name != "app.core.checks"
        assert not name.startswith("app.core.checks.")
        assert name != "app.core.firebase"
        assert root != "random"


# --------------------------------------------------------------------------
# Failure mode 1 -- using this week's demand in this week's anchor
# --------------------------------------------------------------------------


def test_fm1_the_lag_is_not_lost_observations_4_4_4_12() -> None:
    config = make_config(bot_kwargs={"theta": 1.0})
    bot = bot_for(Role.WHOLESALER, config)

    bot.observe(4)
    bot.observe(4)
    bot.observe(4)
    bot.observe(12)
    assert bot.memory.expected_demand == pytest.approx(4.0)

    bot.observe(1)  # the fifth observation
    assert bot.memory.expected_demand == pytest.approx(12.0)


# --------------------------------------------------------------------------
# Failure mode 2 -- sign error on backlog
# --------------------------------------------------------------------------


def test_fm2_backlog_increases_the_order() -> None:
    config = get_preset("CLASSIC_MIT")

    bot_no_backlog = bot_for(Role.WHOLESALER, config)
    order_no_backlog = bot_no_backlog.decide(inventory=12, backlog=0, supply_line=8)

    bot_with_backlog = bot_for(Role.WHOLESALER, config)
    order_with_backlog = bot_with_backlog.decide(
        inventory=12, backlog=14, supply_line=8
    )

    assert order_no_backlog == 11
    assert order_with_backlog == 15
    assert order_with_backlog > order_no_backlog


# --------------------------------------------------------------------------
# Failure mode 3 -- supply line ignored entirely
# --------------------------------------------------------------------------


def test_fm3_beta_0_and_beta_1_give_different_answers_on_the_backlogged_state() -> None:
    base = get_preset("CLASSIC_MIT")

    beta_0_config = with_bot(base, beta=0.0)
    bot_0 = bot_for(Role.WHOLESALER, beta_0_config)
    climb_to_expected_demand(
        bot_0, target=8.0, theta=beta_0_config.bot.theta, anchor=4.0
    )
    order_beta_0 = bot_0.decide(inventory=0, backlog=14, supply_line=22)

    beta_1_config = with_bot(base, beta=1.0)
    bot_1 = bot_for(Role.WHOLESALER, beta_1_config)
    climb_to_expected_demand(
        bot_1, target=8.0, theta=beta_1_config.bot.theta, anchor=4.0
    )
    order_beta_1 = bot_1.decide(inventory=0, backlog=14, supply_line=22)

    assert order_beta_0 == 23
    assert order_beta_1 == 16
    assert order_beta_0 != order_beta_1


# --------------------------------------------------------------------------
# Failure mode 4 -- negative orders floor at 0
# --------------------------------------------------------------------------


def test_fm4_deep_surplus_floors_at_0() -> None:
    config = get_preset("CLASSIC_MIT")
    bot = bot_for(Role.WHOLESALER, config)

    order = bot.decide(inventory=200, backlog=0, supply_line=100)
    assert order == 0


# --------------------------------------------------------------------------
# Failure mode 5 -- banker's rounding
# --------------------------------------------------------------------------


def test_fm5_a_raw_of_exactly_10_5_rounds_to_11_not_10() -> None:
    config = make_config(
        role_kwargs={"initial_order_in_pipeline": 0},
        bot_kwargs={"theta": 0.5, "alpha": 0.0},
    )
    bot = bot_for(Role.RETAILER, config)
    bot.observe(21)
    bot.observe(0)
    assert bot.memory.expected_demand == pytest.approx(10.5)

    order = bot.decide(inventory=0, backlog=0, supply_line=0)
    # Python's round(10.5) is 10 (round-half-to-even); half-up must give 11.
    assert round(10.5) == 10
    assert order == 11


# --------------------------------------------------------------------------
# Failure mode 6 -- shared memory between bots
# --------------------------------------------------------------------------


def test_fm6_four_bots_from_one_config_do_not_share_memory() -> None:
    config = get_preset("CLASSIC_MIT")
    bots = {role: bot_for(role, config) for role in ROLE_ORDER}

    bots[Role.RETAILER].observe(0)
    bots[Role.RETAILER].observe(0)
    bots[Role.WHOLESALER].observe(40)
    bots[Role.WHOLESALER].observe(0)
    bots[Role.DISTRIBUTOR].observe(100)
    bots[Role.DISTRIBUTOR].observe(0)
    bots[Role.FACTORY].observe(4)
    bots[Role.FACTORY].observe(4)

    values = {role: bots[role].memory.expected_demand for role in ROLE_ORDER}
    assert len({round(v, 6) for v in values.values()}) == 4


# --------------------------------------------------------------------------
# Failure mode 7 -- global RNG use
# --------------------------------------------------------------------------


def test_fm7_two_bots_given_identical_inputs_agree_and_no_random_is_used() -> None:
    config = get_preset("CLASSIC_MIT")
    bot_a = bot_for(Role.WHOLESALER, config)
    bot_b = bot_for(Role.WHOLESALER, config)

    for demand in (4, 6, 9, 3):
        bot_a.observe(demand)
        bot_b.observe(demand)

    order_a = bot_a.decide(inventory=10, backlog=2, supply_line=9)
    order_b = bot_b.decide(inventory=10, backlog=2, supply_line=9)

    assert order_a == order_b
    assert bot_a == bot_b

    path = Path(__file__).resolve().parents[2] / "app" / "core" / "bot.py"
    assert "random" not in imported_modules(path, "app.core")


# --------------------------------------------------------------------------
# Failure mode 8 -- float accumulation, approached from below
# --------------------------------------------------------------------------


def test_fm8_observing_12_repeatedly_from_an_anchor_of_4_converges_without_overshoot() -> (
    None
):
    config = make_config(role_kwargs={"initial_order_in_pipeline": 4})
    bot = bot_for(Role.RETAILER, config)
    assert bot.memory.expected_demand == pytest.approx(4.0)

    for _ in range(104):
        bot.observe(12)
        assert bot.memory.expected_demand <= 12.0 + 1e-9

    assert abs(bot.memory.expected_demand - 12.0) < 1e-9


# --------------------------------------------------------------------------
# Failure mode 9 -- max_order_quantity must not be applied twice
# --------------------------------------------------------------------------


def test_fm9_decide_does_not_clamp_to_max_order_quantity() -> None:
    config = make_config(
        role_kwargs={"initial_order_in_pipeline": 0, "initial_inventory": 10},
        bot_kwargs={"alpha": 1.0, "beta": 0.25, "target_stock_multiplier": 3.0},
        visibility_kwargs={"max_order_quantity": 5},
    )
    bot = bot_for(Role.RETAILER, config)
    assert bot.target_stock == pytest.approx(30.0)

    order = bot.decide(inventory=0, backlog=0, supply_line=0)
    assert order == 30  # NOT clamped to 5; that is GameEngine.submit_order's job


# --------------------------------------------------------------------------
# Failure mode 10 -- the Retailer bot still anchors on the preloaded pipeline
# --------------------------------------------------------------------------


def test_fm10_retailer_bot_anchors_on_initial_order_in_pipeline_not_zero() -> None:
    config = make_config(per_role={Role.RETAILER: {"initial_order_in_pipeline": 7}})
    bot = bot_for(Role.RETAILER, config)

    assert bot.memory.expected_demand == pytest.approx(7.0)
    order = bot.decide(inventory=12, backlog=0, supply_line=0)
    assert isinstance(order, int)


# --------------------------------------------------------------------------
# bot_for() sanity
# --------------------------------------------------------------------------


def test_bot_for_returns_a_bot_agent_bound_to_its_role() -> None:
    config = get_preset("CLASSIC_MIT")
    for role in ROLE_ORDER:
        bot = bot_for(role, config)
        assert isinstance(bot, BotAgent)
        assert isinstance(bot.memory, BotMemory)
        assert bot.role is role
