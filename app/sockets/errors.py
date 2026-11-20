"""One guard so that a handler's failure reaches the person who caused it.

**The defect this exists for.** Every socket handler wraps a read-modify-write
of the room document. Nothing caught a failure of that store, so an exception
propagated out of the handler into Socket.IO's own logging and the client was
told *nothing at all* — no error event, no rejection, no acknowledgement.
Measured by injecting a single failed `get` into one `submit_order` mid-game:
the handler raised `ConnectionError` and the player received zero events. Their
submit button stays enabled, `has_submitted` never flips, and the order simply
did not happen, with nothing on screen to say so. Across a real store outage
that is every player in every room clicking into a void.

This is not Redis-specific and does not go away with the in-memory backend: any
unexpected exception in a handler had the same shape.

**What it does not do.** It does not swallow. Every caught exception is logged
with `logger.exception`, so the traceback survives — a bare
`except: pass` cost the sibling project every completed game for an unknown
length of time. This turns an invisible failure into a visible
one in two places at once: the log and the player's screen.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..core.abuse import RATE_LIMITED_MESSAGE, socket_event_allowed
from .manager import socket_manager

__all__ = ["SERVER_ERROR_MESSAGE", "guarded", "requested_room_code"]

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]

#: Deliberately vague. The player can act on "try again"; they can do nothing
#: with a driver message, and none may ever go on the
#: wire — a connection string in an alert box is the same leak by another
#: route.
SERVER_ERROR_MESSAGE = "Something went wrong on the server. Please try again."


def requested_room_code(data: Any) -> str | None:
    """The `room_id` a payload asked for, echoed back as `room_code` on a
    refusal of `start_game` (2026-11-14). It is the caller's own input, so
    echoing it reveals nothing; it is bounded so a refusal cannot be made to
    carry an arbitrary blob."""
    if not isinstance(data, dict):
        return None
    room_id = data.get("room_id")
    if isinstance(room_id, str) and 0 < len(room_id) <= _MAX_ECHOED_ROOM_CODE:
        return room_id
    return None


_MAX_ECHOED_ROOM_CODE = 64


def guarded(
    event: str = "error", *, echo_room_code: bool = False
) -> Callable[[Handler], Handler]:
    """Report an unhandled handler exception to the caller, then continue.

    `event` is `"error"` for the play loop, whose payload carries a `code`,
    and `"join_error"` for the lobby, whose payload is `{message}` only
    (spec §11.2). Both frozen shapes are respected.

    With `echo_room_code`, the refusal also carries the `room_code` the payload
    asked for (`requested_room_code`), next to `message`. `start_game` wears
    it, so the multi-game dashboard can put a "Start all" refusal on that
    game's card (2026-11-14). Additive: a listener that reads `message` only
    is unaffected.

    Apply it *under* `@sio.event`, so Socket.IO registers the wrapper:

        @sio.event
        @guarded()
        async def submit_order(sid, data=None): ...
    """

    async def report(sid: str, message: str, code: str, data: Any = None) -> None:
        payload: dict[str, str] = {"message": message}
        if event == "error":
            payload["code"] = code
        if echo_room_code:
            room_code = requested_room_code(data)
            if room_code is not None:
                payload["room_code"] = room_code
        try:
            await socket_manager.emit_to_sid(sid, event, payload)
        except Exception:  # pragma: no cover - the socket is gone
            logger.exception("Could not report the failure to sid %s.", sid)

    def decorate(fn: Handler) -> Handler:
        @functools.wraps(fn)
        async def wrapper(sid: str, *args: Any, **kwargs: Any) -> Any:
            # The one per-event rate check (`app/core/abuse.py`): every
            # handler wears this decorator, so none repeats it.
            data = args[0] if args else kwargs.get("data")
            if not socket_event_allowed(sid, fn.__name__):
                await report(sid, RATE_LIMITED_MESSAGE, "RATE_LIMITED", data)
                return None
            try:
                return await fn(sid, *args, **kwargs)
            except Exception:
                # The event name, never the payload: a payload may carry a
                # `host_secret` or a `session_token`.
                logger.exception("Handler %r failed for sid %s.", fn.__name__, sid)
                await report(sid, SERVER_ERROR_MESSAGE, "SERVER_ERROR", data)
                return None

        return wrapper

    return decorate
