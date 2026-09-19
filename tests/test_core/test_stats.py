"""Black-box tests for section 07 -- ``app/core/stats.py``.

Covers ``07-game-engine.md §5`` acceptance criteria 18, 19 and 20, and ``§6``
failure modes 9, 10 and 11, against the definitions in ``§3.10`` /
``00-decisions.md D12``.

Driven through the frozen public surfaces of ``§2`` plus sections 03 and 06.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config_models import (
    DEFAULT_LIMITS,
    ConstantDemand,
    DemandConfig,
    FactoryConfig,
    GameConfig,
    RoleConfig,
    StepDemand,
)
from app.core.enums import ROLE_ORDER, Role
from app.core.game_engine import GameEngine, GamePhase, WeekRecord
from app.core.presets import get_preset
from app.core.stats import GameStats, RoleStats, compute_stats, population_variance

SEED = 20260919


def make_config(
    role_kwargs: dict[str, Any] | None = None,
    factory_kwargs: dict[str, Any] | None = None,
    *,
    demand: DemandConfig | None = None,
    duration_weeks: int = 36,
) -> GameConfig:
    base = dict(role_kwargs or {})
    factory_base = {**base, **dict(factory_kwargs or {})}
    return GameConfig.from_host_input(
        {
            "roles": {
                Role.RETAILER.value: RoleConfig(**base),
                Role.WHOLESALER.value: RoleConfig(**base),
                Role.DISTRIBUTOR.value: RoleConfig(**base),
                Role.FACTORY.value: FactoryConfig(**factory_base),
            },
            "demand": demand if demand is not None else ConstantDemand(value=4),
            "duration_weeks": duration_weeks,
        },
        DEFAULT_LIMITS,
    )


def play_week(engine: GameEngine, orders: dict[Role, int]) -> list[WeekRecord]:
    for role in ROLE_ORDER:
        engine.submit_order(role, orders[role])
    return engine.close_week()


def drive_flat(engine: GameEngine, script: list[int]) -> None:
    for quantity in script:
        play_week(engine, {role: quantity for role in ROLE_ORDER})


def drive_anchor_and_panic(engine: GameEngine, weeks: int) -> None:
    """The normative policy of acceptance criterion 20.

    Each role, every week, from its own ``player_view``: replace what was
    demanded and close the whole inventory gap in one order, with no
    supply-line correction.
    """
    for _ in range(weeks):
        orders: dict[Role, int] = {}
        for role in ROLE_ORDER:
            view = engine.player_view(role)
            orders[role] = max(
                0,
                view["incoming_order"] + (12 - view["inventory"] + view["backlog"]),
            )
        play_week(engine, orders)


def stats_of(engine: GameEngine) -> GameStats:
    return compute_stats(engine.history, engine.demand_series, engine.weeks_played)


def orders_of(engine: GameEngine, role: Role) -> list[int]:
    return [record.order for record in engine.history if record.role == role]


# --------------------------------------------------------------------------
# FM 10 -- population variance, ddof = 0
# --------------------------------------------------------------------------


def test_fm10_population_variance_is_not_a_sample_variance() -> None:
    assert population_variance([2, 4]) == pytest.approx(1.0)
    assert population_variance([2, 4]) != pytest.approx(2.0)


def test_population_variance_edge_cases_and_known_values() -> None:
    assert population_variance([]) == 0.0
    assert population_variance([7]) == 0.0
    assert population_variance([5, 5, 5, 5]) == 0.0
    assert population_variance([1, 2, 3, 4, 5]) == pytest.approx(2.0)
    assert population_variance((1, 2, 3, 4, 5)) == pytest.approx(2.0)
    assert population_variance([4, 4, 4, 4, 8, 8, 8, 8]) == pytest.approx(4.0)


# --------------------------------------------------------------------------
# AC 18 -- total_cost agrees with the sum of week_cost
# --------------------------------------------------------------------------


def test_ac18_total_cost_equals_the_sum_of_week_costs() -> None:
    config = make_config(
        {"fixed_order_cost": 1.25, "unit_purchase_cost": 0.35},
        demand=get_preset("CLASSIC_MIT").demand,
        duration_weeks=36,
    )
    engine = GameEngine.start(config, SEED)
    drive_flat(engine, [4, 9, 0, 14, 6, 2, 11, 0, 7, 3, 5, 8] * 3)
    assert engine.phase == GamePhase.FINISHED

    stats = stats_of(engine)
    assert isinstance(stats, GameStats)
    assert stats.weeks_played == 36

    total = 0.0
    for role in ROLE_ORDER:
        role_stats = stats.per_role[role]
        assert isinstance(role_stats, RoleStats)
        expected = sum(
            record.week_cost for record in engine.history if record.role == role
        )
        assert role_stats.total_cost == pytest.approx(expected, abs=1e-9)
        total += expected

    assert stats.chain_total_cost == pytest.approx(total, abs=1e-9)
    assert stats.chain_total_cost == pytest.approx(
        sum(stats.per_role[role].total_cost for role in ROLE_ORDER), abs=1e-9
    )


# --------------------------------------------------------------------------
# AC 19, FM 11 -- the bullwhip ratio and its zero-variance case
# --------------------------------------------------------------------------


def test_fm11_constant_demand_gives_a_null_ratio_not_infinity() -> None:
    config = make_config(demand=ConstantDemand(value=4), duration_weeks=12)
    engine = GameEngine.start(config, SEED)
    drive_flat(engine, [4, 9, 2, 14, 7, 3, 11, 5, 8, 0, 6, 4])

    stats = stats_of(engine)
    assert stats.demand_variance == pytest.approx(0.0)
    for role in ROLE_ORDER:
        role_stats = stats.per_role[role]
        assert role_stats.bullwhip_ratio is None
        assert role_stats.order_variance > 0.0


def test_ac19_step_demand_gives_a_positive_float_ratio() -> None:
    config = make_config(
        demand=StepDemand(initial_value=4, step_week=5, step_value=8),
        duration_weeks=36,
    )
    engine = GameEngine.start(config, SEED)
    drive_flat(engine, [4, 4, 4, 4, 8, 12, 16, 12, 8, 4, 0, 0] * 3)

    stats = stats_of(engine)
    assert stats.demand_variance > 0.0
    assert stats.demand_variance == pytest.approx(
        population_variance(engine.demand_series[:36])
    )
    for role in ROLE_ORDER:
        ratio = stats.per_role[role].bullwhip_ratio
        assert isinstance(ratio, float)
        assert ratio > 0.0
        assert ratio == pytest.approx(
            stats.per_role[role].order_variance / stats.demand_variance
        )


# --------------------------------------------------------------------------
# AC 20 -- the pedagogical result
# --------------------------------------------------------------------------


def test_ac20_bullwhip_increases_strictly_from_retailer_to_factory() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    drive_anchor_and_panic(engine, 36)
    assert engine.phase == GamePhase.FINISHED
    assert engine.weeks_played == 36

    stats = stats_of(engine)
    ratios = [stats.per_role[role].bullwhip_ratio for role in ROLE_ORDER]
    assert all(isinstance(ratio, float) for ratio in ratios)
    assert (
        ratios[0] < ratios[1] < ratios[2] < ratios[3]  # type: ignore[operator]
    ), dict(zip([role.value for role in ROLE_ORDER], ratios))


# --------------------------------------------------------------------------
# FM 9 -- end_early must not count the partial week
# --------------------------------------------------------------------------


def test_fm9_end_early_divides_by_the_completed_weeks() -> None:
    config = make_config(
        demand=get_preset("CLASSIC_MIT").demand,
        duration_weeks=36,
    )
    engine = GameEngine.start(config, SEED)
    script = [4, 5, 6, 7, 8, 9, 10, 11, 12]
    drive_flat(engine, script)
    assert engine.week == 10

    engine.end_early()
    assert engine.weeks_played == 9

    stats = stats_of(engine)
    assert stats.weeks_played == 9
    assert stats.demand_variance == pytest.approx(
        population_variance(engine.demand_series[:9])
    )
    # ... and NOT over the full 36-week series, which the engine still holds
    assert stats.demand_variance != pytest.approx(
        population_variance(engine.demand_series)
    )

    for role in ROLE_ORDER:
        recorded = orders_of(engine, role)
        assert recorded == script
        assert len(recorded) == 9
        assert stats.per_role[role].average_order == pytest.approx(sum(script) / 9)
        assert stats.per_role[role].order_variance == pytest.approx(
            population_variance(script)
        )


# --------------------------------------------------------------------------
# §3.10 / D12 -- the remaining per-role figures
# --------------------------------------------------------------------------


def test_per_role_peaks_backlog_weeks_and_average_order() -> None:
    config = make_config(
        demand=StepDemand(initial_value=4, step_week=5, step_value=20),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    drive_flat(engine, [4] * 12)

    stats = stats_of(engine)
    for role in ROLE_ORDER:
        own = [record for record in engine.history if record.role == role]
        role_stats = stats.per_role[role]
        assert role_stats.role == role
        assert role_stats.peak_inventory == max(r.closing_inventory for r in own)
        assert role_stats.peak_backlog == max(r.closing_backlog for r in own)
        assert role_stats.weeks_in_backlog == sum(
            1 for r in own if r.closing_backlog > 0
        )
        assert role_stats.average_order == pytest.approx(
            sum(r.order for r in own) / len(own)
        )

    # the step is large enough that the Retailer really does go into backlog,
    # so the two counters are not asserting zeroes
    assert stats.per_role[Role.RETAILER].peak_backlog > 0
    assert stats.per_role[Role.RETAILER].weeks_in_backlog > 0


def test_fill_rate_is_shipped_over_obligation() -> None:
    config = make_config(
        demand=StepDemand(initial_value=4, step_week=5, step_value=20),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    drive_flat(engine, [4] * 12)

    stats = stats_of(engine)
    for role in ROLE_ORDER:
        own = [record for record in engine.history if record.role == role]
        shipped = sum(r.shipped for r in own)
        obligation = sum(r.obligation for r in own)
        assert obligation > 0
        rate = stats.per_role[role].fill_rate
        assert rate is not None
        assert rate == pytest.approx(shipped / obligation)
        assert 0.0 <= rate <= 1.0

    assert stats.per_role[Role.RETAILER].fill_rate is not None
    assert stats.per_role[Role.RETAILER].fill_rate < 1.0


def test_fill_rate_is_none_when_nothing_was_ever_demanded() -> None:
    config = make_config(
        {"initial_backlog": 0, "initial_order_in_pipeline": 0},
        demand=ConstantDemand(value=0),
        duration_weeks=12,
    )
    engine = GameEngine.start(config, SEED)
    drive_flat(engine, [0] * 12)

    stats = stats_of(engine)
    for role in ROLE_ORDER:
        own = [record for record in engine.history if record.role == role]
        assert sum(r.obligation for r in own) == 0
        assert stats.per_role[role].fill_rate is None


# --------------------------------------------------------------------------
# §3.10 -- compute_stats reads history, not live agent state
# --------------------------------------------------------------------------


def test_compute_stats_is_a_pure_function_of_its_arguments() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    drive_flat(engine, [4, 4, 4, 4, 8, 12, 16, 12, 8, 4, 0, 0] * 3)

    first = compute_stats(engine.history, engine.demand_series, engine.weeks_played)
    second = compute_stats(
        list(engine.history), list(engine.demand_series), engine.weeks_played
    )
    assert first == second
    assert set(first.per_role) == set(ROLE_ORDER)
