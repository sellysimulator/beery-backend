"""The hard ceilings of `00-decisions.md` §5, in a module of their own.

They used to live inside `config_models.py`. They are here so that a caller
needing one ceiling does not import the whole config model to get it;
`config_models` re-exports both names, so `from app.core.config_models import
DEFAULT_LIMITS` keeps working.

**This is the one copy of every range a host or player can type into.** The
config validator refuses a value outside them, `GET /api/v1/rooms/limits`
serves them, and the host form builds its checks from that response -- the
frontend keeps no hand-copied number (2026-11-02). Add a bound here, not in a
form.

Pure: imports nothing but pydantic and the standard library. The limits arrive
in the domain as an injected `Limits` rather than through `app.config`, which
`app/core` may not import (`00-conventions.md` §4).
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict

__all__ = ["DEFAULT_LIMITS", "Limits"]


class Limits(BaseModel):
    """The hard ceilings from `00-decisions.md` §5, injected rather than imported,
    so `app/core` stays free of `app.config`."""

    model_config = ConfigDict(frozen=True)

    # Orders, demand quantities and both optional caps (`max_order_quantity`,
    # `production_capacity_per_week`). An order is refused outside
    # `[0, max_order_quantity]`; a cap of 0 means "unlimited".
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
    # `STEP` demand: the step may not fall in week 1, which would be constant
    # demand under another name. Its upper bound is the game's own length.
    min_step_week: int = 2
    # `SEASONAL` demand. A period of one week is not a cycle, and a period
    # longer than the longest game is already flat over any game `max_weeks`
    # allows. It was `2**31 - 1` (the column's ceiling) until 2026-11-02.
    min_period_weeks: int = 2
    max_period_weeks: int = 104
    # `SEASONAL` phase, in radians: one full turn either way.
    max_phase: float = 2 * math.pi
    max_currency_symbol_length: int = 4
    # The bot's theta, alpha and beta are weights in [0, max_bot_weight].
    max_bot_weight: float = 1.0
    max_target_stock_multiplier: float = 10.0


DEFAULT_LIMITS: Limits = Limits()
