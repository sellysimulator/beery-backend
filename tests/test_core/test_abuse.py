"""Abuse limits (`app/core/abuse.py`): the limiter, the client address, the
REST middleware and the socket counters.

`tests/conftest.py` resets every counter before each test, so each one here
starts from zero.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.core import abuse
from app.core.abuse import RateLimiter, client_ip
from app.core.presets import get_preset


@pytest.fixture()
def clock(monkeypatch) -> list[float]:
    now = [1_000.0]
    monkeypatch.setattr("app.core.abuse.time.monotonic", lambda: now[0])
    return now


def _scope(xff: str | None = None, client: tuple[str, int] | None = None) -> dict:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return {"type": "http", "headers": headers, "client": client}


# --- RateLimiter ------------------------------------------------------------


def test_the_limiter_allows_the_limit_then_refuses(clock) -> None:
    limiter = RateLimiter(3, window=60)
    assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("b") is True


def test_the_limiter_charges_a_cost_all_or_nothing(clock) -> None:
    # POST /rooms/batch-create charges one hit per room it makes.
    limiter = RateLimiter(5, window=60)

    assert limiter.allow("a", cost=3) is True
    # 3 + 3 > 5: refused, and the refused call is not counted...
    assert limiter.allow("a", cost=3) is False
    # ...so the 2 that are left can still be used.
    assert limiter.allow("a", cost=2) is True
    assert limiter.allow("a") is False


def test_the_limiter_refuses_a_cost_bigger_than_the_limit(clock) -> None:
    limiter = RateLimiter(5, window=60)
    assert limiter.allow("a", cost=6) is False
    assert len(limiter) == 0
    assert limiter.allow("a", cost=5) is True


def test_room_create_allowed_shares_the_create_budget(clock, monkeypatch) -> None:
    monkeypatch.setattr(abuse, "_room_create", RateLimiter(3))
    scope = _scope(xff="203.0.113.9")

    assert abuse.room_create_allowed(scope, 2) is True
    assert abuse.room_create_allowed(scope, 2) is False
    assert abuse.room_create_allowed(scope, 1) is True
    assert abuse.room_create_allowed(_scope(xff="203.0.113.10"), 3) is True


def test_the_limiter_opens_again_when_the_window_ends(clock) -> None:
    limiter = RateLimiter(1, window=60)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    clock[0] += 60
    assert limiter.allow("a") is True


def test_a_full_limiter_trims_to_nine_tenths_not_one_key(clock) -> None:
    """Trimming to exactly the ceiling would rescan on every new key."""
    limiter = RateLimiter(5, window=60, max_keys=10)
    for n in range(10):
        limiter.allow(f"ip-{n}")
    limiter.allow("ip-new")
    assert len(limiter) == 10  # 10 -> 9 by trimming, then the new key
    assert limiter.allow("ip-0") is True  # the oldest went: counting restarts


def test_the_limiter_never_holds_more_than_max_keys(clock) -> None:
    limiter = RateLimiter(5, window=60, max_keys=3)
    for n in range(50):
        assert limiter.allow(f"ip-{n}") is True
        assert len(limiter) <= 3


def test_a_full_limiter_drops_stale_keys_before_fresh_ones(clock) -> None:
    limiter = RateLimiter(1, window=60, max_keys=2)
    limiter.allow("old")
    clock[0] += 61
    limiter.allow("fresh")
    limiter.allow("newer")
    assert limiter.allow("fresh") is False  # still counted: "old" went, not it


def test_forget_and_reset_drop_counts(clock) -> None:
    limiter = RateLimiter(1, window=60)
    limiter.allow("a")
    limiter.forget("a")
    assert limiter.allow("a") is True
    limiter.reset()
    assert len(limiter) == 0


# --- client_ip --------------------------------------------------------------


def test_client_ip_takes_the_rightmost_forwarded_entry() -> None:
    """The leftmost entry is whatever the client wrote; the proxy appends."""
    assert client_ip(_scope("6.6.6.6, 203.0.113.9", ("10.0.0.1", 1))) == "203.0.113.9"


def test_client_ip_uses_the_last_forwarded_header_when_repeated() -> None:
    scope = _scope("6.6.6.6", ("10.0.0.1", 1))
    scope["headers"].append((b"X-Forwarded-For", b"203.0.113.9"))
    assert client_ip(scope) == "203.0.113.9"


def test_client_ip_prefers_cf_connecting_ip() -> None:
    scope = _scope("6.6.6.6, 203.0.113.9", ("10.0.0.1", 1))
    scope["headers"].append((b"cf-connecting-ip", b" 198.51.100.7 "))
    assert client_ip(scope) == "198.51.100.7"


def test_client_ip_skips_empty_trailing_forwarded_entries() -> None:
    assert client_ip(_scope("203.0.113.9, ,", ("10.0.0.1", 1))) == "203.0.113.9"


def test_client_ip_falls_back_to_the_peer_then_unknown() -> None:
    assert client_ip(_scope(None, ("198.51.100.4", 5000))) == "198.51.100.4"
    assert client_ip(_scope(" , ", ("198.51.100.4", 5000))) == "198.51.100.4"
    assert client_ip({}) == "unknown"


def test_client_ip_is_bounded_in_length() -> None:
    assert len(client_ip(_scope("x" * 10_000))) == 64


# --- REST middleware --------------------------------------------------------


def test_room_status_answers_429_past_its_limit(client, fake_redis, monkeypatch):
    monkeypatch.setattr(abuse, "_room_status", RateLimiter(2))
    url = "/api/v1/rooms/ZZZZZZ/status"

    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 200
    response = client.get(url)

    assert response.status_code == 429
    assert response.json() == {"detail": abuse.RATE_LIMITED_MESSAGE}
    assert response.headers["retry-after"] == "60"


def test_room_create_answers_429_past_its_limit(client, fake_redis, monkeypatch):
    monkeypatch.setattr(abuse, "_room_create", RateLimiter(1))

    assert client.post("/api/v1/rooms/create", json={}).status_code == 200
    assert client.post("/api/v1/rooms/create", json={}).status_code == 429


def test_room_preview_answers_429_past_its_limit(client, fake_redis, monkeypatch):
    """Unauthenticated CPU work: its own budget, below the general one."""
    monkeypatch.setattr(abuse, "_room_preview", RateLimiter(1))
    body = {"config": get_preset("CLASSIC_MIT").to_payload()}

    assert client.post("/api/v1/rooms/preview", json=body).status_code == 200
    response = client.post("/api/v1/rooms/preview", json=body)

    assert response.status_code == 429
    assert response.json() == {"detail": abuse.RATE_LIMITED_MESSAGE}


def test_game_export_has_an_hourly_budget_shared_by_both_formats(client, monkeypatch):
    """Open by public_id, so the export carries its own per-address budget:
    CSV and JSON draw on one count, and the 429 says when the hour ends."""
    monkeypatch.setattr(
        abuse,
        "_game_export",
        RateLimiter(2, window=abuse.GAME_EXPORT_WINDOW_SECONDS),
    )
    url = "/api/v1/games/id/" + "0" * 32 + "/export"

    # Not 429: the route ran (and found no database behind it in this suite).
    assert client.get(url, params={"format": "csv"}).status_code != 429
    assert client.get(url, params={"format": "json"}).status_code != 429
    response = client.get(url, params={"format": "csv"})

    assert response.status_code == 429
    assert response.json() == {"detail": abuse.RATE_LIMITED_MESSAGE}
    retry_after = int(response.headers["retry-after"])
    assert 3500 < retry_after <= 3600


def test_the_export_by_room_code_draws_on_the_same_budget(client, monkeypatch):
    monkeypatch.setattr(abuse, "_game_export", RateLimiter(1, window=3600.0))

    assert client.get("/api/v1/games/ABC234/export").status_code != 429
    assert client.get("/api/v1/games/id/" + "0" * 32 + "/export").status_code == 429


def test_the_export_budget_is_one_hundred_an_hour() -> None:
    """A classroom shares one address: never lower than this."""
    assert abuse.GAME_EXPORT_LIMIT_PER_IP == 100
    assert abuse.GAME_EXPORT_WINDOW_SECONDS == 3600
    assert abuse._game_export.limit == 100
    assert abuse._game_export.window == 3600


def test_retry_after_counts_down_the_window(clock) -> None:
    limiter = RateLimiter(1, window=3600.0)
    assert limiter.allow("a") is True
    clock[0] += 600.0

    assert limiter.allow("a") is False
    assert limiter.retry_after("a") == 3000


def test_the_results_route_is_not_on_the_export_budget(client, monkeypatch):
    monkeypatch.setattr(abuse, "_game_export", RateLimiter(0, window=3600.0))

    assert client.get("/api/v1/games/id/" + "0" * 32 + "/results").status_code != 429


def test_each_forwarded_address_has_its_own_budget(client, fake_redis, monkeypatch):
    monkeypatch.setattr(abuse, "_room_status", RateLimiter(1))
    url = "/api/v1/rooms/ZZZZZZ/status"

    assert (
        client.get(url, headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 200
    )
    assert (
        client.get(url, headers={"X-Forwarded-For": "203.0.113.2"}).status_code == 200
    )
    assert (
        client.get(url, headers={"X-Forwarded-For": "203.0.113.1"}).status_code == 429
    )


def test_every_route_shares_the_general_budget(client, monkeypatch):
    monkeypatch.setattr(abuse, "_rest", RateLimiter(1))

    assert client.get("/").status_code == 200
    assert client.get("/api/v1/rooms/presets").status_code == 429


def test_the_liveness_probe_is_never_limited(client, monkeypatch):
    monkeypatch.setattr(abuse, "_rest", RateLimiter(0))

    assert client.get("/api/v1/health").status_code == 200


def test_a_429_still_carries_cors_headers(client, monkeypatch):
    """Otherwise the browser hides the 429 and the page sees a network error."""
    from app.config import settings

    monkeypatch.setattr(abuse, "_rest", RateLimiter(0))
    origin = settings.CORS_ORIGINS[0]

    response = client.get("/api/v1/rooms/presets", headers={"Origin": origin})

    assert response.status_code == 429
    assert response.headers["access-control-allow-origin"] == origin


def test_a_429_exposes_retry_after_to_browsers(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(abuse, "_rest", RateLimiter(0))
    origin = settings.CORS_ORIGINS[0]

    response = client.get("/api/v1/rooms/presets", headers={"Origin": origin})

    assert response.status_code == 429
    assert "Retry-After" in response.headers["access-control-expose-headers"]


def test_a_body_over_the_cap_is_refused_with_413(client, fake_redis) -> None:
    body = {"config": {"x": "y" * abuse.MAX_BODY_BYTES}}

    response = client.post("/api/v1/rooms/preview", json=body)

    assert response.status_code == 413
    assert response.json() == {"detail": abuse.TOO_LARGE_MESSAGE}


def test_a_chunked_body_over_the_cap_is_refused_with_413(client) -> None:
    """A chunked body declares no length: the cap holds as it is read."""

    def chunks():
        yield b'{"config": {"x": "'
        for _ in range(4):
            yield b"y" * abuse.MAX_BODY_BYTES
        yield b'"}}'

    response = client.post(
        "/api/v1/rooms/preview",
        content=chunks(),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": abuse.TOO_LARGE_MESSAGE}


def test_a_small_chunked_body_still_reaches_the_route(client) -> None:
    body = json.dumps({"config": get_preset("CLASSIC_MIT").to_payload()}).encode()

    def chunks():
        yield body[:100]
        yield body[100:]

    response = client.post(
        "/api/v1/rooms/preview",
        content=chunks(),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [b"65537", b"abc"])
async def test_an_oversized_or_unreadable_content_length_is_413_unread(
    length: bytes,
) -> None:
    """Refused from the header alone: the body is never read and nothing is
    counted against the address."""
    sent: list[dict] = []

    async def inner(scope, receive, send) -> None:
        raise AssertionError("the route must not run")

    async def no_receive() -> dict:
        raise AssertionError("the body must not be read")

    async def send(message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/rooms/preview",
        "headers": [(b"content-length", length)],
        "client": ("203.0.113.9", 1),
    }
    await abuse.RateLimitMiddleware(inner)(scope, no_receive, send)

    assert sent[0]["status"] == 413
    assert len(abuse._rest) == 0


def test_a_413_still_carries_cors_headers(client) -> None:
    from app.config import settings

    origin = settings.CORS_ORIGINS[0]
    body = {"config": {"x": "y" * abuse.MAX_BODY_BYTES}}

    response = client.post(
        "/api/v1/rooms/preview", json=body, headers={"Origin": origin}
    )

    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == origin


def test_a_socket_message_has_the_same_cap() -> None:
    from app.sockets.manager import sio

    assert abuse.MAX_BODY_BYTES == 64 * 1024
    assert sio.eio.max_http_buffer_size == abuse.MAX_BODY_BYTES


@pytest.mark.asyncio
async def test_non_http_scopes_pass_straight_through() -> None:
    seen: list[str] = []

    async def inner(scope, receive, send) -> None:
        seen.append(scope["type"])

    await abuse.RateLimitMiddleware(inner)({"type": "lifespan"}, None, None)

    assert seen == ["lifespan"]


# --- sockets ----------------------------------------------------------------


def _environ(ip: str) -> dict:
    return {"asgi.scope": _scope(ip)}


def test_sockets_per_address_are_capped_and_given_back(monkeypatch) -> None:
    monkeypatch.setattr(abuse, "MAX_SOCKETS_PER_IP", 2)

    assert abuse.socket_connect("s1", _environ("203.0.113.1")) is True
    assert abuse.socket_connect("s2", _environ("203.0.113.1")) is True
    assert abuse.socket_connect("s3", _environ("203.0.113.1")) is False
    assert abuse.socket_connect("s4", _environ("203.0.113.2")) is True

    abuse.socket_disconnect("s1")
    abuse.socket_disconnect("s1")  # repeating it gives back nothing extra
    assert abuse.socket_connect("s5", _environ("203.0.113.1")) is True
    assert abuse.socket_connect("s6", _environ("203.0.113.1")) is False


def test_disconnecting_the_last_socket_forgets_the_address() -> None:
    abuse.socket_connect("s1", _environ("203.0.113.1"))
    abuse.socket_disconnect("s1")
    assert abuse._sockets_per_ip == {}
    assert abuse._sid_ip == {}


def test_socket_events_are_limited_per_connection(monkeypatch) -> None:
    monkeypatch.setattr(abuse, "_socket_events", RateLimiter(2))
    abuse.socket_connect("s1", _environ("203.0.113.1"))
    abuse.socket_connect("s2", _environ("203.0.113.1"))

    assert abuse.socket_event_allowed("s1", "request_state") is True
    assert abuse.socket_event_allowed("s1", "request_state") is True
    assert abuse.socket_event_allowed("s1", "request_state") is False
    assert abuse.socket_event_allowed("s2", "request_state") is True


def test_joins_are_limited_per_address_across_connections(monkeypatch) -> None:
    monkeypatch.setattr(abuse, "_socket_joins", RateLimiter(2))
    for sid in ("s1", "s2", "s3"):
        abuse.socket_connect(sid, _environ("203.0.113.1"))

    assert abuse.socket_event_allowed("s1", "join") is True
    assert abuse.socket_event_allowed("s2", "join_waiting") is True
    assert abuse.socket_event_allowed("s3", "join") is False
    assert abuse.socket_event_allowed("s3", "submit_order") is True


def test_a_sid_that_never_connected_is_not_counted(monkeypatch) -> None:
    monkeypatch.setattr(abuse, "_socket_events", RateLimiter(0))
    assert abuse.socket_event_allowed("never-connected", "join") is True


def test_client_ip_source_is_logged_once_and_never_the_ip(caplog) -> None:
    xff = {
        "headers": [(b"x-forwarded-for", b"6.6.6.6, 203.0.113.7")],
        "client": ("10.0.0.1", 1),
    }
    peer = {"headers": [], "client": ("203.0.113.7", 1)}
    with caplog.at_level(logging.INFO, logger="app.core.abuse"):
        assert client_ip(xff) == "203.0.113.7"
        assert client_ip(xff) == "203.0.113.7"
        assert caplog.text.count("client ip source: x-forwarded-for (2 entries)") == 1
        assert client_ip(peer) == "203.0.113.7"
    assert caplog.text.count("client ip source: peer") == 1
    with caplog.at_level(logging.INFO, logger="app.core.abuse"):
        for n in (4, 5, 9):
            many = {
                "headers": [
                    (
                        b"x-forwarded-for",
                        b",".join([b"6.6.6.6"] * (n - 1) + [b"203.0.113.7"]),
                    )
                ]
            }
            assert client_ip(many) == "203.0.113.7"
    assert caplog.text.count("client ip source: x-forwarded-for (3+ entries)") == 1
    assert caplog.text.count("client ip source:") == 3
    assert "203.0.113.7" not in caplog.text
