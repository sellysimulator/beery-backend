"""Versioned REST package and router discovery point (00-decisions.md D19).

A later section adds an endpoint by dropping a module into this package that
exports a module-level `router: APIRouter`. Nothing upstream is edited, and the
package works while it holds no router at all.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from fastapi import APIRouter

logger = logging.getLogger(__name__)


def _discover() -> list[str]:
    """Import every module beside this one; return their names, sorted."""
    importlib.invalidate_caches()
    names = sorted(name for _finder, name, _ispkg in pkgutil.iter_modules(__path__))
    for name in names:
        importlib.import_module(f"{__name__}.{name}")
    return names


def all_routers() -> list[APIRouter]:
    """Every discovered module's `router`, in module-name order."""
    routers: list[APIRouter] = []
    for name in _discover():
        module = importlib.import_module(f"{__name__}.{name}")
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            routers.append(router)
    return routers


_discover()
