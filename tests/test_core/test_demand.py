"""Black-box tests for section 04 -- the demand generator.

Covers every numbered item in ``04-demand-generator.md §5`` (acceptance
criteria) and every item in ``§6`` (failure modes).  Nothing here imports a
symbol that is not declared in a frozen *Public surface*: ``app/core/demand.py``
(section 04) and ``app/core/config_models.py`` / ``app/core/enums.py``
(section 03).
"""

from __future__ import annotations

import ast
import inspect
import random
import statistics
import sys
import types
from typing import Any

import pytest

from app.core import demand as demand_module
from app.core.config_models import (
    ConstantDemand,
    CustomDemand,
    RampDemand,
    SeasonalDemand,
    StepDemand,
    StochasticDemand,
)
from app.core.demand import (
    ConstantGenerator,
    CustomGenerator,
    DemandGenerator,
    RampGenerator,
    SeasonalGenerator,
    StepGenerator,
    StochasticGenerator,
    generate_demand_series,
    generator_for,
)
from app.core.enums import DemandKind, Distribution

# --- shared inputs ---------------------------------------------------------

#: One config per ``DemandKind``, all long enough for a 12-week game.
ALL_KINDS = [
    pytest.param(ConstantDemand(value=4), id="CONSTANT"),
    pytest.param(
        StepDemand(initial_value=4, step_week=5, step_value=8),
        id="STEP",
    ),
    pytest.param(
        RampDemand(initial_value=4, slope_per_week=1.5, start_week=3, cap=None),
        id="RAMP",
    ),
    pytest.param(
        SeasonalDemand(base=8, amplitude=4, period_weeks=12, phase=0.0),
        id="SEASONAL",
    ),
    pytest.param(
        StochasticDemand(
            distribution=Distribution.NORMAL,
            mean=8.0,
            stdev=2.0,
            min=0,
            max=100,
        ),
        id="STOCHASTIC",
    ),
    pytest.param(CustomDemand(values=list(range(1, 41))), id="CUSTOM"),
]

NON_STOCHASTIC_KINDS = [p for p in ALL_KINDS if p.id != "STOCHASTIC"]


@pytest.fixture(autouse=True)
def _preserve_global_random() -> Any:
    """Keep a test's use of the global ``random`` module out of its
    neighbours' way (FM 5 deliberately seeds it)."""
    state = random.getstate()
    yield
    random.setstate(state)


def a_rng(seed: int = 0) -> random.Random:
    """A fresh, independent ``Random`` for a direct ``series()`` call."""
    return random.Random(f"{seed}:demand")


# --- AC 1, 2, 3, FM 7 ------------------------------------------------------


@pytest.mark.parametrize("cfg", ALL_KINDS)
def test_series_has_exactly_duration_weeks_elements(cfg: Any) -> None:
    """AC 1: the returned list is exactly ``duration_weeks`` long."""
    for weeks in (8, 12, 37):
        assert len(generate_demand_series(cfg, weeks, 1234)) == weeks


@pytest.mark.parametrize("cfg", ALL_KINDS)
def test_every_element_is_a_plain_int(cfg: Any) -> None:
    """AC 2 / FM 7: ints, not floats, not bools, not numpy scalars."""
    series = generate_demand_series(cfg, 24, 1234)
    assert all(type(value) is int for value in series), series


def test_ramp_with_a_fractional_slope_does_not_leak_floats() -> None:
    """FM 7: a RAMP is the generator that most easily leaks a float."""
    cfg = RampDemand(initial_value=4, slope_per_week=0.5, start_week=1, cap=None)
    series = generate_demand_series(cfg, 20, 7)
    assert all(type(value) is int for value in series), series


@pytest.mark.parametrize("cfg", ALL_KINDS)
def test_every_element_is_non_negative(cfg: Any) -> None:
    """AC 3."""
    assert all(value >= 0 for value in generate_demand_series(cfg, 24, 1234))


# --- AC 4: the worked examples of §4 (normative) ---------------------------


def test_worked_example_step() -> None:
    cfg = StepDemand(initial_value=4, step_week=5, step_value=8)
    assert generate_demand_series(cfg, 12, 1) == [4, 4, 4, 4, 8, 8, 8, 8, 8, 8, 8, 8]


def test_worked_example_constant() -> None:
    assert generate_demand_series(ConstantDemand(value=4), 5, 1) == [4, 4, 4, 4, 4]


def test_worked_example_ramp_capped() -> None:
    cfg = RampDemand(initial_value=4, slope_per_week=2.0, start_week=3, cap=12)
    assert generate_demand_series(cfg, 8, 1) == [4, 4, 6, 8, 10, 12, 12, 12]


def test_worked_example_ramp_fractional_slope() -> None:
    """AC 4 and FM 1: half-up rounding, not banker's."""
    cfg = RampDemand(initial_value=4, slope_per_week=0.5, start_week=1, cap=None)
    series = generate_demand_series(cfg, 6, 1)
    assert series == [5, 5, 6, 6, 7, 7]
    assert series != [4, 5, 6, 6, 6, 7], "round() was used: banker's rounding"


def test_worked_example_seasonal() -> None:
    cfg = SeasonalDemand(base=8, amplitude=4, period_weeks=4, phase=0.0)
    assert generate_demand_series(cfg, 8, 1) == [8, 12, 8, 4, 8, 12, 8, 4]


def test_worked_example_seasonal_floored_at_zero() -> None:
    cfg = SeasonalDemand(base=2, amplitude=5, period_weeks=4, phase=0.0)
    assert generate_demand_series(cfg, 4, 1) == [2, 7, 2, 0]


def test_worked_example_custom_truncated() -> None:
    cfg = CustomDemand(values=[1, 2, 3, 4, 5])
    assert generate_demand_series(cfg, 3, 1) == [1, 2, 3]


# --- FM 1, 2, 3, 4: the boundary bugs the examples exist to catch ----------


def test_half_up_rounding_on_an_exact_half() -> None:
    """FM 1: a value landing exactly on ``x.5`` rounds **up**.

    ``round()`` is banker's: it would send 0.5 and 2.5 down to 0 and 2 while
    leaving 1.5 at 2, giving ``[0, 1, 2, 2, 2, 3]``.
    """
    cfg = RampDemand(initial_value=0, slope_per_week=0.5, start_week=1, cap=None)
    # w=1 -> 0.5, w=2 -> 1.0, w=3 -> 1.5, w=4 -> 2.0, w=5 -> 2.5, w=6 -> 3.0
    assert generate_demand_series(cfg, 6, 1) == [1, 1, 2, 2, 3, 3]


def test_step_week_boundary_is_not_off_by_one() -> None:
    """FM 2: with ``step_week=5``, week 4 is old and week 5 is new."""
    cfg = StepDemand(initial_value=4, step_week=5, step_value=8)
    series = generate_demand_series(cfg, 12, 1)
    assert series[3] == 4, "week 4 must still carry initial_value"
    assert series[4] == 8, "week 5 must already carry step_value"


def test_seasonal_phase_starts_at_the_base_in_week_one() -> None:
    """FM 3: with ``phase=0`` week 1 is ``base``, not ``base + amplitude``."""
    cfg = SeasonalDemand(base=10, amplitude=6, period_weeks=8, phase=0.0)
    series = generate_demand_series(cfg, 8, 1)
    assert series[0] == 10
    assert series[0] != 16


def test_seasonal_amplitude_larger_than_base_floors_at_zero() -> None:
    """FM 4: a negative customer demand must never be produced."""
    cfg = SeasonalDemand(base=1, amplitude=9, period_weeks=4, phase=0.0)
    series = generate_demand_series(cfg, 16, 1)
    assert min(series) == 0
    assert all(value >= 0 for value in series), series


def test_ramp_with_a_negative_slope_floors_at_zero() -> None:
    """FM 4, the RAMP half: §3.3 floors at 0 after the cap."""
    cfg = RampDemand(initial_value=4, slope_per_week=-2.0, start_week=1, cap=None)
    series = generate_demand_series(cfg, 8, 1)
    assert all(value >= 0 for value in series), series
    assert series[-1] == 0


# --- AC 5, 7, 8: determinism ----------------------------------------------


@pytest.mark.parametrize("cfg", ALL_KINDS)
def test_two_calls_with_identical_arguments_are_equal(cfg: Any) -> None:
    """AC 5."""
    assert generate_demand_series(cfg, 30, 99) == generate_demand_series(cfg, 30, 99)


@pytest.mark.parametrize("cfg", NON_STOCHASTIC_KINDS)
def test_non_stochastic_kinds_ignore_the_seed(cfg: Any) -> None:
    """AC 7 / §3.8."""
    baseline = generate_demand_series(cfg, 30, 1)
    for seed in (2, 7, 1_000_003, -5):
        assert generate_demand_series(cfg, 30, seed) == baseline


def test_stochastic_different_seeds_give_different_series() -> None:
    """AC 6: over 50 weeks a collision is not plausible."""
    cfg = StochasticDemand(
        distribution=Distribution.NORMAL, mean=8.0, stdev=2.0, min=0, max=100
    )
    assert generate_demand_series(cfg, 50, 1) != generate_demand_series(cfg, 50, 2)


# --- AC 8, 9, 10: the three distributions ---------------------------------


def test_stochastic_uniform_covers_its_whole_closed_range() -> None:
    """AC 8."""
    cfg = StochasticDemand(
        distribution=Distribution.UNIFORM, mean=4.0, stdev=1.0, min=2, max=6
    )
    series = generate_demand_series(cfg, 500, 20260919)
    assert all(2 <= value <= 6 for value in series)
    assert 2 in series
    assert 6 in series


def test_stochastic_normal_has_the_requested_mean() -> None:
    """AC 9: within 15% of ``mean`` over 500 weeks, min/max not binding."""
    cfg = StochasticDemand(
        distribution=Distribution.NORMAL, mean=20.0, stdev=4.0, min=0, max=100
    )
    series = generate_demand_series(cfg, 500, 20260919)
    assert abs(statistics.fmean(series) - 20.0) <= 0.15 * 20.0


def test_stochastic_poisson_has_the_requested_mean_and_produces_zeros() -> None:
    """AC 10: Poisson(4) has a ~1.8% chance of a zero in any given week."""
    cfg = StochasticDemand(
        distribution=Distribution.POISSON, mean=4.0, stdev=1.0, min=0, max=20
    )
    series = generate_demand_series(cfg, 500, 20260919)
    assert abs(statistics.fmean(series) - 4.0) <= 0.15 * 4.0
    assert 0 in series


# --- AC 11: the series extends, it does not reshuffle ---------------------


@pytest.mark.parametrize(
    "distribution",
    [Distribution.NORMAL, Distribution.POISSON, Distribution.UNIFORM],
)
def test_stochastic_series_extends_rather_than_reshuffles(
    distribution: Distribution,
) -> None:
    """AC 11 / §3.5: one draw per week, strictly in week order."""
    cfg = StochasticDemand(
        distribution=distribution, mean=6.0, stdev=2.0, min=0, max=20
    )
    short = generate_demand_series(cfg, 30, 4242)
    long = generate_demand_series(cfg, 40, 4242)
    assert long[:30] == short


# --- AC 12: the factory ---------------------------------------------------


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        (ConstantDemand(value=4), ConstantGenerator),
        (StepDemand(), StepGenerator),
        (RampDemand(), RampGenerator),
        (SeasonalDemand(), SeasonalGenerator),
        (StochasticDemand(), StochasticGenerator),
        (CustomDemand(values=[1, 2, 3]), CustomGenerator),
    ],
    ids=[k.value for k in DemandKind],
)
def test_generator_for_returns_the_matching_subclass(cfg: Any, expected: type) -> None:
    """AC 12, first half."""
    generator = generator_for(cfg)
    assert isinstance(generator, expected)
    assert isinstance(generator, DemandGenerator)


def test_generator_for_rejects_an_unknown_kind() -> None:
    """AC 12, second half."""
    with pytest.raises(ValueError):
        generator_for(types.SimpleNamespace(kind="MYSTERY_MEAT"))


def test_demand_generator_is_abstract() -> None:
    """§2: ``DemandGenerator`` is an ABC with an abstract ``series``."""
    assert inspect.isabstract(DemandGenerator)


# --- FM 5: global RNG contamination ---------------------------------------


def test_generation_neither_reads_nor_disturbs_the_global_rng() -> None:
    """FM 5.

    Seeding the global module must not change what is generated, and
    generating must not consume from the global stream.
    """
    cfg = StochasticDemand(
        distribution=Distribution.NORMAL, mean=8.0, stdev=2.0, min=0, max=100
    )

    random.seed(1)
    untouched = [random.random() for _ in range(5)]

    random.seed(1)
    first = generate_demand_series(cfg, 40, 777)
    after_first = [random.random() for _ in range(5)]

    random.seed(1)
    second = generate_demand_series(cfg, 40, 777)
    after_second = [random.random() for _ in range(5)]

    assert first == second, "the series depends on the global RNG"
    assert after_first == untouched, "generation consumed the global RNG stream"
    assert after_second == untouched


def test_seeding_the_global_rng_differently_changes_nothing() -> None:
    """FM 5, the other direction."""
    cfg = StochasticDemand(
        distribution=Distribution.POISSON, mean=4.0, stdev=1.0, min=0, max=20
    )
    random.seed(11)
    first = generate_demand_series(cfg, 40, 555)
    random.seed(9_999)
    second = generate_demand_series(cfg, 40, 555)
    assert first == second


# --- FM 6: shared generator state -----------------------------------------


@pytest.mark.parametrize("cfg", ALL_KINDS)
def test_one_generator_instance_reused_is_not_stateful(cfg: Any) -> None:
    """FM 6: a generator that caches or accumulates is a bug."""
    generator = generator_for(cfg)
    first = generator.series(24, a_rng(31))
    second = generator.series(24, a_rng(31))
    assert first == second


def test_one_generator_instance_does_not_cache_its_answer() -> None:
    """FM 6, the other half.

    Equality across two identical calls is also what a generator that cached
    its first answer would give, so the same instance is asked for a
    different length and a different stream too.
    """
    cfg = StochasticDemand(
        distribution=Distribution.NORMAL, mean=8.0, stdev=2.0, min=0, max=100
    )
    generator = generator_for(cfg)
    first = generator.series(24, a_rng(31))
    assert len(generator.series(37, a_rng(31))) == 37
    assert generator.series(24, a_rng(32)) != first
    assert generator.series(24, a_rng(31)) == first


def test_two_generator_instances_agree() -> None:
    """FM 6: no state leaks through the class either."""
    cfg = StochasticDemand(
        distribution=Distribution.NORMAL, mean=8.0, stdev=2.0, min=0, max=100
    )
    assert generator_for(cfg).series(24, a_rng(5)) == generator_for(cfg).series(
        24, a_rng(5)
    )


# --- FM 8: a CUSTOM series shorter than the game --------------------------


def test_custom_series_shorter_than_the_game_raises() -> None:
    """FM 8 / §3.6: raise, never pad, truncate the game, or return short."""
    cfg = CustomDemand(values=[1, 2, 3])
    with pytest.raises(ValueError):
        generate_demand_series(cfg, 12, 1)


def test_custom_series_exactly_as_long_as_the_game_is_fine() -> None:
    """FM 8's boundary: equal length is not short."""
    cfg = CustomDemand(values=[1, 2, 3, 4, 5])
    assert generate_demand_series(cfg, 5, 1) == [1, 2, 3, 4, 5]


# --- AC 13, 14: what the module is allowed to touch -----------------------

ALLOWED_TOP_LEVEL_IMPORTS = {"pydantic"} | set(sys.stdlib_module_names)


def _module_tree() -> ast.Module:
    return ast.parse(inspect.getsource(demand_module))


def _import_targets(tree: ast.Module) -> list[str]:
    """Every module name ``app/core/demand.py`` imports, as written."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.append("." * node.level + (node.module or ""))
            else:
                names.append(node.module or "")
    return names


def test_module_imports_only_stdlib_pydantic_and_app_core() -> None:
    """AC 13 / ``00-conventions.md §4``: the domain modules are pure."""
    offenders = []
    for name in _import_targets(_module_tree()):
        if name.startswith("."):
            # Relative: one dot stays inside ``app.core``; two escape it.
            if name.startswith(".."):
                offenders.append(name)
            continue
        root = name.split(".")[0]
        if root == "app":
            if name != "app.core" and not name.startswith("app.core."):
                offenders.append(name)
        elif root not in ALLOWED_TOP_LEVEL_IMPORTS:
            offenders.append(name)
    assert offenders == [], f"forbidden imports in app/core/demand.py: {offenders}"


def _import_time_statements(tree: ast.Module) -> list[ast.stmt]:
    """Statements that run when the module is imported.

    That is the module body plus every class body, but **not** function
    bodies -- and not a bare annotation, which is never evaluated.
    """
    collected: list[ast.stmt] = []
    pending: list[ast.stmt] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.ClassDef):
            pending.extend(node.body)
            continue
        collected.append(node)
    return collected


def test_module_does_not_touch_random_at_import_time() -> None:
    """AC 14, first half."""
    offenders = []
    for node in _import_time_statements(_module_tree()):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        scanned: list[ast.AST] = []
        if isinstance(node, ast.AnnAssign):
            if node.value is not None:
                scanned.append(node.value)
        else:
            scanned.append(node)
        for root in scanned:
            for inner in ast.walk(root):
                if isinstance(inner, ast.Name) and inner.id == "random":
                    offenders.append(ast.unparse(node))
    assert offenders == [], f"module-level use of random: {offenders}"


def test_module_never_calls_random_seed() -> None:
    """AC 14, second half."""
    tree = _module_tree()
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "random":
            offenders.extend(
                f"from random import {alias.name}"
                for alias in node.names
                if alias.name == "seed"
            )
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "seed"
            and isinstance(func.value, ast.Name)
            and func.value.id == "random"
        ):
            offenders.append(ast.unparse(node))
    assert offenders == [], f"random.seed is called: {offenders}"
