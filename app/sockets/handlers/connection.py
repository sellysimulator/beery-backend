"""The Socket.IO handshake: identity, established exactly once.

`identity` is server-only — a verified Firebase uid, or a `guest_<uuid4>` — and is
never on the wire (00-conventions.md §2). It is resolved here, at `connect`, and
every later handler reads `socket_manager.sid_to_identity[sid]` rather than
accepting anything the client says about who it is.
"""

import asyncio
import logging
import re
import uuid
from typing import Any

from ...api.deps import verify_firebase_id_token
from ...core.abuse import socket_connect, socket_disconnect
from ..manager import sio, socket_manager
from .play import on_participant_disconnected

logger = logging.getLogger(__name__)

# A Firebase uid can never satisfy this, so a guest can never claim a Firebase
# identity by presenting it as a guest id.
GUEST_ID_RE = re.compile(r"^guest_[0-9a-f-]{36}$")


def _new_guest_identity() -> str:
    """Mint a fresh guest identity for a client that offered no usable one."""
    return f"guest_{uuid.uuid4()}"


def resolve_identity(auth: dict | None) -> str | None:
    """Return the identity for a handshake payload, or None to reject it.

    None means an ID token was presented and did not verify. It is never a
    reason to fall back to an accompanying guest id: a silent downgrade would let
    anyone hold a seat while presenting a stolen or expired token, which is the
    impersonation bug the three-identifier model exists to prevent.

    An unverifiable token and an unconfigured server are the same answer here,
    deliberately.
    """
    if not isinstance(auth, dict):
        return _new_guest_identity()

    id_token = auth.get("idToken")
    if id_token:
        claims = verify_firebase_id_token(id_token)
        if not claims:
            return None
        uid = claims.get("uid")
        if not uid or not isinstance(uid, str):
            return None
        return uid

    guest_id = auth.get("guestId")
    if isinstance(guest_id, str) and GUEST_ID_RE.match(guest_id):
        return guest_id

    return _new_guest_identity()


@sio.event
async def connect(sid: str, environ: dict, auth: Any = None) -> Any:
    """Accept or reject a connection, recording its identity once on accept.

    Verification runs on a worker thread: the Firebase SDK checks the
    signature synchronously and may fetch Google's public keys over HTTP,
    which on the event loop would stall every room in the process.

    The per-address socket cap is checked first, before any token work, and
    the slot it takes is given back on every path that does not accept.
    """
    if not socket_connect(sid, environ):
        logger.warning("Rejecting handshake for sid %s: too many sockets.", sid)
        return False
    accepted = False
    try:
        identity = await asyncio.to_thread(resolve_identity, auth)
        if identity is None:
            # The identity itself is never logged.
            logger.warning("Rejecting handshake for sid %s: the ID token failed.", sid)
            return False

        await socket_manager.connect(sid, environ)
        socket_manager.sid_to_identity[sid] = identity
        accepted = True
        return None
    finally:
        if not accepted:
            socket_disconnect(sid)


@sio.event
async def disconnect(sid: str) -> None:
    """Drop every per-sid mapping, so the identity map cannot leak.

    Section 12's room-level cleanup runs first, while `sid_to_room` and
    `sid_to_alias` still hold this sid -- afterwards there is no room code
    left to look the participant up by (`12-socket-play.md`, module
    docstring).
    """
    try:
        await on_participant_disconnected(sid)
        await socket_manager.disconnect(sid)
    finally:
        socket_disconnect(sid)
