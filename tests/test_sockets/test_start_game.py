"""``start_game`` and ``can_start`` (``11-socket-lobby.md §2.2``, ``§3.6``).

Covers acceptance criteria 27-32, 28a, 28b, 29, and failure modes 12, 13, 15
and 17.
"""

from __future__ import annotations

import asyncio

import pytest

from app.sockets.handlers.lobby import claim_role, join, join_waiting, start_game
from tests.test_sockets.conftest import new_guest

pytestmark = pytest.mark.asyncio


def _contains_key_recursive(payload, key: str) -> bool:
    if isinstance(payload, dict):
        if key in payload:
            return True
        return any(_contains_key_recursive(value, key) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_contains_key_recursive(item, key) for item in payload)
    return False


async def _seat_n(make_room, connect_identity, n, **config_kwargs):
    room = await make_room(**config_kwargs)
    for i in range(n):
        sid = f"sid-{i}"
        await connect_identity(sid, new_guest())
        await join(sid, {"room_id": room["room_code"]})
    return room


# --- AC 27 -------------------------------------------------------------------- #


async def test_start_game_with_three_players_and_bot_fill_creates_one_bot(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room = await _seat_n(
        make_room,
        connect_identity,
        3,
        role_assignment_mode="PLAYER_CHOOSES",
        bot_fill_empty_roles=True,
    )
    for i, role in enumerate(["RETAILER", "WHOLESALER", "DISTRIBUTOR"]):
        await claim_role(f"sid-{i}", {"room_id": room["room_code"], "role": role})

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] == "RUNNING"
    bot_participants = [p for p in stored["participants"].values() if p["is_bot"]]
    assert len(bot_participants) == 1
    assert bot_participants[0]["role"] == "FACTORY"
    assert bot_participants[0]["display_name"] == "Factory (bot)"
    assert "game_started" in fake_socket_manager.events_for(room["room_code"])


# --- AC 28 -------------------------------------------------------------------- #


async def test_start_game_with_three_players_no_bot_fill_is_rejected_with_matching_reason(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room = await _seat_n(
        make_room,
        connect_identity,
        3,
        role_assignment_mode="PLAYER_CHOOSES",
        bot_fill_empty_roles=False,
    )
    for i, role in enumerate(["RETAILER", "WHOLESALER", "DISTRIBUTOR"]):
        await claim_role(f"sid-{i}", {"room_id": room["room_code"], "role": role})

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    lobby_updates = [
        payload
        for room_id, event, payload in fake_socket_manager.room_emits
        if event == "lobby_update"
    ]
    last_reason = lobby_updates[-1]["start_blocked_reason"]

    fake_socket_manager.clear()
    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] != "RUNNING"
    assert stored["engine"] is None
    error_message = fake_socket_manager.emits_for("sid-host")[0][1]["message"]
    assert error_message == last_reason
    assert error_message == "One role is still empty. Assign them, or turn on bot fill."


# --- AC 28a ------------------------------------------------------------------- #


async def test_can_start_false_for_unbuildable_config(
    make_room, connect_identity, state_svc
):
    room = await make_room()
    tampered = await state_svc.get_room(room["room_code"])
    tampered["config"] = {"nonsense": True}
    await state_svc.save_room(room["room_code"], tampered)

    from app.services.room_service import RoomService

    ok, reason = RoomService().can_start(await state_svc.get_room(room["room_code"]))
    assert ok is False
    assert reason == "The configuration is not valid yet."

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] != "RUNNING"


# --- AC 28b ------------------------------------------------------------------- #


async def test_random_mode_can_start_true_with_four_roleless_seated(
    make_room, connect_identity, state_svc
):
    room = await _seat_n(
        make_room,
        connect_identity,
        4,
        role_assignment_mode="RANDOM",
        bot_fill_empty_roles=False,
    )
    from app.services.room_service import RoomService

    stored = await state_svc.get_room(room["room_code"])
    ok, reason = RoomService().can_start(stored)
    assert ok is True
    assert reason is None


async def test_random_mode_can_start_false_with_three_seated(
    make_room, connect_identity, state_svc
):
    room = await _seat_n(
        make_room,
        connect_identity,
        3,
        role_assignment_mode="RANDOM",
        bot_fill_empty_roles=False,
    )
    from app.services.room_service import RoomService

    stored = await state_svc.get_room(room["room_code"])
    ok, _reason = RoomService().can_start(stored)
    assert ok is False


# --- AC 29 / failure mode 13 --------------------------------------------------- #


async def test_random_deal_is_reproducible_for_the_same_seed(
    make_room, connect_identity, state_svc
):
    room_a = await _seat_n(
        make_room, connect_identity, 4, role_assignment_mode="RANDOM", random_seed=4242
    )
    room_b_config_room = await make_room(
        role_assignment_mode="RANDOM", random_seed=4242
    )
    for i in range(4):
        sid = f"sidb-{i}"
        await connect_identity(sid, new_guest())
        await join(sid, {"room_id": room_b_config_room["room_code"]})

    await connect_identity("sid-host-a", new_guest())
    await join_waiting(
        "sid-host-a",
        {"room_id": room_a["room_code"], "host_secret": room_a["host_secret"]},
    )
    await start_game(
        "sid-host-a",
        {"room_id": room_a["room_code"], "host_secret": room_a["host_secret"]},
    )

    await connect_identity("sid-host-b", new_guest())
    await join_waiting(
        "sid-host-b",
        {
            "room_id": room_b_config_room["room_code"],
            "host_secret": room_b_config_room["host_secret"],
        },
    )
    await start_game(
        "sid-host-b",
        {
            "room_id": room_b_config_room["room_code"],
            "host_secret": room_b_config_room["host_secret"],
        },
    )

    stored_a = await state_svc.get_room(room_a["room_code"])
    stored_b = await state_svc.get_room(room_b_config_room["room_code"])
    assert stored_a["role_to_alias"] == stored_b["role_to_alias"]
    assert all(alias is not None for alias in stored_a["role_to_alias"].values())


async def test_random_deal_differs_for_different_seeds_over_several_trials(
    make_room, connect_identity, state_svc
):
    outcomes = set()
    for trial_seed in range(5):
        room = await _seat_n(
            make_room,
            connect_identity,
            4,
            role_assignment_mode="RANDOM",
            random_seed=1000 + trial_seed,
        )
        await connect_identity(f"sid-host-{trial_seed}", new_guest())
        await join_waiting(
            f"sid-host-{trial_seed}",
            {"room_id": room["room_code"], "host_secret": room["host_secret"]},
        )
        await start_game(
            f"sid-host-{trial_seed}",
            {"room_id": room["room_code"], "host_secret": room["host_secret"]},
        )
        stored = await state_svc.get_room(room["room_code"])
        outcomes.add(tuple(sorted(stored["role_to_alias"].items())))

    assert len(outcomes) > 1


# --- AC 30 -------------------------------------------------------------------- #


async def test_after_start_state_running_engine_populated_week_one(
    make_room, connect_identity, state_svc
):
    room = await _seat_n(
        make_room, connect_identity, 4, role_assignment_mode="PLAYER_CHOOSES"
    )
    for i, role in enumerate(["RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY"]):
        await claim_role(f"sid-{i}", {"room_id": room["room_code"], "role": role})

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] == "RUNNING"
    engine = state_svc.load_engine(stored)
    assert engine is not None
    assert engine.week == 1


# --- AC 31 / failure mode 15 --------------------------------------------------- #


async def test_start_game_twice_is_a_no_op_the_second_time(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room = await _seat_n(
        make_room, connect_identity, 4, role_assignment_mode="PLAYER_CHOOSES"
    )
    for i, role in enumerate(["RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY"]):
        await claim_role(f"sid-{i}", {"room_id": room["room_code"], "role": role})

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    stored = await state_svc.get_room(room["room_code"])
    started_at = stored["started_at"]

    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    stored_again = await state_svc.get_room(room["room_code"])
    assert stored_again["started_at"] == started_at
    assert stored_again["engine"] == stored["engine"]

    game_started_count = sum(
        1
        for _room_id, event, _payload in fake_socket_manager.room_emits
        if event == "game_started"
    )
    assert game_started_count == 1


async def test_concurrent_start_game_calls_produce_one_engine_and_one_broadcast(
    make_room, connect_identity, fake_socket_manager, state_svc, monkeypatch
):
    room = await _seat_n(
        make_room, connect_identity, 4, role_assignment_mode="PLAYER_CHOOSES"
    )
    for i, role in enumerate(["RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY"]):
        await claim_role(f"sid-{i}", {"room_id": room["room_code"], "role": role})

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    original_get_room = state_svc.get_room

    async def slow_get_room(room_code):
        result = await original_get_room(room_code)
        await asyncio.sleep(0)
        return result

    monkeypatch.setattr(state_svc, "get_room", slow_get_room)

    await asyncio.gather(
        start_game(
            "sid-host",
            {"room_id": room["room_code"], "host_secret": room["host_secret"]},
        ),
        start_game(
            "sid-host",
            {"room_id": room["room_code"], "host_secret": room["host_secret"]},
        ),
    )

    stored = await original_get_room(room["room_code"])
    assert stored["state"] == "RUNNING"
    engine = state_svc.load_engine(stored)
    assert engine.week == 1

    game_started_count = sum(
        1
        for _room_id, event, _payload in fake_socket_manager.room_emits
        if event == "game_started"
    )
    assert game_started_count == 1


# --- AC 32 -------------------------------------------------------------------- #


async def test_game_started_config_public_has_no_cost_delay_or_demand(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room = await _seat_n(
        make_room, connect_identity, 4, role_assignment_mode="PLAYER_CHOOSES"
    )
    for i, role in enumerate(["RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY"]):
        await claim_role(f"sid-{i}", {"room_id": room["room_code"], "role": role})

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    game_started = next(
        payload
        for _room_id, event, payload in fake_socket_manager.room_emits
        if event == "game_started"
    )
    config_public = game_started["config_public"]

    # The exact shape §3.6 gives: top-level keys, and the visibility
    # sub-object's keys. No cost, delay or demand *value* field is
    # anywhere in it -- `show_running_cost_to_players` is a visibility
    # toggle, not a cost figure, and is the one place "cost" legitimately
    # appears in the key names.
    assert set(config_public.keys()) == {
        "duration_weeks",
        "stage_count",
        "currency_symbol",
        "visibility",
    }
    assert set(config_public["visibility"].keys()) == {
        "show_true_customer_demand_to_all",
        "show_neighbour_inventory",
        "show_all_inventories",
        "show_supply_line_prominently",
        "show_running_cost_to_players",
        "show_leaderboard_during_game",
        "max_order_quantity",
        "allow_negative_orders",
    }
    forbidden_fields = (
        "holding_cost_per_unit_week",
        "backlog_cost_per_unit_week",
        "fixed_order_cost",
        "unit_purchase_cost",
        "starting_capital",
        "shipping_delay_weeks",
        "information_delay_weeks",
        "production_delay_weeks",
        "random_seed",
        "kind",
        "value",
        "mean",
        "stdev",
    )
    import json

    serialised = json.dumps(config_public)
    for field in forbidden_fields:
        assert f'"{field}"' not in serialised, field


# --- failure mode 12 ----------------------------------------------------------- #


async def test_silent_start_with_an_empty_link_never_starts(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    room = await _seat_n(
        make_room,
        connect_identity,
        2,
        role_assignment_mode="PLAYER_CHOOSES",
        bot_fill_empty_roles=False,
    )
    await claim_role("sid-0", {"room_id": room["room_code"], "role": "RETAILER"})
    await claim_role("sid-1", {"room_id": room["room_code"], "role": "WHOLESALER"})

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    stored = await state_svc.get_room(room["room_code"])
    assert stored["state"] != "RUNNING"
    assert stored["engine"] is None


# --- failure mode 17 ------------------------------------------------------------ #


async def test_start_game_refuses_with_can_starts_exact_sentence(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    """A content-based refusal carries `can_start`'s own sentence, verbatim.

    A second implementation of the rule inside `start_game` passes every
    other criterion in this section and then drifts from the sentence the
    host's disabled Start button is already showing them.
    """
    room = await _seat_n(
        make_room, connect_identity, 4, role_assignment_mode="PLAYER_CHOOSES"
    )
    for i, role in enumerate(["RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY"]):
        await claim_role(f"sid-{i}", {"room_id": room["room_code"], "role": role})

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    tampered = await state_svc.get_room(room["room_code"])
    tampered["config"] = {"nonsense": True}
    await state_svc.save_room(room["room_code"], tampered)

    from app.services.room_service import RoomService

    expected_ok, expected_reason = RoomService().can_start(
        await state_svc.get_room(room["room_code"])
    )
    assert expected_ok is False

    fake_socket_manager.clear()
    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    got_message = fake_socket_manager.emits_for("sid-host")[0][1]["message"]
    assert got_message == expected_reason == "The configuration is not valid yet."
    assert (await state_svc.get_room(room["room_code"]))["engine"] is None


async def test_start_game_on_an_already_started_room_is_silent(
    make_room, connect_identity, fake_socket_manager, state_svc
):
    """AC 31: a double-click is not an error, so it gets no `join_error`."""
    room = await _seat_n(
        make_room, connect_identity, 4, role_assignment_mode="PLAYER_CHOOSES"
    )
    for i, role in enumerate(["RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY"]):
        await claim_role(f"sid-{i}", {"room_id": room["room_code"], "role": role})

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    fake_socket_manager.clear()
    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    assert fake_socket_manager.emits_for("sid-host") == []
    assert fake_socket_manager.room_emits == []
