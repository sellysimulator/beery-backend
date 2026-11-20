"""Finished exports, kept in this process's memory.

The export by `public_id` is open to anyone who can see a game's results, so
a class downloading the same game would rebuild the same file from PostgreSQL once
per student. A finished game never changes (D4: PostgreSQL is written once, at game
end), so the file is the same every time and can be kept.

- **Bounded LRU**, `EXPORT_CACHE_MAX_ENTRIES` files keyed by
  `(public_id, format)`, holding the final bytes and headers. No expiry: a
  stored game is immutable. Only a 200 is ever put here.
- **In process memory, per instance.** No Redis and no database, so it works
  the same in every Novus game's deployment. It is empty after a restart, and
  at N instances each holds its own copy; neither costs anything but a rebuild.
- **The rate limit is not affected.** `core.abuse`'s export budget is charged
  by the middleware before the route runs, so a cached download counts
  exactly like a built one.

Thread-safe: the export routes are plain `def`s, run on FastAPI's threadpool.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import NamedTuple

__all__ = [
    "EXPORT_CACHE_MAX_ENTRIES",
    "CachedExport",
    "ExportCache",
    "export_cache",
]

#: Files held at most. A CSV of a 104-week game is ~60 KB, so this is a few MB.
EXPORT_CACHE_MAX_ENTRIES = 64


class CachedExport(NamedTuple):
    """One finished export, exactly as the route answered it."""

    body: bytes
    media_type: str
    headers: dict[str, str]


class ExportCache:
    """A bounded, thread-safe LRU of finished exports."""

    def __init__(self, max_entries: int = EXPORT_CACHE_MAX_ENTRIES) -> None:
        self.max_entries = max_entries
        self._entries: OrderedDict[tuple[str, str], CachedExport] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, str]) -> CachedExport | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(self, key: tuple[str, str], entry: CachedExport) -> None:
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        return key in self._entries


#: The process's one cache, shared by both export routes.
export_cache = ExportCache()
