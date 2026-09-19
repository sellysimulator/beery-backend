"""Startup-check registry and discovery point (00-decisions.md D19).

A later section adds a check by dropping a module into this package that calls
`register_check` at import time. Nothing upstream is edited, and the package
works while it is empty.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable

logger = logging.getLogger(__name__)

Check = Callable[[], None]

_checks: list[Check] = []


def _discover() -> None:
    """Import every module beside this one, so its registrations happen."""
    importlib.invalidate_caches()
    for _finder, name, _ispkg in pkgutil.iter_modules(__path__):
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception:
            logger.exception("Startup check module %r could not be imported.", name)


def register_check(fn: Check) -> Check:
    """Register `fn` to be run by `run_startup_checks`. Usable as a decorator."""
    if fn not in _checks:
        _checks.append(fn)
    return fn


def run_startup_checks() -> None:
    """Run every registered check.

    A check that raises is logged and neither aborts the remaining checks nor
    aborts startup.
    """
    _discover()
    for check in _checks.copy():
        name = getattr(check, "__name__", repr(check))
        try:
            check()
        except Exception:
            logger.exception("Startup check %r failed.", name)


_discover()
