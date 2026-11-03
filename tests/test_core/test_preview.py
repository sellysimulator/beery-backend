"""`app.core.preview` and the figures the server now sends instead of the
client working them out (2026-11-03).

The host form, the order form, the recap and the 3D board each used to carry
a copy of an engine formula: the demand generators, the lead time, the
"inventory after arrival" step, the arrival week, a slot's weeks to go and
the weeks played. Each test here pins the server's figure to **the function
the engine itself uses**, so the two cannot drift: a preview is compared with
the series `GameEngine.start` really generates, a lead time with the one the
engine sends the player, and so on. The normative demand table of
`18 §2.6` lives here now, because the generator it describes lives here.
"""

from __future__ import annotations

import pytest

from app.core.config_models import (
    DEFAULT_LIMITS,
    ConstantDemand,
    GameConfig,
    RampDemand,
    SeasonalDemand,
    StepDemand,
    StochasticDemand,
)
from app.core.demand import generate_demand_series, uses_seed
from app.core.enums import ROLE_ORDER, Distribution, Role
from app.core.game_engine import GameEngine, GamePhase
from app.core.pipeline import Pipeline
from app.core.presets import PRESETS, get_preset, scenario_summary
from app.core.preview import EXAMPLE_SEED, preview_config

SEED = 20261103


def classic_with(**overrides: object) -> GameConfig:
    """The Classic preset with top-level fields replaced, through the host-input
    path a draft takes."""
    payload = get_preset("CLASSIC_MIT").to_payload()
    for key, value in overrides.items():
        payload[key] = value
    return GameConfig.from_host_input(payload, DEFAULT_LIMITS)


def demand_payload(demand: object) -> dict:
    return demand.model_dump(mode="json")  # type: ignore[attr-defined]


# --- the demand series: 18 §2.6's normative table ---------------------------


@pytest.mark.parametrize(
    ("weeks", "demand", "expected"),
    [
        (8, ConstantDemand(value=4), [4] * 8),
        (
            12,
            StepDemand(initial_value=4, step_week=5, step_value=8),
            [4, 4, 4, 4, 8, 8, 8, 8, 8, 8, 8, 8],
        ),
        # The ramp's `+ 1`: the first ramped week already carries one step.
        (
            8,
            RampDemand(initial_value=4, slope_per_week=2, start_week=3, cap=12),
            [4, 4, 6, 8, 10, 12, 12, 12],
        ),
        # Half-up, never banker's rounding.
        (
            8,
            RampDemand(initial_value=4, slope_per_week=0.5, start_week=1, cap=None),
            [5, 5, 6, 6, 7, 7, 8, 8],
        ),
        # The seasonal `w - 1`: week 1 sits at the base, not the peak.
        (
            8,
            SeasonalDemand(base=8, amplitude=4, period_weeks=4, phase=0.0),
            [8, 12, 8, 4, 8, 12, 8, 4],
        ),
    ],
)
def test_preview_series_is_the_table_and_the_engines_own(
    weeks: int, demand: object, expected: list[int]
) -> None:
    config = classic_with(duration_weeks=weeks, demand=demand_payload(demand))

    preview = preview_config(config)

    assert preview.demand_series == expected
    assert preview.demand_is_example is False
    # The very series a started game plays, whatever seed it is given.
    assert GameEngine.start(config, SEED).demand_series == expected


def test_float_noise_on_an_exact_half_still_rounds_up() -> None:
    """0.29 x 50 is 14.5 on paper and 14.499999999999998 in binary."""
    config = classic_with(
        duration_weeks=50,
        demand={
            "kind": "RAMP",
            "initial_value": 0,
            "slope_per_week": 0.29,
            "start_week": 1,
        },
    )

    assert preview_config(config).demand_series[-1] == 15


def test_preview_follows_duration() -> None:
    short = preview_config(classic_with(duration_weeks=8))
    long = preview_config(classic_with(duration_weeks=40))

    assert len(short.demand_series) == 8
    assert len(long.demand_series) == 40
    assert long.demand_series[:8] == short.demand_series


def test_stochastic_without_a_seed_is_an_example_draw_from_the_example_seed() -> None:
    demand = StochasticDemand(
        distribution=Distribution.NORMAL, mean=8.0, stdev=3.0, min=0, max=20
    )
    config = classic_with(duration_weeks=20, demand=demand_payload(demand))

    preview = preview_config(config)

    assert config.random_seed is None
    assert preview.demand_is_example is True
    assert preview.demand_series == generate_demand_series(
        config.demand, 20, EXAMPLE_SEED
    )
    # Stable: the same draft never redraws.
    assert preview_config(config).demand_series == preview.demand_series


def test_stochastic_with_a_seed_is_exactly_the_series_the_game_plays() -> None:
    demand = StochasticDemand(
        distribution=Distribution.POISSON, mean=6.0, stdev=0.0, min=0, max=15
    )
    config = classic_with(
        duration_weeks=24, demand=demand_payload(demand), random_seed=4242
    )

    preview = preview_config(config)

    assert preview.demand_is_example is False
    assert preview.demand_series == GameEngine.start(config, 4242).demand_series


def test_only_stochastic_demand_reads_the_seed() -> None:
    assert uses_seed(StochasticDemand()) is True
    for demand in (
        ConstantDemand(),
        StepDemand(),
        RampDemand(),
        SeasonalDemand(),
    ):
        assert uses_seed(demand) is False


# --- lead times --------------------------------------------------------------


def test_preview_lead_times_are_the_ones_the_engine_sends_each_player() -> None:
    payload = get_preset("CLASSIC_MIT").to_payload()
    # Every delay different, so a wrong term cannot cancel out.
    payload["roles"]["RETAILER"].update(shipping_delay_weeks=1)
    payload["roles"]["WHOLESALER"].update(
        shipping_delay_weeks=2, information_delay_weeks=3
    )
    payload["roles"]["DISTRIBUTOR"].update(
        shipping_delay_weeks=4, information_delay_weeks=5
    )
    payload["roles"]["FACTORY"].update(
        information_delay_weeks=6, production_delay_weeks=7
    )
    config = GameConfig.from_host_input(payload, DEFAULT_LIMITS)

    preview = preview_config(config)
    engine = GameEngine.start(config, SEED)

    assert list(preview.lead_times) == list(ROLE_ORDER)
    for role in ROLE_ORDER:
        assert (
            preview.lead_times[role]
            == engine.player_view(role)["order_arrival_lead_weeks"]
        )
    # The supplier's order delay plus this role's own inbound delay...
    assert preview.lead_times[Role.RETAILER] == 3 + 1
    assert preview.lead_times[Role.WHOLESALER] == 5 + 2
    assert preview.lead_times[Role.DISTRIBUTOR] == 6 + 4
    # ...and the Factory's production delay alone.
    assert preview.lead_times[Role.FACTORY] == 7


# --- the preset cards' figures ----------------------------------------------


def test_scenario_summary_of_the_presets() -> None:
    assert scenario_summary(PRESETS["CLASSIC_MIT"]) == (
        "36 weeks, 12 units on hand, 2-week delays, "
        "demand steps from 4 to 8 at week 5, "
        "$0.50 holding and $1.00 backlog a unit a week."
    )
    assert scenario_summary(PRESETS["FAST_GAME"]).startswith("20 weeks, ")
    assert scenario_summary(PRESETS["CHAOS"]) == (
        "52 weeks, 8 units on hand, 4-week shipping and 3-week order delays, "
        "random demand around 8 (standard deviation 4), between 0 and 25, "
        "$0.50 holding and $1.00 backlog a unit a week, "
        "production capped at 20 a week, no supply-line prompt."
    )


def test_scenario_summary_is_read_off_the_config_not_written_twice() -> None:
    changed = classic_with(
        duration_weeks=20,
        demand={"kind": "STEP", "initial_value": 5, "step_week": 3, "step_value": 9},
    )

    summary = scenario_summary(changed)

    assert summary.startswith("20 weeks, ")
    assert "demand steps from 5 to 9 at week 3" in summary

    payload = changed.to_payload()
    payload["currency_symbol"] = "€"
    for role in payload["roles"].values():
        role["holding_cost_per_unit_week"] = 0.25
    euros = scenario_summary(GameConfig.from_host_input(payload, DEFAULT_LIMITS))
    assert "€0.25 holding and €1.00 backlog a unit a week" in euros


# --- figures the views now carry ---------------------------------------------


def test_settlement_carries_the_stock_after_arrival() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    for _week in range(6):
        for role in ROLE_ORDER:
            engine.submit_order(role, 7)
        engine.close_week()

    for role in ROLE_ORDER:
        settlement = engine.player_view(role)["settlement"]
        assert settlement["inventory_after_arrival"] == (
            settlement["opening_inventory"] + settlement["arrived"]
        )
        assert settlement["closing_inventory"] == (
            settlement["inventory_after_arrival"] - settlement["shipped"]
        )


def test_a_settlement_saved_before_the_field_existed_still_loads() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    payload = engine.to_payload()
    for settlement in payload["settlements"].values():
        del settlement["inventory_after_arrival"]

    restored = GameEngine.from_payload(payload, engine.config)

    assert restored == engine


def test_player_view_names_the_arrival_week_and_each_slots_weeks_to_go() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    for _week in range(3):
        for role in ROLE_ORDER:
            engine.submit_order(role, 4)
        engine.close_week()

    for role in ROLE_ORDER:
        view = engine.player_view(role)
        assert view["order_arrival_week"] == (
            view["week"] + view["order_arrival_lead_weeks"]
        )
        assert view["supply_line_slot_weeks"] == list(
            range(1, len(view["supply_line_slots"]) + 1)
        )
        assert len(view["orders_in_flight_slot_weeks"]) == len(
            view["orders_in_flight_slots"]
        )
    assert engine.player_view(Role.FACTORY)["orders_in_flight_slot_weeks"] == []


def test_slot_weeks_are_when_each_slot_really_comes_out() -> None:
    """Each slot comes out of the pipeline after exactly as many weekly
    `advance()` calls as `weeks_until_out` says."""
    line = Pipeline(length=3)
    for quantity in (5, 6, 7):
        line.advance()
        line.push(quantity)
    slots = line.slots()
    weeks = line.weeks_until_out()
    assert slots == [5, 6, 7]

    came_out: dict[int, int] = {}
    for week in range(1, 4):
        came_out[week] = line.advance()
        line.push(0)

    for slot, weeks_to_go in zip(slots, weeks, strict=True):
        assert came_out[weeks_to_go] == slot


def test_host_view_weeks_played_is_what_ending_now_keeps() -> None:
    engine = GameEngine.start(get_preset("CLASSIC_MIT"), SEED)
    for _week in range(4):
        for role in ROLE_ORDER:
            engine.submit_order(role, 4)
        engine.close_week()

    assert engine.host_view()["week"] == 5
    assert engine.host_view()["weeks_played"] == 4

    engine.end_early()

    assert engine.phase is GamePhase.FINISHED
    assert engine.host_view()["weeks_played"] == engine.weeks_played == 4
