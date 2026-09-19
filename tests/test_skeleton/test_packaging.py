"""Toolchain, migrations, image and lint gates (01 §3.7-3.10).

Acceptance criteria 1, 12, 21 and 22.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 01-backend-skeleton.md §3.8 -- runtime pins, verbatim.
RUNTIME_PINS = {
    "fastapi": "0.141.1",
    "uvicorn": "0.53.0",
    "python-socketio": "5.17.0",
    "pydantic": "2.13.5",
    "pydantic-settings": "2.15.0",
    "sqlalchemy": "2.0.54",
    "pymysql": "1.2.3",
    "alembic": "1.20.0",
    "firebase-admin": "7.6.0",
    "python-dotenv": "1.2.3",
    "httpx": "0.28.1",
    "redis": "8.1.0",
}

# §3.8 -- dev pins.  ``testcontainers`` is deliberately unpinned in the plan.
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
    the plan pins ``testcontainers[mysql]`` but not its version, so accept
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


# --- AC 1 -------------------------------------------------------------------


def test_running_on_python_311():
    """§3.8: the image runs ``python:3.11-slim``; the venv must match it."""
    assert sys.version_info[:2] == (
        3,
        11,
    ), "create the venv with python3.11 -m venv venv, never bare python3"


def test_runtime_dependencies_are_installed_at_the_declared_pins():
    """AC 1: ``pip install -r requirements.txt`` succeeded, at §3.8's pins."""
    mismatches = {
        name: (expected, _installed(name))
        for name, expected in RUNTIME_PINS.items()
        if _installed(name) != expected
    }
    assert mismatches == {}


def test_dev_dependencies_are_installed_at_the_declared_pins():
    """AC 1: ``pip install -r requirements-dev.txt`` succeeded."""
    mismatches = {
        name: (expected, _installed(name))
        for name, expected in DEV_PINS.items()
        if _installed(name) != expected
    }
    assert mismatches == {}


def test_testcontainers_is_installed_and_pinned():
    """§3.8: ``testcontainers[mysql]`` -- version resolved at install time, but
    it must actually be installed or section 13 has no MySQL to migrate."""
    assert _installed("testcontainers") is not None


def test_asyncio_is_not_a_declared_dependency():
    """00-conventions.md §4: the PyPI package named ``asyncio`` is an
    abandoned 2015 backport that shadows the standard library module."""
    assert (
        _installed("asyncio") is None
    ), "the asyncio PyPI package is installed; it shadows the stdlib module"


# --- AC 12 ------------------------------------------------------------------


def test_alembic_upgrade_head_creates_exactly_the_declared_tables():
    """AC 12 / §3.7: ``alembic upgrade head`` runs against an empty database and
    creates ``alembic_version`` plus exactly the tables the models declare.

    This asserted ``only alembic_version`` until section 13 shipped the first
    revision, which made it true for exactly as long as there were no
    migrations -- the same expiring-criterion trap as section 16's empty route
    registry.  Comparing against ``Base.metadata`` instead holds for every
    later revision as well, and still catches the thing this test is for: a
    migration that creates a table nobody declared, or fails to create one
    somebody did."""
    pytest.importorskip("testcontainers.mysql")
    if not _docker_available():
        pytest.skip("Docker is not available; cannot start a throwaway MySQL")

    import sqlalchemy

    MySqlContainer = _container_class("mysql", "MySqlContainer")

    with MySqlContainer("mysql:8.4") as mysql:
        env = dict(os.environ)
        env.update(
            {
                "DB_HOST": mysql.get_container_host_ip(),
                "DB_PORT": str(mysql.get_exposed_port(3306)),
                "DB_USER": mysql.username,
                "DB_PASSWORD": mysql.password,
                "DB_DATABASE": mysql.dbname,
                "DB_REQUIRE_SSL": "False",
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

        url = (
            f"mysql+pymysql://{mysql.username}:{mysql.password}"
            f"@{env['DB_HOST']}:{env['DB_PORT']}/{mysql.dbname}"
        )
        engine = sqlalchemy.create_engine(url)
        try:
            tables = set(sqlalchemy.inspect(engine).get_table_names())
        finally:
            engine.dispose()

    import app.models  # noqa: F401  -- populates Base.metadata
    from app.db.base import Base

    assert tables == {"alembic_version"} | set(Base.metadata.tables)


# --- AC 21 ------------------------------------------------------------------


def test_docker_image_builds_and_runs():
    """AC 21: ``docker build .`` succeeds and the resulting image runs the app.

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
        ["docker", "run", "-d", "-P", tag],
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


# --- AC 22 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,args",
    [
        ("black", ["--check", "app"]),
        ("ruff", ["check", "app"]),
        ("mypy", ["app/core"]),
    ],
)
def test_lint_and_type_gates_are_clean(tool, args):
    """AC 22: ``black --check app``, ``ruff check app`` and ``mypy app/core``
    are clean -- ``app/core`` is empty in this section, which still must not
    error."""
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


# --- §3.9 / §3.10 scaffolding exists ---------------------------------------


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
    """§3.7-3.10: the files the section is responsible for standing up."""
    assert (PROJECT_ROOT / relative).exists(), f"{relative} is missing"
