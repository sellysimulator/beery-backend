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


# --- section 12 additions ---------------------------------------------------
#
# The play loop's tests need two more things beyond section 11's fixtures:
# a way to recursively check a broadcast payload for a forbidden key or leaked
# value (``12-socket-play.md §5``, failure modes 1-3), and a way to drive a
# room all the way to ``RUNNING`` with four seated humans, since ``submit_order``
# and the host controls only make sense once a game exists. Both are additive
# -- nothing section 11 already defined above is changed.


def contains_key(payload: Any, key: str) -> bool:
    """True if ``key`` appears anywhere in ``payload``, at any depth.

    Recursive means recursive: this descends into nested dicts *and* into
    lists (and tuples) of dicts, so a key hiding inside e.g.
    ``participants[0]`` does not slip past a shallow ``key in payload`` check
    (``12-socket-play.md §5`` failure mode 2).
    """
    if isinstance(payload, dict):
        if key in payload:
            return True
        return any(contains_key(value, key) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(contains_key(item, key) for item in payload)
    return False


def contains_value(payload: Any, value: Any) -> bool:
    """True if ``value`` appears anywhere in ``payload``, at any depth.

    Same recursive shape as ``contains_key``, but for values rather than
    keys. Used to probe for a specific number (an order quantity, or a
    role's inventory) leaking into a broadcast, without needing to know what
    key it would be filed under (``12-socket-play.md §5`` failure modes 1 and
    3) -- section 07's exact view-payload shape is not part of this section's
    frozen surface, only the rule that certain numbers must never appear in a
    room broadcast.

    Booleans are excluded from the scan even though ``bool`` is a subclass of
    ``int`` in Python (``True == 1``), because probe values like ``0``/``1``
    would otherwise spuriously match unrelated boolean flags such as
    ``is_bot`` or ``connected``.
    """
    if isinstance(payload, dict):
        return any(contains_value(v, value) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(contains_value(item, value) for item in payload)
    if isinstance(payload, bool) or isinstance(value, bool):
        return False
    return payload == value


@pytest.fixture()
def make_room_from_config(state_svc):
    """``await make_room_from_config(config, host_display_name="Host") -> dict``.

    For tests that need a hand-built ``GameConfig`` (distinct per-role
    values, a non-default ``visibility`` or ``pause_on_disconnect``) rather
    than the fixed shape ``make_config``/``make_room`` produce. Built only
    through section 03's frozen surface, exactly as ``tests/test_core/
    test_game_config.py``'s ``hand_built_config`` does.
    """

    async def _make(config: GameConfig, host_display_name: str = "Host") -> dict:
        return await state_svc.create_room(config, host_display_name)

    return _make


@pytest.fixture()
def start_running_game(connect_identity, state_svc):
    """``await start_running_game(room) -> dict`` -- seats four human players
    (one per role) into ``room`` and starts it, leaving it ``RUNNING``.

    Drives section 11's lobby handlers exactly as its own tests do (``join``,
    ``claim_role``, ``join_waiting``, ``start_game``); never touches
    ``play.py``. ``room`` must already be in ``PLAYER_CHOOSES`` mode with
    ``bot_fill_empty_roles=False`` (or not set) for the four ``claim_role``
    calls below to be meaningful -- pass ``role_assignment_mode=
    "PLAYER_CHOOSES"`` to ``make_config``/``make_room_from_config`` when
    building it.
    """
    from app.sockets.handlers.lobby import claim_role, join, join_waiting, start_game

    async def _start(room: dict) -> dict:
        room_code = room["room_code"]
        sids_by_role: dict[str, str] = {}
        identities_by_role: dict[str, str] = {}
        for role in ("RETAILER", "WHOLESALER", "DISTRIBUTOR", "FACTORY"):
            sid = f"sid-{role.lower()}"
            identity = new_guest()
            await connect_identity(sid, identity)
            await join(sid, {"room_id": room_code, "display_name": role.title()})
            await claim_role(sid, {"room_id": room_code, "role": role})
            sids_by_role[role] = sid
            identities_by_role[role] = identity

        host_sid = "sid-host"
        host_identity = new_guest()
        await connect_identity(host_sid, host_identity)
        await join_waiting(
            host_sid, {"room_id": room_code, "host_secret": room["host_secret"]}
        )
        await start_game(
            host_sid, {"room_id": room_code, "host_secret": room["host_secret"]}
        )

        stored = await state_svc.get_room(room_code)
        alias_by_role = {
            role: stored["sid_to_alias"][sid] for role, sid in sids_by_role.items()
        }
        return {
            "room_code": room_code,
            "host_secret": room["host_secret"],
            "host_sid": host_sid,
            "host_identity": host_identity,
            "sids_by_role": sids_by_role,
            "identities_by_role": identities_by_role,
            "alias_by_role": alias_by_role,
        }

    return _start
