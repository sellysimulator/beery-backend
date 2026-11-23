"""The Socket.IO handshake: identity, established exactly once.

`identity` is server-only — a verified Firebase uid, or a `guest_<uuid4>` — and is
never on the wire. It is resolved here, at `connect`, and
every later handler reads `socket_manager.sid_to_identity[sid]` rather than
accepting anything the client says about who it is. A verified uid is also
recorded in `socket_manager.sid_to_verified_uid[sid]`, the one place a
handler may learn that a connection is signed in.
"""

import asyncio
import logging
import re
import uuid
from typing import Any, NamedTuple

import socketio  # type: ignore[import-untyped]

from ...api.deps import verify_firebase_id_token
from ...core.abuse import socket_connect, socket_disconnect
from ..manager import sio, socket_manager
from .play import on_participant_disconnected

logger = logging.getLogger(__name__)

# A Firebase uid can never satisfy this, so a guest can never claim a Firebase
# identity by presenting it as a guest id.
GUEST_ID_RE = re.compile(r"^guest_[0-9a-f-]{36}$")

# Shown by the client's `connect_error`. Deliberately free of auth words: being
# over the limit is no reason to sign in again.
TOO_MANY_CONNECTIONS_MESSAGE = (
    "Too many connections from your network. Try again in a minute."
)


def _new_guest_identity() -> str:
    """Mint a fresh guest identity for a client that offered no usable one."""
    return f"guest_{uuid.uuid4()}"


class Handshake(NamedTuple):
    """Who a connection is. `verified_uid` is the uid of an ID token that
    verified, and None for every guest: it is set from the verification
    itself, never inferred from the shape of `identity`."""

    identity: str
    verified_uid: str | None


def resolve_handshake(auth: dict | None) -> Handshake | None:
    """Return the identity for a handshake payload, or None to reject it.

    None means an ID token was presented and did not verify, or that no
    identity was presented at all. A failed token is never a reason to fall
    back to an accompanying guest id: a silent downgrade would let anyone hold
    a seat while presenting a stolen or expired token, which is the
    impersonation bug the three-identifier model exists to prevent.

    No identity -- neither an ID token nor a guest id -- is what a signed-in
    browser sends when it could not fetch its token; a fresh guest would seat
    a signed-in user under a throwaway identity, so it is refused too (the
    client retries). Every real guest sends the guest id its browser keeps; a
    malformed one is still replaced by a fresh guest.

    An unverifiable token and an unconfigured server are the same answer here,
    deliberately.
    """
    if not isinstance(auth, dict):
        return None

    id_token = auth.get("idToken")
    if id_token:
        claims = verify_firebase_id_token(id_token)
        if not claims:
            return None
        uid = claims.get("uid")
        if not uid or not isinstance(uid, str):
            return None
        return Handshake(uid, uid)

    guest_id = auth.get("guestId")
    if not guest_id:
        return None
    if isinstance(guest_id, str) and GUEST_ID_RE.match(guest_id):
        return Handshake(guest_id, None)

    return Handshake(_new_guest_identity(), None)


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
        raise socketio.exceptions.ConnectionRefusedError(TOO_MANY_CONNECTIONS_MESSAGE)
    accepted = False
    try:
        handshake = await asyncio.to_thread(resolve_handshake, auth)
        if handshake is None:
            # The identity itself is never logged.
            logger.warning(
                "Rejecting handshake for sid %s: no identity, or the ID token failed.",
                sid,
            )
            return False

        await socket_manager.connect(sid, environ)
        socket_manager.sid_to_identity[sid] = handshake.identity
        if handshake.verified_uid is not None:
            socket_manager.sid_to_verified_uid[sid] = handshake.verified_uid
        accepted = True
        return None
    finally:
        if not accepted:
            socket_disconnect(sid)


@sio.event
async def disconnect(sid: str) -> None:
    """Drop every per-sid mapping, so the identity map cannot leak.

    The play loop's room-level cleanup (`on_participant_disconnected`) runs
    first, while `sid_to_room` and `sid_to_alias` still hold this sid --
    afterwards there is no room code left to look the participant up by.
    """
    try:
        await on_participant_disconnected(sid)
        await socket_manager.disconnect(sid)
    finally:
        socket_disconnect(sid)
