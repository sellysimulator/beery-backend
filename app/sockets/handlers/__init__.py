"""Socket.IO handler discovery point (00-decisions.md D19).

A later section adds behaviour by dropping a module into this package; its
`@sio.event` decorators register as the module is imported. Nothing upstream is
edited, and the package works while it is empty.
"""

from __future__ import annotations

import importlib
import pkgutil


def _discover() -> None:
    """Import every handler module beside this one, registering its events."""
    importlib.invalidate_caches()
    for _finder, name, _ispkg in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{name}")


_discover()
