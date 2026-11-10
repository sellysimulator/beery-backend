"""Abuse limits at the socket layer's two single entry points: the handshake
(`connection.py`) and `@guarded` (`errors.py`). The counters themselves are
covered in `tests/test_core/test_abuse.py`."""

from __future__ import annotations

import pytest

from app.core import abuse
from app.core.abuse import RATE_LIMITED_MESSAGE
from app.sockets import errors

pytestmark = pytest.mark.asyncio


def _environ(ip: str) -> dict:
    return {"asgi.scope": {"headers": [(b"x-forwarded-for", ip.encode())]}}


async def test_a_refused_play_event_says_so_and_does_not_run(
    fake_redis, fake_socket_manager, monkeypatch
):
    from app.sockets.handlers.play import submit_order

    monkeypatch.setattr(errors, "socket_event_allowed", lambda sid, event: False)
    writes: list[str] = []

    async def _no_write(*args):  # pragma: no cover - must not be called
        writes.append("setex")

    monkeypatch.setattr(fake_redis, "setex", _no_write)

    await submit_order("sid-1", {"room_id": "ABCDEF", "week": 1, "order": 4})

    assert fake_socket_manager.emits_for("sid-1") == [
        ("error", {"message": RATE_LIMITED_MESSAGE, "code": "RATE_LIMITED"})
    ]
    assert writes == []


async def test_a_refused_lobby_event_uses_the_lobby_error_shape(
    fake_redis, fake_socket_manager, monkeypatch
):
    from app.sockets.handlers.lobby import join

    seen: list[str] = []

    def _refuse(sid: str, event: str) -> bool:
        seen.append(event)
        return False

    monkeypatch.setattr(errors, "socket_event_allowed", _refuse)

    await join("sid-1", {"room_id": "ABCDEF"})

    assert seen == ["join"]
    assert fake_socket_manager.emits_for("sid-1") == [
        ("join_error", {"message": RATE_LIMITED_MESSAGE})
    ]


async def test_the_handshake_refuses_past_the_socket_cap(
    fake_redis, fake_socket_manager, monkeypatch
):
    from app.sockets.handlers.connection import connect

    monkeypatch.setattr(abuse, "MAX_SOCKETS_PER_IP", 1)

    assert await connect("sid-1", _environ("203.0.113.1"), None) is None
    assert await connect("sid-2", _environ("203.0.113.1"), None) is False
    assert "sid-2" not in fake_socket_manager.sid_to_identity
    assert await connect("sid-3", _environ("203.0.113.2"), None) is None


async def test_a_refused_token_gives_its_socket_slot_back(
    fake_redis, fake_socket_manager, firebase_tokens, monkeypatch
):
    from app.sockets.handlers.connection import connect

    firebase_tokens.configure()
    monkeypatch.setattr(abuse, "MAX_SOCKETS_PER_IP", 1)

    assert (
        await connect("sid-1", _environ("203.0.113.1"), {"idToken": "forged"}) is False
    )
    assert await connect("sid-2", _environ("203.0.113.1"), None) is None


async def test_disconnect_gives_the_socket_slot_back(
    fake_redis, fake_socket_manager, monkeypatch
):
    from app.sockets.handlers.connection import connect, disconnect

    monkeypatch.setattr(abuse, "MAX_SOCKETS_PER_IP", 1)

    assert await connect("sid-1", _environ("203.0.113.1"), None) is None
    await disconnect("sid-1")
    assert await connect("sid-2", _environ("203.0.113.1"), None) is None


async def test_a_handshake_that_fails_gives_its_socket_slot_back(
    fake_redis, fake_socket_manager, monkeypatch
):
    from app.sockets.handlers.connection import connect

    monkeypatch.setattr(abuse, "MAX_SOCKETS_PER_IP", 1)

    real_connect = fake_socket_manager.connect
    failures = [RuntimeError("socket manager down")]

    async def _fails_once(sid: str, environ: dict) -> None:
        if failures:
            raise failures.pop()
        await real_connect(sid, environ)

    monkeypatch.setattr(fake_socket_manager, "connect", _fails_once)
    with pytest.raises(RuntimeError):
        await connect("sid-1", _environ("203.0.113.1"), None)

    assert await connect("sid-2", _environ("203.0.113.1"), None) is None


async def test_a_disconnect_that_fails_still_gives_the_slot_back(
    fake_redis, fake_socket_manager, monkeypatch
):
    from app.sockets.handlers.connection import connect, disconnect

    monkeypatch.setattr(abuse, "MAX_SOCKETS_PER_IP", 1)
    assert await connect("sid-1", _environ("203.0.113.1"), None) is None

    async def _boom(sid: str) -> None:
        raise RuntimeError("socket manager down")

    monkeypatch.setattr(fake_socket_manager, "disconnect", _boom)
    with pytest.raises(RuntimeError):
        await disconnect("sid-1")

    assert await connect("sid-2", _environ("203.0.113.1"), None) is None
