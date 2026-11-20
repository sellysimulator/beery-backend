"""Toolchain, migrations, image and lint gates."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Runtime pins, verbatim.
RUNTIME_PINS = {
    "fastapi": "0.141.1",
    "uvicorn": "0.53.0",
    "python-socketio": "5.17.0",
    "pydantic": "2.13.5",
    "pydantic-settings": "2.15.0",
    "sqlalchemy": "2.0.54",
    "psycopg": "3.3.9",
    "alembic": "1.20.0",
    "firebase-admin": "7.6.0",
    "python-dotenv": "1.2.3",
    "httpx": "0.28.1",
    "redis": "8.1.0",
}

# Dev pins.  ``testcontainers`` is checked by its own test below.
DEV_PINS = {
    "pytest": "9.1.1",
    "pytest-asyncio": "1.4.0",
    "pytest-cov": "7.1.0",
    "pytest-mock": "3.15.1",
    "black": "26.5.1",
    "ruff": "0.16.8",
    "mypy": "2.3.1",
}


def _container_class(module: str, name: str):
    """``testcontainers.<module>`` moved to ``testcontainers.community.<module>``;
    ``requirements-dev.txt`` pins ``testcontainers[postgres]``; accept
    either layout rather than a DeprecationWarning per run."""
    import importlib

    for candidate in (f"testcontainers.community.{module}", f"testcontainers.{module}"):
        try:
            return getattr(importlib.import_module(candidate), name)
        except (ImportError, AttributeError):
            continue
    pytest.skip(f"no testcontainers {module} module available")


def _installed(distribution: str) -> str | None:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"],
                capture_output=True,
                check=False,
                timeout=60,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _poll_until_served(url: str, timeout: float) -> dict:
    """Poll ``url`` until it answers 200, or fail after ``timeout`` seconds."""
    import json
    import time
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            time.sleep(0.5)
    raise AssertionError(f"{url} never answered within {timeout}s: {last!r}")


# --- toolchain and pins -----------------------------------------------------


def test_running_on_python_311():
    """The image runs ``python:3.11-slim``; the venv must match it."""
    assert sys.version_info[:2] == (
        3,
        11,
    ), "create the venv with python3.11 -m venv venv, never bare python3"


def test_runtime_dependencies_are_installed_at_the_declared_pins():
    """``pip install -r requirements.txt`` succeeded, at the declared pins."""
    mismatches = {
        name: (expected, _installed(name))
        for name, expected in RUNTIME_PINS.items()
        if _installed(name) != expected
    }
    assert mismatches == {}


def test_dev_dependencies_are_installed_at_the_declared_pins():
    """``pip install -r requirements-dev.txt`` succeeded."""
    mismatches = {
        name: (expected, _installed(name))
        for name, expected in DEV_PINS.items()
        if _installed(name) != expected
    }
    assert mismatches == {}


def test_testcontainers_is_installed_and_pinned():
    """``testcontainers[postgres]==4.15.0`` -- it must actually be installed
    or the ``dbschema`` suite has no PostgreSQL to migrate."""
    assert _installed("testcontainers") == "4.15.0"
    assert "testcontainers[postgres]==4.15.0" in (
        (PROJECT_ROOT / "requirements-dev.txt").read_text().split()
    )


def test_asyncio_is_not_a_declared_dependency():
    """The PyPI package named ``asyncio`` is an
    abandoned 2015 backport that shadows the standard library module."""
    assert (
        _installed("asyncio") is None
    ), "the asyncio PyPI package is installed; it shadows the stdlib module"


# --- migrations -------------------------------------------------------------


def test_alembic_upgrade_head_creates_exactly_the_declared_tables():
    """``alembic upgrade head`` runs against an empty database and creates
    ``alembic_version`` plus exactly the tables the models declare.

    Comparing against ``Base.metadata`` rather than a fixed table list holds
    for every later revision as well, and still catches the thing this test is for: a
    migration that creates a table nobody declared, or fails to create one
    somebody did."""
    pytest.importorskip("testcontainers.postgres")
    if not _docker_available():
        pytest.skip("Docker is not available; cannot start a throwaway PostgreSQL")

    import sqlalchemy

    PostgresContainer = _container_class("postgres", "PostgresContainer")

    with PostgresContainer("postgres:17", driver="psycopg") as pg:
        env = dict(os.environ)
        env.update(
            {
                "DB_HOST": pg.get_container_host_ip(),
                "DB_PORT": str(pg.get_exposed_port(5432)),
                "DB_USER": pg.username,
                "DB_PASSWORD": pg.password,
                "DB_DATABASE": pg.dbname,
                "DB_REQUIRE_SSL": "False",
                "DB_SSL_CA": "",
            }
        )
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

        engine = sqlalchemy.create_engine(pg.get_connection_url())
        try:
            tables = set(sqlalchemy.inspect(engine).get_table_names())
        finally:
            engine.dispose()

    import app.models  # noqa: F401  -- populates Base.metadata
    from app.db.base import Base

    assert tables == {"alembic_version"} | set(Base.metadata.tables)


# --- the Docker image -------------------------------------------------------

_STARTUP_MIGRATION = "alembic upgrade head && "


def _serve_only_cmd() -> list[str]:
    """The Dockerfile's exec-form ``CMD`` without its startup migration
    (``alembic upgrade head && exec uvicorn ...``): this test has no database,
    so it runs exactly the committed uvicorn command and nothing before it."""
    text = (PROJECT_ROOT / "Dockerfile").read_text().replace("\\\n", "")
    match = re.search(r"^CMD\s*(\[.*\])\s*$", text, re.MULTILINE)
    assert match, "Dockerfile has no exec-form CMD"
    argv = json.loads(match.group(1))
    assert argv[-1].startswith(_STARTUP_MIGRATION)
    return [*argv[:-1], argv[-1][len(_STARTUP_MIGRATION) :]]


def test_docker_image_builds_and_runs():
    """``docker build .`` succeeds and the resulting image runs the app.

    Opt-in: a cold build pulls ``python:3.11-slim`` and installs the whole
    requirement set, which is minutes, not seconds.  Set
    ``BEERY_DOCKER_BUILD_TEST=1`` to run it (CI should)."""
    if os.environ.get("BEERY_DOCKER_BUILD_TEST") != "1":
        pytest.skip("set BEERY_DOCKER_BUILD_TEST=1 to run the image build")
    if not _docker_available():
        pytest.skip("Docker is not available")

    tag = "beery-backend-skeleton-test:latest"
    build = subprocess.run(
        ["docker", "build", "-t", tag, "."],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=1800,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    started = subprocess.run(
        ["docker", "run", "-d", "-P", tag, *_serve_only_cmd()],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert started.returncode == 0, started.stdout + started.stderr
    container = started.stdout.strip()
    try:
        mapping = subprocess.run(
            ["docker", "port", container, "8080/tcp"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert mapping.returncode == 0, mapping.stdout + mapping.stderr
        port = int(mapping.stdout.strip().splitlines()[0].rsplit(":", 1)[1])
        body = _poll_until_served(f"http://127.0.0.1:{port}/", timeout=120.0)
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            capture_output=True,
            check=False,
            timeout=120,
        )
    assert body["status"] == "ok"


# --- lint and type gates ----------------------------------------------------


@pytest.mark.parametrize(
    "tool,args",
    [
        ("black", ["--check", "app"]),
        ("ruff", ["check", "app"]),
        ("mypy", ["app/core"]),
    ],
)
def test_lint_and_type_gates_are_clean(tool, args):
    """``black --check app``, ``ruff check app`` and ``mypy app/core`` are
    clean."""
    pytest.importorskip(tool)
    proc = subprocess.run(
        [sys.executable, "-m", tool, *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# --- scaffolding exists -----------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        "requirements.txt",
        "requirements-dev.txt",
        "pytest.ini",
        "Dockerfile",
        ".dockerignore",
        ".env.example",
        ".gitignore",
        "alembic.ini",
        "alembic/env.py",
    ],
)
def test_scaffold_file_exists(relative):
    """The toolchain, image and migration files the project stands on."""
    assert (PROJECT_ROOT / relative).exists(), f"{relative} is missing"
