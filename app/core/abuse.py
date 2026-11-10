"""Abuse limits: request and event rates, and open sockets per address.

**Not a domain module.** It reads the clock, so -- like ``firebase.py`` and
``checks/`` -- it is a non-domain exception inside ``app/core``, and no domain
module may import it. It imports nothing from ``app`` at all.

Everything is counted in this process's memory, which matches the single
instance ``REDIS_ENABLED=false`` already requires. At N instances every limit
becomes N times looser, never tighter.

It is wired in at three single points and nowhere else:

* ``RateLimitMiddleware`` -- added once in ``app/main.py``; every REST route.
* ``socket_connect`` / ``socket_disconnect`` -- the handshake in
  ``app/sockets/handlers/connection.py``.
* ``socket_event_allowed`` -- inside ``@guarded`` (``app/sockets/errors.py``),
  which every socket event handler already wears.

The numbers are module constants, not settings (the §5 rule in
``CLAUDE.md``). They are sized for a classroom behind one NAT address --
about forty people sharing one IP -- not for one person.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_SOCKETS_PER_IP",
    "RATE_LIMITED_MESSAGE",
    "RateLimitMiddleware",
    "RateLimiter",
    "client_ip",
    "reset",
    "socket_connect",
    "socket_disconnect",
    "socket_event_allowed",
]

#: Every limit counts within a fixed window of this many seconds.
WINDOW_SECONDS = 60.0
#: All REST requests from one address per window.
REST_LIMIT_PER_IP = 1200
#: ``POST /api/v1/rooms/create`` per address per window. Unauthenticated, and
#: each one is a room held in memory for 24 h.
ROOM_CREATE_LIMIT_PER_IP = 30
#: ``GET /api/v1/rooms/{code}/status`` per address per window. Unauthenticated
#: and answers for any code -- the REST half of room-code guessing.
ROOM_STATUS_LIMIT_PER_IP = 120
#: ``POST /api/v1/rooms/preview`` per address per window. Unauthenticated, and
#: each one builds a config and draws a demand series. The host form debounces
#: edits at 300 ms, so one teacher typing flat out stays under 200 a minute.
ROOM_PREVIEW_LIMIT_PER_IP = 300
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

#: The ``detail`` of a REST 429 and the ``message`` of a refused socket event.
RATE_LIMITED_MESSAGE = "Too many requests. Please slow down and try again."

#: Never limited: Render's health check and the frontend's cold-start probe.
_EXEMPT_PATHS = frozenset({"/api/v1/health"})
_JOIN_EVENTS = frozenset({"join", "join_waiting"})
_ROOMS_PREFIX = "/api/v1/rooms/"
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

    def allow(self, key: str) -> bool:
        """Count one hit for ``key``; False once it is over the limit."""
        now = time.monotonic()
        started, count = self._hits.get(key, (now, 0))
        if now - started >= self.window:
            started, count = now, 0
        if count >= self.limit:
            return False
        if key not in self._hits and len(self._hits) >= self.max_keys:
            self._make_room(now)
        self._hits[key] = (started, count + 1)
        return True

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
_room_create = RateLimiter(ROOM_CREATE_LIMIT_PER_IP)
_room_status = RateLimiter(ROOM_STATUS_LIMIT_PER_IP)
_room_preview = RateLimiter(ROOM_PREVIEW_LIMIT_PER_IP)


def _route_limiter(method: str, path: str) -> RateLimiter | None:
    """The stricter limiter for this route, if it has one."""
    if method == "POST" and path == "/api/v1/rooms/create":
        return _room_create
    if method == "POST" and path == "/api/v1/rooms/preview":
        return _room_preview
    if method == "GET" and path.startswith(_ROOMS_PREFIX) and path.endswith("/status"):
        return _room_status
    return None


async def _too_many_requests(send: Send) -> None:
    """A 429 whose body is FastAPI's ``{"detail": "<string>"}`` shape, which
    the frontend's ``errorMessage`` helper already renders."""
    body = json.dumps({"detail": RATE_LIMITED_MESSAGE}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"retry-after", str(int(WINDOW_SECONDS)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RateLimitMiddleware:
    """Pure ASGI middleware: a 429 once an address is over its REST budget.

    Add it *before* ``CORSMiddleware`` in ``app/main.py``, so CORS wraps it:
    the 429 then carries CORS headers the browser lets the page read, and a
    preflight is answered by CORS without being counted here.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        ip = client_ip(scope)
        strict = _route_limiter(scope["method"], scope["path"])
        if not _rest.allow(ip) or (strict is not None and not strict.allow(ip)):
            await _too_many_requests(send)
            return
        await self.app(scope, receive, send)


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
        _socket_events,
        _socket_joins,
    ):
        limiter.reset()
    _sockets_per_ip.clear()
    _sid_ip.clear()
    _logged_ip_sources.clear()
