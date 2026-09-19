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
* ``firebase_tokens`` -- a ``{token: claims}`` dict installed at the
  ``firebase_admin`` SDK boundary, so ``init_firebase``,
  ``verify_firebase_id_token`` and ``resolve_identity`` run **un-mocked** on
  top of it and the reject path proves something.
* ``fake_socket_manager`` -- a ``FakeSocketManager`` recording every emit as
  ``(target, event, data)`` instead of touching transport.

``firebase_tokens`` and ``fake_socket_manager`` live here, in the shared file,
because sections 02, 11 and 12 all need them and **D19** forbids a later
section editing this one: a per-section copy would be three implementations of
one fake, drifting apart.

It also neutralises ``Beery_Backend/.env`` before ``app`` is ever imported --
see ``TEST_ENVIRONMENT`` below.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


# --- Firebase (01-backend-skeleton.md §4, 00-conventions.md §5) -------------

# Shaped like a real service account file so that anything which parses it
# before handing it to the SDK sees what it expects.  It is not a key: the
# private_key field is a placeholder and the fake never signs anything.
FAKE_SERVICE_ACCOUNT_JSON = json.dumps(
    {
        "type": "service_account",
        "project_id": "beery-test",
        "private_key_id": "0" * 40,
        "private_key": (
            "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n"
        ),
        "client_email": "beery-test@beery-test.iam.gserviceaccount.com",
        "client_id": "0" * 21,
        "token_uri": "https://oauth2.googleapis.com/token",
    }
)


class _FakeFirebaseApp:
    """What the faked ``firebase_admin.initialize_app`` hands back."""

    def __init__(self, name: str = "[DEFAULT]", credential: Any = None) -> None:
        self.name = name
        self.credential = credential
        self.project_id = "beery-test"


class _FakeCertificate:
    """Stand-in for ``firebase_admin.credentials.Certificate``."""

    def __init__(self, cert: Any) -> None:
        self.cert = cert


class FirebaseTokens(dict):
    """A ``{token: claims}`` mapping standing in for Firebase verification.

    A token present in the mapping verifies and yields its claims; **any**
    other token fails verification with the SDK's own
    ``InvalidIdTokenError``, which is what the real SDK raises for a bad or
    expired token.  The starting state is empty **and unconfigured**, so
    nothing is ever trusted by accident of test ordering.

    A test that needs Firebase configured calls ``configure()``; one that
    wants the unconfigured path again calls ``unconfigure()``.
    """

    def __init__(self, set_service_account: Callable[[str], None]) -> None:
        super().__init__()
        self._set_service_account = set_service_account
        self.certificate_calls: list[Any] = []
        self.initialize_app_calls: list[Any] = []
        self.verify_calls: list[Any] = []

    def configure(self, service_account_json: str = FAKE_SERVICE_ACCOUNT_JSON) -> str:
        """Put ``settings.FIREBASE_SERVICE_ACCOUNT_JSON`` into a configured
        state and return what it was set to."""
        self._set_service_account(service_account_json)
        return service_account_json

    def unconfigure(self) -> None:
        self._set_service_account("")

    def add(self, token: str, **claims: Any) -> dict:
        """Register ``token`` as verifiable, with ``claims`` (normally a
        ``uid``)."""
        self[token] = dict(claims)
        return self[token]


@pytest.fixture()
def firebase_tokens(monkeypatch) -> FirebaseTokens:
    """Fake Firebase at the SDK boundary, never at Beery's own boundary.

    ``auth.verify_id_token``, ``credentials.Certificate`` and
    ``initialize_app`` are replaced, plus
    ``settings.FIREBASE_SERVICE_ACCOUNT_JSON``.  Everything Beery wrote on top
    -- ``init_firebase``, ``verify_firebase_id_token``, ``resolve_identity``
    -- runs as real, un-mocked code; mocking those instead would make the
    reject path prove nothing.

    Like ``fake_redis``, it tolerates the module it reaches for not existing
    yet: ``app/core/firebase.py`` is section 02's.
    """
    firebase_admin = pytest.importorskip("firebase_admin")
    firebase_auth = importlib.import_module("firebase_admin.auth")
    firebase_credentials = importlib.import_module("firebase_admin.credentials")

    from app.config import settings

    def _set_service_account(value: str) -> None:
        monkeypatch.setattr(
            settings, "FIREBASE_SERVICE_ACCOUNT_JSON", value, raising=False
        )

    tokens = FirebaseTokens(_set_service_account)

    def _verify_id_token(id_token: Any, *args: Any, **kwargs: Any) -> dict:
        tokens.verify_calls.append(id_token)
        try:
            claims = tokens[id_token]
        except (KeyError, TypeError):
            raise _invalid_id_token_error(firebase_auth) from None
        return dict(claims)

    def _certificate(cert: Any) -> _FakeCertificate:
        tokens.certificate_calls.append(cert)
        return _FakeCertificate(cert)

    def _initialize_app(
        credential: Any = None, options: Any = None, name: str = "[DEFAULT]"
    ) -> _FakeFirebaseApp:
        tokens.initialize_app_calls.append(credential)
        return _FakeFirebaseApp(name, credential)

    # Unconfigured by default -- and pinned, so a `.env` entry or an earlier
    # test cannot leave Firebase configured behind our back.
    _set_service_account("")
    monkeypatch.setattr(firebase_auth, "verify_id_token", _verify_id_token)
    monkeypatch.setattr(firebase_credentials, "Certificate", _certificate)
    monkeypatch.setattr(firebase_admin, "initialize_app", _initialize_app)

    try:
        firebase_module = importlib.import_module("app.core.firebase")
    except ModuleNotFoundError:
        pass  # section 02 has not landed yet
    else:
        # Covers a `from firebase_admin.auth import verify_id_token` style
        # re-export, which patching the SDK module alone would not reach.
        if hasattr(firebase_module, "verify_id_token"):
            monkeypatch.setattr(firebase_module, "verify_id_token", _verify_id_token)

    return tokens


def _invalid_id_token_error(firebase_auth: Any) -> Exception:
    """The error the real SDK raises for a bad or expired token."""
    error_cls = getattr(firebase_auth, "InvalidIdTokenError", None)
    message = "Fake Firebase: this token was not registered with firebase_tokens"
    if error_cls is None:  # pragma: no cover - the SDK always defines it
        return ValueError(message)
    try:
        return error_cls(message)
    except TypeError:  # pragma: no cover - defensive
        return ValueError(message)


# --- Socket.IO (01-backend-skeleton.md §4) ---------------------------------


class FakeSocketManager:
    """Stand-in for ``app.sockets.manager.socket_manager``.

    Mirrors the frozen ``SocketManager`` surface and records every emit as
    ``(target, event, data)`` instead of touching transport, so a handler runs
    as real code against fakes for its two I/O dependencies.  ``target`` is
    the room id for ``emit_to_room`` and the sid for ``emit_to_sid``.
    """

    def __init__(self) -> None:
        self.sid_to_room: dict[str, str] = {}
        self.sid_to_alias: dict[str, str] = {}
        self.sid_to_identity: dict[str, str] = {}
        self.emits: list[tuple[str, str, Any]] = []
        self.room_emits: list[tuple[str, str, Any]] = []
        self.sid_emits: list[tuple[str, str, Any]] = []

    async def connect(self, sid: str, environ: dict) -> None:
        return None

    async def disconnect(self, sid: str) -> None:
        self.sid_to_room.pop(sid, None)
        self.sid_to_alias.pop(sid, None)
        self.sid_to_identity.pop(sid, None)

    async def join_room(self, sid: str, room_id: str, alias: str) -> None:
        self.sid_to_room[sid] = room_id
        self.sid_to_alias[sid] = alias

    async def leave_room(self, sid: str, room_id: str) -> None:
        if self.sid_to_room.get(sid) == room_id:
            self.sid_to_room.pop(sid, None)
            self.sid_to_alias.pop(sid, None)

    async def emit_to_room(self, room_id: str, event: str, data: Any) -> None:
        record = (room_id, event, data)
        self.emits.append(record)
        self.room_emits.append(record)

    async def emit_to_sid(self, sid: str, event: str, data: Any) -> None:
        record = (sid, event, data)
        self.emits.append(record)
        self.sid_emits.append(record)

    # -- read helpers -------------------------------------------------------

    def emits_for(self, target: str) -> list[tuple[str, Any]]:
        """``[(event, data), ...]`` for one room id or sid."""
        return [(event, data) for who, event, data in self.emits if who == target]

    def events_for(self, target: str) -> list[str]:
        return [event for event, _data in self.emits_for(target)]

    def clear(self) -> None:
        self.emits.clear()
        self.room_emits.clear()
        self.sid_emits.clear()


@pytest.fixture()
def fake_socket_manager(monkeypatch) -> FakeSocketManager:
    """A ``FakeSocketManager`` patched over the ``socket_manager`` singleton.

    Every already-imported ``app.*`` module holding a reference to the
    singleton is repointed at the fake, because a handler that did
    ``from ..manager import socket_manager`` holds its own binding.  Like
    ``fake_redis``, it tolerates the module not existing yet.
    """
    fsm = FakeSocketManager()
    try:
        manager_module = importlib.import_module("app.sockets.manager")
    except ModuleNotFoundError:
        return fsm  # a section-01-only checkout must still collect

    monkeypatch.setattr(manager_module, "socket_manager", fsm, raising=False)
    for name, module in list(sys.modules.items()):
        if not name.startswith("app.") or module is None or module is manager_module:
            continue
        if hasattr(module, "socket_manager"):
            monkeypatch.setattr(module, "socket_manager", fsm, raising=False)
    return fsm


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
