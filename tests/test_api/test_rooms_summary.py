"""``POST /api/v1/rooms/summary``: what the multi-game host dashboard polls
(``10-rooms-rest-api.md §3.5d``).

Read-only, no oracle for which codes are live, and never a secret in the
answer. ``can_start`` is ``RoomService.can_start``'s, the rule ``start_game``
applies.
"""

from __future__ import annotations

import json

from app.core.config_models import GameConfig
from app.core.enums import ROLE_ORDER
from app.core.game_engine import GameEngine
from app.services.room_service import RoomService

from .conftest import keys_recursive, values_recursive

SUMMARY = "/api/v1/rooms/summary"
_NULL_FIELDS = (
    "state",
    "seats_taken",
    "seats_total",
    "week",
    "duration_weeks",
    "finished",
    "can_start",
    "start_blocked_reason",
)


def _create(client, count=2, **body):
    r = client.post("/api/v1/rooms/batch-create", json={"count": count, **body})
    assert r.status_code == 200, r.text
    return r.json()["rooms"]


def _summary(client, pairs):
    return client.post(
        SUMMARY,
        json={"rooms": [{"room_code": c, "host_secret": s} for c, s in pairs]},
    )


def _pairs(rooms):
    return [(room["room_code"], room["host_secret"]) for room in rooms]


def _edit(fake_redis, room_code, change):
    """Rewrite one stored room document in place, as the handlers would."""
    key = f"room:{room_code}"
    room = json.loads(fake_redis._store[key])
    change(room)
    fake_redis._store[key] = json.dumps(room)


def _seat(room, n):
    for i in range(1, n + 1):
        alias = f"P{i}"
        room["participants"][alias] = {
            "alias": alias,
            "identity": f"guest_{i}",
            "session_token": f"token-{i}",
            "display_name": f"Player {i}",
            "role": None,
            "is_bot": False,
            "connected": True,
        }


def _start(room):
    config = GameConfig.from_payload(room["config"])
    room["state"] = "RUNNING"
    room["engine"] = GameEngine.start(config, 7, frozenset(ROLE_ORDER)).to_payload()


def test_summary_reports_each_room_in_order(client, fake_redis):
    a, b = _create(client, config={"bot_fill_empty_roles": True})
    _create(client)  # rooms nobody asks about are not in the answer
    _edit(fake_redis, b["room_code"], lambda room: _seat(room, 2))

    r = _summary(client, _pairs([b, a]))
    assert r.status_code == 200, r.text
    first, second = r.json()["rooms"]

    duration = a["config"]["duration_weeks"]
    assert first == {
        "room_code": b["room_code"],
        "exists": True,
        "state": "LOBBY",
        "seats_taken": 2,
        "seats_total": 4,
        "week": None,
        "duration_weeks": duration,
        "finished": False,
        # Bot fill is on but the two people hold no role yet (HOST_ASSIGNS).
        "can_start": False,
        "start_blocked_reason": RoomService().can_start(
            json.loads(fake_redis._store[f"room:{b['room_code']}"])
        )[1],
    }
    assert "no role yet" in first["start_blocked_reason"]
    assert second["room_code"] == a["room_code"]
    assert second["seats_taken"] == 0
    assert second["can_start"] is True
    assert second["start_blocked_reason"] is None


def test_can_start_is_the_lobby_s_rule(client, fake_redis):
    (room,) = _create(client, count=2)[:1]  # default preset: bot fill off
    (summary,) = _summary(client, _pairs([room])).json()["rooms"]
    assert summary["can_start"] is False
    assert summary["start_blocked_reason"].startswith("Four roles are still empty")

    def seat_all(doc):
        _seat(doc, 4)
        for alias, role in zip(("P1", "P2", "P3", "P4"), ROLE_ORDER, strict=True):
            doc["participants"][alias]["role"] = role.value
            doc["role_to_alias"][role.value] = alias

    _edit(fake_redis, room["room_code"], seat_all)
    (summary,) = _summary(client, _pairs([room])).json()["rooms"]
    assert summary["can_start"] is True
    assert summary["seats_taken"] == 4


def test_a_running_room_reports_its_week_and_cannot_start(client, fake_redis):
    (room,) = _create(client, count=2)[:1]
    _edit(fake_redis, room["room_code"], _start)

    (summary,) = _summary(client, _pairs([room])).json()["rooms"]
    assert summary["state"] == "RUNNING"
    assert summary["week"] == 1
    assert summary["finished"] is False
    assert summary["can_start"] is False
    assert summary["start_blocked_reason"] == "The game is already running."


def test_a_finished_or_abandoned_room_is_finished(client, fake_redis):
    a, b = _create(client)
    for room, state in ((a, "FINISHED"), (b, "ABANDONED")):

        def finish(doc, state=state):
            _start(doc)
            doc["state"] = state

        _edit(fake_redis, room["room_code"], finish)

    rooms = _summary(client, _pairs([a, b])).json()["rooms"]
    assert [s["state"] for s in rooms] == ["FINISHED", "ABANDONED"]
    assert all(s["finished"] is True and s["can_start"] is False for s in rooms)


def test_wrong_secret_and_missing_room_look_the_same(client, fake_redis):
    a, b = _create(client)
    rooms = _summary(
        client,
        [
            (a["room_code"], "wrong"),
            (b["room_code"], ""),
            ("ZZZZZZ", "anything"),
            (a["room_code"], b["host_secret"]),
            (a["room_code"], "ñ-not-ascii"),
            ("bad code!", "s"),
        ],
    ).json()["rooms"]

    assert len(rooms) == 6
    for summary in rooms:
        assert summary["exists"] is False
        assert all(summary[key] is None for key in _NULL_FIELDS)


def test_an_empty_stored_secret_never_matches(client, fake_redis):
    (room,) = _create(client, count=2)[:1]
    _edit(fake_redis, room["room_code"], lambda doc: doc.update(host_secret=""))
    (summary,) = _summary(client, [(room["room_code"], "")]).json()["rooms"]
    assert summary["exists"] is False


def test_summary_never_contains_a_host_secret(client, fake_redis):
    a, b = _create(client)
    r = _summary(client, [(a["room_code"], a["host_secret"]), (b["room_code"], "x")])
    body = r.json()
    assert "host_secret" not in keys_recursive(body)
    assert a["host_secret"] not in values_recursive(body)
    assert a["host_secret"] not in r.text
    assert b["host_secret"] not in r.text


def test_summary_does_not_write_the_room(client, fake_redis, monkeypatch):
    """Polling must not extend a room's TTL, and takes no lock."""
    (room,) = _create(client, count=2)[:1]
    calls = []

    async def no_setex(key, ttl, value):
        calls.append(("setex", key))

    def no_lock(key, timeout=10):
        calls.append(("lock", key))
        raise AssertionError("summary took a lock")

    monkeypatch.setattr(fake_redis, "setex", no_setex)
    monkeypatch.setattr(fake_redis, "lock", no_lock)
    before = dict(fake_redis._store)

    assert _summary(client, _pairs([room])).status_code == 200
    assert calls == []
    assert fake_redis._store == before


def test_summary_request_bounds(client, fake_redis):
    assert client.post(SUMMARY, json={"rooms": []}).status_code == 422
    too_many = [(f"ZZZZZ{i}", "s") for i in range(11)]
    assert _summary(client, too_many).status_code == 422
    assert _summary(client, [("A" * 65, "s")]).status_code == 422


def test_a_lowercase_code_finds_the_room(client, fake_redis):
    (room,) = _create(client, count=2)[:1]
    lower = room["room_code"].lower()
    (summary,) = _summary(client, [(lower, room["host_secret"])]).json()["rooms"]
    assert summary["exists"] is True
    assert summary["room_code"] == lower
