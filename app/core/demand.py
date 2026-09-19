"""End-customer demand generation (`04-demand-generator.md`).

A `DemandConfig` plus a seed becomes the *complete* list of weekly end-customer
demand, produced once at game start and then persisted. The series is never
regenerated and never extended mid-game: that is what makes a game reproducible
and what lets the results screen chart true demand against every player's orders.

Weeks are 1-indexed in the specification and 0-indexed in the returned list, so
`series[w - 1]` is the demand for week `w`.

Pure (`00-conventions.md` §4): this module imports nothing from `app.config`,
`app.services`, `app.db`, `app.sockets` or `app.api`, performs no I/O, reads no
clock, and never touches the global `random` module. Every draw comes from the
`random.Random` instance derived from the seed in `generate_demand_series`
(`00-decisions.md` D11).
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod

from .config_models import (
    ConstantDemand,
    CustomDemand,
    DemandConfig,
    RampDemand,
    SeasonalDemand,
    StepDemand,
    StochasticDemand,
)
from .enums import Distribution

__all__ = [
    "ConstantGenerator",
    "CustomGenerator",
    "DemandGenerator",
    "RampGenerator",
    "SeasonalGenerator",
    "StepGenerator",
    "StochasticGenerator",
    "generate_demand_series",
    "generator_for",
]


def _round_half_up(value: float) -> int:
    """Round to the nearest integer, halves away from zero's side of the floor.

    `04-demand-generator.md` §3.7: rounding is always half-up. Python's built-in
    `round()` is banker's rounding -- `round(2.5) == 2` but `round(3.5) == 4` --
    which makes SEASONAL and RAMP series subtly asymmetric and impossible to
    reason about in a test.
    """
    return math.floor(value + 0.5)


def _clamp(value: int, low: int, high: int) -> int:
    """`value` confined to `[low, high]`."""
    return max(low, min(high, value))


def _poisson(rng: random.Random, mean: float) -> int:
    """A Poisson draw by Knuth's algorithm, using only `rng`.

    Multiply uniform draws until the running product falls at or below
    `exp(-mean)`, then report the number of draws minus one. Implemented on top
    of the supplied `rng` -- rather than `random.expovariate`, `numpy` or any
    module keeping its own state -- so the series stays reproducible from the
    seed (`04-demand-generator.md` §3.5).
    """
    target = math.exp(-mean)
    product = 1.0
    draws = 0
    while True:
        draws += 1
        product *= rng.random()
        if product <= target:
            return draws - 1


class DemandGenerator(ABC):
    """One subclass per `DemandKind`. Stateless.

    A generator holds its (frozen) slice of the configuration and nothing else.
    It caches no result, so calling `series` twice with two freshly seeded
    `random.Random` objects yields two equal lists.
    """

    @abstractmethod
    def series(self, duration_weeks: int, rng: random.Random) -> list[int]:
        """Demand for weeks `1..duration_weeks`, as a 0-indexed list.

        Every element is an `int` and `>= 0`.
        """


class ConstantGenerator(DemandGenerator):
    """`demand(w) = value` -- flat forever (`§3.1`)."""

    def __init__(self, config: ConstantDemand) -> None:
        self._config = config

    def series(self, duration_weeks: int, rng: random.Random) -> list[int]:
        value = max(0, self._config.value)
        return [value for _ in range(max(duration_weeks, 0))]


class StepGenerator(DemandGenerator):
    """The classic MIT one-time jump (`§3.2`).

    `demand(w) = initial_value` while `w < step_week`, `step_value` from
    `step_week` onwards.
    """

    def __init__(self, config: StepDemand) -> None:
        self._config = config

    def series(self, duration_weeks: int, rng: random.Random) -> list[int]:
        config = self._config
        result: list[int] = []
        for week in range(1, max(duration_weeks, 0) + 1):
            raw = config.initial_value if week < config.step_week else config.step_value
            result.append(max(0, raw))
        return result


class RampGenerator(DemandGenerator):
    """A gradual increase, optionally capped (`§3.3`).

    `demand(w) = initial_value + slope_per_week * (w - start_week + 1)` from
    `start_week` onwards, `initial_value` before it. The `+ 1` makes the first
    ramped week already carry one slope step, so a ramp is visibly a ramp from
    the week it starts rather than repeating the baseline.
    """

    def __init__(self, config: RampDemand) -> None:
        self._config = config

    def series(self, duration_weeks: int, rng: random.Random) -> list[int]:
        config = self._config
        result: list[int] = []
        for week in range(1, max(duration_weeks, 0) + 1):
            if week < config.start_week:
                raw = float(config.initial_value)
            else:
                steps = week - config.start_week + 1
                raw = config.initial_value + config.slope_per_week * steps
            value = _round_half_up(raw)
            if config.cap is not None:
                value = min(value, config.cap)
            result.append(max(0, value))
        return result


class SeasonalGenerator(DemandGenerator):
    """Sinusoidal demand (`§3.4`).

    `demand(w) = base + amplitude * sin(2*pi * (w - 1) / period_weeks + phase)`,
    with `phase` in radians. The `(w - 1)` puts week 1 at phase offset 0, so with
    `phase = 0` week 1 sits at `base` rather than at the peak.
    """

    def __init__(self, config: SeasonalDemand) -> None:
        self._config = config

    def series(self, duration_weeks: int, rng: random.Random) -> list[int]:
        config = self._config
        result: list[int] = []
        for week in range(1, max(duration_weeks, 0) + 1):
            angle = 2.0 * math.pi * (week - 1) / config.period_weeks + config.phase
            raw = config.base + config.amplitude * math.sin(angle)
            result.append(max(0, _round_half_up(raw)))
        return result


class StochasticGenerator(DemandGenerator):
    """One independent draw per week, in week order (`§3.5`).

    Drawing strictly in week order, one draw per week, means lengthening the
    game extends the series rather than reshuffling it.
    """

    def __init__(self, config: StochasticDemand) -> None:
        self._config = config

    def series(self, duration_weeks: int, rng: random.Random) -> list[int]:
        config = self._config
        low = config.min
        high = config.max
        result: list[int] = []
        for _ in range(max(duration_weeks, 0)):
            if config.distribution is Distribution.UNIFORM:
                value = rng.randint(low, high)
            elif config.distribution is Distribution.POISSON:
                value = _clamp(_poisson(rng, config.mean), low, high)
            elif config.distribution is Distribution.NORMAL:
                drawn = rng.gauss(config.mean, config.stdev)
                value = _clamp(_round_half_up(drawn), low, high)
            else:
                raise ValueError(f"Unknown distribution {config.distribution!r}.")
            result.append(max(0, value))
        return result


class CustomGenerator(DemandGenerator):
    """A host-supplied series (`§3.6`).

    Entries beyond `duration_weeks` are truncated. A series *shorter* than
    `duration_weeks` raises `ValueError`: it cannot arrive through
    `from_host_input`, but a config rehydrated out of Redis reaches this code
    directly, and silently padding or returning a short list would break the
    length guarantee somewhere far away from the cause.
    """

    def __init__(self, config: CustomDemand) -> None:
        self._config = config

    def series(self, duration_weeks: int, rng: random.Random) -> list[int]:
        values = self._config.values
        wanted = max(duration_weeks, 0)
        if len(values) < wanted:
            raise ValueError(
                f"CUSTOM demand needs at least {wanted} values, got {len(values)}."
            )
        return [max(0, int(value)) for value in values[:wanted]]


def generator_for(demand: DemandConfig) -> DemandGenerator:
    """The generator matching `demand`'s kind.

    Raises `ValueError` for an unknown kind.
    """
    kind = getattr(demand, "kind", None)
    if isinstance(demand, ConstantDemand):
        return ConstantGenerator(demand)
    if isinstance(demand, StepDemand):
        return StepGenerator(demand)
    if isinstance(demand, RampDemand):
        return RampGenerator(demand)
    if isinstance(demand, SeasonalDemand):
        return SeasonalGenerator(demand)
    if isinstance(demand, StochasticDemand):
        return StochasticGenerator(demand)
    if isinstance(demand, CustomDemand):
        return CustomGenerator(demand)
    raise ValueError(f"Unknown demand kind {kind!r}.")


def generate_demand_series(
    demand: DemandConfig,
    duration_weeks: int,
    seed: int,
) -> list[int]:
    """The full series, length exactly `duration_weeks`.

    Index 0 is week 1. Every value is an int >= 0. Deterministic: identical
    arguments always produce an identical list.

    The RNG is derived as `random.Random(f"{seed}:demand")` (`00-decisions.md`
    D11), a named stream, so that changing the role assignment draw does not
    shift the demand draw. Only `STOCHASTIC` consumes it; the other five kinds
    ignore the seed entirely.
    """
    rng = random.Random(f"{seed}:demand")
    return generator_for(demand).series(duration_weeks, rng)
