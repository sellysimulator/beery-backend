"""``join_waiting`` and ``config_update`` -- host authority (``11-socket-lobby.md``).

Covers acceptance criteria 1-9, 20 and 21, and failure modes 4, 5, 6, 7, 8 and
9. Driven entirely through ``app.sockets.handlers.lobby``'s frozen event
handlers plus the shared fixtures from ``tests/conftest.py`` and this
package's own ``conftest.py``. No private name and no internal room-document
shape beyond the one ``09-state-service.md §2`` freezes is asserted on.
"""

from __future__ import annotations

import hmac

import pytest

from app.sockets.handlers.lobby import config_update, join_waiting
from tests.test_sockets.conftest import make_config, new_guest

pytestmark = pytest.mark.asyncio


def _contains_key_recursive(payload, key: str) -> bool:
    """True if ``key`` appears anywhere in ``payload``, at any depth."""
    if isinstance(payload, dict):
        if key in payload:
            return True
        return any(_contains_key_recursive(value, key) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_contains_key_recursive(item, key) for item in payload)
    return False


# --- AC 1 ---------------------------------------------------------------- #


async def test_correct_secret_claims_the_room(
    make_room, fake_socket_manager, register_sid
):
    room = await make_room()
    await register_sid("sid-host")

    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    from app.services.state_service import get_state_service

    stored = await get_state_service().get_room(room["room_code"])
    assert stored["host_sid"] == "sid-host"
    assert fake_socket_manager.events_for(room["room_code"]) == ["lobby_update"]


async def test_wrong_secret_and_no_identity_match_is_rejected(
    make_room, fake_socket_manager, register_sid
):
    room = await make_room()
    await register_sid("sid-bad")

    await join_waiting(
        "sid-bad", {"room_id": room["room_code"], "host_secret": "not-it"}
    )

    from app.services.state_service import get_state_service

    stored = await get_state_service().get_room(room["room_code"])
    assert stored["host_sid"] is None
    assert fake_socket_manager.events_for("sid-bad") == ["join_error"]


# --- AC 2 ------------------------------------------------------------------ #


async def test_secret_claim_records_identity_and_emits_host_claimed(
    make_room, fake_socket_manager, connect_identity
):
    room = await make_room()
    identity = new_guest()
    await connect_identity("sid-host", identity)

    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    from app.services.state_service import get_state_service

    stored = await get_state_service().get_room(room["room_code"])
    assert stored["host_identity"] == identity

    sid_emits = fake_socket_manager.emits_for("sid-host")
    claimed = [data for event, data in sid_emits if event == "host_claimed"]
    assert len(claimed) == 1
    assert claimed[0]["host_secret"] == room["host_secret"]
    assert claimed[0]["room_id"] == room["room_code"]
    # host_claimed never reaches the room.
    assert "host_claimed" not in fake_socket_manager.events_for(room["room_code"])


# --- AC 3 ------------------------------------------------------------------- #


async def test_identity_recovery_reclaims_and_reemits_same_secret(
    make_room, fake_socket_manager, connect_identity
):
    room = await make_room()
    identity = new_guest()
    await connect_identity("sid-host-1", identity)
    await join_waiting(
        "sid-host-1", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    fake_socket_manager.clear()

    await connect_identity("sid-host-2", identity)
    await join_waiting("sid-host-2", {"room_id": room["room_code"]})

    from app.services.state_service import get_state_service

    stored = await get_state_service().get_room(room["room_code"])
    assert stored["host_sid"] == "sid-host-2"

    claimed = [
        data
        for event, data in fake_socket_manager.emits_for("sid-host-2")
        if event == "host_claimed"
    ]
    assert len(claimed) == 1
    assert claimed[0]["host_secret"] == room["host_secret"]


# --- AC 4 -------------------------------------------------------------------- #


async def test_no_secret_from_different_identity_is_rejected(
    make_room, fake_socket_manager, connect_identity
):
    room = await make_room()
    identity_a = new_guest()
    identity_b = new_guest()
    await connect_identity("sid-a", identity_a)
    await join_waiting(
        "sid-a", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    fake_socket_manager.clear()

    await connect_identity("sid-b", identity_b)
    await join_waiting("sid-b", {"room_id": room["room_code"]})

    from app.services.state_service import get_state_service

    stored = await get_state_service().get_room(room["room_code"])
    assert stored["host_sid"] == "sid-a"
    assert fake_socket_manager.events_for("sid-b") == ["join_error"]


# --- AC 5 / failure mode 6 --------------------------------------------------- #


async def test_null_host_identity_does_not_bootstrap(
    make_room, fake_socket_manager, connect_identity
):
    """The single highest-severity bug this handler can produce: a fresh
    room's ``host_identity`` is ``null``, and neither ``None == None`` nor a
    falsy check may treat that as a match."""
    room = await make_room()
    identity = new_guest()
    await connect_identity("sid-stranger", identity)

    await join_waiting("sid-stranger", {"room_id": room["room_code"]})

    from app.services.state_service import get_state_service

    stored = await get_state_service().get_room(room["room_code"])
    assert stored["host_sid"] is None
    assert stored["host_identity"] is None
    assert fake_socket_manager.events_for("sid-stranger") == ["join_error"]


# --- AC 6 --------------------------------------------------------------------- #


async def test_identity_path_never_overwrites_host_identity(
    make_room, fake_socket_manager, connect_identity
):
    room = await make_room()
    identity_a = new_guest()
    await connect_identity("sid-a", identity_a)
    await join_waiting(
        "sid-a", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    identity_b = new_guest()
    await connect_identity("sid-b", identity_b)
    await join_waiting(
        "sid-b", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    from app.services.state_service import get_state_service

    stored = await get_state_service().get_room(room["room_code"])
    # Both claims used the SECRET, so both are legitimate hosts (two tabs);
    # host_identity must still be pinned to whichever claimed it first.
    assert stored["host_identity"] == identity_a


# --- AC 7 --------------------------------------------------------------------- #


async def test_rejection_message_is_identical_for_bad_secret_and_bad_identity(
    make_room, fake_socket_manager, connect_identity
):
    room = await make_room()

    await connect_identity("sid-1", new_guest())
    await join_waiting("sid-1", {"room_id": room["room_code"], "host_secret": "wrong"})
    message_1 = fake_socket_manager.emits_for("sid-1")[0][1]["message"]

    fake_socket_manager.clear()
    await connect_identity("sid-2", new_guest())
    await join_waiting("sid-2", {"room_id": room["room_code"]})
    message_2 = fake_socket_manager.emits_for("sid-2")[0][1]["message"]

    assert message_1 == message_2


# --- AC 8 --------------------------------------------------------------------- #


async def test_join_waiting_twice_leaves_most_recent_as_host_sid(
    make_room, fake_socket_manager, register_sid
):
    room = await make_room()
    await register_sid("sid-1")
    await register_sid("sid-2")

    await join_waiting(
        "sid-1", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    await join_waiting(
        "sid-2", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    from app.services.state_service import get_state_service

    stored = await get_state_service().get_room(room["room_code"])
    assert stored["host_sid"] == "sid-2"


# --- AC 9 --------------------------------------------------------------------- #


async def test_host_holds_no_seat(make_room, fake_socket_manager, register_sid):
    room = await make_room()
    await register_sid("sid-host")

    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    from app.services.state_service import get_state_service

    stored = await get_state_service().get_room(room["room_code"])
    assert stored["participants"] == {}
    assert "sid-host" not in stored["sid_to_alias"]


# --- failure mode 4 ----------------------------------------------------------- #


async def test_empty_secret_never_matches_empty_stored_secret(
    fake_redis, fake_socket_manager, register_sid, state_svc
):
    """``hmac.compare_digest("", "")`` is ``True``, so a bare comparison
    would authorise an empty payload against a room whose stored secret is
    somehow empty. Build exactly that room, and assert the claim is
    rejected regardless."""
    config_room = await state_svc.create_room(make_config(), "Host")
    room_code = config_room["room_code"]
    tampered = await state_svc.get_room(room_code)
    tampered["host_secret"] = ""
    await state_svc.save_room(room_code, tampered)
    await register_sid("sid-1")

    assert hmac.compare_digest("", "") is True  # the trap this guards against

    await join_waiting("sid-1", {"room_id": room_code, "host_secret": ""})
    assert fake_socket_manager.events_for("sid-1") == ["join_error"]

    fake_socket_manager.clear()
    await register_sid("sid-2")
    await join_waiting("sid-2", {"room_id": room_code})
    assert fake_socket_manager.events_for("sid-2") == ["join_error"]


async def test_secret_differing_by_one_character_is_rejected(
    make_room, fake_socket_manager, register_sid
):
    room = await make_room()
    await register_sid("sid-1")
    tampered = room["host_secret"][:-1] + (
        "x" if room["host_secret"][-1] != "x" else "y"
    )

    await join_waiting("sid-1", {"room_id": room["room_code"], "host_secret": tampered})

    assert fake_socket_manager.events_for("sid-1") == ["join_error"]


# --- failure mode 5 ------------------------------------------------------------ #


async def test_second_claimant_with_wrong_secret_does_not_become_host(
    make_room, fake_socket_manager, register_sid
):
    room = await make_room()
    await register_sid("sid-1")
    await register_sid("sid-2")

    await join_waiting(
        "sid-1", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    await join_waiting(
        "sid-2", {"room_id": room["room_code"], "host_secret": "definitely-wrong"}
    )

    from app.services.state_service import get_state_service

    stored = await get_state_service().get_room(room["room_code"])
    assert stored["host_sid"] == "sid-1"


# --- failure mode 7 ------------------------------------------------------------- #


async def test_recovery_path_cannot_seize_a_room(
    make_room, fake_socket_manager, connect_identity
):
    room = await make_room()
    identity_a = new_guest()
    await connect_identity("sid-a", identity_a)
    await join_waiting(
        "sid-a", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    identity_b = new_guest()
    await connect_identity("sid-b", identity_b)
    await join_waiting("sid-b", {"room_id": room["room_code"]})

    from app.services.state_service import get_state_service

    stored = await get_state_service().get_room(room["room_code"])
    assert stored["host_identity"] == identity_a
    assert stored["host_sid"] == "sid-a"


# --- failure mode 8 -------------------------------------------------------------- #


async def test_host_secret_never_appears_in_a_room_broadcast(
    make_room, fake_socket_manager, connect_identity
):
    room = await make_room()
    identity = new_guest()
    await connect_identity("sid-host", identity)

    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    for room_id, event, payload in fake_socket_manager.room_emits:
        assert not _contains_key_recursive(payload, "host_secret"), (room_id, event)

    host_claimed = [
        (sid, event, payload)
        for sid, event, payload in fake_socket_manager.sid_emits
        if event == "host_claimed"
    ]
    assert len(host_claimed) == 1
    assert host_claimed[0][0] == "sid-host"


# --- AC 20 / AC 21 / failure mode 9 (config_update) ------------------------------ #


async def test_config_update_with_right_secret_updates_and_emits_to_host_only(
    make_room, fake_socket_manager, connect_identity
):
    room = await make_room()
    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    fake_socket_manager.clear()

    await config_update(
        "sid-host",
        {
            "room_id": room["room_code"],
            "host_secret": room["host_secret"],
            "config": {"duration_weeks": 20},
        },
    )

    from app.services.state_service import get_state_service

    stored = await get_state_service().get_room(room["room_code"])
    assert stored["config"]["duration_weeks"] == 20

    host_config_updates = [
        payload
        for sid, event, payload in fake_socket_manager.sid_emits
        if event == "config_updated"
    ]
    assert len(host_config_updates) == 1

    for room_id, event, payload in fake_socket_manager.room_emits:
        assert not _contains_key_recursive(payload, "config"), (room_id, event)
    for sid, event, payload in fake_socket_manager.sid_emits:
        if sid != "sid-host":
            assert not _contains_key_recursive(payload, "config"), (sid, event)


async def test_config_update_after_start_is_rejected_and_changes_nothing(
    make_room, fake_socket_manager, connect_identity, state_svc
):
    room = await make_room(bot_fill_empty_roles=True)
    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    running_room = await state_svc.get_room(room["room_code"])
    running_room["state"] = "RUNNING"
    await state_svc.save_room(room["room_code"], running_room)
    before = await state_svc.get_room(room["room_code"])

    await config_update(
        "sid-host",
        {
            "room_id": room["room_code"],
            "host_secret": room["host_secret"],
            "config": {"duration_weeks": 99},
        },
    )

    after = await state_svc.get_room(room["room_code"])
    assert after["config"] == before["config"]
    assert "join_error" in fake_socket_manager.events_for("sid-host")


# --- host refresh resync ------------------------------------------------- #


async def _reclaim_as_refreshed_host(game, connect_identity, fake_socket_manager):
    """A refresh is a new sid for the same identity, with the stored secret."""
    fake_socket_manager.clear()
    await connect_identity("sid-host-refreshed", game["host_identity"])
    await join_waiting(
        "sid-host-refreshed",
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )
    return fake_socket_manager.emits_for("sid-host-refreshed")


def _highest_other_seq(fake_socket_manager) -> int:
    return max(
        data["seq"]
        for _target, event, data in fake_socket_manager.emits
        if event != "host_state" and isinstance(data, dict) and "seq" in data
    )


async def test_reclaim_of_running_room_resends_host_state_last(
    make_room, start_running_game, connect_identity, fake_socket_manager
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)

    emits = await _reclaim_as_refreshed_host(
        game, connect_identity, fake_socket_manager
    )

    host_states = [data for event, data in emits if event == "host_state"]
    assert len(host_states) == 1
    # The client drops any seq it has already seen, so this one must be the
    # newest of the claim or the console stays on its waiting notice.
    assert host_states[0]["seq"] > _highest_other_seq(fake_socket_manager)


async def test_reclaim_of_paused_room_resends_host_state_without_game_started(
    make_room, start_running_game, connect_identity, fake_socket_manager
):
    from app.sockets.handlers.play import pause_game

    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await pause_game(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )

    emits = await _reclaim_as_refreshed_host(
        game, connect_identity, fake_socket_manager
    )

    events = [event for event, _data in emits]
    assert "host_state" in events
    assert "game_started" not in fake_socket_manager.events_for(game["room_code"])
    assert "game_started" not in events


async def test_reclaim_of_finished_room_resends_host_state(
    make_room, start_running_game, connect_identity, fake_socket_manager
):
    from app.sockets.handlers.play import end_game_early

    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    game = await start_running_game(room)
    await end_game_early(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )

    emits = await _reclaim_as_refreshed_host(
        game, connect_identity, fake_socket_manager
    )

    host_states = [data for event, data in emits if event == "host_state"]
    assert len(host_states) == 1
    assert host_states[0]["phase"] == "FINISHED"


async def test_claim_of_lobby_room_sends_no_host_state(
    make_room, fake_socket_manager, register_sid
):
    room = await make_room()
    await register_sid("sid-host")

    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    assert "host_state" not in fake_socket_manager.events_for("sid-host")
