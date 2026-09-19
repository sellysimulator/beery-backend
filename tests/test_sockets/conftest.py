"""Shared fixtures for section 11 -- socket lobby, seating and start.

Owned by section 11's test agent. Not one of the four files section 11's
``Test agent writes`` line names, but ordinary pytest infrastructure shared
by all of them, in the same spirit as ``tests/test_auth/conftest.py``
(section 02) -- a per-file copy of the same room-building and identity
helpers would drift, and ``00-conventions.md §5`` already tolerates a
section owning fixtures beyond its literal file list (``firebase_tokens``
and ``fake_socket_manager`` live in section 01's shared ``conftest.py`` for
exactly this reason).

``register_sid`` exists because ``join_waiting`` calls the real
``sio.enter_room`` directly rather than the faked ``socket_manager``
(``11-socket-lobby.md §3.1`` step 5, deliberately -- the host holds no alias
and must never be written into ``socket_manager.sid_to_alias``). ``sio`` is
python-socketio's real ``AsyncServer``; without a genuine transport
handshake it has never heard of a sid a test invents, and
``sio.enter_room`` raises ``KeyError``/``ValueError``. ``register_sid``
performs the same local bookkeeping a real connection would for one chosen
sid -- entirely in-memory, no transport, no Redis -- so the real call
succeeds exactly as it would in production.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.core.config_models import ConstantDemand, FactoryConfig, GameConfig, RoleConfig
from app.core.enums import Role
from app.services.state_service import get_state_service


def make_config(
    duration_weeks: int = 8,
    role_assignment_mode: str = "HOST_ASSIGNS",
    bot_fill_empty_roles: bool = False,
    random_seed: int | None = None,
) -> GameConfig:
    """A minimal, valid ``GameConfig``, built only through section 03's
    frozen surface -- mirrors the helper sections 07 and 09's own tests
    use."""
    roles = {
        Role.RETAILER.value: RoleConfig(),
        Role.WHOLESALER.value: RoleConfig(),
        Role.DISTRIBUTOR.value: RoleConfig(),
        Role.FACTORY.value: FactoryConfig(),
    }
    return GameConfig(
        roles=roles,
        demand=ConstantDemand(value=4),
        duration_weeks=duration_weeks,
        role_assignment_mode=role_assignment_mode,
        bot_fill_empty_roles=bot_fill_empty_roles,
        random_seed=random_seed,
    )


@pytest.fixture()
def state_svc(fake_redis):
    """The real ``StateService``, driven by the shared ``FakeRedis``."""
    return get_state_service()


@pytest.fixture()
def make_room(state_svc):
    """``await make_room(**config_kwargs) -> dict`` -- a fresh LOBBY room."""

    async def _make(host_display_name: str = "Host", **config_kwargs: Any) -> dict:
        config = make_config(**config_kwargs)
        return await state_svc.create_room(config, host_display_name)

    return _make


@pytest.fixture(autouse=True)
def _reset_sio_manager():
    """Start every test with the real ``sio``'s room bookkeeping empty.

    ``sio.manager`` is a process-wide singleton (section 01, ``app/sockets/
    manager.py``), so without this a sid registered by one test would still
    look "connected" to the next -- and a stale room membership from an
    earlier test would make a broadcast-target assertion pass for the wrong
    reason.
    """
    from app.sockets.manager import sio

    sio.manager.rooms = {}
    sio.manager.eio_to_sid = {}
    yield
    sio.manager.rooms = {}
    sio.manager.eio_to_sid = {}


@pytest.fixture()
def register_sid() -> Callable[[str], Awaitable[None]]:
    """``await register_sid("sid-1")``.

    Makes the real ``sio`` treat ``sid`` as a genuinely connected client of
    the default namespace, the way a real Socket.IO handshake would --
    without a transport and without touching Redis. Section 09's
    ``AsyncRedisManager``-backed room tracking only publishes to Redis for a
    sid it does *not* recognise as locally connected; registering the sid
    first keeps every later ``sio.enter_room`` call on the fast, local-only
    path.
    """

    async def _register(sid: str) -> None:
        from app.sockets.manager import sio

        original_generate_id = sio.eio.generate_id
        sio.eio.generate_id = lambda: sid
        try:
            await sio.manager.connect(sid, "/")
        finally:
            sio.eio.generate_id = original_generate_id

    return _register


@pytest.fixture()
def connect_identity(register_sid, fake_socket_manager):
    """``await connect_identity("sid-1", "guest_...")``.

    The two things a real handshake establishes before any lobby handler
    runs: a sid the real ``sio`` recognises, and a verified identity
    recorded on ``socket_manager`` (faked here, exactly as
    ``app/sockets/handlers/connection.py`` would record it for real).
    """

    async def _connect(sid: str, identity: str) -> None:
        await register_sid(sid)
        fake_socket_manager.sid_to_identity[sid] = identity

    return _connect


def new_guest() -> str:
    """A fresh ``guest_<uuid4>`` identity, shaped exactly as section 02
    mints one."""
    return f"guest_{uuid.uuid4()}"
