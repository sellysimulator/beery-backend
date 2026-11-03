"""What a draft config will play, for the host form (2026-11-03).

The host form used to carry its own copy of the demand generators (half-up
rounding, the ramp's `+ 1`, the seasonal `w - 1` offset). Two copies agree on
the day they are written. This module is the replacement: it answers "what
will this config play?" by calling **the functions the engine itself calls**
-- `generate_demand_series` and `GameConfig.order_arrival_lead_weeks` -- and
never a second implementation of either. `POST /api/v1/rooms/preview` serves
it.

Pure (`00-conventions.md` §4): no I/O, no clock, no global RNG.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config_models import GameConfig
from .demand import generate_demand_series, uses_seed
from .enums import ROLE_ORDER, Role

__all__ = ["EXAMPLE_SEED", "ConfigPreview", "preview_config"]

# The seed an example draw uses when the host left `random_seed` blank. Fixed,
# so the preview stays still while a host reads it rather than redrawing on
# every keystroke. A blank seed is chosen at random when the game starts
# (`11 §3.6`), so no seed here could show the series that will be played.
EXAMPLE_SEED = 0


@dataclass(frozen=True)
class ConfigPreview:
    """The figures the host form shows for a draft config."""

    # Weeks 1..duration_weeks of customer demand, index 0 = week 1.
    demand_series: list[int]
    # True when the series depends on a seed the server has not chosen yet
    # (`STOCHASTIC` with a blank `random_seed`): an example, not the game.
    demand_is_example: bool
    # Weeks from each role ordering to that order arriving, in `ROLE_ORDER`.
    lead_times: dict[Role, int]


def preview_config(config: GameConfig) -> ConfigPreview:
    """The demand series and lead times `config` will play.

    With a `random_seed` set, the series is exactly the one `GameEngine.start`
    generates, because it is the same call with the same seed. With none, a
    seed-free generator is still exact, and a `STOCHASTIC` one is drawn from
    `EXAMPLE_SEED` and flagged as an example.
    """
    seed = config.random_seed if config.random_seed is not None else EXAMPLE_SEED
    return ConfigPreview(
        demand_series=generate_demand_series(
            config.demand, config.duration_weeks, seed
        ),
        demand_is_example=config.random_seed is None and uses_seed(config.demand),
        lead_times={role: config.order_arrival_lead_weeks(role) for role in ROLE_ORDER},
    )
