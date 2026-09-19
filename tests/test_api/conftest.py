"""Shared helpers for section 10's REST tests (``10-rooms-rest-api.md``).

Local to this package only: plain helper functions used by both
``test_rooms.py`` and ``test_room_config.py`` so the room-code oracle,
recursive key/value scans and the "create a room and grab its secret" dance
are not three implementations of the same thing. Nothing here overrides a
fixture from the shared ``tests/conftest.py`` -- ``client`` and ``fake_redis``
still come from there.
"""

from __future__ import annotations

from typing import Any

CREATE = "/api/v1/rooms/create"
PRESETS = "/api/v1/rooms/presets"


def status_url(room_code: str) -> str:
    return f"/api/v1/rooms/{room_code}/status"


def config_url(room_code: str) -> str:
    return f"/api/v1/rooms/{room_code}/config"


def create_room(client: Any, **body: Any) -> tuple[str, str]:
    """``POST /rooms/create`` and return ``(room_code, host_secret)``."""
    response = client.post(CREATE, json=body)
    payload = response.json()
    return payload["room_code"], payload["host_secret"]


def iter_pairs(obj: Any):
    """Yield every ``(key, value)`` pair at any depth of a JSON-like value.

    The recursive-assertion tool the *failure modes to test* keep asking
    for: a broadcast or response leaking a secret three levels deep is not
    caught by checking the top-level keys alone.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from iter_pairs(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_pairs(item)


def keys_recursive(obj: Any) -> set[str]:
    return {key for key, _value in iter_pairs(obj)}


def values_recursive(obj: Any) -> list[Any]:
    return [value for _key, value in iter_pairs(obj)]
