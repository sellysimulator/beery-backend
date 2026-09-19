"""The three assembly packages (01 §2 "The three assembly packages", D19).

Acceptance criteria 14, 16, 17 and 18, and failure mode 7.

Every later backend section registers its behaviour by dropping a module into
one of these packages.  If discovery assumes at least one member, or if a
failing check can abort startup, the build stops at section 02.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest
from fastapi import APIRouter

import app.api.v1 as api_v1
import app.core.checks as checks_pkg

PROJECT_ROOT = Path(__file__).resolve().parents[2]

ASSEMBLY_PACKAGES = [
    "app.api.v1",
    "app.core.checks",
    "app.sockets.handlers",
]


def _paths(routers) -> set[str]:
    found = set()
    for router in routers:
        for route in router.routes:
            found.add(getattr(route, "path", ""))
    return found


# --- AC 14 / FM 7 -----------------------------------------------------------


@pytest.mark.parametrize("package_name", ASSEMBLY_PACKAGES)
def test_assembly_package_imports_cleanly(package_name):
    """AC 14: all three packages import cleanly."""
    import importlib

    assert importlib.import_module(package_name) is not None


@pytest.mark.parametrize("package_name", ASSEMBLY_PACKAGES)
def test_assembly_package_imports_cleanly_while_empty(package_name, make_empty_package):
    """AC 14 / FM 7: "Every one of the three must work while empty".

    ``app/api/v1/`` already ships ``health.py`` in this very section, so the
    empty state is reproduced against a copy of the discovery ``__init__.py``
    with no members beside it -- ``iter_modules`` over an empty package yields
    nothing, and that must not raise."""
    assert make_empty_package(package_name) is not None


def test_all_routers_on_an_empty_package_returns_empty_list(make_empty_package):
    """AC 14 / FM 7: ``all_routers()`` returns ``[]`` rather than raising."""
    empty = make_empty_package("app.api.v1")
    assert empty.all_routers() == []


def test_run_startup_checks_on_an_empty_package_is_a_no_op(make_empty_package):
    """AC 14 / FM 7: ``run_startup_checks()`` is a no-op with no members."""
    empty = make_empty_package("app.core.checks")
    assert empty.run_startup_checks() is None


def test_importing_the_handlers_package_succeeds():
    """AC 14: ``import app.sockets.handlers`` succeeds -- ``app/main.py``
    imports it purely for the side effect of the discovery loop (§3.1 step 6),
    and in this section it has no members at all."""
    from app.sockets import handlers

    assert handlers.__name__ == "app.sockets.handlers"


def test_all_routers_returns_a_list_of_routers():
    routers = api_v1.all_routers()
    assert isinstance(routers, list)
    assert all(isinstance(r, APIRouter) for r in routers)


# --- AC 16 ------------------------------------------------------------------


ROUTER_MODULE = textwrap.dedent('''
    """Temporary module written by a test (01 §5 item 16)."""

    from fastapi import APIRouter

    router = APIRouter()


    @router.get("/tests-drop-in-probe")
    def probe() -> dict:
        return {"probe": True}
    ''')


def test_a_dropped_in_router_module_is_discovered(drop_module):
    """AC 16: a module dropped into ``app/api/v1/`` exporting a ``router`` is
    picked up by ``all_routers()`` with no edit anywhere else."""
    reloaded = drop_module("app.api.v1", "zzz_probe_router", ROUTER_MODULE)
    assert "/tests-drop-in-probe" in _paths(reloaded.all_routers())


def test_dropped_in_routers_come_back_in_module_name_order(drop_module):
    """§2: "every discovered module's ``router``, in module-name order"."""
    drop_module("app.api.v1", "aaa_probe_router", ROUTER_MODULE)
    reloaded = drop_module("app.api.v1", "zzz_probe_router_two", ROUTER_MODULE)
    routers = reloaded.all_routers()
    owners = [
        index
        for index, router in enumerate(routers)
        if "/tests-drop-in-probe" in _paths([router])
    ]
    assert len(owners) == 2
    # ``aaa_…`` sorts before every other module in the package; ``zzz_…``
    # after every one of them.
    assert owners[0] == 0
    assert owners[-1] == len(routers) - 1


def test_dropped_in_router_does_not_require_editing_main(drop_module):
    """AC 16 / D19: discovery is the whole registration mechanism."""
    source = (PROJECT_ROOT / "app" / "main.py").read_text()
    drop_module("app.api.v1", "zzz_probe_router_three", ROUTER_MODULE)
    assert "zzz_probe_router_three" not in source


# --- AC 17 ------------------------------------------------------------------


CHECK_MODULE = textwrap.dedent('''
    """Temporary module written by a test (01 §5 item 17)."""

    import os
    import pathlib

    from app.core.checks import register_check


    @register_check
    def probe_check() -> None:
        target = os.environ.get("BEERY_TEST_CHECK_SENTINEL")
        if target:
            pathlib.Path(target).write_text("ran")
    ''')

RAISING_CHECK_MODULE = textwrap.dedent('''
    """Temporary module written by a test (01 §5 item 18)."""

    import os
    import pathlib

    from app.core.checks import register_check


    @register_check
    def exploding_check() -> None:
        raise RuntimeError("this check is supposed to blow up")


    @register_check
    def survivor_check() -> None:
        target = os.environ.get("BEERY_TEST_SURVIVOR_SENTINEL")
        if target:
            pathlib.Path(target).write_text("ran")
    ''')


def test_a_dropped_in_check_module_runs_at_startup(drop_module, monkeypatch, tmp_path):
    """AC 17: a module dropped into ``app/core/checks/`` calling
    ``register_check`` runs at startup -- this is exactly how section 02 adds
    the Firebase check without editing ``app/main.py`` (§3.2)."""
    from fastapi.testclient import TestClient

    sentinel = tmp_path / "check-ran.txt"
    monkeypatch.setenv("BEERY_TEST_CHECK_SENTINEL", str(sentinel))
    drop_module("app.core.checks", "zzz_probe_check", CHECK_MODULE)

    from app.main import app

    assert not sentinel.exists()
    with TestClient(app):
        pass
    assert sentinel.exists()


def test_register_check_returns_the_function():
    """§2: ``register_check`` is usable as a decorator, so it must hand the
    function back."""

    def probe() -> None:
        return None

    assert checks_pkg.register_check(probe) is probe


# --- AC 18 ------------------------------------------------------------------


def test_a_raising_check_does_not_stop_the_others(caplog):
    """AC 18: an exception in one check is logged and does not abort the
    others."""
    survivors: list[str] = []

    def exploding() -> None:
        raise RuntimeError("this check is supposed to blow up")

    def survivor() -> None:
        survivors.append("ran")

    checks_pkg.register_check(exploding)
    checks_pkg.register_check(survivor)

    with caplog.at_level(logging.DEBUG):
        assert checks_pkg.run_startup_checks() is None

    assert survivors == ["ran"]
    assert any(
        record.levelno >= logging.WARNING for record in caplog.records
    ), "the failing check was swallowed silently"


def test_a_raising_check_does_not_abort_startup(drop_module, monkeypatch, tmp_path):
    """AC 18: "never aborts startup" -- a misconfigured backing service must
    degrade the app, not refuse to boot it."""
    from fastapi.testclient import TestClient

    sentinel = tmp_path / "survivor-ran.txt"
    monkeypatch.setenv("BEERY_TEST_SURVIVOR_SENTINEL", str(sentinel))
    drop_module("app.core.checks", "zzz_raising_check", RAISING_CHECK_MODULE)

    from app.main import app

    with TestClient(app) as c:
        assert c.get("/api/v1/health").status_code == 200
    assert sentinel.exists()
