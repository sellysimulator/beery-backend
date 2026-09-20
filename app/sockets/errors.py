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
with `logger.exception`, so the traceback survives — `14 §3.4` records what a
bare `except: pass` cost the sibling project, which was every completed game
for an unknown length of time. This turns an invisible failure into a visible
one in two places at once: the log and the player's screen.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .manager import socket_manager

__all__ = ["SERVER_ERROR_MESSAGE", "guarded"]

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]

#: Deliberately vague. The player can act on "try again"; they can do nothing
#: with a driver message, and `15 §3.7` already forbids putting one on the
#: wire — a connection string in an alert box is the same leak by another
#: route.
SERVER_ERROR_MESSAGE = "Something went wrong on the server. Please try again."


def guarded(event: str = "error") -> Callable[[Handler], Handler]:
    """Report an unhandled handler exception to the caller, then continue.

    `event` is `"error"` for the play loop, whose payload carries a `code`
    (`12 §2`), and `"join_error"` for the lobby, whose payload is `{message}`
    only (`11 §2`). Both frozen shapes are respected.

    Apply it *under* `@sio.event`, so Socket.IO registers the wrapper:

        @sio.event
        @guarded()
        async def submit_order(sid, data=None): ...
    """

    def decorate(fn: Handler) -> Handler:
        @functools.wraps(fn)
        async def wrapper(sid: str, *args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(sid, *args, **kwargs)
            except Exception:
                # The event name, never the payload: a payload may carry a
                # `host_secret` or a `session_token` (`00-conventions.md §2`).
                logger.exception("Handler %r failed for sid %s.", fn.__name__, sid)
                payload: dict[str, str] = {"message": SERVER_ERROR_MESSAGE}
                if event == "error":
                    payload["code"] = "SERVER_ERROR"
                try:
                    await socket_manager.emit_to_sid(sid, event, payload)
                except Exception:  # pragma: no cover - the socket is gone
                    logger.exception("Could not report the failure to sid %s.", sid)
                return None

        return wrapper

    return decorate
