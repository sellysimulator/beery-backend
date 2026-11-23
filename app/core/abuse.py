"""Abuse limits: request and event rates, and open sockets per address.

**Not a domain module.** It reads the clock, so -- like ``firebase.py`` and
``checks/`` -- it is a non-domain exception inside ``app/core``, and no domain
module may import it. It imports nothing from ``app`` at all.

Everything is counted in this process's memory, which matches the single
instance ``REDIS_ENABLED=false`` already requires. At N instances every limit
becomes N times looser, never tighter.

It is wired in at four single points and nowhere else:

* ``RateLimitMiddleware`` -- added once in ``app/main.py``; every REST route,
  including the hourly game-export budget (``GAME_EXPORT_LIMIT_PER_IP``), and
  the ``MAX_BODY_BYTES`` cap on a request body.
* ``room_create_allowed`` -- ``POST /api/v1/rooms/batch-create`` charges the
  room-create budget one hit per room it makes, from inside the route, because
  only the route knows the count once the body has been validated.
* ``socket_connect`` / ``socket_disconnect`` -- the handshake in
  ``app/sockets/handlers/connection.py``.
* ``socket_event_allowed`` -- inside ``@guarded`` (``app/sockets/errors.py``),
  which every socket event handler already wears.

The numbers are module constants, not settings (the hard-limits rule
in ``CLAUDE.md``). They are sized for a classroom behind one NAT address --
about forty people sharing one IP -- not for one person.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "GAME_EXPORT_LIMIT_PER_IP",
    "GAME_EXPORT_WINDOW_SECONDS",
    "MAX_BODY_BYTES",
    "MAX_SOCKETS_PER_IP",
    "RATE_LIMITED_MESSAGE",
    "ROOM_CREATE_LIMIT_PER_IP",
    "ROOM_CREATE_WINDOW_SECONDS",
    "TOO_LARGE_MESSAGE",
    "RateLimitMiddleware",
    "RateLimiter",
    "client_ip",
    "reset",
    "room_create_allowed",
    "room_create_retry_after",
    "socket_connect",
    "socket_disconnect",
    "socket_event_allowed",
]

#: Every limit counts within a fixed window of this many seconds.
WINDOW_SECONDS = 60.0
#: All REST requests from one address per window.
REST_LIMIT_PER_IP = 1200
#: Rooms created per address per ``ROOM_CREATE_WINDOW_SECONDS``, shared by
#: ``POST /api/v1/rooms/create`` (one each) and ``POST /api/v1/rooms/batch-create``
#: (one per room, at most ``max_batch_rooms`` = 10 a request). Unauthenticated,
#: and each one is a room held in memory for 2 h (24 h once started). The standard across every
#: Novus game: 30 an hour, so three full batches.
ROOM_CREATE_LIMIT_PER_IP = 30
#: The room-create budget's window: an hour, not ``WINDOW_SECONDS``.
ROOM_CREATE_WINDOW_SECONDS = 3600.0
#: ``GET /api/v1/rooms/{code}/status`` per address per window. Unauthenticated
#: and answers for any code -- the REST half of room-code guessing.
ROOM_STATUS_LIMIT_PER_IP = 120
#: ``POST /api/v1/rooms/preview`` per address per window. Unauthenticated, and
#: each one builds a config and draws a demand series. The host form debounces
#: edits at 300 ms, so one teacher typing flat out stays under 200 a minute.
ROOM_PREVIEW_LIMIT_PER_IP = 300
#: ``GET /api/v1/games/id/{public_id}/export`` (CSV and JSON alike) per
#: address per ``GAME_EXPORT_WINDOW_SECONDS``. The export by ``public_id`` is
#: open to anyone who can see the game's results, and each one rebuilds the
#: whole record from PostgreSQL, so this is the compute guard; the results screen's
#: once-per-session download button is only a courtesy. A classroom shares
#: one address, so never lower it below a download per student per format.
GAME_EXPORT_LIMIT_PER_IP = 100
#: The export budget's window: an hour, not ``WINDOW_SECONDS``.
GAME_EXPORT_WINDOW_SECONDS = 3600.0
#: Sockets open at once from one address.
MAX_SOCKETS_PER_IP = 200
#: Socket events per connection per window. The host form debounces config
#: saves at 400 ms; nobody clicks at two a second for a whole minute.
SOCKET_EVENTS_PER_SID = 120
#: ``join`` + ``join_waiting`` per address per window -- the socket half of
#: room-code guessing.
SOCKET_JOINS_PER_IP = 300
#: Keys one limiter tracks at most, whatever arrives.
MAX_TRACKED_KEYS = 10_000
#: The largest REST request body, and the largest Socket.IO message
#: (``max_http_buffer_size`` in ``app/sockets/manager.py``). The largest
#: legitimate one is a whole config with a 104-week ``CUSTOM`` demand series,
#: a few KB.
MAX_BODY_BYTES = 64 * 1024

#: The ``detail`` of a REST 429 and the ``message`` of a refused socket event.
RATE_LIMITED_MESSAGE = "Too many requests. Please slow down and try again."
#: The ``detail`` of a REST 413.
TOO_LARGE_MESSAGE = "That request is too large."

#: Never limited: Render's health check and the frontend's cold-start probe.
_EXEMPT_PATHS = frozenset({"/api/v1/health"})
_JOIN_EVENTS = frozenset({"join", "join_waiting"})
_ROOMS_PREFIX = "/api/v1/rooms/"
_GAMES_PREFIX = "/api/v1/games/"
_MAX_IP_LENGTH = 64

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RateLimiter:
    """A fixed-window counter per string key, bounded in memory.

    At most ``max_keys`` keys are held. A new key arriving at the ceiling
    first drops every key whose window has ended (at most once per window),
    then, if still full, the oldest keys down to nine tenths of the ceiling.
    Either way the next tenth of new keys costs no scan at all, so a flood
    of new addresses cannot turn every call into an O(n) pass.
    """

    def __init__(
        self,
        limit: int,
        window: float = WINDOW_SECONDS,
        max_keys: int = MAX_TRACKED_KEYS,
    ) -> None:
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._hits: dict[str, tuple[float, int]] = {}
        self._pruned_at = float("-inf")

    def allow(self, key: str, cost: int = 1) -> bool:
        """Count ``cost`` hits for ``key``; False when they would take it over
        the limit.

        All or nothing: a refused call counts nothing, so a batch too big for
        what is left of the window (``POST /rooms/batch-create`` costs one hit
        per room) does not use up the rest of it.
        """
        now = time.monotonic()
        started, count = self._hits.get(key, (now, 0))
        if now - started >= self.window:
            started, count = now, 0
        if count + cost > self.limit:
            return False
        if key not in self._hits and len(self._hits) >= self.max_keys:
            self._make_room(now)
        self._hits[key] = (started, count + cost)
        return True

    def retry_after(self, key: str) -> int:
        """Whole seconds until ``key``'s window ends (at least 1), for a 429's
        ``Retry-After``."""
        started, _count = self._hits.get(key, (time.monotonic(), 0))
        left = self.window - (time.monotonic() - started)
        return max(1, math.ceil(left))

    def forget(self, key: str) -> None:
        self._hits.pop(key, None)

    def reset(self) -> None:
        self._hits.clear()
        self._pruned_at = float("-inf")

    def __len__(self) -> int:
        return len(self._hits)

    def _make_room(self, now: float) -> None:
        if now - self._pruned_at >= self.window:
            self._pruned_at = now
            self._hits = {
                key: hit
                for key, hit in self._hits.items()
                if now - hit[0] < self.window
            }
        if len(self._hits) >= self.max_keys:
            excess = len(self._hits) - self.max_keys * 9 // 10
            for key in list(self._hits)[:excess]:
                del self._hits[key]


_logged_ip_sources: set[str] = set()


def _note_ip_source(source: str) -> None:
    """Log, once per process, which branch of ``client_ip`` answered (never the IP)."""
    if source not in _logged_ip_sources:
        _logged_ip_sources.add(source)
        logger.info("client ip source: %s", source)


def client_ip(scope: Mapping[str, Any]) -> str:
    """The caller's address for an ASGI ``scope``: REST, or a socket's
    ``environ["asgi.scope"]``.

    In order: ``CF-Connecting-IP`` (set by Render's Cloudflare edge), the
    **rightmost** non-empty ``X-Forwarded-For`` entry, the ASGI peer. The
    Dockerfile runs uvicorn with ``--proxy-headers`` and
    ``FORWARDED_ALLOW_IPS=*``, so ``scope["client"]`` holds the *leftmost*
    entry -- whatever the client chose to write. Render's proxy appends the
    address it accepted the connection from, so the last entry is the one
    a client cannot pick.
    """
    connecting = b""
    forwarded = b""
    for name, value in scope.get("headers") or ():
        lowered = name.lower()
        if lowered == b"cf-connecting-ip":
            connecting = value
        elif lowered == b"x-forwarded-for":
            forwarded = value
    direct = connecting.decode("latin-1").strip()
    if direct:
        _note_ip_source("cf-connecting-ip")
        return direct[:_MAX_IP_LENGTH]
    entries = [e.strip() for e in forwarded.decode("latin-1").split(",") if e.strip()]
    if entries:
        n = len(entries)
        n_label = str(n) if n <= 3 else "3+"
        _note_ip_source(f"x-forwarded-for ({n_label} entries)")
        return entries[-1][:_MAX_IP_LENGTH]
    client = scope.get("client")
    if client:
        _note_ip_source("peer")
        return str(client[0])[:_MAX_IP_LENGTH]
    _note_ip_source("unknown")
    return "unknown"


# --- REST ------------------------------------------------------------------ #

_rest = RateLimiter(REST_LIMIT_PER_IP)
_room_create = RateLimiter(ROOM_CREATE_LIMIT_PER_IP, window=ROOM_CREATE_WINDOW_SECONDS)
_room_status = RateLimiter(ROOM_STATUS_LIMIT_PER_IP)
_room_preview = RateLimiter(ROOM_PREVIEW_LIMIT_PER_IP)
_game_export = RateLimiter(GAME_EXPORT_LIMIT_PER_IP, window=GAME_EXPORT_WINDOW_SECONDS)


def _route_limiter(method: str, path: str) -> RateLimiter | None:
    """The stricter limiter for this route, if it has one.

    ``POST /api/v1/rooms/batch-create`` is deliberately absent: the route
    charges ``_room_create`` itself, one hit per room (``room_create_allowed``),
    so here it counts against the general REST budget only. Charging it here
    as well would bill a batch one room more than it made.
    """
    if method == "POST" and path == "/api/v1/rooms/create":
        return _room_create
    if method == "POST" and path == "/api/v1/rooms/preview":
        return _room_preview
    if method == "GET" and path.startswith(_ROOMS_PREFIX) and path.endswith("/status"):
        return _room_status
    if method == "GET" and path.startswith(_GAMES_PREFIX) and path.endswith("/export"):
        return _game_export
    return None


def room_create_allowed(scope: Mapping[str, Any], rooms: int) -> bool:
    """Charge ``rooms`` room creations to the caller's address, all or nothing.

    The batch route's half of the room-create budget: ``POST /rooms/create``
    and ``POST /rooms/batch-create`` share ``ROOM_CREATE_LIMIT_PER_IP``, so a
    batch of ten is ten creates. A refusal charges nothing. The caller answers
    a refusal with the same 429 the middleware sends.
    """
    return _room_create.allow(client_ip(scope), cost=rooms)


def room_create_retry_after(scope: Mapping[str, Any]) -> int:
    """Seconds until the caller's room-create window ends, for the batch
    route's 429 ``Retry-After`` (the middleware computes its own)."""
    return _room_create.retry_after(client_ip(scope))


async def _too_many_requests(send: Send, retry_after: int) -> None:
    """A 429 whose body is FastAPI's ``{"detail": "<string>"}`` shape, which
    the frontend's ``errorMessage`` helper already renders, and whose
    ``Retry-After`` is the seconds left in the window that refused it."""
    await _refuse(
        send,
        429,
        RATE_LIMITED_MESSAGE,
        [(b"retry-after", str(retry_after).encode("ascii"))],
    )


async def _too_large(send: Send) -> None:
    """A 413 in the same ``{"detail": "<string>"}`` shape."""
    await _refuse(send, 413, TOO_LARGE_MESSAGE, [])


async def _refuse(
    send: Send, status: int, detail: str, extra: list[tuple[bytes, bytes]]
) -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                *extra,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _declared_too_large(scope: Scope) -> bool:
    """A ``Content-Length`` over ``MAX_BODY_BYTES``, or one that is not a
    number."""
    for name, value in scope.get("headers") or ():
        if name.lower() == b"content-length":
            return not value.isdigit() or int(value) > MAX_BODY_BYTES
    return False


async def _read_body(receive: Receive) -> tuple[list[Message], bool]:
    """The request's messages up to the end of its body, and whether that
    body is over ``MAX_BODY_BYTES``. A chunked body declares no
    ``Content-Length``, so its cap holds here, as it arrives."""
    messages: list[Message] = []
    size = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            return messages, False
        size += len(message.get("body", b""))
        if size > MAX_BODY_BYTES:
            return messages, True
        if not message.get("more_body", False):
            return messages, False


class RateLimitMiddleware:
    """Pure ASGI middleware: a 429 once an address is over its REST budget,
    and a 413 for a body over ``MAX_BODY_BYTES``.

    A declared ``Content-Length`` over the cap is refused before anything is
    counted or read; any other body is read up to the cap and handed on to
    the route unchanged.

    Add it *before* ``CORSMiddleware`` in ``app/main.py``, so CORS wraps it:
    the 429 or 413 then carries CORS headers the browser lets the page read,
    and a preflight is answered by CORS without being counted here.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        if _declared_too_large(scope):
            await _too_large(send)
            return
        ip = client_ip(scope)
        strict = _route_limiter(scope["method"], scope["path"])
        if not _rest.allow(ip):
            await _too_many_requests(send, _rest.retry_after(ip))
            return
        if strict is not None and not strict.allow(ip):
            await _too_many_requests(send, strict.retry_after(ip))
            return
        messages, too_large = await _read_body(receive)
        if too_large:
            await _too_large(send)
            return

        async def replay() -> Message:
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)


# --- Socket.IO ------------------------------------------------------------- #

_sockets_per_ip: dict[str, int] = {}
_sid_ip: dict[str, str] = {}
_socket_events = RateLimiter(SOCKET_EVENTS_PER_SID)
_socket_joins = RateLimiter(SOCKET_JOINS_PER_IP)


def socket_connect(sid: str, environ: Mapping[str, Any]) -> bool:
    """Take a socket slot for ``sid``'s address; False when it has none left.

    A True must be paired with ``socket_disconnect(sid)``, including when the
    handshake is refused afterwards for another reason.
    """
    ip = client_ip(environ.get("asgi.scope") or {})
    open_now = _sockets_per_ip.get(ip, 0)
    if open_now >= MAX_SOCKETS_PER_IP:
        return False
    _sockets_per_ip[ip] = open_now + 1
    _sid_ip[sid] = ip
    return True


def socket_disconnect(sid: str) -> None:
    """Give back ``sid``'s slot and forget its counters. Safe to repeat."""
    _socket_events.forget(sid)
    ip = _sid_ip.pop(sid, None)
    if ip is None:
        return
    left = _sockets_per_ip.get(ip, 0) - 1
    if left > 0:
        _sockets_per_ip[ip] = left
    else:
        _sockets_per_ip.pop(ip, None)


def socket_event_allowed(sid: str, event: str) -> bool:
    """Count one ``event`` from ``sid``; False when it is over a limit.

    A sid that never passed ``socket_connect`` is not counted. Socket.IO
    dispatches no event for a connection whose ``connect`` did not accept,
    and ``connect`` always calls ``socket_connect`` first, so in the running
    app every sid that reaches a handler is known here; the unknown case is
    a test calling a handler directly.
    """
    ip = _sid_ip.get(sid)
    if ip is None:
        return True
    if not _socket_events.allow(sid):
        return False
    return event not in _JOIN_EVENTS or _socket_joins.allow(ip)


def reset() -> None:
    """Forget every count and every open socket (tests)."""
    for limiter in (
        _rest,
        _room_create,
        _room_status,
        _room_preview,
        _game_export,
        _socket_events,
        _socket_joins,
    ):
        limiter.reset()
    _sockets_per_ip.clear()
    _sid_ip.clear()
    _logged_ip_sources.clear()
