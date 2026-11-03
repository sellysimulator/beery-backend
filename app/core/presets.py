"""The three host-selectable configuration presets (`03-game-config.md` §3.8).

Pure: no I/O, no clock, no RNG, and nothing imported from outside `app.core`.

Every preset carries its own key in `preset_name`, and every preset must survive
`GameConfig.from_host_input` unchanged — a preset its own validator would alter
is a bug.

`scenario_summary` writes a preset card's figures out of the preset's own
config (2026-11-03). The card copy used to restate them by hand, once here and
once in the host form, and a number typed twice is a number that drifts.
"""

from __future__ import annotations

from collections.abc import Iterable

from .config_models import (
    ConstantDemand,
    CustomDemand,
    FactoryConfig,
    GameConfig,
    RampDemand,
    RoleConfig,
    SeasonalDemand,
    StepDemand,
    StochasticDemand,
    VisibilityConfig,
)
from .enums import ROLE_ORDER, Distribution, Role

__all__ = ["PRESETS", "get_preset", "preset_names", "scenario_summary"]


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


# --------------------------------------------------------------------------- #
# Card copy, written from the config
# --------------------------------------------------------------------------- #


def _number(value: float) -> str:
    """`4`, `0.5`: a figure as a host would write it."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _span(values: Iterable[int]) -> str:
    """`2` when every role agrees, `2–4` when they do not."""
    distinct = sorted(set(values))
    if len(distinct) == 1:
        return str(distinct[0])
    return f"{distinct[0]}–{distinct[-1]}"


def _delays(config: GameConfig) -> str:
    """The chain's delays. The Retailer's order delay is unused, so not read."""
    inbound = _span(config.inbound_delay_weeks(role) for role in ROLE_ORDER)
    ordering = _span(
        config.order_delay_weeks(role)
        for role in ROLE_ORDER
        if role is not Role.RETAILER
    )
    if inbound == ordering:
        return f"{inbound}-week delays"
    return f"{inbound}-week shipping and {ordering}-week order delays"


def _demand(config: GameConfig) -> str:
    demand = config.demand
    if isinstance(demand, ConstantDemand):
        return f"constant demand of {demand.value}"
    if isinstance(demand, StepDemand):
        return (
            f"demand steps from {demand.initial_value} to {demand.step_value} "
            f"at week {demand.step_week}"
        )
    if isinstance(demand, RampDemand):
        capped = "" if demand.cap is None else f", capped at {demand.cap}"
        return (
            f"demand ramps from {demand.initial_value} by "
            f"{_number(demand.slope_per_week)} a week from week {demand.start_week}"
            f"{capped}"
        )
    if isinstance(demand, SeasonalDemand):
        return (
            f"demand swings {demand.amplitude} either side of {demand.base} "
            f"every {demand.period_weeks} weeks"
        )
    if isinstance(demand, StochasticDemand):
        between = f"between {demand.min} and {demand.max}"
        if demand.distribution is Distribution.UNIFORM:
            return f"random demand {between}"
        if demand.distribution is Distribution.POISSON:
            return f"random demand around {_number(demand.mean)} (Poisson), {between}"
        return (
            f"random demand around {_number(demand.mean)} "
            f"(standard deviation {_number(demand.stdev)}), {between}"
        )
    if isinstance(demand, CustomDemand):
        return "a custom demand series"
    raise ValueError(f"Unknown demand {demand!r}.")


def _costs(config: GameConfig) -> str:
    """The carrying costs, per unit a week, when every role pays the same."""
    symbol = config.currency_symbol
    holding = {
        config.role_config(role).holding_cost_per_unit_week for role in ROLE_ORDER
    }
    backlog = {
        config.role_config(role).backlog_cost_per_unit_week for role in ROLE_ORDER
    }
    if len(holding) != 1 or len(backlog) != 1:
        return "costs that differ by role"
    return (
        f"{symbol}{holding.pop():.2f} holding and {symbol}{backlog.pop():.2f} "
        "backlog a unit a week"
    )


def scenario_summary(config: GameConfig) -> str:
    """One sentence of the figures a config plays, read off the config itself.

    `"36 weeks, 12 units on hand, 2-week delays, demand steps from 4 to 8 at
    week 5, $0.50 holding and $1.00 backlog a unit a week."` -- every number
    comes from `config`, so a preset card cannot describe a preset the engine
    does not play.
    """
    on_hand = _span(config.role_config(role).initial_inventory for role in ROLE_ORDER)
    facts = [
        f"{config.duration_weeks} weeks",
        f"{on_hand} units on hand",
        _delays(config),
        _demand(config),
        _costs(config),
    ]
    capacity = config.factory_config().production_capacity_per_week
    if capacity is not None:
        facts.append(f"production capped at {capacity} a week")
    if not config.visibility.show_supply_line_prominently:
        facts.append("no supply-line prompt")
    return ", ".join(facts) + "."
