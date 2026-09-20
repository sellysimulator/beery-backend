"""A store failure must reach the player, not vanish (`12 §3.10`).

The defect these cover: every handler wraps a read-modify-write of the room
document, nothing caught a failure of that store, and the exception
propagated out of the handler into Socket.IO's logging while the client was
told nothing at all. The player's submit button stays enabled, their order
did not happen, and there is nothing on screen to say so.
"""

from __future__ import annotations

import pytest

from app.services import state_service as ss
from app.sockets.errors import SERVER_ERROR_MESSAGE

from .conftest import make_config, new_guest

pytestmark = pytest.mark.asyncio


class _Broken:
    """A backend whose `get` always fails, wrapping a working one.

    Only `get` fails: it is the first thing every handler does, so this is
    the shortest path to the behaviour under test, and it leaves the
    underlying store untouched so "nothing was written" is checkable.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner

    async def get(self, key: str) -> str | None:
        raise ConnectionError("the state store is unreachable")

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def _running(make_room_from_config, start_running_game):
    room = await make_room_from_config(
        make_config(role_assignment_mode="PLAYER_CHOOSES", duration_weeks=36)
    )
    return await start_running_game(room)


async def test_a_store_failure_tells_the_player_instead_of_vanishing(
    make_room_from_config,
    start_running_game,
    fake_redis,
    fake_socket_manager,
    monkeypatch,
):
    from app.sockets.handlers.play import submit_order

    game = await _running(make_room_from_config, start_running_game)
    sid = next(iter(game["sids_by_role"].values()))
    monkeypatch.setattr(ss, "redis_client", _Broken(fake_redis))
    fake_socket_manager.clear()

    await submit_order(sid, {"room_id": game["room_code"], "week": 1, "order": 8})

    events = fake_socket_manager.emits_for(sid)
    assert events, "the player was told nothing at all -- this is the defect"
    event, payload = events[0]
    assert event == "error"
    assert payload["code"] == "SERVER_ERROR"
    assert payload["message"] == SERVER_ERROR_MESSAGE


async def test_the_handler_does_not_raise_out_of_the_socket_layer(
    make_room_from_config,
    start_running_game,
    fake_redis,
    fake_socket_manager,
    monkeypatch,
):
    """An exception escaping a handler is what produced the silence."""
    from app.sockets.handlers.play import submit_order

    game = await _running(make_room_from_config, start_running_game)
    sid = next(iter(game["sids_by_role"].values()))
    monkeypatch.setattr(ss, "redis_client", _Broken(fake_redis))

    await submit_order(sid, {"room_id": game["room_code"], "week": 1, "order": 8})


async def test_a_lobby_store_failure_uses_join_error(
    make_room, fake_redis, fake_socket_manager, connect_identity, monkeypatch
):
    """The lobby's frozen channel is `join_error {message}` -- no `code`."""
    from app.sockets.handlers.lobby import join

    room = await make_room()
    await connect_identity("sid-1", new_guest())
    monkeypatch.setattr(ss, "redis_client", _Broken(fake_redis))
    fake_socket_manager.clear()

    await join("sid-1", {"room_id": room["room_code"], "display_name": "Ana"})

    events = fake_socket_manager.emits_for("sid-1")
    assert events
    event, payload = events[0]
    assert event == "join_error"
    assert payload == {"message": SERVER_ERROR_MESSAGE}


async def test_the_failure_is_logged_with_its_traceback(
    make_room_from_config,
    start_running_game,
    fake_redis,
    fake_socket_manager,
    monkeypatch,
    caplog,
):
    """Reported to the player **and** logged -- never swallowed (`14 §3.4`)."""
    from app.sockets.handlers.play import submit_order

    game = await _running(make_room_from_config, start_running_game)
    sid = next(iter(game["sids_by_role"].values()))
    monkeypatch.setattr(ss, "redis_client", _Broken(fake_redis))

    with caplog.at_level("ERROR", logger="app.sockets.errors"):
        await submit_order(sid, {"room_id": game["room_code"], "week": 1, "order": 8})

    records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert records, "a reported failure must still leave a traceback behind"
    assert records[0].exc_info is not None


async def test_the_error_message_carries_no_driver_detail(
    make_room_from_config,
    start_running_game,
    fake_redis,
    fake_socket_manager,
    monkeypatch,
):
    """`15 §3.7`'s rule, on the socket side: never `str(e)` on the wire."""
    from app.sockets.handlers.play import submit_order

    game = await _running(make_room_from_config, start_running_game)
    sid = next(iter(game["sids_by_role"].values()))
    monkeypatch.setattr(ss, "redis_client", _Broken(fake_redis))
    fake_socket_manager.clear()

    await submit_order(sid, {"room_id": game["room_code"], "week": 1, "order": 8})

    _event, payload = fake_socket_manager.emits_for(sid)[0]
    assert "unreachable" not in payload["message"]
    assert "ConnectionError" not in payload["message"]
