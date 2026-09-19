"""The three host-selectable configuration presets (`03-game-config.md` §3.8).

Pure: no I/O, no clock, no RNG, and nothing imported from outside `app.core`.

Every preset carries its own key in `preset_name`, and every preset must survive
`GameConfig.from_host_input` unchanged — a preset its own validator would alter
is a bug.
"""

from __future__ import annotations

from .config_models import (
    FactoryConfig,
    GameConfig,
    RoleConfig,
    StepDemand,
    StochasticDemand,
    VisibilityConfig,
)
from .enums import Distribution, Role

__all__ = ["PRESETS", "get_preset", "preset_names"]


def _roles(base: RoleConfig, factory: FactoryConfig) -> dict[Role, RoleConfig]:
    """The same starting conditions for the three downstream roles."""
    return {
        Role.RETAILER: base,
        Role.WHOLESALER: base,
        Role.DISTRIBUTOR: base,
        Role.FACTORY: factory,
    }


# The textbook scenario: every default of beer-game-spec.md §6, stated in full.
_CLASSIC_ROLE = RoleConfig(
    initial_inventory=12,
    initial_backlog=0,
    shipping_delay_weeks=2,
    information_delay_weeks=2,
    initial_pipeline_quantity=4,
    initial_order_in_pipeline=4,
    holding_cost_per_unit_week=0.50,
    backlog_cost_per_unit_week=1.00,
    fixed_order_cost=0.00,
    unit_purchase_cost=0.00,
    starting_capital=0.00,
)
_CLASSIC_FACTORY = FactoryConfig(
    **_CLASSIC_ROLE.model_dump(),
    production_delay_weeks=2,
    production_capacity_per_week=None,
)

CLASSIC_MIT = GameConfig(
    duration_weeks=36,
    preset_name="CLASSIC_MIT",
    roles=_roles(_CLASSIC_ROLE, _CLASSIC_FACTORY),
    demand=StepDemand(initial_value=4, step_week=5, step_value=8),
    visibility=VisibilityConfig(),
)

FAST_GAME = GameConfig(
    duration_weeks=20,
    preset_name="FAST_GAME",
    roles=_roles(_CLASSIC_ROLE, _CLASSIC_FACTORY),
    demand=StepDemand(initial_value=4, step_week=5, step_value=8),
    visibility=VisibilityConfig(),
)

# Long delays, a noisy customer and no supply-line prompt: the oscillation is
# unavoidable, which is the lesson.
_CHAOS_ROLE = RoleConfig(
    initial_inventory=8,
    initial_backlog=0,
    shipping_delay_weeks=4,
    information_delay_weeks=3,
    initial_pipeline_quantity=4,
    initial_order_in_pipeline=4,
)
_CHAOS_FACTORY = FactoryConfig(
    **_CHAOS_ROLE.model_dump(),
    production_delay_weeks=4,
    production_capacity_per_week=20,
)

CHAOS = GameConfig(
    duration_weeks=52,
    preset_name="CHAOS",
    roles=_roles(_CHAOS_ROLE, _CHAOS_FACTORY),
    demand=StochasticDemand(
        distribution=Distribution.NORMAL,
        mean=8.0,
        stdev=4.0,
        min=0,
        max=25,
    ),
    visibility=VisibilityConfig(show_supply_line_prominently=False),
)

PRESETS: dict[str, GameConfig] = {
    "CLASSIC_MIT": CLASSIC_MIT,
    "FAST_GAME": FAST_GAME,
    "CHAOS": CHAOS,
}


def get_preset(name: str) -> GameConfig:
    """The preset stored under `name`.

    Raises `KeyError` for an unknown name.
    """
    return PRESETS[name]


def preset_names() -> list[str]:
    """The available preset keys, sorted alphabetically.

    Sorted rather than in insertion order so the host UI's picker is stable
    across processes.
    """
    return sorted(PRESETS)
