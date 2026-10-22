"""The hard ceilings of `00-decisions.md` §5, in a module of their own.

They used to live inside `config_models.py`. They are here so that a caller
needing one ceiling does not import the whole config model to get it;
`config_models` re-exports both names, so `from app.core.config_models import
DEFAULT_LIMITS` keeps working.

Pure: imports nothing but pydantic. The limits arrive in the domain as an
injected `Limits` rather than through `app.config`, which `app/core` may not
import (`00-conventions.md` §4).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["DEFAULT_LIMITS", "Limits"]


class Limits(BaseModel):
    """The hard ceilings from `00-decisions.md` §5, injected rather than imported,
    so `app/core` stays free of `app.config`."""

    model_config = ConfigDict(frozen=True)

    max_order_quantity: int = 9_999
    max_weeks: int = 104
    min_weeks: int = 8
    max_delay_weeks: int = 8
    min_delay_weeks: int = 1
    max_initial_quantity: int = 9_999
    max_unit_value: float = 1_000_000.0
    # `random_seed` is held to +/- this. It is the largest integer a browser
    # holds exactly, so the host reads back the seed the game really used, and
    # it sits well inside the `BIGINT` `games.rng_seed` column, which a seed
    # past 2**63 would overflow at the end-of-game write.
    max_random_seed: int = 2**53 - 1
    # The `INT` `demand_configs.period_weeks` column's ceiling. A period this
    # long is already flat over any game `max_weeks` allows; a longer one only
    # makes the end-of-game write fail.
    max_period_weeks: int = 2**31 - 1


DEFAULT_LIMITS: Limits = Limits()
