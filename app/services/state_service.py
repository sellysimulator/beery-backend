"""The Redis-backed store for live room and game state (spec §11.3).

This is the boundary between the pure core (`app/core`) and the outside
world: the core knows how to serialise itself, this module decides where
those bytes live, how long they live, and who is allowed to touch them at a
time. Implements D4 -- Redis is authoritative for a live
game; PostgreSQL is written at the end.

This module does not validate config, does not check `host_secret`, does not
emit anything, and does not touch PostgreSQL.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import re
import secrets
from datetime import datetime, timezone

from ..config import settings
from ..core.bot import BotAgent
from ..core.config_models import GameConfig
from ..core.enums import ROLE_ORDER, Role
from ..core.game_engine import GameEngine
from .state_backend import StateBackend, build_state_backend

logger = logging.getLogger(__name__)

# 2: the server-only `host_uid` key (spec D23).
SCHEMA_VERSION: int = 2
ROOM_CODE_ALPHABET: str = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L
ROOM_CODE_LENGTH: int = 6
ROOM_TTL_SECONDS: int = 86_400  # 24 hours; every save_room refreshes it
# A room that has not started (no `started_at`) lives 2 hours after its last
# save: rooms come from an unauthenticated route, so one nobody plays must not
# hold the store for a day. `start_game`'s save moves it to ROOM_TTL_SECONDS.
UNSTARTED_ROOM_TTL_SECONDS: int = 7_200
LOCK_TIMEOUT_SECONDS: int = 10  # deadlock escape hatch, never a normal path

_ROOM_KEY_PREFIX = "room:"
_LOCK_KEY_PREFIX = "lock:room:"
_MAX_CODE_ATTEMPTS = 10

# Module-level singleton, deliberately built here rather than lazily. The
# name stays `redis_client` although it may hold an `InMemoryBackend`: it is
# read by that exact name at 25 call sites and by `tests/conftest.py`'s
# monkeypatch. Renaming it buys clarity and costs a sweep of both;
# `build_state_backend` is where the choice is legible.
# Every method below reads this name -- `redis_client`, exactly -- at call time
# rather than capturing it anywhere, because `tests/conftest.py` monkeypatches
# `app.services.state_service.redis_client` after `get_state_service()` has
# already built its singleton; a service holding a reference captured at
# construction would keep talking to this real client forever.
redis_client: StateBackend = build_state_backend(
    settings.REDIS_ENABLED, settings.REDIS_URL
)


def new_room_document(
    room_code: str, config: GameConfig, host_display_name: str
) -> dict:
    """A fresh LOBBY document, with `host_secret` minted here.

    The secret is `secrets.token_urlsafe(32)` -- `secrets`, never `random`,
    which is seeded and predictable and is the module the rest of this build
    deliberately avoids. It is minted at this one point because the document
    is the only thing that ever holds it: the create endpoint reads it back
    out of the returned document and hands it to the caller once, and no
    other code path produces one.
    """
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "room_code": room_code,
        "state": "LOBBY",
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        # --- authority ---
        "host_secret": secrets.token_urlsafe(32),
        "host_sid": None,
        "host_identity": None,
        # The verified Firebase uid of the connection whose `start_game`
        # started the game, or None (a guest, or not started yet). Server-only,
        # never in any payload; persistence resolves it to
        # `games.host_user_id` (D23). Not `host_identity`: that one is D18's
        # live recovery key and may be a guest id.
        "host_uid": None,
        "host_display_name": host_display_name,
        # --- people ---
        "participants": {},
        "sid_to_alias": {},
        "role_to_alias": {role.value: None for role in ROLE_ORDER},
        # --- game ---
        "seed": None,
        "config": config.to_payload(),
        "engine": None,
        "bots": {},
        # --- bookkeeping ---
        "seq": 0,
        "paused_reason": None,
        "persisted": False,
    }


def generate_room_code(rng: random.Random | None = None) -> str:
    """Six characters drawn uniformly from `ROOM_CODE_ALPHABET`.

    `rng` defaults to a fresh `random.Random()` per call. Tests pass one to
    force a collision.
    """
    generator = rng if rng is not None else random.Random()
    return "".join(
        generator.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH)
    )


def next_free_alias(room: dict) -> str:
    """Lowest unused P<N>. Never len(participants) + 1.

    A pre-start `leave()` frees a slot, and the count-based form then
    reissues a live participant's alias and silently overwrites their record
    (`[HARD-WON]`).
    """
    participants = room["participants"]
    n = 1
    while f"P{n}" in participants:
        n += 1
    return f"P{n}"


_ROOM_CODE_RE = re.compile(f"[{ROOM_CODE_ALPHABET}]{{{ROOM_CODE_LENGTH}}}")


def is_room_code(value: object) -> bool:
    """True when `value` could be a code `generate_room_code` minted.

    Case-insensitive, as `_room_key` is. Checked before any lock or read, so
    a client-supplied string that cannot name a room never reaches the store.
    """
    return (
        isinstance(value, str)
        and len(value) == ROOM_CODE_LENGTH
        and _ROOM_CODE_RE.fullmatch(value.upper()) is not None
    )


def _room_key(room_code: str) -> str:
    return _ROOM_KEY_PREFIX + room_code.upper()


def _decode(raw: bytes | str) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


class StateService:
    """Reads and writes the Redis room document, and owns its per-room lock.

    Holds no state of its own -- every method reads the module-level
    `redis_client` by name at call time, so a test that monkeypatches that
    name after this service is constructed is honoured by every call.
    """

    # --- lifecycle ---

    async def create_room(self, config: GameConfig, host_display_name: str) -> dict:
        """Generate a unique code, build and store the document, return it."""
        for _ in range(_MAX_CODE_ATTEMPTS):
            room_code = generate_room_code()
            if not await self.room_exists(room_code):
                room = new_room_document(room_code, config, host_display_name)
                await self.save_room(room_code, room)
                return room
        raise RuntimeError(
            f"Could not generate a unique room code after {_MAX_CODE_ATTEMPTS} attempts."
        )

    async def get_room(self, room_code: str) -> dict | None:
        """None for absent OR stale-schema. Deletes a stale document.

        A document whose `schema_version` is absent or not equal to
        `SCHEMA_VERSION` is discarded rather than migrated in place
        (`[HARD-WON]`). A malformed code is None
        without touching the store.
        """
        if not is_room_code(room_code):
            return None
        key = _room_key(room_code)
        raw = await redis_client.get(key)
        if raw is None:
            return None
        document = _decode(raw)
        if document.get("schema_version") != SCHEMA_VERSION:
            await redis_client.delete(key)
            return None
        return document

    async def save_room(self, room_code: str, room: dict) -> None:
        """setex, refreshing the TTL on every write: ROOM_TTL_SECONDS for a
        room that has started, UNSTARTED_ROOM_TTL_SECONDS for one that has
        not."""
        ttl = ROOM_TTL_SECONDS if room.get("started_at") else UNSTARTED_ROOM_TTL_SECONDS
        await redis_client.setex(_room_key(room_code), ttl, json.dumps(room))

    async def room_exists(self, room_code: str) -> bool:
        """Routed through get_room, so a stale room reports as absent."""
        return await self.get_room(room_code) is not None

    async def delete_room(self, room_code: str) -> None:
        await redis_client.delete(_room_key(room_code))

    # --- concurrency ---

    def lock(self, room_code: str):  # type: ignore[no-untyped-def]
        """An async context manager. LOCK_TIMEOUT_SECONDS.

        `LOCK_TIMEOUT_SECONDS` is a deadlock escape hatch, not a normal path
        -- no operation inside a lock may take seconds, and the lock must
        never be held across a database write.

        A malformed code gets a no-op lock and no lock entry: every caller
        reads `get_room` next, which is None for it, so nothing is mutated.
        """
        if not is_room_code(room_code):
            return contextlib.nullcontext()
        return redis_client.lock(
            f"{_LOCK_KEY_PREFIX}{room_code.upper()}", timeout=LOCK_TIMEOUT_SECONDS
        )

    # --- engine helpers ---

    def load_config(self, room: dict) -> GameConfig:
        return GameConfig.from_payload(room["config"])

    def load_engine(self, room: dict) -> GameEngine | None:
        if room["engine"] is None:
            return None
        return GameEngine.from_payload(room["engine"], self.load_config(room))

    def store_engine(self, room: dict, engine: GameEngine) -> None:
        room["engine"] = engine.to_payload()

    def load_bots(self, room: dict) -> dict[Role, BotAgent]:
        config = self.load_config(room)
        return {
            Role(role_name): BotAgent.from_payload(payload, config)
            for role_name, payload in room["bots"].items()
        }

    def store_bots(self, room: dict, bots: dict[Role, BotAgent]) -> None:
        room["bots"] = {role.value: bot.to_payload() for role, bot in bots.items()}

    # --- events ---

    def next_seq(self, room: dict) -> int:
        """Increment and return room['seq']. Caller must save_room afterwards."""
        room["seq"] += 1
        return int(room["seq"])


_state_service: StateService | None = None


def get_state_service() -> StateService:
    """Module-level singleton."""
    global _state_service
    if _state_service is None:
        _state_service = StateService()
    return _state_service
