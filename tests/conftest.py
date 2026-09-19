"""Shared test fixtures for the Beery backend suite.

Ported from ``Selly_Backend/tests/conftest.py`` as required by
``01-backend-skeleton.md §4``:

* ``FakeRedis`` / ``_FakeLock`` -- an in-memory async-redis stand-in good
  enough to drive the *real* ``StateService`` once section 09 lands, with a
  real per-key ``asyncio.Lock`` so concurrent ``async with`` usages against
  the same room id serialise exactly as the real Redis lock would.
* ``fake_redis`` -- the instance, patched over the real client **only when
  the module it patches already exists** (``app/services/state_service.py``
  does not exist until section 09).
* ``client`` -- ``TestClient(app)`` that **depends on nothing**, so no route
  test drags the Redis patch (or the lifespan) in behind it.
* ``lifespan_client`` -- ``with TestClient(app) as c`` for startup tests.

It also neutralises ``Beery_Backend/.env`` before ``app`` is ever imported --
see ``TEST_ENVIRONMENT`` below.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- .env neutralisation (01-backend-skeleton.md §4) ------------------------
#
# ``Settings`` reads ``.env`` relative to the working directory, and the
# developer's ``.env`` points at a REAL, REACHABLE managed MySQL and a REAL
# managed Redis.  A suite run from ``Beery_Backend`` therefore talks to
# production unless it is stopped: ``/api/v1/health/deep`` answers
# ``database: true`` and AC 4 fails for a reason that has nothing to do with
# the code, and from section 13 onward a migration or a batch insert would run
# against live data.
#
# An environment variable takes priority over a ``.env`` entry in
# pydantic-settings, so setting these here -- at the very top of the file,
# before any ``from app...`` import anywhere in the suite -- wins without
# deleting or editing anybody's ``.env``.  Section 13's Testcontainers suite
# overrides them again with its throwaway container's own coordinates.
#
# The ports are deliberately ones nothing listens on locally, so "unreachable"
# is a property of the configuration rather than of the machine.
TEST_ENVIRONMENT = {
    "DB_HOST": "127.0.0.1",
    "DB_PORT": "13306",
    "DB_DATABASE": "beery_test",
    "DB_USER": "beery_test",
    "DB_PASSWORD": "beery_test",
    "DB_REQUIRE_SSL": "False",
    "REDIS_URL": "redis://127.0.0.1:16379/0",
    "FIREBASE_SERVICE_ACCOUNT_JSON": "",
    "DEBUG": "False",
}

os.environ.update(TEST_ENVIRONMENT)


class _FakeLock:
    """Stand-in for ``redis.asyncio``'s ``Redis.lock(...)`` async context
    manager.

    A real per-key ``asyncio.Lock`` so concurrent ``async with`` usages
    against the same key still serialise the way the real Redis lock would.
    The registry is class-level and is cleared between tests by the
    ``fake_redis`` fixture.
    """

    _locks: dict[str, asyncio.Lock] = {}  # noqa: RUF012 - shared registry

    def __init__(self, key: str, timeout: int = 10) -> None:
        self._key = key
        self._timeout = timeout
        if key not in _FakeLock._locks:
            _FakeLock._locks[key] = asyncio.Lock()
        self._lock = _FakeLock._locks[key]

    async def __aenter__(self) -> _FakeLock:  # noqa: PYI034
        await self._lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._lock.release()
        return False


class FakeRedis:
    """Minimal async-redis-compatible in-memory store.

    Implements only the handful of operations the state service uses:
    ``get``/``setex``/``delete``/``exists``/``incrby``/``expire``, plus
    ``.lock(...)``.
    """

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._store[key] = value

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if k in self._store:
                del self._store[k]
                removed += 1
        return removed

    async def exists(self, key: str) -> int:
        return 1 if key in self._store else 0

    async def incrby(self, key: str, amount: int = 1) -> int:
        current = int(self._store.get(key, "0"))
        new_val = current + amount
        self._store[key] = str(new_val)
        return new_val

    async def expire(self, key: str, ttl: int) -> None:
        # No-op: expiry semantics are not exercised by these unit tests.
        return None

    def lock(self, key: str, timeout: int = 10) -> _FakeLock:
        return _FakeLock(key, timeout)


@pytest.fixture()
def fake_redis(monkeypatch) -> FakeRedis:
    """A ``FakeRedis``, patched over the real client when it exists.

    ``monkeypatch.setattr("app.services.state_service.redis_client", ...)``
    *imports* its target module, and ``app/services/state_service.py`` is not
    written until section 09.  Written naively this fixture would raise
    ``ModuleNotFoundError`` in every section-01 test that touched it, so the
    patch is conditional (``01-backend-skeleton.md §4``).
    """
    fr = FakeRedis()
    try:
        import app.services.state_service  # noqa: F401
    except ModuleNotFoundError:
        pass  # section 09 has not landed yet
    else:
        monkeypatch.setattr("app.services.state_service.redis_client", fr)
    _FakeLock._locks.clear()
    return fr


@pytest.fixture()
def client():
    """``TestClient`` driving the real ASGI app. Depends on nothing.

    Deliberately *not* used as a context manager: no route in this section
    needs anything from the lifespan handler, and skipping it keeps the
    CRITICAL "Firebase is not configured" boot message out of every route
    test's captured log.  A test that asserts on *startup* behaviour must use
    ``lifespan_client`` instead -- a bare ``TestClient`` never runs lifespan.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture()
def lifespan_client():
    """``TestClient`` entered as a context manager, so lifespan runs."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture()
def make_empty_package(tmp_path: Path) -> Callable[[str], object]:
    """Import a copy of a package's ``__init__.py`` with **no members**.

    ``app/api/v1/`` already ships ``health.py`` in this very section, so the
    "works while empty" guarantee cannot be observed on the live package.
    Copying just the discovery ``__init__.py`` into a throwaway package
    reproduces the state section 02 would otherwise be the first to hit --
    and keeps the test honest for every future section too.
    """
    created: list[str] = []
    root = tmp_path / "empty_packages"
    root.mkdir()
    sys.path.insert(0, str(root))

    def _make(package_name: str):
        pkg = importlib.import_module(package_name)
        src = Path(pkg.__file__)
        alias = f"empty_pkg_{uuid.uuid4().hex}"
        target = root / alias
        target.mkdir()
        shutil.copyfile(src, target / "__init__.py")
        importlib.invalidate_caches()
        created.append(alias)
        return importlib.import_module(alias)

    yield _make

    for alias in created:
        for name in [
            n for n in list(sys.modules) if n == alias or n.startswith(alias + ".")
        ]:
            del sys.modules[name]
    try:
        sys.path.remove(str(root))
    except ValueError:
        pass
    importlib.invalidate_caches()


@pytest.fixture()
def drop_module():
    """Write a module into a live package and re-run its discovery loop.

    This is how ``01-backend-skeleton.md §5`` items 16 and 17 are specified to
    be asserted: "a temporary module written by the test".  Sibling modules
    are purged from ``sys.modules`` before the reload so that *every* member
    re-registers, and the file (and the package's state) is restored
    afterwards whatever the test does.
    """
    created: list[tuple[str, str, Path]] = []

    def _purge(package_name: str) -> None:
        prefix = package_name + "."
        for name in [n for n in list(sys.modules) if n.startswith(prefix)]:
            del sys.modules[name]

    def _drop(package_name: str, module_name: str, source: str):
        pkg = importlib.import_module(package_name)
        pkg_dir = Path(pkg.__file__).parent
        path = pkg_dir / f"{module_name}.py"
        assert not path.exists(), f"{path} already exists; refusing to clobber it"
        path.write_text(source)
        created.append((package_name, module_name, path))
        importlib.invalidate_caches()
        _purge(package_name)
        return importlib.reload(pkg)

    yield _drop

    packages = []
    for package_name, module_name, path in created:
        path.unlink(missing_ok=True)
        cache = path.parent / "__pycache__"
        if cache.is_dir():
            for stale in cache.glob(f"{module_name}.*"):
                stale.unlink(missing_ok=True)
        sys.modules.pop(f"{package_name}.{module_name}", None)
        if package_name not in packages:
            packages.append(package_name)
    importlib.invalidate_caches()
    for package_name in packages:
        _purge(package_name)
        try:
            importlib.reload(importlib.import_module(package_name))
        except Exception:  # noqa: BLE001, S110 - restoration is best effort
            pass
