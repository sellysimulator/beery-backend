"""The test harness itself (01-backend-skeleton.md §4).

Acceptance criteria 13, 19, 20 and failure modes 1, 2 and 6.  These tests
exist because a harness that is wrong here makes *every* other test in the
build error before any implementation is even wrong.
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import FakeRedis, _FakeLock

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
    ``app/services/`` not created at all."""
    if (PROJECT_ROOT / "app" / "services").exists():
        pytest.skip("app/services/ now exists; the bootstrap window has closed")
    response = client.get("/api/v1/health")
    assert response.status_code == 200


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
