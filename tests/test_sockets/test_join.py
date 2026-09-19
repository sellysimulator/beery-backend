"""``join`` and ``leave`` -- players arriving, reconnecting and departing
(``11-socket-lobby.md``).

Covers acceptance criteria 10-19 and failure modes 1, 2, 3, 14 and 16.
"""

from __future__ import annotations

import pytest

from app.sockets.handlers.lobby import join, join_waiting, leave, start_game
from tests.test_sockets.conftest import new_guest

pytestmark = pytest.mark.asyncio


async def _room_after(state_svc, room_code):
    return await state_svc.get_room(room_code)


# --- AC 10 ------------------------------------------------------------------ #


async def test_first_join_creates_participant_and_emits_session_token(
    make_room, fake_socket_manager, connect_identity, state_svc
):
    room = await make_room()
    await connect_identity("sid-1", new_guest())

    await join("sid-1", {"room_id": room["room_code"], "display_name": "Ana"})

    stored = await _room_after(state_svc, room["room_code"])
    assert list(stored["participants"].keys()) == ["P1"]

    joined = [
        payload
        for sid, event, payload in fake_socket_manager.sid_emits
        if event == "joined"
    ]
    assert len(joined) == 1
    assert joined[0]["alias"] == "P1"
    assert isinstance(joined[0]["session_token"], str) and joined[0]["session_token"]


# --- AC 11 / failure mode 1 -------------------------------------------------- #


async def test_one_identity_four_sids_no_token_yields_one_participant(
    make_room, fake_socket_manager, connect_identity, state_svc
):
    """THE regression test: one identity joining on four different sids,
    none carrying a session_token, must never fill the room."""
    room = await make_room()
    identity = new_guest()

    aliases = []
    for i in range(4):
        sid = f"sid-{i}"
        await connect_identity(sid, identity)
        await join(sid, {"room_id": room["room_code"]})
        stored = await _room_after(state_svc, room["room_code"])
        assert len(stored["participants"]) == 1
        aliases.append(next(iter(stored["participants"].keys())))

    assert len(set(aliases)) == 1


# --- AC 12 -------------------------------------------------------------------- #


async def test_four_distinct_identities_get_four_distinct_aliases(
    make_room, connect_identity, state_svc
):
    room = await make_room()
    for i in range(4):
        sid = f"sid-{i}"
        await connect_identity(sid, new_guest())
        await join(sid, {"room_id": room["room_code"]})

    stored = await _room_after(state_svc, room["room_code"])
    assert len(stored["participants"]) == 4
    assert len(set(stored["participants"].keys())) == 4


# --- AC 13 -------------------------------------------------------------------- #


async def test_guest_and_firebase_uid_are_two_distinct_participants(
    make_room, connect_identity, state_svc
):
    room = await make_room()
    await connect_identity("sid-guest", new_guest())
    await connect_identity("sid-user", "firebase-uid-123")

    await join("sid-guest", {"room_id": room["room_code"]})
    await join("sid-user", {"room_id": room["room_code"]})

    stored = await _room_after(state_svc, room["room_code"])
    assert len(stored["participants"]) == 2
    identities = {p["identity"] for p in stored["participants"].values()}
    assert len(identities) == 2

    # A second, unrelated guest never collides with the first guest's alias.
    await connect_identity("sid-guest-2", new_guest())
    await join("sid-guest-2", {"room_id": room["room_code"]})
    stored = await _room_after(state_svc, room["room_code"])
    assert len(stored["participants"]) == 3


# --- AC 14 -------------------------------------------------------------------- #


async def test_fifth_distinct_identity_is_rejected_as_full(
    make_room, fake_socket_manager, connect_identity, state_svc
):
    room = await make_room()
    for i in range(4):
        sid = f"sid-{i}"
        await connect_identity(sid, new_guest())
        await join(sid, {"room_id": room["room_code"]})

    fake_socket_manager.clear()
    await connect_identity("sid-5", new_guest())
    await join("sid-5", {"room_id": room["room_code"]})

    stored = await _room_after(state_svc, room["room_code"])
    assert len(stored["participants"]) == 4

    errors = [
        payload
        for sid, event, payload in fake_socket_manager.sid_emits
        if event == "join_error"
    ]
    assert len(errors) == 1
    assert "full" in errors[0]["message"].lower()


# --- AC 15 -------------------------------------------------------------------- #


async def test_valid_session_token_same_identity_reconnects_same_alias_and_token(
    make_room, fake_socket_manager, connect_identity, state_svc
):
    room = await make_room()
    identity = new_guest()
    await connect_identity("sid-1", identity)
    await join("sid-1", {"room_id": room["room_code"]})
    stored = await _room_after(state_svc, room["room_code"])
    alias = next(iter(stored["participants"].keys()))
    token = stored["participants"][alias]["session_token"]

    fake_socket_manager.clear()
    await connect_identity("sid-2", identity)
    await join("sid-2", {"room_id": room["room_code"], "session_token": token})

    stored = await _room_after(state_svc, room["room_code"])
    assert list(stored["participants"].keys()) == [alias]
    joined = [
        payload
        for sid, event, payload in fake_socket_manager.sid_emits
        if event == "joined"
    ]
    assert joined[0]["session_token"] == token
    assert joined[0]["alias"] == alias


# --- AC 16 -------------------------------------------------------------------- #


async def test_valid_token_from_different_identity_does_not_reconnect(
    make_room, connect_identity, state_svc
):
    room = await make_room()
    identity_a = new_guest()
    await connect_identity("sid-1", identity_a)
    await join("sid-1", {"room_id": room["room_code"]})
    stored = await _room_after(state_svc, room["room_code"])
    alias_a = next(iter(stored["participants"].keys()))
    token_a = stored["participants"][alias_a]["session_token"]

    identity_b = new_guest()
    await connect_identity("sid-2", identity_b)
    await join("sid-2", {"room_id": room["room_code"], "session_token": token_a})

    stored = await _room_after(state_svc, room["room_code"])
    assert len(stored["participants"]) == 2
    assert stored["participants"][alias_a]["identity"] == identity_a


# --- AC 17 -------------------------------------------------------------------- #


async def test_lost_token_rejoins_own_seat_by_identity_and_resyncs(
    make_room, fake_socket_manager, connect_identity, state_svc
):
    room = await make_room(
        role_assignment_mode="PLAYER_CHOOSES",
        bot_fill_empty_roles=True,
        duration_weeks=8,
    )
    identity = new_guest()
    await connect_identity("sid-1", identity)
    await join("sid-1", {"room_id": room["room_code"]})

    from app.sockets.handlers.lobby import claim_role

    await claim_role("sid-1", {"room_id": room["room_code"], "role": "RETAILER"})

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    fake_socket_manager.clear()
    await connect_identity("sid-2", identity)  # same identity, no session_token
    await join("sid-2", {"room_id": room["room_code"]})

    events = fake_socket_manager.events_for("sid-2")
    assert "join_error" not in events
    assert "game_started" in events
    assert "your_state" in events


# --- AC 18 -------------------------------------------------------------------- #


async def test_leave_before_start_removes_participant_and_frees_role(
    make_room, fake_socket_manager, connect_identity, state_svc
):
    room = await make_room(role_assignment_mode="PLAYER_CHOOSES")
    identity = new_guest()
    await connect_identity("sid-1", identity)
    await join("sid-1", {"room_id": room["room_code"]})
    stored = await _room_after(state_svc, room["room_code"])
    alias = next(iter(stored["participants"].keys()))

    from app.sockets.handlers.lobby import claim_role

    await claim_role("sid-1", {"room_id": room["room_code"], "role": "RETAILER"})

    await leave("sid-1", {"room_id": room["room_code"]})

    stored = await _room_after(state_svc, room["room_code"])
    assert stored["participants"] == {}
    assert stored["role_to_alias"]["RETAILER"] is None
    assert alias not in stored["sid_to_alias"].values()


async def test_leave_after_start_is_rejected(
    make_room, fake_socket_manager, connect_identity, state_svc
):
    room = await make_room(
        role_assignment_mode="PLAYER_CHOOSES", bot_fill_empty_roles=True
    )
    await connect_identity("sid-1", new_guest())
    await join("sid-1", {"room_id": room["room_code"]})

    from app.sockets.handlers.lobby import claim_role

    await claim_role("sid-1", {"room_id": room["room_code"], "role": "RETAILER"})

    await connect_identity("sid-host", new_guest())
    await join_waiting(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )
    await start_game(
        "sid-host", {"room_id": room["room_code"], "host_secret": room["host_secret"]}
    )

    before = await _room_after(state_svc, room["room_code"])
    fake_socket_manager.clear()
    await leave("sid-1", {"room_id": room["room_code"]})

    after = await _room_after(state_svc, room["room_code"])
    assert after["participants"] == before["participants"]
    assert fake_socket_manager.events_for("sid-1") == ["join_error"]


# --- AC 19 / failure mode 14 --------------------------------------------------- #


async def test_leave_never_removes_anyone_but_the_caller(
    make_room, connect_identity, state_svc
):
    room = await make_room()
    await connect_identity("sid-1", new_guest())
    await join("sid-1", {"room_id": room["room_code"]})
    await connect_identity("sid-2", new_guest())
    await join("sid-2", {"room_id": room["room_code"]})

    stored = await _room_after(state_svc, room["room_code"])
    assert set(stored["participants"].keys()) == {"P1", "P2"}

    # P1's sid claims to be leaving as P2 -- must remove P1, not P2.
    await leave("sid-1", {"room_id": room["room_code"], "alias": "P2"})

    stored = await _room_after(state_svc, room["room_code"])
    assert set(stored["participants"].keys()) == {"P2"}


# --- failure mode 2 ------------------------------------------------------------ #


async def test_leaving_participant_does_not_clobber_a_survivors_alias(
    make_room, connect_identity, state_svc
):
    """P1, P2, P3 join; P2 leaves; a fourth person joins and gets P2. P3's
    session_token and display_name must be untouched."""
    room = await make_room()
    for i, name in enumerate(["Ana", "Bo", "Cy"]):
        sid = f"sid-{i}"
        await connect_identity(sid, new_guest())
        await join(sid, {"room_id": room["room_code"], "display_name": name})

    stored = await _room_after(state_svc, room["room_code"])
    p3_token_before = stored["participants"]["P3"]["session_token"]
    p3_name_before = stored["participants"]["P3"]["display_name"]

    await leave("sid-1", {"room_id": room["room_code"]})  # P2's sid

    await connect_identity("sid-4", new_guest())
    await join("sid-4", {"room_id": room["room_code"], "display_name": "Dee"})

    stored = await _room_after(state_svc, room["room_code"])
    assert "P2" in stored["participants"]
    assert stored["participants"]["P2"]["display_name"] == "Dee"
    assert stored["participants"]["P3"]["session_token"] == p3_token_before
    assert stored["participants"]["P3"]["display_name"] == p3_name_before


# --- failure mode 3 ------------------------------------------------------------- #


async def test_session_token_replay_from_a_different_identity_never_takes_over(
    make_room, connect_identity, state_svc
):
    room = await make_room()
    identity_a = new_guest()
    await connect_identity("sid-1", identity_a)
    await join("sid-1", {"room_id": room["room_code"], "display_name": "Original"})
    stored = await _room_after(state_svc, room["room_code"])
    alias_a = next(iter(stored["participants"].keys()))
    token_a = stored["participants"][alias_a]["session_token"]

    identity_thief = new_guest()
    await connect_identity("sid-thief", identity_thief)
    await join("sid-thief", {"room_id": room["room_code"], "session_token": token_a})

    stored = await _room_after(state_svc, room["room_code"])
    assert stored["participants"][alias_a]["display_name"] == "Original"
    assert stored["participants"][alias_a]["identity"] == identity_a
    assert len(stored["participants"]) == 2


# --- failure mode 16 ------------------------------------------------------------- #


async def test_reconnect_moves_the_sid_mapping_rather_than_duplicating_it(
    make_room, connect_identity, state_svc
):
    room = await make_room()
    identity = new_guest()
    await connect_identity("sid-old", identity)
    await join("sid-old", {"room_id": room["room_code"]})
    stored = await _room_after(state_svc, room["room_code"])
    alias = next(iter(stored["participants"].keys()))
    assert stored["sid_to_alias"]["sid-old"] == alias

    await connect_identity("sid-new", identity)
    await join("sid-new", {"room_id": room["room_code"]})

    stored = await _room_after(state_svc, room["room_code"])
    assert stored["sid_to_alias"].get("sid-new") == alias
    assert "sid-old" not in stored["sid_to_alias"]
