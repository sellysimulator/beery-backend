"""``submit_order`` (``12-socket-play.md §3.1``).

Covers acceptance criteria 1-9 and failure modes 4, 5, 6 and 7.

Scope is exactly section 12's frozen *Public surface*: the ``submit_order``
socket handler in ``app.sockets.handlers.play``, plus the room document and
``GameEngine`` attributes ``09-state-service.md §2`` and
``07-game-engine.md §2`` already freeze (``engine.week``,
``engine.pending_orders``). No private name, no internal data structure and
no unspecified log message is asserted (``00-conventions.md §5``).

A ``RUNNING`` game is reached only by driving section 11's lobby handlers
(``join``, ``claim_role``, ``join_waiting``, ``start_game``) through the
shared ``start_running_game`` fixture -- ``app/sockets/handlers/play.py`` and
``app/services/game_service.py`` are never read, only imported.
"""

from __future__ import annotations

import math

import pytest

from app.core.enums import Role
from app.sockets.handlers.play import submit_order
from tests.test_sockets.conftest import new_guest

pytestmark = pytest.mark.asyncio


async def _current_engine(state_svc, room_code: str):
    stored = await state_svc.get_room(room_code)
    return state_svc.load_engine(stored)


# --- AC 1 --------------------------------------------------------------- #


async def test_submit_records_order_and_emits_order_submitted_to_room(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    engine = await _current_engine(state_svc, game["room_code"])
    week = engine.week
    fake_socket_manager.clear()

    await submit_order(
        game["sids_by_role"]["RETAILER"],
        {"room_id": game["room_code"], "week": week, "order": 7},
    )

    engine = await _current_engine(state_svc, game["room_code"])
    assert engine.pending_orders[Role.RETAILER] == 7
    assert "order_submitted" in fake_socket_manager.events_for(game["room_code"])


# --- AC 2 --------------------------------------------------------------- #


async def test_order_submitted_has_role_and_display_name_never_a_quantity(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    engine = await _current_engine(state_svc, game["room_code"])
    fake_socket_manager.clear()

    await submit_order(
        game["sids_by_role"]["RETAILER"],
        {"room_id": game["room_code"], "week": engine.week, "order": 13},
    )

    payload = next(
        data
        for room_id, event, data in fake_socket_manager.room_emits
        if event == "order_submitted"
    )
    assert set(payload.keys()) == {"seq", "week", "role", "display_name", "is_bot"}
    assert payload["role"] == "RETAILER"
    assert isinstance(payload["display_name"], str)
    assert "order" not in payload
    assert "quantity" not in payload
    assert 13 not in payload.values()


# --- AC 3 --------------------------------------------------------------- #


async def test_resubmission_replaces_value_and_all_orders_in_needs_four_roles(
    make_room, start_running_game, state_svc
):
    """AC 3's second half guards against a wrong implementation that counts
    *submissions* rather than *distinct roles*: two submissions from the same
    role plus two more roles is four total submissions but only three
    distinct roles, and ``all_orders_in`` must still be ``False``. (Checking
    it after all four *roles* have ordered is not observable here: that
    submission is the one that auto-closes the week, per §3.1 step 8, so by
    the time control returns the engine has already moved on to the next
    week's empty ``pending_orders`` -- AC 10/11 in ``test_week_broadcast.py``
    cover that closing behaviour directly.)"""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    engine = await _current_engine(state_svc, game["room_code"])
    week = engine.week
    sid = game["sids_by_role"]["RETAILER"]

    await submit_order(sid, {"room_id": game["room_code"], "week": week, "order": 5})
    await submit_order(sid, {"room_id": game["room_code"], "week": week, "order": 9})

    engine = await _current_engine(state_svc, game["room_code"])
    assert engine.pending_orders[Role.RETAILER] == 9
    assert engine.all_orders_in() is False  # only one of four roles has ordered

    for role in ("WHOLESALER", "DISTRIBUTOR"):
        await submit_order(
            game["sids_by_role"][role],
            {"room_id": game["room_code"], "week": week, "order": 6},
        )
    engine = await _current_engine(state_svc, game["room_code"])
    # Four submissions have now happened (two of them RETAILER's), but only
    # three distinct roles -- must not be mistaken for "all in".
    assert engine.all_orders_in() is False
    assert engine.week == week  # the window is still open


# --- AC 4 --------------------------------------------------------------- #


async def test_21st_submission_rejected_20th_stands(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    engine = await _current_engine(state_svc, game["room_code"])
    week = engine.week
    sid = game["sids_by_role"]["RETAILER"]

    for i in range(1, 21):
        await submit_order(
            sid, {"room_id": game["room_code"], "week": week, "order": i}
        )
    engine = await _current_engine(state_svc, game["room_code"])
    assert engine.pending_orders[Role.RETAILER] == 20

    fake_socket_manager.clear()
    await submit_order(sid, {"room_id": game["room_code"], "week": week, "order": 999})

    engine = await _current_engine(state_svc, game["room_code"])
    assert engine.pending_orders[Role.RETAILER] == 20  # last accepted value stands
    error = next(
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "error" and target == sid
    )
    assert error["code"] == "TOO_MANY_SUBMISSIONS"


# --- AC 5 --------------------------------------------------------------- #


async def test_stale_week_is_rejected_and_triggers_your_state_resend(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    engine = await _current_engine(state_svc, game["room_code"])
    week = engine.week
    sid = game["sids_by_role"]["RETAILER"]
    fake_socket_manager.clear()

    await submit_order(
        sid, {"room_id": game["room_code"], "week": week - 1, "order": 4}
    )

    engine = await _current_engine(state_svc, game["room_code"])
    assert Role.RETAILER not in engine.pending_orders

    sid_events = [
        (event, data)
        for target, event, data in fake_socket_manager.sid_emits
        if target == sid
    ]
    error = next(data for event, data in sid_events if event == "error")
    assert error["code"] == "STALE_WEEK"
    assert any(event == "your_state" for event, _data in sid_events)


# --- AC 6 --------------------------------------------------------------- #


@pytest.mark.parametrize("bad_order", ["abc", None, math.nan, math.inf, True, 7.5])
async def test_invalid_order_is_rejected_and_records_nothing(
    make_room, start_running_game, fake_socket_manager, state_svc, bad_order
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    engine = await _current_engine(state_svc, game["room_code"])
    week = engine.week
    sid = game["sids_by_role"]["RETAILER"]
    fake_socket_manager.clear()

    await submit_order(
        sid, {"room_id": game["room_code"], "week": week, "order": bad_order}
    )

    engine = await _current_engine(state_svc, game["room_code"])
    assert Role.RETAILER not in engine.pending_orders
    error = next(
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "error" and target == sid
    )
    assert error["code"] == "INVALID_ORDER"


# --- AC 7 --------------------------------------------------------------- #


@pytest.mark.parametrize("huge", [1_000_000_000, 1e9])
async def test_huge_order_is_clamped_to_9999(
    make_room, start_running_game, fake_socket_manager, state_svc, huge
):
    """``your_state`` is frozen as ``{seq, ...player_view(role)}``, and
    ``player_view`` carries ``has_submitted``/``awaiting_roles`` but no
    pending-order-quantity field -- there is nothing for the clamped value to
    be echoed in. The clamp is asserted on the record instead: ``has_submitted``
    flips ``True`` in the submitter's refreshed ``your_state``, and the
    week's ``your_week_closed`` record for that role carries ``order ==
    9999`` once the week closes."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    engine = await _current_engine(state_svc, game["room_code"])
    week = engine.week
    sid = game["sids_by_role"]["RETAILER"]
    fake_socket_manager.clear()

    await submit_order(sid, {"room_id": game["room_code"], "week": week, "order": huge})

    engine = await _current_engine(state_svc, game["room_code"])
    assert engine.pending_orders[Role.RETAILER] == 9_999

    your_state_payload = next(
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "your_state" and target == sid
    )
    assert your_state_payload["has_submitted"] is True

    for role in ("WHOLESALER", "DISTRIBUTOR", "FACTORY"):
        await submit_order(
            game["sids_by_role"][role],
            {"room_id": game["room_code"], "week": week, "order": 4},
        )

    engine_after = await _current_engine(state_svc, game["room_code"])
    retailer_record = next(
        r for r in engine_after.history if r.week == week and r.role == Role.RETAILER
    )
    assert retailer_record.order == 9_999


# --- AC 8 --------------------------------------------------------------- #


async def test_submit_from_sid_with_no_alias_is_rejected(
    make_room, start_running_game, connect_identity, fake_socket_manager, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    engine = await _current_engine(state_svc, game["room_code"])
    week = engine.week

    stranger_sid = "sid-stranger"
    await connect_identity(stranger_sid, new_guest())
    fake_socket_manager.clear()

    await submit_order(
        stranger_sid, {"room_id": game["room_code"], "week": week, "order": 3}
    )

    error = next(
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "error" and target == stranger_sid
    )
    assert error["code"] == "NOT_IN_ROOM"
    engine = await _current_engine(state_svc, game["room_code"])
    assert len(engine.pending_orders) == 0


# --- AC 9 --------------------------------------------------------------- #


async def test_roleless_participant_cannot_order(
    make_room, start_running_game, fake_socket_manager, state_svc
):
    """§3.1 step 3: a roleless participant is rejected with `error
    {code: "NO_ROLE"}`.

    A first attempt at this test seated a never-role-claiming bystander
    alongside three role-claiming humans plus a bot-filled fourth role, on
    the theory that ``seats_taken`` counting participants rather than roles
    (``11-socket-lobby.md §2.1``) would let all five coexist. Run against the
    real implementation, that bystander's ``submit_order`` came back
    `NOT_IN_ROOM`, not `NO_ROLE`: whatever section 11's `start_game` does
    with a participant who never claimed a seat, they are no longer found
    by `room["sid_to_alias"]` once the game is `RUNNING`, which is section
    11's behaviour and out of scope here to read or second-guess. That
    means "seated, in `sid_to_alias`, but `role: None`" is not a state a
    normal sequence of public lobby calls reaches during `RUNNING` -- so it
    is built directly against the frozen room-document schema
    (`09-state-service.md §2`, `"role": ... // or null while unassigned`),
    exactly as section 11's own tests build an unreachable-by-sequence edge
    case (e.g. `test_can_start_false_for_unbuildable_config`'s tampered
    ``config``)."""
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    retailer_sid = game["sids_by_role"]["RETAILER"]
    retailer_alias = game["alias_by_role"]["RETAILER"]

    stored = await state_svc.get_room(game["room_code"])
    stored["participants"][retailer_alias]["role"] = None
    stored["role_to_alias"][
        "RETAILER"
    ] = None  # keep the document internally consistent
    await state_svc.save_room(game["room_code"], stored)

    engine = await _current_engine(state_svc, game["room_code"])
    week = engine.week
    before_orders = dict(engine.pending_orders)
    fake_socket_manager.clear()

    await submit_order(
        retailer_sid, {"room_id": game["room_code"], "week": week, "order": 2}
    )

    error = next(
        data
        for target, event, data in fake_socket_manager.sid_emits
        if event == "error" and target == retailer_sid
    )
    assert error["code"] == "NO_ROLE"
    engine = await _current_engine(state_svc, game["room_code"])
    assert dict(engine.pending_orders) == before_orders


# --- failure mode 4 -------------------------------------------------------- #


async def test_client_supplied_alias_is_ignored(
    make_room, start_running_game, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    engine = await _current_engine(state_svc, game["room_code"])
    week = engine.week
    retailer_alias = game["alias_by_role"]["RETAILER"]
    wholesaler_alias = game["alias_by_role"]["WHOLESALER"]
    assert retailer_alias != wholesaler_alias

    await submit_order(
        game["sids_by_role"]["RETAILER"],
        {
            "room_id": game["room_code"],
            "week": week,
            "order": 11,
            "alias": wholesaler_alias,
        },
    )

    engine = await _current_engine(state_svc, game["room_code"])
    assert engine.pending_orders[Role.RETAILER] == 11
    assert Role.WHOLESALER not in engine.pending_orders


# --- failure mode 5 -------------------------------------------------------- #


async def test_client_supplied_accumulated_cost_is_ignored(
    make_room, start_running_game, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    engine = await _current_engine(state_svc, game["room_code"])
    week = engine.week
    before = engine.agents[Role.RETAILER].accumulated_cost

    await submit_order(
        game["sids_by_role"]["RETAILER"],
        {
            "room_id": game["room_code"],
            "week": week,
            "order": 6,
            "accumulated_cost": 0,
        },
    )

    engine = await _current_engine(state_svc, game["room_code"])
    assert engine.agents[Role.RETAILER].accumulated_cost == before


# --- failure mode 6 -------------------------------------------------------- #


async def test_stale_tab_double_week_leaves_earlier_week_untouched(
    make_room, start_running_game, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    week4_engine = await _current_engine(state_svc, game["room_code"])
    week4 = week4_engine.week

    # Close week 4 by having every role order, opening week 5.
    for role in ("RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY"):
        await submit_order(
            game["sids_by_role"][role],
            {"room_id": game["room_code"], "week": week4, "order": 4},
        )
    engine = await _current_engine(state_svc, game["room_code"])
    assert engine.week == week4 + 1
    week4_record = next(r for r in engine.history if r.week == week4)
    assert week4_record.order == 4

    # A stale client still believes week 4 is open.
    await submit_order(
        game["sids_by_role"]["RETAILER"],
        {"room_id": game["room_code"], "week": week4, "order": 999},
    )

    engine = await _current_engine(state_svc, game["room_code"])
    week4_record_after = next(r for r in engine.history if r.week == week4)
    assert week4_record_after == week4_record
    assert week4_record_after.order == 4


# --- failure mode 7 -------------------------------------------------------- #


async def test_resubmission_storm_does_not_exceed_the_cap_or_append_history(
    make_room, start_running_game, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    engine = await _current_engine(state_svc, game["room_code"])
    week = engine.week
    sid = game["sids_by_role"]["RETAILER"]

    for i in range(500):
        await submit_order(
            sid, {"room_id": game["room_code"], "week": week, "order": i}
        )

    engine = await _current_engine(state_svc, game["room_code"])
    assert engine.pending_orders[Role.RETAILER] == 19  # the 20th accepted value (i=19)
    assert engine.week == week  # the week never closed from one role alone
    assert engine.history == []  # nothing was appended by the storm
