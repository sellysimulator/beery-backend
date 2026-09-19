"""``verify_firebase_id_token`` -- the one verification path (02 §2, §3.2).

Covers acceptance criteria 7 and 8.

``None`` means **reject**.  An unconfigured Firebase is deliberately
indistinguishable from an invalid token, because failing open would reinstate
the impersonation bug the three-identifier model exists to prevent
(``00-conventions.md §2``).
"""

from __future__ import annotations

import pytest

from app.api.deps import get_optional_firebase_user, verify_firebase_id_token

# --- the configured, working path -------------------------------------------


def test_a_registered_token_returns_its_decoded_claims(firebase_tokens):
    firebase_tokens.configure()
    firebase_tokens.add("good-token", uid="firebase-uid-123", email="a@example.com")

    claims = verify_firebase_id_token("good-token")

    assert claims is not None
    assert claims["uid"] == "firebase-uid-123"


@pytest.mark.parametrize(
    "token",
    [
        "forged-token",
        "expired-token",
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJub3BlIn0.not-a-real-signature",
    ],
    ids=["forged", "expired", "well-formed-jwt-shape"],
)
def test_an_unverifiable_token_returns_none(firebase_tokens, token):
    """Anything the SDK raises on -- malformed, expired, wrong audience,
    revoked -- collapses to ``None``.  §3.2 says "raises anything -> None",
    and the fake raises the SDK's own ``InvalidIdTokenError``, so the
    ``except`` clause under test is the one that runs in production."""
    firebase_tokens.configure()
    firebase_tokens.add("good-token", uid="firebase-uid-123")

    assert verify_firebase_id_token(token) is None


# --- AC 7 --------------------------------------------------------------------


@pytest.mark.parametrize("token", ["", None], ids=["empty-string", "none"])
def test_a_falsy_token_is_rejected_without_calling_firebase(firebase_tokens, token):
    """AC 7: ``""`` and ``None`` return ``None`` without calling Firebase.

    Firebase *is* configured here, and the shared fake records every call it
    receives, so "did not call Firebase" is an observation rather than an
    assumption.  A round trip to Google for an obviously empty token is a
    network call per unauthenticated connection attempt.
    """
    firebase_tokens.configure()

    assert verify_firebase_id_token(token) is None
    assert firebase_tokens.verify_calls == []


# --- AC 8 --------------------------------------------------------------------


def test_unconfigured_firebase_rejects_every_input(firebase_tokens):
    """AC 8: with Firebase unconfigured, **every** input returns ``None`` --
    including a token the SDK would have verified.

    ``firebase_tokens`` starts unconfigured and is never told otherwise here,
    while the registered token *would* verify, so a ``None`` can only have come
    from the unconfigured check.  This is the "fail closed" half of §3.2: the
    alternative -- trusting the raw value when there is nothing to verify it
    against -- is unauthenticated impersonation of any uid a caller names.
    """
    firebase_tokens.add("would-verify", uid="firebase-uid-123")

    assert verify_firebase_id_token("would-verify") is None
    assert verify_firebase_id_token("anything-else") is None
    assert verify_firebase_id_token("") is None


# --- the guest-tolerant dependency (§2) --------------------------------------


def test_optional_user_is_none_for_a_bad_token_and_claims_for_a_good_one(
    firebase_tokens,
):
    """§2: ``get_optional_firebase_user`` "returns None instead of raising, for
    routes open to guests" -- a guest reaching such a route must not be turned
    away by a 401."""
    firebase_tokens.configure()
    firebase_tokens.add("good-token", uid="firebase-uid-123")

    assert get_optional_firebase_user("") is None
    assert get_optional_firebase_user("Bearer forged-token") is None

    claims = get_optional_firebase_user("Bearer good-token")
    assert claims is not None
    assert claims["uid"] == "firebase-uid-123"
