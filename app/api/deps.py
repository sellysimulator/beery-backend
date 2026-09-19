"""Shared FastAPI dependencies.

This module holds the **one** Firebase verification helper, reused by the REST
layer and by the Socket.IO handshake. There must not be a second verification
path: two of them drift, and the one that drifts is the one that fails open.
"""

import logging

from fastapi import Header, HTTPException, status
from firebase_admin import auth as firebase_auth

from ..core.firebase import init_firebase

logger = logging.getLogger(__name__)

_BEARER_PREFIX = "Bearer "

_MISSING_TOKEN_DETAIL = "Missing bearer token."
_INVALID_TOKEN_DETAIL = "Invalid or expired token."
_UNCONFIGURED_DETAIL = "Firebase authentication is not configured on this server."


def verify_firebase_id_token(token: str) -> dict | None:
    """Verify a Firebase ID token and return its decoded claims.

    Returns the claims on success, or None when Firebase is unconfigured on this
    server OR verification fails for any reason — expired, malformed, wrong
    audience, revoked.

    Callers MUST treat None as "reject". Never fall back to trusting the raw
    value: an unconfigured server is deliberately indistinguishable from an
    invalid token, because failing open reinstates the impersonation bug the
    three-identifier model exists to prevent.
    """
    if not token:
        return None

    app = init_firebase()
    if app is None:
        return None

    try:
        claims = firebase_auth.verify_id_token(token, app=app)
    except Exception:  # noqa: BLE001 — any failure whatsoever means reject
        return None
    return claims


def _bearer_token(authorization: str) -> str:
    """Extract the token from an `Authorization: Bearer <token>` header."""
    if not authorization or not authorization.startswith(_BEARER_PREFIX):
        return ""
    return authorization.removeprefix(_BEARER_PREFIX).strip()


def _verified_claims(authorization: str) -> dict | None:
    """Claims for a bearer header, or None if it is absent or does not verify.

    A decoded value whose `uid` is missing, empty or not a string is rejected
    too. The test is on the **value**, not on the key: `"uid" not in claims`
    would accept `""`, and every connection presenting such a token would then
    share one identity. It is also what makes the reject path fire for an empty
    claims dict, which a bare truthiness test on the decoded value lets through.
    """
    token = _bearer_token(authorization)
    if not token:
        return None
    claims = verify_firebase_id_token(token)
    if not claims:
        return None
    uid = claims.get("uid")
    if not uid or not isinstance(uid, str):
        return None
    return claims


def get_current_firebase_user(authorization: str = Header(default="")) -> dict:
    """Require a verified Firebase ID token and return its decoded claims.

    Raises 401 when the header is missing or the token does not verify, and 503
    when Firebase is unconfigured — the two are deliberately distinguishable to a
    caller, so a deployment error does not look like a user's expired session.
    """
    if not _bearer_token(authorization):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_MISSING_TOKEN_DETAIL
        )

    claims = _verified_claims(authorization)
    if claims is None:
        if init_firebase() is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=_UNCONFIGURED_DETAIL,
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_INVALID_TOKEN_DETAIL
        )
    return claims


def get_optional_firebase_user(
    authorization: str = Header(default=""),
) -> dict | None:
    """As `get_current_firebase_user`, but returns None instead of raising.

    For routes that are open to guests. The caller still gets None for an invalid
    token, so an unverifiable token never becomes an identity.
    """
    return _verified_claims(authorization)
