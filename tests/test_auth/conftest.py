"""Fixtures for the identity and authentication tests.

Firebase is **not** faked here.  ``firebase_tokens`` and ``fake_socket_manager``
live in the shared ``tests/conftest.py`` because the auth, lobby and play tests
all need them; a per-area copy would be three implementations of one fake,
drifting apart.  This file adds only what is genuinely this package's:

* **A database.** The shared conftest pins the ``DB_*`` variables at an
  unreachable host on purpose and provides no session.  This package needs one,
  so it builds a SQLite in-memory engine, runs ``Base.metadata.create_all()``
  against it, and exposes ``db_session`` plus an ``api_client`` that overrides
  the ``get_db`` dependency for the duration of a route test.

  SQLite is the right choice *here* and the wrong one for the schema tests:
  this package tests upsert **logic** -- that the uid comes from the verified
  token, that a second call updates rather than inserts, that ``display_name``
  is truncated -- none of which depends on the dialect.  The ``dbschema`` suite
  tests the **schema**, which does, and uses Testcontainers against a real
  PostgreSQL for exactly that reason.  There is deliberately no Docker
  dependency here.

* **A cold Firebase singleton between tests.**  ``init_firebase`` memoises a
  successful initialisation for the life of the process, so without a reset
  the first test to call ``configure()`` would leave every later test
  configured -- and the reject-when-unconfigured tests need the unconfigured
  state to be real.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def _reset_firebase_singleton() -> None:
    """Return ``init_firebase``'s lazy singleton to its cold state.

    Reloading the module resets its globals in place (``importlib.reload``
    reuses the module's ``__dict__``, so a function already imported elsewhere
    sees the reset too).  ``cache_clear`` covers the other plausible spelling
    of "lazy singleton", an ``lru_cache``-wrapped function, whose cache lives
    on the function object rather than in the module.  Neither touches a
    private name, and neither asserts anything.
    """
    for holder_name in ("app.core.firebase", "app.api.deps"):
        try:
            holder = importlib.import_module(holder_name)
        except ModuleNotFoundError:  # pragma: no cover - module absent
            continue
        cache_clear = getattr(
            getattr(holder, "init_firebase", None), "cache_clear", None
        )
        if callable(cache_clear):
            cache_clear()
    try:
        module = importlib.import_module("app.core.firebase")
    except ModuleNotFoundError:  # pragma: no cover - module absent
        return
    importlib.reload(module)


def _clear_socket_maps() -> None:
    """Empty the four public sid maps on the manager singleton."""
    from app.sockets.manager import socket_manager

    socket_manager.sid_to_room.clear()
    socket_manager.sid_to_alias.clear()
    socket_manager.sid_to_identity.clear()
    socket_manager.sid_to_verified_uid.clear()


@pytest.fixture(autouse=True)
def _isolate_auth_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test in this package starts unconfigured and with empty maps.

    "Unconfigured" is the default deliberately -- it is also the shared
    ``firebase_tokens`` fixture's default -- so a test that wants a working
    Firebase says ``firebase_tokens.configure()`` out loud, and nothing is ever
    trusted by accident of the order tests happened to run in.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "FIREBASE_SERVICE_ACCOUNT_JSON", "", raising=False)
    _reset_firebase_singleton()
    _clear_socket_maps()
    yield
    _clear_socket_maps()
    _reset_firebase_singleton()


@pytest.fixture()
def sock_mgr(_isolate_auth_state: None):
    """The real ``socket_manager``, with its four maps emptied.

    Not a fake: the handshake's whole observable effect is what it writes into
    ``sid_to_identity``, so the tests read it from the real singleton.  (The
    shared ``fake_socket_manager`` exists for handlers that emit; the paths
    tested here emit nothing, so there is nothing for it to record.)
    """
    from app.sockets.manager import socket_manager

    return socket_manager


# --- Database ----------------------------------------------------------------


@pytest.fixture()
def db_engine() -> Iterator[Engine]:
    """A SQLite in-memory engine with the mapped schema created.

    ``StaticPool`` + ``check_same_thread=False`` keeps every connection on the
    single in-memory database, including the one ``TestClient`` uses from its
    own thread.  A fresh engine per test is what isolates tests from each
    other; the schema is rebuilt each time.
    """
    import app.models.user  # noqa: F401  -- registers `users` on Base.metadata
    from app.models.base import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def db_session(db_engine: Engine) -> Iterator[Session]:
    """A session bound to the in-memory engine; rolled back and closed after."""
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture()
def api_client(client, db_session: Session):
    """The shared ``TestClient`` with ``get_db`` overridden for this test.

    The override is removed afterwards whatever the test does, so no route
    test in any other package inherits this package's SQLite session.
    """
    from app.db.session import get_db
    from app.main import app

    def _override_get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture()
def bearer() -> Callable[[str], dict[str, str]]:
    """``bearer("tok")`` -> the ``Authorization`` header a client would send."""

    def _bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    return _bearer
