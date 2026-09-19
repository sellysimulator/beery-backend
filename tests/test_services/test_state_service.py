"""Black-box tests for section 09 -- ``app/services/state_service.py``'s
``StateService`` and the Redis-backed behaviour of the module.

Covers ``09-state-service.md §5`` acceptance criteria 1, 3, 5-12 and 14-18,
and ``§6`` failure modes 2, 3, 4, 6, 7 and 8. Criteria 2, 4 and 13, and
failure mode 1, live in ``test_room_schema.py`` because they need none of
the Redis machinery.

Driven entirely through the frozen public surface of section 09 (``§3``)
plus the public surfaces of sections 01, 03, 07 and 08, and the shared
``fake_redis`` harness from ``tests/conftest.py`` (``FakeRedis``,
``_FakeLock``). ``app/services/state_service.py`` itself is never opened.
No private name, no internal data structure and no unspecified log message
is asserted on; independent-emit ordering does not apply here since this
section emits nothing.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

import app.services.state_service as state_service_module
from app.core.bot import bot_for
from app.core.config_models import (
    DEFAULT_LIMITS,
    ConstantDemand,
    FactoryConfig,
    GameConfig,
    RoleConfig,
)
from app.core.enums import ROLE_ORDER, Role
from app.core.game_engine import GameEngine, GamePhase
from app.core.presets import get_preset
from app.services.state_service import (
    ROOM_TTL_SECONDS,
    get_state_service,
    new_room_document,
)

EXPECTED_TOP_LEVEL_KEYS = {
    "schema_version",
    "room_code",
    "state",
    "created_at",
    "started_at",
    "finished_at",
    "host_secret",
    "host_sid",
    "host_identity",
    "host_display_name",
    "participants",
    "sid_to_alias",
    "role_to_alias",
    "seed",
    "config",
    "engine",
    "bots",
    "seq",
    "paused_reason",
    "persisted",
}


def make_config(duration_weeks: int = 8, demand=None) -> GameConfig:
    """A minimal, valid ``GameConfig`` built only through section 03's
    frozen surface, mirroring section 07's own test helper."""
    roles = {
        Role.RETAILER.value: RoleConfig(),
        Role.WHOLESALER.value: RoleConfig(),
        Role.DISTRIBUTOR.value: RoleConfig(),
        Role.FACTORY.value: FactoryConfig(),
    }
    payload = {
        "roles": roles,
        "demand": demand if demand is not None else ConstantDemand(value=4),
        "duration_weeks": duration_weeks,
    }
    return GameConfig.from_host_input(payload, DEFAULT_LIMITS)


def install_setex_spy(fake_redis) -> list[dict]:
    """Record every ``setex`` call made against ``fake_redis`` without
    changing its behaviour, so a test can assert on the TTL a write used.

    Matches by parameter name rather than position, so it is agnostic to
    whether the implementation calls ``setex`` positionally or by keyword.
    """
    calls: list[dict] = []
    original = fake_redis.setex

    async def spy(key, ttl, value):
        calls.append({"key": key, "ttl": ttl, "value": value})
        return await original(key, ttl, value)

    fake_redis.setex = spy
    return calls


# ---------------------------------------------------------------------------
# AC 1 -- create_room's document shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac1_create_room_key_set_and_fresh_defaults(fake_redis):
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")

    assert set(room) == EXPECTED_TOP_LEVEL_KEYS
    assert room["participants"] == {}
    assert room["sid_to_alias"] == {}
    assert room["bots"] == {}
    assert isinstance(room["host_secret"], str)
    assert room["host_secret"] != ""


# ---------------------------------------------------------------------------
# AC 3 -- create_room retries on a forced collision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac3_create_room_retries_on_forced_collision(fake_redis, monkeypatch):
    svc = get_state_service()

    first_room = await svc.create_room(make_config(), "Host One")
    occupied_code = first_room["room_code"]

    # Force generate_room_code to hand back the already-occupied code
    # twice before giving a free one, simulating a collision.
    forced_codes = iter([occupied_code, occupied_code, "ZZZZZZ"])
    monkeypatch.setattr(
        state_service_module,
        "generate_room_code",
        lambda *args, **kwargs: next(forced_codes),
    )

    second_room = await svc.create_room(make_config(), "Host Two")

    assert second_room["room_code"] == "ZZZZZZ"
    assert second_room["room_code"] != occupied_code


# ---------------------------------------------------------------------------
# AC 5, AC 6 -- get_room lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac5_get_room_lookup_is_case_insensitive(fake_redis):
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")
    code = room["room_code"]
    assert code == code.upper()

    found = await svc.get_room(code.lower())

    assert found is not None
    assert found["room_code"] == code


@pytest.mark.asyncio
async def test_ac6_get_room_returns_none_for_an_absent_code(fake_redis):
    svc = get_state_service()
    assert await svc.get_room("ZZZZZZ") is None


# ---------------------------------------------------------------------------
# AC 7, AC 8 / FM 2 -- stale-schema resurrection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac7_get_room_deletes_a_document_with_wrong_schema_version(fake_redis):
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")
    code = room["room_code"]

    stale = dict(room)
    stale["schema_version"] = 0
    await fake_redis.setex(f"room:{code}", ROOM_TTL_SECONDS, json.dumps(stale))

    assert await svc.get_room(code) is None
    assert await fake_redis.exists(f"room:{code}") == 0


@pytest.mark.asyncio
async def test_ac8_and_fm2_get_room_deletes_a_document_missing_schema_version(
    fake_redis,
):
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")
    code = room["room_code"]

    stale = dict(room)
    del stale["schema_version"]
    await fake_redis.setex(f"room:{code}", ROOM_TTL_SECONDS, json.dumps(stale))

    first = await svc.get_room(code)
    assert first is None

    # A service that "repairs" the document instead of deleting it would
    # let this second call find it again.
    second = await svc.get_room(code)
    assert second is None
    assert await fake_redis.exists(f"room:{code}") == 0


# ---------------------------------------------------------------------------
# AC 9 -- round-trip fidelity over a full game
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac9_round_trip_fidelity_over_a_full_classic_mit_game(fake_redis):
    svc = get_state_service()
    config = get_preset("CLASSIC_MIT")
    seed = 20260919

    reference_engine = GameEngine.start(config, seed)
    while reference_engine.phase != GamePhase.FINISHED:
        for role in ROLE_ORDER:
            reference_engine.submit_order(role, 4)
        reference_engine.close_week()

    room = await svc.create_room(config, "Host")
    code = room["room_code"]
    engine = GameEngine.start(config, seed)
    svc.store_engine(room, engine)
    await svc.save_room(code, room)

    while True:
        room = await svc.get_room(code)
        engine = svc.load_engine(room)
        if engine.phase == GamePhase.FINISHED:
            break
        for role in ROLE_ORDER:
            engine.submit_order(role, 4)
        engine.close_week()
        svc.store_engine(room, engine)
        await svc.save_room(code, room)

    assert engine.weeks_played == reference_engine.weeks_played
    assert engine.history == reference_engine.history


# ---------------------------------------------------------------------------
# AC 10 / FM 4 -- save_room refreshes the TTL via setex, never set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac10_save_room_calls_setex_with_room_ttl_on_every_write(fake_redis):
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")
    code = room["room_code"]
    calls = install_setex_spy(fake_redis)

    await svc.save_room(code, room)
    await svc.save_room(code, room)

    assert len(calls) == 2
    for call in calls:
        assert call["ttl"] == ROOM_TTL_SECONDS


@pytest.mark.asyncio
async def test_fm4_save_room_uses_setex_not_a_bare_set(fake_redis):
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")
    code = room["room_code"]

    # FakeRedis deliberately has no `set` method: a save_room that used
    # `.set()` instead of `.setex()` would raise AttributeError here
    # rather than silently dropping the TTL.
    assert not hasattr(fake_redis, "set")

    calls = install_setex_spy(fake_redis)
    await svc.save_room(code, room)

    assert len(calls) == 1
    assert calls[0]["ttl"] == ROOM_TTL_SECONDS


# ---------------------------------------------------------------------------
# AC 11, AC 12 -- locking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac11_lock_serialises_fifty_concurrent_increments(fake_redis):
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")
    code = room["room_code"]
    room["seq"] = 0
    await svc.save_room(code, room)

    async def bump() -> None:
        async with svc.lock(code):
            current = await svc.get_room(code)
            # FakeRedis's own reads and writes never truly suspend, so two
            # coroutines would otherwise run this whole body back-to-back
            # regardless of locking and the assertion below would hold
            # vacuously. This explicit yield forces genuine interleaving,
            # so the increment is lost unless the lock actually serialises.
            await asyncio.sleep(0)
            current["seq"] = current["seq"] + 1
            await svc.save_room(code, current)

    await asyncio.gather(*(bump() for _ in range(50)))

    final = await svc.get_room(code)
    assert final["seq"] == 50


@pytest.mark.asyncio
async def test_ac12_lock_is_independent_per_room_code(fake_redis):
    svc = get_state_service()
    room_a = await svc.create_room(make_config(), "Host A")
    room_b = await svc.create_room(make_config(), "Host B")

    a_holding = asyncio.Event()
    b_acquired = asyncio.Event()

    async def hold_a() -> None:
        async with svc.lock(room_a["room_code"]):
            a_holding.set()
            # If room B's lock were the same lock, acquire_b() below would
            # deadlock waiting for this to release, and this line would
            # itself never wake up -- so a hang here IS the failure signal,
            # caught by the outer wait_for.
            await asyncio.wait_for(b_acquired.wait(), timeout=1)

    async def acquire_b() -> None:
        await a_holding.wait()
        async with svc.lock(room_b["room_code"]):
            b_acquired.set()

    await asyncio.wait_for(asyncio.gather(hold_a(), acquire_b()), timeout=2)

    assert b_acquired.is_set()


# ---------------------------------------------------------------------------
# AC 14 -- next_seq
# ---------------------------------------------------------------------------


def test_ac14_next_seq_starts_at_one_and_increments_by_one():
    svc = get_state_service()
    room = {"seq": 0}

    assert svc.next_seq(room) == 1
    assert svc.next_seq(room) == 2
    assert svc.next_seq(room) == 3
    assert room["seq"] == 3


# ---------------------------------------------------------------------------
# AC 15 -- room_exists routes through get_room
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac15_room_exists_is_false_for_a_stale_schema_room(fake_redis):
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")
    code = room["room_code"]

    stale = dict(room)
    stale["schema_version"] = 0
    await fake_redis.setex(f"room:{code}", ROOM_TTL_SECONDS, json.dumps(stale))

    assert await svc.room_exists(code) is False


@pytest.mark.asyncio
async def test_ac15_room_exists_is_true_for_a_live_room(fake_redis):
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")
    assert await svc.room_exists(room["room_code"]) is True


# ---------------------------------------------------------------------------
# AC 16 -- load_config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac16_load_config_reconstructs_the_original_config(fake_redis):
    svc = get_state_service()
    config = get_preset("CLASSIC_MIT")
    room = await svc.create_room(config, "Host")

    assert svc.load_config(room) == config


# ---------------------------------------------------------------------------
# AC 17 -- get_state_service singleton
# ---------------------------------------------------------------------------


def test_ac17_get_state_service_returns_the_same_instance():
    assert get_state_service() is get_state_service()


# ---------------------------------------------------------------------------
# AC 18 -- redis_client is read dynamically, at call time
# ---------------------------------------------------------------------------


def test_ac18_module_level_name_is_exactly_redis_client():
    assert hasattr(state_service_module, "redis_client")


@pytest.mark.asyncio
async def test_ac18_state_service_reads_redis_client_at_call_time(fake_redis):
    # get_state_service() may have been called by an earlier test, before
    # *this* test's fake_redis existed. If StateService captured the
    # client at construction instead of reading the module-level name on
    # every call, this write would land on some other, stale client -- and
    # this test's own fake_redis would never see it.
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")
    code = room["room_code"]

    assert await fake_redis.exists(f"room:{code}") == 1


# ---------------------------------------------------------------------------
# Engine/bot round-trip helpers (§3, adjacent to AC 9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_and_load_bots_round_trip(fake_redis):
    svc = get_state_service()
    config = get_preset("CLASSIC_MIT")
    room = await svc.create_room(config, "Host")

    bots = {Role.FACTORY: bot_for(Role.FACTORY, config)}
    bots[Role.FACTORY].observe(6)
    svc.store_bots(room, bots)
    await svc.save_room(room["room_code"], room)

    reloaded = await svc.get_room(room["room_code"])
    loaded_bots = svc.load_bots(reloaded)

    assert loaded_bots == bots


# ---------------------------------------------------------------------------
# FM 3 -- lost update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm3_lock_prevents_a_lost_update(fake_redis):
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")
    code = room["room_code"]

    async def add_participant(alias: str, display_name: str) -> None:
        async with svc.lock(code):
            current = await svc.get_room(code)
            # Force genuine interleaving inside the critical section -- see
            # the comment in test_ac11 for why this is necessary against
            # FakeRedis, which never truly suspends on its own.
            await asyncio.sleep(0)
            current["participants"][alias] = {
                "alias": alias,
                "identity": f"guest_{alias}",
                "session_token": f"tok_{alias}",
                "display_name": display_name,
                "role": None,
                "is_bot": False,
                "connected": True,
            }
            await svc.save_room(code, current)

    await asyncio.gather(
        add_participant("P1", "Ana"),
        add_participant("P2", "Ben"),
    )

    final = await svc.get_room(code)
    assert set(final["participants"]) == {"P1", "P2"}


@pytest.mark.asyncio
async def test_fm3_demonstration_without_the_lock_a_write_can_be_lost(fake_redis):
    """Demonstration only, per 09-state-service.md §6 FM 3's note: this does
    not call ``svc.lock`` at all, so it exercises no code path the
    implementation has -- it only shows why the test above matters. It must
    not be read as asserting anything about ``StateService`` itself.
    """
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")
    code = room["room_code"]

    async def add_participant_without_lock(alias: str, display_name: str) -> None:
        current = await svc.get_room(code)
        await asyncio.sleep(0)  # force the two coroutines to interleave
        current["participants"][alias] = {"alias": alias, "display_name": display_name}
        await svc.save_room(code, current)

    await asyncio.gather(
        add_participant_without_lock("P1", "Ana"),
        add_participant_without_lock("P2", "Ben"),
    )

    final = await svc.get_room(code)
    # One of the two writes was lost -- this is the bug the lock exists to
    # prevent, reproduced deliberately with no lock in the picture.
    assert len(final["participants"]) == 1


# ---------------------------------------------------------------------------
# FM 6 -- no undocumented helper leaks a secret
# ---------------------------------------------------------------------------


def test_fm6_no_undocumented_secret_leaking_helper_exists():
    documented = {
        "SCHEMA_VERSION",
        "ROOM_CODE_ALPHABET",
        "ROOM_CODE_LENGTH",
        "ROOM_TTL_SECONDS",
        "LOCK_TIMEOUT_SECONDS",
        "redis_client",
        "new_room_document",
        "generate_room_code",
        "next_free_alias",
        "StateService",
        "get_state_service",
    }

    room = new_room_document("ABC234", make_config(), "Host")
    room["host_identity"] = "uid_secret_value"
    room["participants"]["P1"] = {
        "alias": "P1",
        "identity": "guest_identity_value",
        "session_token": "token_secret_value",
        "display_name": "Ana",
        "role": None,
        "is_bot": False,
        "connected": True,
    }
    secret_values = (
        room["host_secret"],
        "uid_secret_value",
        "guest_identity_value",
        "token_secret_value",
    )

    for name in dir(state_service_module):
        if name.startswith("_") or name in documented:
            continue
        candidate = getattr(state_service_module, name)
        if not (
            inspect.isfunction(candidate)
            and inspect.getmodule(candidate) is state_service_module
        ):
            continue
        try:
            result = candidate(room)
        except TypeError:
            continue  # not a room -> dict style helper; not this failure mode
        dumped = json.dumps(result, default=str)
        for secret_value in secret_values:
            assert secret_value not in dumped, (
                f"{name}() leaks a secret and is not documented as " "redacting it"
            )


# ---------------------------------------------------------------------------
# FM 7 -- engine payload growth stays bounded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm7_document_size_stays_under_512kb_after_104_weeks(fake_redis):
    svc = get_state_service()
    base_config = get_preset("CLASSIC_MIT")
    config_payload = base_config.to_payload()
    config_payload["duration_weeks"] = 104
    config = GameConfig.from_payload(config_payload)

    room = await svc.create_room(config, "Host")
    code = room["room_code"]
    room["participants"] = {
        f"P{index}": {
            "alias": f"P{index}",
            "identity": f"guest_{index}",
            "session_token": f"tok_{index}",
            "display_name": f"Player {index}",
            "role": role.value,
            "is_bot": False,
            "connected": True,
        }
        for index, role in enumerate(ROLE_ORDER, start=1)
    }

    engine = GameEngine.start(config, seed=20260919)
    while engine.phase != GamePhase.FINISHED:
        for role in ROLE_ORDER:
            engine.submit_order(role, 1234)  # four-digit orders throughout
        engine.close_week()
    svc.store_engine(room, engine)
    await svc.save_room(code, room)

    stored = await svc.get_room(code)
    size_bytes = len(json.dumps(stored).encode("utf-8"))

    assert size_bytes < 512 * 1024


# ---------------------------------------------------------------------------
# FM 8 -- normalisation applied consistently across the whole lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm8_lower_case_code_works_across_the_whole_lifecycle(fake_redis):
    svc = get_state_service()
    room = await svc.create_room(make_config(), "Host")
    code = room["room_code"]
    lower = code.lower()

    assert await svc.get_room(lower) is not None
    assert await svc.room_exists(lower) is True

    await svc.delete_room(lower)

    assert await svc.get_room(code) is None
