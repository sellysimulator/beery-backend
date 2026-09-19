"""``/api/v1/users/*`` -- the uid comes from the token, never from the caller.

Covers acceptance criteria 9-13, 16 (route half), 17 (REST half), 18 and 19,
and failure mode 4 (``02-identity-and-auth.md §5``, ``§6``).
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.deps import get_current_firebase_user
from app.models.user import User
from app.services.user_service import UserService

UPSERT = "/api/v1/users/upsert"
ME = "/api/v1/users/me"


def _users(db_session) -> list[User]:
    return list(db_session.scalars(select(User)).all())


# --- AC 9 / §3.5b ------------------------------------------------------------


def test_upsert_without_an_authorization_header_is_401(api_client, firebase_tokens):
    """AC 9: no header, no profile."""
    firebase_tokens.configure()

    response = api_client.post(UPSERT, json={"display_name": "Nobody"})

    assert response.status_code == 401


def test_me_without_an_authorization_header_is_401(api_client, firebase_tokens):
    firebase_tokens.configure()

    response = api_client.get(ME)

    assert response.status_code == 401


def test_upsert_with_an_unverifiable_token_is_401(api_client, firebase_tokens, bearer):
    """A token that does not verify is exactly as good as no token at all."""
    firebase_tokens.configure()
    firebase_tokens.add("good-token", uid="uid-caller")

    response = api_client.post(
        UPSERT, json={"display_name": "Nobody"}, headers=bearer("forged-token")
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "header",
    [{}, {"Authorization": ""}, {"Authorization": "Basic dXNlcjpwYXNz"}],
    ids=["absent", "empty", "not-bearer"],
)
def test_a_missing_or_non_bearer_header_is_401_even_unconfigured(
    api_client, firebase_tokens, header
):
    """§3.5b: the **header is checked first**, so a missing or non-``Bearer``
    header is always 401 -- Firebase is left unconfigured here, and the answer
    is still 401 rather than 503.

    An unauthenticated caller never learns whether the server has Firebase
    configured, and the operator's 503 signal stays specific to a token that
    was actually presented.
    """
    response = api_client.post(UPSERT, json={"display_name": "Nobody"}, headers=header)

    assert response.status_code == 401


# --- AC 10 / failure mode 4 --------------------------------------------------


def test_upsert_takes_the_uid_from_the_token_not_the_body(
    api_client, db_session, firebase_tokens, bearer
):
    """AC 10: the row's ``firebase_uid`` is the **verified token's** uid, even
    though the body offers a perfectly uid-shaped alternative.

    This is the only place a Firebase uid enters the database, so if the body
    can steer it, any signed-in user can overwrite any other user's profile by
    naming their uid -- and every statistic and match-history row that section
    14 attributes by uid follows it.
    """
    firebase_tokens.configure()
    firebase_tokens.add("tok-caller", uid="uid-caller", email="caller@example.com")

    response = api_client.post(
        UPSERT,
        json={
            "display_name": "Caller",
            "email": "caller@example.com",
            "photo_url": "https://example.com/caller.png",
            "firebase_uid": "aZ3kQ9pLmN2bV7cX1yT4uR6wE0sD",
            "uid": "aZ3kQ9pLmN2bV7cX1yT4uR6wE0sD",
        },
        headers=bearer("tok-caller"),
    )

    assert response.status_code == 200

    service = UserService()
    assert (
        service.get_by_firebase_uid(db_session, "aZ3kQ9pLmN2bV7cX1yT4uR6wE0sD") is None
    )

    stored = service.get_by_firebase_uid(db_session, "uid-caller")
    assert stored is not None
    assert response.json()["id"] == stored.id

    rows = _users(db_session)
    assert len(rows) == 1
    assert rows[0].firebase_uid == "uid-caller"


def test_upsert_ignores_a_firebase_uid_in_the_body(
    api_client, db_session, firebase_tokens, bearer
):
    """Failure mode 4: ``{"firebase_uid": "OTHER"}`` writes the caller's uid.

    ``UserUpsertRequest`` carries only ``display_name``, ``email`` and
    ``photo_url`` (§2); the field simply must not be reachable.
    """
    firebase_tokens.configure()
    firebase_tokens.add("tok-caller", uid="uid-caller")

    response = api_client.post(
        UPSERT,
        json={"firebase_uid": "OTHER", "display_name": "Caller"},
        headers=bearer("tok-caller"),
    )

    assert response.status_code == 200

    service = UserService()
    assert service.get_by_firebase_uid(db_session, "OTHER") is None
    assert service.get_by_firebase_uid(db_session, "uid-caller") is not None
    assert [row.firebase_uid for row in _users(db_session)] == ["uid-caller"]


# --- AC 11 / AC 18 -----------------------------------------------------------


def test_upsert_twice_updates_one_row_and_answers_200_each_time(
    api_client, db_session, firebase_tokens, bearer
):
    """AC 11: the second call is an update, not a second row.
    AC 18: **200** on the create call and on the refresh call alike.

    Sockets reconnect and React re-mounts under StrictMode, so this route is
    called far more often than a user signs in (``00-conventions.md §3``,
    idempotency).  A duplicate row would make ``get_by_firebase_uid``
    ambiguous for every later section, and a 201/200 split would invite the
    client to branch on a distinction §2 says carries no information.
    """
    firebase_tokens.configure()
    firebase_tokens.add("tok-caller", uid="uid-caller")

    first = api_client.post(
        UPSERT,
        json={"display_name": "First", "email": "first@example.com"},
        headers=bearer("tok-caller"),
    )
    assert first.status_code == 200

    second = api_client.post(
        UPSERT,
        json={"display_name": "Second", "email": "second@example.com"},
        headers=bearer("tok-caller"),
    )
    assert second.status_code == 200

    rows = _users(db_session)
    assert len(rows) == 1
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["display_name"] == "Second"
    assert second.json()["email"] == "second@example.com"


# --- AC 12 -------------------------------------------------------------------


def test_me_returns_the_callers_own_row(api_client, firebase_tokens, bearer):
    """AC 12: each caller sees their own profile and nobody else's.

    §2: "There is deliberately no route that accepts a uid as a parameter" --
    so the only thing that can select a row is the verified token.
    """
    firebase_tokens.configure()
    firebase_tokens.add("tok-a", uid="uid-a")
    firebase_tokens.add("tok-b", uid="uid-b")

    api_client.post(
        UPSERT,
        json={"display_name": "Ana", "email": "ana@example.com"},
        headers=bearer("tok-a"),
    )
    api_client.post(
        UPSERT,
        json={"display_name": "Bo", "email": "bo@example.com"},
        headers=bearer("tok-b"),
    )

    me_a = api_client.get(ME, headers=bearer("tok-a"))
    me_b = api_client.get(ME, headers=bearer("tok-b"))

    assert me_a.status_code == 200
    assert me_b.status_code == 200
    assert me_a.json()["display_name"] == "Ana"
    assert me_a.json()["email"] == "ana@example.com"
    assert me_b.json()["display_name"] == "Bo"
    assert me_a.json()["id"] != me_b.json()["id"]


def test_me_is_404_before_the_caller_has_ever_upserted(
    api_client, firebase_tokens, bearer
):
    """§2: "404 if never upserted" -- a verified token is not a profile."""
    firebase_tokens.configure()
    firebase_tokens.add("tok-caller", uid="uid-caller")

    response = api_client.get(ME, headers=bearer("tok-caller"))

    assert response.status_code == 404


def test_user_responses_never_carry_the_firebase_uid(
    api_client, firebase_tokens, bearer
):
    """``UserResponse`` is ``id``, ``display_name``, ``email``, ``photo_url``
    (§2).  The uid is the server-only identity of ``00-conventions.md §2`` and
    does not go on the wire."""
    firebase_tokens.configure()
    firebase_tokens.add("tok-caller", uid="uid-caller")

    upserted = api_client.post(
        UPSERT, json={"display_name": "Caller"}, headers=bearer("tok-caller")
    )
    me = api_client.get(ME, headers=bearer("tok-caller"))

    for payload in (upserted.json(), me.json()):
        assert "firebase_uid" not in payload
        assert "uid-caller" not in payload.values()


# --- AC 13 -------------------------------------------------------------------


def test_users_routes_answer_503_when_firebase_is_unconfigured(
    api_client, firebase_tokens, bearer
):
    """AC 13: unconfigured Firebase is 503, and 503 is not 401.

    ``firebase_tokens`` is left unconfigured while holding a token that would
    otherwise verify.  The two codes are different operational problems: 401
    tells the user to sign in again, 503 tells the operator that
    ``FIREBASE_SERVICE_ACCOUNT_JSON`` is missing -- the same thing §3.7's
    CRITICAL log says to whoever reads the boot output.
    """
    firebase_tokens.add("would-verify", uid="uid-caller")

    upserted = api_client.post(
        UPSERT, json={"display_name": "Caller"}, headers=bearer("would-verify")
    )
    me = api_client.get(ME, headers=bearer("would-verify"))

    assert upserted.status_code == 503
    assert me.status_code == 503
    assert upserted.status_code != 401
    assert me.status_code != 401


# --- AC 17 (REST half) -------------------------------------------------------


@pytest.mark.parametrize(
    "uid",
    ["", None, 0, False, 12345, ["uid-caller"]],
    ids=["empty", "none", "zero", "false", "int", "list"],
)
def test_verified_claims_without_a_usable_uid_are_401(firebase_tokens, uid):
    """AC 17, REST half: ``get_current_firebase_user`` raises **401**.

    §3.5b: 401, not 503 -- the token was presented and Firebase answered, so
    nothing is wrong with the server's configuration.  And not a 500 either:
    without this check ``claims["uid"]`` raises ``KeyError`` on an absent uid,
    or writes ``""`` as a shared identity on an empty one.
    """
    firebase_tokens.configure()
    firebase_tokens.add("bad-uid-token", uid=uid, email="a@example.com")

    with pytest.raises(HTTPException) as raised:
        get_current_firebase_user("Bearer bad-uid-token")

    assert raised.value.status_code == 401


def test_a_token_with_no_uid_claim_at_all_is_401_through_the_route(
    api_client, db_session, firebase_tokens, bearer
):
    """AC 17 end to end: an absent ``uid`` reaches the client as 401, and no
    row is written for it."""
    firebase_tokens.configure()
    firebase_tokens.add("no-uid-token", email="a@example.com")

    response = api_client.post(
        UPSERT, json={"display_name": "Caller"}, headers=bearer("no-uid-token")
    )

    assert response.status_code == 401
    assert _users(db_session) == []


# --- AC 16 (route half) ------------------------------------------------------


def test_a_long_display_name_is_stored_truncated(
    api_client, db_session, firebase_tokens, bearer
):
    """AC 16 through the route: 200 characters in, 24 stored.

    ``display_name`` is ``VARCHAR(24)``, so an untruncated value is a write
    error on MySQL -- silently truncated on SQLite, which is exactly why the
    assertion is on the stored length rather than on the write succeeding.
    The truncation itself is asserted against ``UserService.upsert_user`` in
    ``test_user_service.py``, which is where §3.6 puts it.
    """
    from app.config import settings

    firebase_tokens.configure()
    firebase_tokens.add("tok-caller", uid="uid-caller")

    response = api_client.post(
        UPSERT, json={"display_name": "a" * 200}, headers=bearer("tok-caller")
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "a" * 24
    assert settings.MAX_DISPLAY_NAME_LENGTH == 24

    stored = UserService().get_by_firebase_uid(db_session, "uid-caller")
    assert stored is not None
    assert stored.display_name is not None
    assert len(stored.display_name) == 24


# --- AC 19 -------------------------------------------------------------------


def test_an_empty_body_field_is_stored_empty_not_taken_from_the_claim(
    api_client, db_session, firebase_tokens, bearer
):
    """AC 19: the claim fallback must **not** fire on ``""``.

    A truthiness test (``body or claim``) fires on the empty string and
    overwrites a deliberately-blanked name with the Google one -- so a user who
    clears their display name watches it come back on the next sign-in, and
    §3.6's own rule that a name sanitising to ``""`` is stored as ``""`` is
    contradicted.  The fallback condition is "absent or ``None``", not
    "falsy".
    """
    firebase_tokens.configure()
    firebase_tokens.add(
        "tok-caller",
        uid="uid-caller",
        name="Google Name",
        email="google@example.com",
        picture="https://example.com/google.png",
    )

    response = api_client.post(
        UPSERT, json={"display_name": ""}, headers=bearer("tok-caller")
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == ""

    stored = UserService().get_by_firebase_uid(db_session, "uid-caller")
    assert stored is not None
    assert stored.display_name == ""


def test_an_absent_body_field_falls_back_to_the_verified_claim(
    api_client, db_session, firebase_tokens, bearer
):
    """AC 19, the other half: a body of ``{}`` takes the claim.

    §3.6 -- the claim is verified data, and the fallback is what gives a
    Google user a profile without the client having to echo one back.
    """
    firebase_tokens.configure()
    firebase_tokens.add(
        "tok-caller",
        uid="uid-caller",
        name="Google Name",
        email="google@example.com",
        picture="https://example.com/google.png",
    )

    response = api_client.post(UPSERT, json={}, headers=bearer("tok-caller"))

    assert response.status_code == 200
    assert response.json()["display_name"] == "Google Name"
    assert response.json()["email"] == "google@example.com"
    assert response.json()["photo_url"] == "https://example.com/google.png"

    stored = UserService().get_by_firebase_uid(db_session, "uid-caller")
    assert stored is not None
    assert stored.display_name == "Google Name"


def test_an_explicit_null_body_field_also_falls_back_to_the_claim(
    api_client, firebase_tokens, bearer
):
    """§3.6: "absent **or** ``None``" -- the client that sends an explicit
    ``null`` is saying the same thing as the client that omits the key."""
    firebase_tokens.configure()
    firebase_tokens.add("tok-caller", uid="uid-caller", name="Google Name")

    response = api_client.post(
        UPSERT, json={"display_name": None}, headers=bearer("tok-caller")
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "Google Name"


def test_a_non_string_claim_is_ignored_rather_than_stored(
    api_client, firebase_tokens, bearer
):
    """§3.6: "A claim that is not a string is ignored."

    The claims are verified, but verified does not mean well-typed -- and a
    number or an object reaching ``display_name`` (``VARCHAR(24)``) or
    ``photo_url`` (rendered into an ``<img src>``) is a write error or a
    broken page rather than a profile.
    """
    firebase_tokens.configure()
    firebase_tokens.add(
        "tok-caller", uid="uid-caller", name=12345, email={"a": 1}, picture=["u"]
    )

    response = api_client.post(UPSERT, json={}, headers=bearer("tok-caller"))

    assert response.status_code == 200
    assert response.json()["display_name"] is None
    assert response.json()["email"] is None
    assert response.json()["photo_url"] is None
