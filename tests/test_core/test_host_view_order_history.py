"""Black-box tests for ``host_view()['order_history']`` (``07 §3.9``,
``CHARTS-PLAN.md`` BE-4).

Driven only through ``GameEngine``'s frozen public surface. Orders are made
distinct per role and per week so an off-by-one, a swapped role or a
sum/duplicate cannot pass by coincidence.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config_models import (
    DEFAULT_LIMITS,
    ConstantDemand,
    FactoryConfig,
    GameConfig,
    RoleConfig,
)
from app.core.enums import ROLE_ORDER, Role
from app.core.game_engine import GameEngine

SEED = 20261102

# `weeks_played` joined on 2026-11-03 (`07 §3.9`): the console's "ending now
# keeps N weeks" reads it instead of working it out from `week`.
HOST_VIEW_KEYS = {
    "week",
    "duration_weeks",
    "weeks_played",
    "phase",
    "currency_symbol",
    "demand_series",
    "awaiting_roles",
    "chain_total_cost",
    "roles",
    "order_history",
}


def make_config(duration_weeks: int = 12) -> GameConfig:
    roles: dict[str, RoleConfig] = {
        Role.RETAILER.value: RoleConfig(),
        Role.WHOLESALER.value: RoleConfig(),
        Role.DISTRIBUTOR.value: RoleConfig(),
        Role.FACTORY.value: FactoryConfig(),
    }
    return GameConfig.from_host_input(
        {
            "roles": roles,
            "demand": ConstantDemand(value=4),
            "duration_weeks": duration_weeks,
        },
        DEFAULT_LIMITS,
    )


def new_engine(duration_weeks: int = 12, bots: frozenset[Role] = frozenset()):
    return GameEngine.start(make_config(duration_weeks), SEED, bot_roles=bots)


def distinct_orders(week: int) -> dict[Role, int]:
    """Orders that differ by role and by week: 10*(role index + 1) + week."""
    return {role: 10 * (role.index + 1) + week for role in ROLE_ORDER}


def play_week(engine: GameEngine, orders: dict[Role, int]) -> None:
    for role in ROLE_ORDER:
        engine.submit_order(role, orders[role])
    engine.close_week()


def history(engine: GameEngine) -> dict[str, list[Any]]:
    return engine.host_view()["order_history"]


def test_fresh_engine_has_all_four_roles_with_empty_lists():
    view = new_engine().host_view()
    assert list(view["order_history"].keys()) == [r.value for r in ROLE_ORDER]
    assert all(view["order_history"][r.value] == [] for r in ROLE_ORDER)


def test_open_week_orders_do_not_appear_until_the_week_closes():
    engine = new_engine()
    for role in ROLE_ORDER:
        engine.submit_order(role, 33)
    assert all(history(engine)[r.value] == [] for r in ROLE_ORDER)
    engine.close_week()
    assert all(history(engine)[r.value] == [33] for r in ROLE_ORDER)


def test_each_role_lists_its_closed_weeks_orders_in_week_order():
    engine = new_engine()
    for week in range(1, 6):
        play_week(engine, distinct_orders(week))
    for role in ROLE_ORDER:
        expected = [distinct_orders(w)[role] for w in range(1, 6)]
        assert history(engine)[role.value] == expected


def test_week_i_plus_1_is_at_index_i_not_shifted():
    engine = new_engine()
    play_week(engine, distinct_orders(1))
    play_week(engine, distinct_orders(2))
    for role in ROLE_ORDER:
        assert history(engine)[role.value][0] == distinct_orders(1)[role]
        assert history(engine)[role.value][1] == distinct_orders(2)[role]
        assert len(history(engine)[role.value]) == 2


def test_history_matches_the_order_on_each_returned_week_record():
    engine = new_engine()
    seen: dict[Role, list[int]] = {r: [] for r in ROLE_ORDER}
    for week in range(1, 5):
        for role in ROLE_ORDER:
            engine.submit_order(role, distinct_orders(week)[role])
        for record in engine.close_week():
            seen[record.role].append(record.order)
    for role in ROLE_ORDER:
        assert history(engine)[role.value] == seen[role]


def test_resubmitting_in_a_week_records_the_final_order_only():
    engine = new_engine()
    for role in ROLE_ORDER:
        engine.submit_order(role, 5)
        engine.submit_order(role, 6 + role.index)
    engine.close_week()
    for role in ROLE_ORDER:
        assert history(engine)[role.value] == [6 + role.index]


def test_forced_week_contributes_the_zero_the_engine_recorded():
    engine = new_engine()
    play_week(engine, distinct_orders(1))
    engine.submit_order(Role.RETAILER, 21)
    engine.submit_order(Role.WHOLESALER, 22)
    # DISTRIBUTOR and FACTORY absent
    records = engine.close_week(force=True)
    assert {r.role: r.was_forced for r in records} == {
        Role.RETAILER: False,
        Role.WHOLESALER: False,
        Role.DISTRIBUTOR: True,
        Role.FACTORY: True,
    }
    assert history(engine)["RETAILER"] == [distinct_orders(1)[Role.RETAILER], 21]
    assert history(engine)["WHOLESALER"] == [distinct_orders(1)[Role.WHOLESALER], 22]
    assert history(engine)["DISTRIBUTOR"] == [distinct_orders(1)[Role.DISTRIBUTOR], 0]
    assert history(engine)["FACTORY"] == [distinct_orders(1)[Role.FACTORY], 0]


def test_bot_week_contributes_the_order_that_was_recorded():
    """The engine refuses an order above the ceiling rather than clamping it,
    so the play loop caps a bot's figure before submitting (`12 §3.2`); the
    history carries exactly what was recorded."""
    engine = new_engine()
    engine.set_bot(Role.WHOLESALER)
    stored = engine.submit_order(Role.WHOLESALER, DEFAULT_LIMITS.max_order_quantity)
    assert stored == DEFAULT_LIMITS.max_order_quantity
    for role in (Role.RETAILER, Role.DISTRIBUTOR, Role.FACTORY):
        engine.submit_order(role, 8)
    records = {r.role: r for r in engine.close_week()}
    assert records[Role.WHOLESALER].was_bot is True
    assert history(engine)["WHOLESALER"] == [records[Role.WHOLESALER].order]
    assert history(engine)["WHOLESALER"] == [DEFAULT_LIMITS.max_order_quantity]
    assert history(engine)["RETAILER"] == [8]


def test_end_early_abandoned_week_never_appears():
    engine = new_engine()
    play_week(engine, distinct_orders(1))
    play_week(engine, distinct_orders(2))
    for role in ROLE_ORDER:
        engine.submit_order(role, 77)  # open week 3, never closed
    engine.end_early()
    for role in ROLE_ORDER:
        assert history(engine)[role.value] == [
            distinct_orders(1)[role],
            distinct_orders(2)[role],
        ]


def test_end_early_with_no_closed_week_leaves_empty_lists_for_every_role():
    engine = new_engine()
    engine.end_early()
    assert list(history(engine).keys()) == [r.value for r in ROLE_ORDER]
    assert all(v == [] for v in history(engine).values())


def test_finished_game_has_one_entry_per_week_for_every_role():
    engine = new_engine(duration_weeks=8)
    for week in range(1, 9):
        play_week(engine, distinct_orders(week))
    for role in ROLE_ORDER:
        assert history(engine)[role.value] == [
            distinct_orders(w)[role] for w in range(1, 9)
        ]


def test_values_are_plain_ints_in_plain_lists():
    engine = new_engine()
    play_week(engine, distinct_orders(1))
    for role in ROLE_ORDER:
        entries = history(engine)[role.value]
        assert isinstance(entries, list)
        assert all(type(x) is int for x in entries)


def test_no_other_host_view_key_is_added_or_removed():
    engine = new_engine()
    assert set(engine.host_view().keys()) == HOST_VIEW_KEYS
    play_week(engine, distinct_orders(1))
    assert set(engine.host_view().keys()) == HOST_VIEW_KEYS


def test_returned_history_is_a_snapshot_not_a_live_handle():
    engine = new_engine()
    play_week(engine, distinct_orders(1))
    snapshot = history(engine)
    snapshot["RETAILER"].append(999)
    assert history(engine)["RETAILER"] == [distinct_orders(1)[Role.RETAILER]]


# --- player_view never carries it (AC 3) ---------------------------------- #


@pytest.mark.parametrize("with_history", [True, False])
def test_player_view_never_contains_order_history(with_history):
    engine = new_engine()
    for week in range(1, 4):
        play_week(engine, distinct_orders(week))

    def keys_of(value: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for k, v in value.items():
                found.add(k)
                found |= keys_of(v)
        elif isinstance(value, (list, tuple)):
            for item in value:
                found |= keys_of(item)
        return found

    for role in ROLE_ORDER:
        view = engine.player_view(role, with_history=with_history)
        assert "order_history" not in keys_of(view)


def test_player_view_without_history_has_no_order_history_before_first_close():
    engine = new_engine()
    for role in ROLE_ORDER:
        assert "order_history" not in engine.player_view(role)


# --- derived, not stored (round trip) -------------------------------------- #


def test_order_history_is_not_part_of_the_persisted_payload():
    engine = new_engine()
    play_week(engine, distinct_orders(1))
    assert "order_history" not in engine.to_payload()


def test_order_history_survives_a_payload_round_trip_and_equality_holds():
    config = make_config()
    engine = GameEngine.start(config, SEED)
    for week in range(1, 5):
        play_week(engine, distinct_orders(week))
    restored = GameEngine.from_payload(engine.to_payload(), config)
    assert restored == engine
    assert restored.host_view()["order_history"] == engine.host_view()["order_history"]
    for role in ROLE_ORDER:
        assert restored.host_view()["order_history"][role.value] == [
            distinct_orders(w)[role] for w in range(1, 5)
        ]


def test_a_restored_engine_keeps_extending_the_history():
    config = make_config()
    engine = GameEngine.start(config, SEED)
    play_week(engine, distinct_orders(1))
    restored = GameEngine.from_payload(engine.to_payload(), config)
    play_week(restored, distinct_orders(2))
    for role in ROLE_ORDER:
        assert restored.host_view()["order_history"][role.value] == [
            distinct_orders(1)[role],
            distinct_orders(2)[role],
        ]
