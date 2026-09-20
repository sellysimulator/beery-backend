"""``app/main.py`` -- assembly, routes, CORS (01 §2, §3.1).

Acceptance criteria 2, 3, 4, 11 and 15, and failure mode 5.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app, application

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# A routable-but-dead RFC1918 address: a client that really dials it blocks
# until its own timeout instead of failing fast the way localhost does.
BLACKHOLE_REDIS_URL = "redis://10.255.255.1:6379/0"


def _tcp_open(host: str, port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _redis_reachable() -> bool:
    url = settings.REDIS_URL
    if not url:
        return False
    parsed = urlparse(url)
    if not parsed.hostname:
        return False
    return _tcp_open(parsed.hostname, parsed.port or 6379)


def _database_reachable() -> bool:
    return _tcp_open(settings.DB_HOST, settings.DB_PORT)


# --- AC 2 -------------------------------------------------------------------


def test_root_route_payload(client):
    """AC 2: ``GET /`` -> ``{"app": ..., "version": ..., "status": "ok"}``."""
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"app", "version", "status"}
    assert body["status"] == "ok"
    assert body["app"] == settings.APP_NAME
    assert body["version"] == settings.VERSION


def test_root_route_is_served_through_the_socketio_asgi_app():
    """AC 2 / AC 5: ``uvicorn app.main:application`` is the only supported
    entry point, so the REST app must still answer through the wrapper."""
    response = TestClient(application).get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# --- AC 3 / FM 5 ------------------------------------------------------------


def test_shallow_health_is_exactly_status_ok(client):
    """AC 3: ``GET /api/v1/health`` -> ``{"status": "ok"}``."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_shallow_health_survives_a_dead_redis(monkeypatch, client):
    """FM 5 / AC 3: the frontend polls this as a cold-start wake-up probe, so
    it must stay dependency-free -- a dead Redis may not slow it or fail it."""
    monkeypatch.setattr(settings, "REDIS_URL", BLACKHOLE_REDIS_URL, raising=False)
    started = time.monotonic()
    response = client.get("/api/v1/health")
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert elapsed < 5.0, f"health took {elapsed:.1f}s; it dialled something"


def test_shallow_health_survives_an_unreachable_database(monkeypatch, client):
    """AC 3: "never touches DB or Redis" -- a slow database must not turn the
    liveness probe into an app that appears dead."""
    monkeypatch.setattr(settings, "DB_HOST", "10.255.255.1", raising=False)
    monkeypatch.setattr(settings, "DB_PORT", 3306, raising=False)
    started = time.monotonic()
    response = client.get("/api/v1/health")
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert elapsed < 5.0, f"health took {elapsed:.1f}s; it dialled something"


# --- AC 4 -------------------------------------------------------------------


def test_deep_health_shape_is_always_http_200(client):
    """AC 4: always HTTP 200, with every flag reported.

    ``state_backend`` was added when Redis became optional: ``redis`` alone
    cannot distinguish "reachable" from "not in use", and a reader needs to
    know which.
    """
    response = client.get("/api/v1/health/deep")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "database", "redis", "state_backend"}
    assert body["status"] in {"ok", "degraded"}
    assert isinstance(body["database"], bool)
    assert isinstance(body["redis"], bool)
    assert body["state_backend"] in {"redis", "memory"}


def test_deep_health_with_redis_off_ignores_redis_entirely(client, monkeypatch):
    """Redis switched off must not pin the probe at ``degraded`` forever.

    A readiness probe that always says "not ready" is a probe nobody reads,
    so an unused dependency reports healthy and ``state_backend`` carries
    the fact that it is unused.
    """
    from app.api.v1 import health as health_module

    monkeypatch.setattr(health_module.settings, "REDIS_ENABLED", False)
    body = client.get("/api/v1/health/deep").json()
    assert body["state_backend"] == "memory"
    assert body["redis"] is True
    # The database is still pinned unreachable by `tests/conftest.py`.
    assert body["database"] is False
    assert body["status"] == "degraded"


def test_deep_health_is_degraded_when_neither_backend_is_reachable(client, monkeypatch):
    """AC 4: ``status: "degraded"`` and both flags ``false``.

    ``tests/conftest.py`` pins the whole suite at unreachable, test-only
    coordinates, so "neither is reachable" is a property of the configuration
    rather than of whatever happens to be listening on this machine.
    Redis has to be switched **on** for its reachability to be asked about
    at all."""
    from app.api.v1 import health as health_module

    monkeypatch.setattr(health_module.settings, "REDIS_ENABLED", True)
    assert not _database_reachable()
    assert not _redis_reachable()
    response = client.get("/api/v1/health/deep")
    assert response.status_code == 200
    body = response.json()
    assert body["database"] is False
    assert body["redis"] is False
    assert body["status"] == "degraded"


# The intermediate cases need a backing service that really answers, and the
# app reads its coordinates at import, so each combination is observed in a
# fresh interpreter pointed at a throwaway container.

DEEP_HEALTH_PROBE = (
    "import json\n"
    "from fastapi.testclient import TestClient\n"
    "from app.main import app\n"
    "print(json.dumps(TestClient(app).get('/api/v1/health/deep').json()))\n"
)


def _deep_health_with(**overrides) -> dict:
    env = dict(os.environ)
    env.update(overrides)
    proc = subprocess.run(
        [sys.executable, "-c", DEEP_HEALTH_PROBE],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _container_class(module: str, name: str):
    """``testcontainers.<module>`` moved to ``testcontainers.community.<module>``;
    the plan pins ``testcontainers[mysql]`` but not its version, so accept
    either layout rather than a DeprecationWarning per run."""
    import importlib

    for candidate in (f"testcontainers.community.{module}", f"testcontainers.{module}"):
        try:
            return getattr(importlib.import_module(candidate), name)
        except (ImportError, AttributeError):
            continue
    pytest.skip(f"no testcontainers {module} module available")


def _require_docker() -> None:
    pytest.importorskip("testcontainers")
    if shutil.which("docker") is None:
        pytest.skip("Docker is not available; cannot start a throwaway backend")


@pytest.fixture(scope="module")
def live_redis_url() -> str:
    _require_docker()
    RedisContainer = _container_class("redis", "RedisContainer")

    with RedisContainer("redis:7-alpine") as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"


@pytest.fixture(scope="module")
def live_mysql_env() -> dict:
    _require_docker()
    MySqlContainer = _container_class("mysql", "MySqlContainer")

    with MySqlContainer("mysql:8.0") as container:
        yield {
            "DB_HOST": container.get_container_host_ip(),
            "DB_PORT": str(container.get_exposed_port(3306)),
            "DB_USER": container.username,
            "DB_PASSWORD": container.password,
            "DB_DATABASE": container.dbname,
            "DB_REQUIRE_SSL": "False",
        }


def test_deep_health_is_degraded_when_only_redis_is_reachable(live_redis_url):
    """AC 4 / §2: ``status`` is ``"ok"`` **only** when both flags are true --
    exactly one reachable is still ``"degraded"``.  A partial outage is the
    one a health check exists to surface."""
    body = _deep_health_with(REDIS_ENABLED="true", REDIS_URL=live_redis_url)
    assert body["redis"] is True
    assert body["database"] is False
    assert body["status"] == "degraded"


def test_deep_health_is_degraded_when_only_the_database_is_reachable(live_mysql_env):
    """AC 4 / §2: the mirror image of the case above."""
    body = _deep_health_with(REDIS_ENABLED="true", **live_mysql_env)
    assert body["database"] is True
    assert body["redis"] is False
    assert body["status"] == "degraded"


def test_deep_health_is_ok_only_when_both_are_reachable(live_redis_url, live_mysql_env):
    """AC 4 / §2: the positive half of the same rule."""
    body = _deep_health_with(
        REDIS_ENABLED="true", REDIS_URL=live_redis_url, **live_mysql_env
    )
    assert body["database"] is True
    assert body["redis"] is True
    assert body["status"] == "ok"


# --- AC 11 ------------------------------------------------------------------


def test_cors_allows_a_configured_origin(client):
    origin = settings.CORS_ORIGINS[0]
    response = client.get("/", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == origin
    assert response.headers.get("access-control-allow-credentials") == "true"


def test_cors_rejects_an_unconfigured_origin(client):
    """AC 11: an origin outside ``CORS_ORIGINS`` gets no allow header, so the
    browser drops the response."""
    response = client.get("/", headers={"Origin": "https://evil.example.com"})
    assert response.headers.get("access-control-allow-origin") is None


def test_cors_preflight_from_an_unconfigured_origin_is_not_allowed(client):
    response = client.options(
        "/api/v1/health",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.headers.get("access-control-allow-origin") is None


# --- AC 15 ------------------------------------------------------------------


@pytest.mark.parametrize("word", ["rooms", "users", "lobby", "play"])
def test_main_names_no_individual_router_or_handler_module(word):
    """AC 15 / D19: ``app/main.py`` is written once and never edited again, so
    it may name no member of the three assembly packages."""
    source = (PROJECT_ROOT / "app" / "main.py").read_text()
    assert not re.search(rf"\b{word}\b", source, flags=re.IGNORECASE), (
        f"app/main.py mentions {word!r}; section 01 owns this file outright "
        "and later sections register by dropping a module into a package"
    )


def test_app_is_a_fastapi_application():
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)


def test_application_wraps_app_in_a_socketio_asgi_app():
    """AC 5: ``application`` is a ``socketio.ASGIApp`` wrapping ``app``."""
    import socketio

    assert isinstance(application, socketio.ASGIApp)
    assert application.other_asgi_app is app
