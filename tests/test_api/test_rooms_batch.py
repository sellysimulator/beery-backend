"""``POST /api/v1/rooms/batch-create``: N rooms with one config, made at once
for the multi-game host dashboard (``10-rooms-rest-api.md §3.5c``).

The config is resolved and validated once, before any room exists; the
room-create budget is charged one hit per room, all or nothing; and a store
that fills part-way leaves no room of the batch behind.
"""

from __future__ import annotations

import json

import pytest

from app.core import abuse
from app.core.abuse import RateLimiter
from app.core.limits import DEFAULT_LIMITS
from app.core.presets import get_preset
from app.services import state_backend
from app.services.state_backend import InMemoryBackend

from .conftest import CREATE, config_url, create_room

BATCH = "/api/v1/rooms/batch-create"


def _batch(client, count=3, **body):
    return client.post(BATCH, json={"count": count, **body})


def _stored_rooms(fake_redis) -> dict[str, dict]:
    return {
        key.removeprefix("room:"): json.loads(value)
        for key, value in fake_redis._store.items()
        if key.startswith("room:")
    }


def test_batch_creates_count_rooms_with_the_config_applied(client, fake_redis):
    r = _batch(client, count=4, config={"duration_weeks": 20})
    assert r.status_code == 200, r.text
    rooms = r.json()["rooms"]

    assert len(rooms) == 4
    assert len({room["room_code"] for room in rooms}) == 4
    assert len({room["host_secret"] for room in rooms}) == 4
    stored = _stored_rooms(fake_redis)
    for room in rooms:
        assert room["state"] == "LOBBY"
        assert room["config"]["duration_weeks"] == 20
        saved = stored[room["room_code"]]
        assert saved["config"]["duration_weeks"] == 20
        assert saved["host_secret"] == room["host_secret"]
        assert saved["participants"] == {}


def test_batch_rooms_do_not_share_a_config(client, fake_redis):
    rooms = _batch(client, count=2, config={"duration_weeks": 30}).json()["rooms"]
    first, second = rooms
    r = client.put(
        config_url(first["room_code"]),
        json={"config": {"duration_weeks": 12}},
        headers={"X-Host-Secret": first["host_secret"]},
    )
    assert r.status_code == 200, r.text

    stored = _stored_rooms(fake_redis)
    assert stored[first["room_code"]]["config"]["duration_weeks"] == 12
    assert stored[second["room_code"]]["config"]["duration_weeks"] == 30


def test_a_batch_with_no_config_is_what_create_makes(client, fake_redis):
    default = client.post(CREATE, json={}).json()["config"]
    rooms = _batch(client, count=2).json()["rooms"]
    assert all(room["config"] == default for room in rooms)


def test_a_preset_means_what_it_means_on_create(client, fake_redis):
    single = client.post(CREATE, json={"preset": "FAST_GAME"}).json()["config"]
    rooms = _batch(client, count=2, preset="FAST_GAME").json()["rooms"]
    assert all(room["config"] == single for room in rooms)
    assert single == get_preset("FAST_GAME").to_payload()


def test_the_config_is_a_patch_over_the_preset(client, fake_redis):
    preset = get_preset("CHAOS").to_payload()
    rooms = _batch(
        client,
        count=2,
        preset="CHAOS",
        config={"bot_fill_empty_roles": True, "preset_name": None},
    ).json()["rooms"]
    for room in rooms:
        assert room["config"]["bot_fill_empty_roles"] is True
        assert room["config"]["demand"] == preset["demand"]
        assert room["config"]["duration_weeks"] == preset["duration_weeks"]


def test_an_unknown_preset_is_create_s_400_and_creates_nothing(client, fake_redis):
    single = client.post(CREATE, json={"preset": "NOPE"})
    r = _batch(client, count=3, preset="NOPE")
    assert r.status_code == single.status_code == 400
    assert r.json() == single.json()
    assert len(_stored_rooms(fake_redis)) == 0


def test_a_bad_config_is_the_config_save_s_422_and_creates_nothing(client, fake_redis):
    code, secret = create_room(client)
    patch = {"duration_weeks": DEFAULT_LIMITS.max_weeks + 1}
    saved = client.put(
        config_url(code), json={"config": patch}, headers={"X-Host-Secret": secret}
    )

    r = _batch(client, count=3, config=patch)

    assert r.status_code == saved.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert r.json()["detail"].startswith("duration_weeks")
    assert r.json() == saved.json()
    assert list(_stored_rooms(fake_redis)) == [code]


def test_a_refused_batch_does_not_use_up_the_create_limit(
    client, fake_redis, monkeypatch
):
    monkeypatch.setattr(abuse, "_room_create", RateLimiter(3))
    assert _batch(client, count=3, preset="NOPE").status_code == 400
    assert _batch(client, count=3, config={"duration_weeks": 1}).status_code == 422
    assert _batch(client, count=3).status_code == 200


def test_count_outside_the_bounds_is_a_422(client, fake_redis):
    assert _batch(client, count=DEFAULT_LIMITS.min_batch_rooms - 1).status_code == 422
    assert _batch(client, count=DEFAULT_LIMITS.max_batch_rooms + 1).status_code == 422
    assert client.post(BATCH, json={}).status_code == 422
    assert len(_stored_rooms(fake_redis)) == 0


def test_the_bounds_are_served_with_the_other_limits(client):
    limits = client.get("/api/v1/rooms/limits").json()
    assert limits["min_batch_rooms"] == 2
    assert limits["max_batch_rooms"] == 10


def test_each_room_counts_against_the_create_limit(client, fake_redis, monkeypatch):
    monkeypatch.setattr(abuse, "_room_create", RateLimiter(5))

    assert _batch(client, count=3).status_code == 200
    r = _batch(client, count=3)  # 3 + 3 > 5: all or nothing
    assert r.status_code == 429
    assert r.json() == {"detail": abuse.RATE_LIMITED_MESSAGE}
    assert r.headers["retry-after"] == str(int(abuse.WINDOW_SECONDS))
    assert len(_stored_rooms(fake_redis)) == 3

    # The refused batch used none of what was left.
    assert _batch(client, count=2).status_code == 200
    assert client.post(CREATE, json={}).status_code == 429


def test_thirty_rooms_an_hour_then_batch_and_single_create_are_429(client, fake_redis):
    """The real budget, not a monkeypatched one (2026-11-14): 30 rooms per
    address per hour, the standard across the Novus games. Three full batches
    of 10 use it up; the next batch and the next single create are both
    refused, with a Retry-After inside the hour."""
    assert abuse.ROOM_CREATE_LIMIT_PER_IP == 30
    assert abuse.ROOM_CREATE_WINDOW_SECONDS == 3600

    for _ in range(3):
        assert _batch(client, count=10).status_code == 200
    assert len(_stored_rooms(fake_redis)) == 30

    refused_batch = _batch(client, count=2)
    assert refused_batch.status_code == 429
    assert refused_batch.json() == {"detail": abuse.RATE_LIMITED_MESSAGE}
    assert 3500 < int(refused_batch.headers["retry-after"]) <= 3600

    refused_single = client.post(CREATE, json={})
    assert refused_single.status_code == 429
    assert refused_single.json() == {"detail": abuse.RATE_LIMITED_MESSAGE}
    assert 3500 < int(refused_single.headers["retry-after"]) <= 3600
    assert len(_stored_rooms(fake_redis)) == 30


def test_a_batch_may_not_exceed_ten_rooms(client, fake_redis):
    assert _batch(client, count=11).status_code == 422
    assert _stored_rooms(fake_redis) == {}


def test_single_creates_and_batches_share_one_budget(client, fake_redis, monkeypatch):
    monkeypatch.setattr(abuse, "_room_create", RateLimiter(3))
    assert client.post(CREATE, json={}).status_code == 200
    assert client.post(CREATE, json={}).status_code == 200
    assert _batch(client, count=2).status_code == 429
    assert client.post(CREATE, json={}).status_code == 200


def test_the_middleware_does_not_also_charge_a_batch(client, fake_redis, monkeypatch):
    """A batch of N is N creates, not N + 1: the route charges the budget,
    the middleware leaves the path on the general limit."""
    monkeypatch.setattr(abuse, "_room_create", RateLimiter(4))
    assert _batch(client, count=2).status_code == 200
    assert _batch(client, count=2).status_code == 200
    assert _batch(client, count=2).status_code == 429


def test_a_batch_still_counts_against_the_general_limit(
    client, fake_redis, monkeypatch
):
    monkeypatch.setattr(abuse, "_rest", RateLimiter(1))
    assert _batch(client, count=2).status_code == 200
    assert _batch(client, count=2).status_code == 429


def test_the_batch_takes_no_custom_room_code(client, fake_redis):
    r = client.post(BATCH, json={"count": 2, "room_code": "ABCDEF"})
    assert r.status_code == 200
    assert all(room["room_code"] != "ABCDEF" for room in r.json()["rooms"])


def test_the_host_name_is_sanitised_like_create_s(client, fake_redis):
    rooms = _batch(client, count=2, host_display_name="  Ms\tFrizzle  ").json()["rooms"]
    stored = _stored_rooms(fake_redis)
    assert {stored[r["room_code"]]["host_display_name"] for r in rooms} == {
        "Ms Frizzle"
    }


@pytest.fixture()
def small_store(monkeypatch) -> InMemoryBackend:
    """The real in-memory backend, with room for three rooms."""
    backend = InMemoryBackend()
    monkeypatch.setattr("app.services.state_service.redis_client", backend)
    monkeypatch.setattr(state_backend, "MAX_ROOMS", 3)
    return backend


def test_a_store_that_fills_part_way_keeps_no_room_of_the_batch(client, small_store):
    kept = client.post(CREATE, json={}).json()["room_code"]

    r = _batch(client, count=3)  # the batch's third room would be the fourth

    assert r.status_code == 503
    assert r.json() == {
        "detail": "The server is at its room limit. Please try again later."
    }
    # The two rooms the batch did make are gone; the earlier room is not.
    assert list(small_store._store) == [f"room:{kept}"]


def test_a_full_store_is_create_s_503(client, small_store):
    for _ in range(3):
        assert client.post(CREATE, json={}).status_code == 200
    single = client.post(CREATE, json={})
    r = _batch(client, count=2)
    assert r.status_code == single.status_code == 503
    assert r.json() == single.json()
    assert len(small_store._store) == 3


def test_the_rollback_frees_the_rooms_it_made(client, small_store):
    client.post(CREATE, json={})
    assert _batch(client, count=3).status_code == 503
    # Two places are free again, so a batch that fits now succeeds.
    r = _batch(client, count=2)
    assert r.status_code == 200, r.text
    assert len(small_store._store) == 3


def test_any_other_failure_part_way_rolls_back_and_is_a_500(
    client, fake_redis, monkeypatch
):
    from app.services.state_service import get_state_service

    state_svc = get_state_service()
    real_create = state_svc.create_room
    calls = {"n": 0}

    async def flaky_create(config, host_display_name):
        calls["n"] += 1
        if calls["n"] == 3:
            raise ConnectionError("store went away")
        return await real_create(config, host_display_name)

    monkeypatch.setattr(state_svc, "create_room", flaky_create)

    r = _batch(client, count=4)

    assert r.status_code == 500
    assert r.json() == {"detail": "Internal server error."}
    assert _stored_rooms(fake_redis) == {}
