"""Black-box tests for section 03 -- ``app/core/presets.py``.

Covers ``§3.8``, ``§5`` items 11, 12 and 13, and the ``§4`` worked example that
starts from ``get_preset("CLASSIC_MIT")``.

``CLASSIC_MIT`` is checked field for field against ``beer-game-spec.md §6``
(``§6.1`` game-level, ``§6.2`` per-role starting conditions, ``§6.3`` costs,
``§6.4`` demand pattern, ``§6.5`` visibility), as amended by ``00-decisions.md``
``D6`` (no timer), ``D14`` (no chat) and ``D5``.
"""

from __future__ import annotations

import json

import pytest

from app.core.config_models import (
    DEFAULT_LIMITS,
    BotConfig,
    FactoryConfig,
    GameConfig,
    RoleConfig,
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
)
from app.core.presets import PRESETS, get_preset, preset_names

# §2: ``preset_names()`` is sorted alphabetically.
EXPECTED_NAMES = sorted(["CLASSIC_MIT", "FAST_GAME", "CHAOS"])


# ---------------------------------------------------------------------------
# The registry -- AC 11, AC 13
# ---------------------------------------------------------------------------


def test_the_three_presets_exist() -> None:
    assert sorted(PRESETS) == EXPECTED_NAMES


def test_preset_names_is_sorted_alphabetically() -> None:
    """``§2``: sorted, "so the host UI's preset picker has a stable order across
    processes rather than one that depends on dict insertion"."""
    names = preset_names()
    assert isinstance(names, list)
    assert names == EXPECTED_NAMES  # EXPECTED_NAMES is in sorted order
    assert names == sorted(names)


def test_preset_names_lists_exactly_the_registry_keys() -> None:
    assert sorted(preset_names()) == sorted(PRESETS)


def test_preset_names_is_stable_across_calls() -> None:
    assert preset_names() == preset_names()


def test_preset_names_returns_a_fresh_list_each_call() -> None:
    """A caller that sorts or appends in place must not corrupt the registry."""
    names = preset_names()
    names.append("NOT_A_PRESET")
    assert preset_names() == EXPECTED_NAMES


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_get_preset_returns_the_registered_game_config(name: str) -> None:
    cfg = get_preset(name)
    assert isinstance(cfg, GameConfig)
    assert cfg == PRESETS[name]


def test_get_preset_raises_key_error_for_an_unknown_name() -> None:
    with pytest.raises(KeyError):
        get_preset("NOPE")


@pytest.mark.parametrize("name", ["", "classic_mit", "Classic MIT", "CLASSIC"])
def test_get_preset_raises_key_error_for_other_unknown_names(name: str) -> None:
    with pytest.raises(KeyError):
        get_preset(name)


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_every_preset_carries_its_own_key_in_preset_name(name: str) -> None:
    assert get_preset(name).preset_name == name


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_every_preset_survives_from_host_input_unchanged(name: str) -> None:
    """``§3.8``: a preset that its own validator would alter is a bug."""
    cfg = get_preset(name)
    assert GameConfig.from_host_input(cfg.to_payload(), DEFAULT_LIMITS) == cfg


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_every_preset_round_trips_through_its_payload(name: str) -> None:
    cfg = get_preset(name)
    assert GameConfig.from_payload(cfg.to_payload()) == cfg
    assert GameConfig.from_payload(json.loads(json.dumps(cfg.to_payload()))) == cfg


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_every_preset_defines_all_four_roles(name: str) -> None:
    cfg = get_preset(name)
    assert set(cfg.roles) == set(ROLE_ORDER)
    assert isinstance(cfg.roles[Role.FACTORY], FactoryConfig)


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_every_preset_has_a_playable_duration_and_delays(name: str) -> None:
    cfg = get_preset(name)
    assert DEFAULT_LIMITS.min_weeks <= cfg.duration_weeks <= DEFAULT_LIMITS.max_weeks
    for role in ROLE_ORDER:
        assert (
            DEFAULT_LIMITS.min_delay_weeks
            <= cfg.inbound_delay_weeks(role)
            <= DEFAULT_LIMITS.max_delay_weeks
        )
        assert (
            DEFAULT_LIMITS.min_delay_weeks
            <= cfg.order_delay_weeks(role)
            <= DEFAULT_LIMITS.max_delay_weeks
        )


# ---------------------------------------------------------------------------
# CLASSIC_MIT versus beer-game-spec.md §6 -- AC 12
# ---------------------------------------------------------------------------


def test_classic_mit_game_level_defaults() -> None:
    """``beer-game-spec.md §6.1``, as amended by ``D6`` and ``D14``."""
    cfg = get_preset("CLASSIC_MIT")
    assert cfg.duration_weeks == 36
    assert cfg.stage_count == 4
    assert cfg.pause_on_disconnect is True
    assert cfg.bot_fill_empty_roles is False
    assert cfg.random_seed is None
    assert cfg.currency_symbol == "$"  # §6.3, display setting
    assert cfg.role_assignment_mode == RoleAssignmentMode.HOST_ASSIGNS


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_classic_mit_per_role_starting_conditions(role: Role) -> None:
    """``beer-game-spec.md §6.2`` and ``03-game-config.md §3.8``."""
    rc = get_preset("CLASSIC_MIT").role_config(role)
    assert rc.initial_inventory == 12
    assert rc.initial_backlog == 0
    assert rc.shipping_delay_weeks == 2
    assert rc.information_delay_weeks == 2
    assert rc.initial_pipeline_quantity == 4
    assert rc.initial_order_in_pipeline == 4


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_classic_mit_per_role_costs(role: Role) -> None:
    """``beer-game-spec.md §6.3``."""
    rc = get_preset("CLASSIC_MIT").role_config(role)
    assert rc.holding_cost_per_unit_week == 0.50
    assert rc.backlog_cost_per_unit_week == 1.00
    assert rc.fixed_order_cost == 0.00
    assert rc.unit_purchase_cost == 0.00
    assert rc.starting_capital == 0.00


def test_classic_mit_factory_production() -> None:
    """``beer-game-spec.md §6.2``, factory block."""
    factory = get_preset("CLASSIC_MIT").factory_config()
    assert isinstance(factory, FactoryConfig)
    assert factory.production_delay_weeks == 2
    assert factory.production_capacity_per_week is None


def test_classic_mit_demand_is_the_classic_step() -> None:
    """``beer-game-spec.md §6.4``: the classic one-time jump."""
    demand = get_preset("CLASSIC_MIT").demand
    assert isinstance(demand, StepDemand)
    assert demand.kind == DemandKind.STEP
    assert demand.initial_value == 4
    assert demand.step_week == 5
    assert demand.step_value == 8


def test_classic_mit_visibility_defaults() -> None:
    """``beer-game-spec.md §6.5``."""
    vis = get_preset("CLASSIC_MIT").visibility
    assert vis.show_true_customer_demand_to_all is False
    assert vis.show_neighbour_inventory is False
    assert vis.show_all_inventories is False
    assert vis.show_supply_line_prominently is True
    assert vis.show_running_cost_to_players is True
    assert vis.show_leaderboard_during_game is False
    assert vis.max_order_quantity is None
    assert vis.allow_negative_orders is False
    assert vis == VisibilityConfig()


def test_classic_mit_is_every_model_default_made_explicit() -> None:
    """``§3.8``: "Every default above, explicitly"."""
    cfg = get_preset("CLASSIC_MIT")
    for role in (Role.RETAILER, Role.WHOLESALER, Role.DISTRIBUTOR):
        assert cfg.role_config(role) == RoleConfig()
    assert cfg.factory_config() == FactoryConfig()
    assert cfg.visibility == VisibilityConfig()
    assert cfg.bot == BotConfig()
    assert cfg.demand == StepDemand()


# ---------------------------------------------------------------------------
# FAST_GAME -- §3.8
# ---------------------------------------------------------------------------


def test_fast_game_is_classic_mit_with_twenty_weeks() -> None:
    fast = get_preset("FAST_GAME")
    classic = get_preset("CLASSIC_MIT")

    assert fast.duration_weeks == 20
    assert fast.preset_name == "FAST_GAME"
    assert (
        classic.model_copy(update={"duration_weeks": 20, "preset_name": "FAST_GAME"})
        == fast
    )


def test_fast_game_step_week_is_still_inside_its_shorter_game() -> None:
    fast = get_preset("FAST_GAME")
    assert isinstance(fast.demand, StepDemand)
    assert 2 <= fast.demand.step_week <= fast.duration_weeks


# ---------------------------------------------------------------------------
# CHAOS -- §3.8
# ---------------------------------------------------------------------------


def test_chaos_duration() -> None:
    assert get_preset("CHAOS").duration_weeks == 52


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_chaos_per_role_starting_conditions(role: Role) -> None:
    rc = get_preset("CHAOS").role_config(role)
    assert rc.shipping_delay_weeks == 4
    assert rc.information_delay_weeks == 3
    assert rc.initial_inventory == 8
    assert rc.initial_pipeline_quantity == 4
    assert rc.initial_order_in_pipeline == 4


@pytest.mark.parametrize("role", list(ROLE_ORDER))
def test_chaos_keeps_the_default_costs(role: Role) -> None:
    """``§3.8`` names no cost override for CHAOS, so the defaults stand."""
    rc = get_preset("CHAOS").role_config(role)
    assert rc.holding_cost_per_unit_week == 0.50
    assert rc.backlog_cost_per_unit_week == 1.00
    assert rc.fixed_order_cost == 0.00
    assert rc.unit_purchase_cost == 0.00
    assert rc.starting_capital == 0.00
    assert rc.initial_backlog == 0


def test_chaos_factory_production() -> None:
    factory = get_preset("CHAOS").factory_config()
    assert isinstance(factory, FactoryConfig)
    assert factory.production_delay_weeks == 4
    assert factory.production_capacity_per_week == 20


def test_chaos_demand_is_stochastic_normal() -> None:
    demand = get_preset("CHAOS").demand
    assert isinstance(demand, StochasticDemand)
    assert demand.kind == DemandKind.STOCHASTIC
    assert demand.distribution == Distribution.NORMAL
    assert demand.mean == 8
    assert demand.stdev == 4
    assert demand.min == 0
    assert demand.max == 25
    assert demand.min <= demand.max


def test_chaos_hides_the_supply_line() -> None:
    vis = get_preset("CHAOS").visibility
    assert vis.show_supply_line_prominently is False
    # §3.8 names no other visibility change.
    assert vis == VisibilityConfig(show_supply_line_prominently=False)


def test_chaos_delays_use_the_factory_production_special_case() -> None:
    cfg = get_preset("CHAOS")
    assert cfg.inbound_delay_weeks(Role.RETAILER) == 4
    assert cfg.inbound_delay_weeks(Role.FACTORY) == 4  # production_delay_weeks
    assert cfg.order_delay_weeks(Role.FACTORY) == 3  # information_delay_weeks


# ---------------------------------------------------------------------------
# The presets are immutable value objects like any other config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_a_preset_cannot_be_mutated_through_get_preset(name: str) -> None:
    cfg = get_preset(name)
    before = cfg.duration_weeks
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError
        cfg.duration_weeks = 99
    assert get_preset(name).duration_weeks == before
