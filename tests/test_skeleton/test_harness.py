"""The test harness itself (01-backend-skeleton.md §4).

Acceptance criteria 13, 19, 20 and failure modes 1, 2 and 6, plus the two
shared fakes §4 hands to sections 02, 11 and 12 -- ``firebase_tokens`` and
``fake_socket_manager``.  These tests exist because a harness that is wrong
here makes *every* other test in the build error before any implementation is
even wrong.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import (
    FakeRedis,
    FakeSocketManager,
    FirebaseTokens,
    _FakeLock,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _state_service_exists() -> bool:
    try:
        return importlib.util.find_spec("app.services.state_service") is not None
    except ModuleNotFoundError:
        return False


# --- AC 13 / FM 1 -----------------------------------------------------------


def test_asyncio_mode_is_strict(pytestconfig):
    """AC 13 / FM 1: the suite runs under ``asyncio_mode = strict``."""
    assert pytestconfig.getini("asyncio_mode") == "strict"


def test_asyncio_default_fixture_loop_scope_is_function(pytestconfig):
    """AC 13: the second half of the fixed pytest.ini (00-conventions.md §5)."""
    assert pytestconfig.getini("asyncio_default_fixture_loop_scope") == "function"


def test_unmarked_async_test_fails_rather_than_being_skipped(tmp_path):
    """FM 1: under pytest-asyncio 0.21 an unmarked ``async def test_*`` was
    silently skipped, so whole files never ran.  Strict mode must fail it."""
    test_file = tmp_path / "test_unmarked_async.py"
    test_file.write_text(textwrap.dedent("""
            async def test_unmarked_async():
                assert False, "this body must never be treated as a pass"
            """))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            "-o",
            "asyncio_mode=strict",
            "-p",
            "no:cacheprovider",
            "-q",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "1 passed" not in combined, combined
    assert "1 skipped" not in combined, combined


# --- FM 2 -------------------------------------------------------------------


def test_bare_testclient_skips_lifespan_and_with_block_runs_it():
    """FM 2: a bare ``TestClient(app)`` does not execute the lifespan handler;
    ``with TestClient(app)`` does.  Observed through the only public hook the
    lifespan has -- ``run_startup_checks()`` (01 §3.2)."""
    from fastapi.testclient import TestClient

    from app.core.checks import register_check
    from app.main import app

    calls: list[str] = []

    def _probe() -> None:
        calls.append("ran")

    register_check(_probe)

    bare = TestClient(app)
    assert bare.get("/api/v1/health").status_code == 200
    assert calls == []

    with TestClient(app) as entered:
        assert entered.get("/api/v1/health").status_code == 200
    assert calls == ["ran"]


def test_lifespan_client_fixture_runs_startup(lifespan_client):
    """The ``lifespan_client`` fixture is the supported way to assert on
    startup behaviour."""
    assert lifespan_client.get("/api/v1/health").status_code == 200


# --- AC 19 / FM 6 -----------------------------------------------------------


def test_client_fixture_does_not_depend_on_fake_redis(request, client):
    """FM 6: ``client`` must not drag the Redis patch into every route test.

    If ``client`` requested ``fake_redis`` the name would appear in this
    item's fixture closure -- and with ``app/services/`` absent the patch
    would raise ``ModuleNotFoundError`` before a single route was exercised.
    """
    assert "fake_redis" not in request.fixturenames


def test_client_fixture_works_with_app_services_absent(client):
    """AC 19 / FM 6: a test requesting ``client`` passes even with
    ``app/services/`` not created at all.

    Once section 09 lands, ``app/services/`` exists and this can no longer be
    observed directly -- which is why the guarantee is *also* enforced
    structurally by ``test_client_fixture_does_not_depend_on_fake_redis``,
    which never goes stale.  Serving a route through the fixture stays
    meaningful either way, so this does not skip itself into silence."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_naive_unconditional_patch_would_raise(monkeypatch):
    """FM 6: demonstrates the bug the ``fake_redis`` fixture guards against --
    an unconditional ``monkeypatch.setattr`` on a module section 09 has not
    written yet."""
    if _state_service_exists():
        pytest.skip("section 09 has landed; the unconditional patch now works")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.services.state_service")
    # monkeypatch re-raises the same failure (as a plain ImportError) because
    # resolving the dotted target imports the module first.
    with pytest.raises(ImportError):
        monkeypatch.setattr("app.services.state_service.redis_client", object())


# --- AC 20 ------------------------------------------------------------------


def test_fake_redis_fixture_is_usable_without_state_service(fake_redis):
    """AC 20: ``fake_redis`` yields a usable ``FakeRedis`` with
    ``app/services/state_service.py`` absent."""
    assert isinstance(fake_redis, FakeRedis)


@pytest.mark.asyncio
async def test_fake_redis_implements_the_required_operations(fake_redis):
    """AC 20 / §4: get, setex, delete, exists, incrby, expire and .lock()."""
    assert await fake_redis.get("room:ABCD") is None
    assert await fake_redis.exists("room:ABCD") == 0

    await fake_redis.setex("room:ABCD", 86_400, '{"week": 1}')
    assert await fake_redis.get("room:ABCD") == '{"week": 1}'
    assert await fake_redis.exists("room:ABCD") == 1

    assert await fake_redis.incrby("seq:ABCD") == 1
    assert await fake_redis.incrby("seq:ABCD", 2) == 3

    await fake_redis.expire("room:ABCD", 10)
    assert await fake_redis.get("room:ABCD") == '{"week": 1}'

    assert await fake_redis.delete("room:ABCD") == 1
    assert await fake_redis.exists("room:ABCD") == 0
    assert await fake_redis.delete("room:ABCD") == 0


@pytest.mark.asyncio
async def test_fake_redis_lock_serialises_concurrent_holders(fake_redis):
    """§4: the per-key lock is a real ``asyncio.Lock``, so two concurrent
    ``async with`` blocks against one room id serialise."""
    import asyncio

    order: list[str] = []

    async def critical(tag: str) -> None:
        async with fake_redis.lock("room:ABCD", timeout=10):
            order.append(f"{tag}:enter")
            await asyncio.sleep(0)
            order.append(f"{tag}:exit")

    await asyncio.gather(critical("a"), critical("b"))

    assert order[0].endswith(":enter")
    assert order[1] == order[0].replace(":enter", ":exit")
    assert order[2].endswith(":enter")
    assert order[3] == order[2].replace(":enter", ":exit")


@pytest.mark.asyncio
async def test_fake_redis_lock_registry_is_cleared_between_tests(fake_redis):
    """§4: "The registry is cleared between tests" -- otherwise a lock left
    held by a failed test wedges every later one."""
    lock = fake_redis.lock("room:ZZZZ")
    async with lock:
        pass
    assert _FakeLock._locks["room:ZZZZ"].locked() is False


def test_two_fake_redis_instances_do_not_share_state(fake_redis):
    """Each test gets its own store; nothing leaks between rooms or tests."""
    other = FakeRedis()
    assert other is not fake_redis


# --- the shared Firebase fake (§4) ------------------------------------------


def test_firebase_tokens_starts_empty_and_unconfigured(firebase_tokens):
    """§4: "The default state is **unconfigured**, so nothing is ever trusted
    by accident of test ordering"."""
    from app.config import settings

    assert isinstance(firebase_tokens, FirebaseTokens)
    assert firebase_tokens == {}
    assert settings.FIREBASE_SERVICE_ACCOUNT_JSON == ""


def test_firebase_tokens_configure_and_unconfigure(firebase_tokens):
    from app.config import settings

    configured = firebase_tokens.configure()
    assert settings.FIREBASE_SERVICE_ACCOUNT_JSON == configured
    assert json.loads(configured)["project_id"]

    firebase_tokens.unconfigure()
    assert settings.FIREBASE_SERVICE_ACCOUNT_JSON == ""


def test_firebase_tokens_verifies_a_registered_token(firebase_tokens):
    """§4: the fake is installed at ``auth.verify_id_token``, the SDK
    boundary -- so everything Beery wrote on top of it runs un-mocked."""
    from firebase_admin import auth

    firebase_tokens.add("good-token", uid="firebase-uid-1", email="a@example.com")

    claims = auth.verify_id_token("good-token")

    assert claims["uid"] == "firebase-uid-1"
    assert claims["email"] == "a@example.com"


def test_firebase_tokens_rejects_an_unregistered_token(firebase_tokens):
    """§4: "Any token **not** in the dict fails verification, matching the
    real SDK for a bad or expired token" -- and it fails with the SDK's own
    error, not a generic one, so a handler's ``except`` clause is exercised
    exactly as it will be in production."""
    from firebase_admin import auth

    firebase_tokens.add("good-token", uid="firebase-uid-1")

    with pytest.raises(auth.InvalidIdTokenError):
        auth.verify_id_token("some-other-token")


def test_firebase_tokens_records_sdk_initialisation(firebase_tokens):
    """§4: ``credentials.Certificate`` and ``initialize_app`` are faked too,
    so an init path can run without a service account and without a
    network."""
    import firebase_admin
    from firebase_admin import credentials

    cert = credentials.Certificate(json.loads(firebase_tokens.configure()))
    app_handle = firebase_admin.initialize_app(cert)

    assert firebase_tokens.certificate_calls
    assert firebase_tokens.initialize_app_calls == [cert]
    assert app_handle is not None


def test_firebase_tokens_does_not_leak_between_tests(firebase_tokens):
    """The reject path is only worth something if the dict really is empty at
    the start of every test."""
    from app.config import settings

    assert firebase_tokens == {}
    assert settings.FIREBASE_SERVICE_ACCOUNT_JSON == ""


# --- the shared Socket.IO fake (§4) -----------------------------------------


def test_fake_socket_manager_is_patched_over_the_singleton(fake_socket_manager):
    """§4: handlers run as real code against fakes for their two I/O
    dependencies, so the singleton they reach for must be the fake."""
    import app.sockets.manager as manager_module

    assert isinstance(fake_socket_manager, FakeSocketManager)
    assert manager_module.socket_manager is fake_socket_manager


@pytest.mark.asyncio
async def test_fake_socket_manager_records_every_emit(fake_socket_manager):
    """§4: "recording every emit as ``(target, event, data)`` instead of
    touching transport"."""
    await fake_socket_manager.emit_to_room("ABCD", "room_state", {"week": 1})
    await fake_socket_manager.emit_to_sid("sid-1", "your_state", {"inventory": 12})

    assert fake_socket_manager.emits == [
        ("ABCD", "room_state", {"week": 1}),
        ("sid-1", "your_state", {"inventory": 12}),
    ]
    assert fake_socket_manager.room_emits == [("ABCD", "room_state", {"week": 1})]
    assert fake_socket_manager.sid_emits == [("sid-1", "your_state", {"inventory": 12})]
    assert fake_socket_manager.emits_for("ABCD") == [("room_state", {"week": 1})]
    assert fake_socket_manager.events_for("sid-1") == ["your_state"]

    fake_socket_manager.clear()
    assert fake_socket_manager.emits == []


@pytest.mark.asyncio
async def test_fake_socket_manager_mirrors_the_frozen_surface(fake_socket_manager):
    """§2: the fake stands in for ``SocketManager``, so it tracks the same
    three maps."""
    await fake_socket_manager.connect("sid-1", {})
    await fake_socket_manager.join_room("sid-1", "ABCD", "P1")

    assert fake_socket_manager.sid_to_room["sid-1"] == "ABCD"
    assert fake_socket_manager.sid_to_alias["sid-1"] == "P1"

    await fake_socket_manager.leave_room("sid-1", "ABCD")
    assert "sid-1" not in fake_socket_manager.sid_to_room

    await fake_socket_manager.join_room("sid-2", "ABCD", "P2")
    await fake_socket_manager.disconnect("sid-2")
    assert fake_socket_manager.sid_to_room == {}
    assert fake_socket_manager.sid_to_alias == {}


def test_shared_fakes_compose_with_the_client_fixture(
    client, fake_redis, firebase_tokens, fake_socket_manager
):
    """§4: a test that needs several of them simply requests several of them;
    none of the four drags another in behind it."""
    assert client.get("/api/v1/health").status_code == 200
    assert isinstance(fake_redis, FakeRedis)
    assert firebase_tokens == {}
    assert fake_socket_manager.emits == []
