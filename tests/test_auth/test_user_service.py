"""``UserService`` -- the only writer of the ``users`` table (02 §2, §3.6).

Covers acceptance criterion 16 at the layer §3.6 puts it, and supports 11 and
12.  Guests are never written here (§3.6): they exist only in Redis room state.

Sanitisation lives in ``upsert_user`` rather than in the route so that **every**
caller gets it -- including section 14's guest-claim path, which reaches the
service without an HTTP request.  So these tests call the service directly, with
no client and no token in sight.
"""

from __future__ import annotations

import unicodedata

from sqlalchemy import select

from app.config import settings
from app.models.user import User
from app.services.user_service import UserService

# VARCHAR widths declared in §2; §3.6 requires a truncated row rather than a
# driver error when a caller sends more.
EMAIL_WIDTH = 200
PHOTO_URL_WIDTH = 500


def _users(db_session) -> list[User]:
    return list(db_session.scalars(select(User)).all())


def _control_characters(value: str) -> list[str]:
    return [c for c in value if unicodedata.category(c) == "Cc"]


# --- the upsert itself -------------------------------------------------------


def test_upsert_inserts_then_updates_the_same_row(db_session):
    """§3.6: select by ``firebase_uid``, then update or insert."""
    service = UserService()

    created = service.upsert_user(
        db_session,
        "uid-caller",
        "First",
        "first@example.com",
        "https://example.com/first.png",
    )
    db_session.flush()
    created_id = created.id

    assert created_id is not None
    assert created.firebase_uid == "uid-caller"

    updated = service.upsert_user(
        db_session,
        "uid-caller",
        "Second",
        "second@example.com",
        "https://example.com/second.png",
    )
    db_session.flush()

    assert updated.id == created_id
    assert updated.display_name == "Second"
    assert updated.email == "second@example.com"
    assert updated.photo_url == "https://example.com/second.png"
    assert len(_users(db_session)) == 1


def test_upsert_keeps_two_uids_apart(db_session):
    service = UserService()

    service.upsert_user(db_session, "uid-a", "Ana", "ana@example.com", None)
    service.upsert_user(db_session, "uid-b", "Bo", "bo@example.com", None)
    db_session.flush()

    assert len(_users(db_session)) == 2
    ana = service.get_by_firebase_uid(db_session, "uid-a")
    bo = service.get_by_firebase_uid(db_session, "uid-b")
    assert ana is not None
    assert bo is not None
    assert ana.id != bo.id
    assert ana.display_name == "Ana"
    assert bo.display_name == "Bo"


def test_upsert_accepts_a_profile_with_nothing_in_it(db_session):
    """Every profile field is optional (§2): a Google account may expose no
    photo, and a user may have no display name at all."""
    service = UserService()

    created = service.upsert_user(db_session, "uid-caller", None, None, None)
    db_session.flush()

    assert created.firebase_uid == "uid-caller"
    assert created.display_name is None
    assert created.email is None
    assert created.photo_url is None


def test_get_by_firebase_uid_returns_none_for_an_unknown_uid(db_session):
    """The "never upserted" case the ``GET /users/me`` 404 rests on."""
    assert UserService().get_by_firebase_uid(db_session, "uid-nobody") is None


# --- AC 16 -------------------------------------------------------------------


def test_the_service_truncates_a_long_display_name(db_session):
    """AC 16: 200 characters in, 24 stored -- asserted against the **service**,
    which is where §3.6 puts the sanitisation.

    Section 14's guest-claim path calls this method with a name that never
    passed through a request body, so a route-level truncation would leave
    ``display_name`` (``VARCHAR(24)``) to be enforced by the driver.
    """
    created = UserService().upsert_user(db_session, "uid-caller", "a" * 200, None, None)
    db_session.flush()

    assert created.display_name == "a" * 24
    assert len(created.display_name) == settings.MAX_DISPLAY_NAME_LENGTH

    stored = UserService().get_by_firebase_uid(db_session, "uid-caller")
    assert stored is not None
    assert stored.display_name == "a" * 24


def test_the_service_truncates_an_over_long_email_and_photo_url(db_session):
    """§3.6: truncate ``email`` and ``photo_url`` to their column widths "so an
    over-long value becomes a truncated row rather than a driver error"."""
    long_email = "b" * 400 + "@example.com"
    long_photo = "https://example.com/" + "c" * 900

    created = UserService().upsert_user(
        db_session, "uid-caller", "Caller", long_email, long_photo
    )
    db_session.flush()

    assert created.email is not None
    assert created.photo_url is not None
    assert len(created.email) == EMAIL_WIDTH
    assert len(created.photo_url) == PHOTO_URL_WIDTH
    assert long_email.startswith(created.email)
    assert long_photo.startswith(created.photo_url)


def test_the_service_strips_control_characters_from_all_three_fields(db_session):
    """§3.6: strip Unicode category ``Cc`` from ``display_name``, ``email`` and
    ``photo_url``.

    Every one of the three is rendered somewhere -- a name in a room broadcast
    and on the results screen, a photo url in an ``<img src>`` -- and NUL, BEL
    and ESC have no business in any of them.
    """
    created = UserService().upsert_user(
        db_session,
        "uid-caller",
        "Bo\x00b\x07by\x1b",
        "ca\x00ller@ex\x1bample.com",
        "https://exa\x00mple.com/\x07p.png",
    )
    db_session.flush()

    assert created.display_name is not None
    assert created.email is not None
    assert created.photo_url is not None
    assert _control_characters(created.display_name) == []
    assert _control_characters(created.email) == []
    assert _control_characters(created.photo_url) == []


def test_a_name_that_sanitises_to_empty_is_stored_empty_not_null(db_session):
    """§3.6: "A name that sanitises to the empty string is stored as ``""``,
    not converted to ``None``."

    ``""`` and ``None`` are different answers -- ``None`` is "this user has no
    name on file", which is what the claim fallback in §3.6 fills in, and
    turning a deliberately-blanked name into ``None`` would hand it straight
    back to the Google one on the next sign-in (AC 19).
    """
    created = UserService().upsert_user(
        db_session, "uid-caller", "\x00\x07\x1b", None, None
    )
    db_session.flush()

    assert created.display_name == ""
    assert created.display_name is not None

    also_empty = UserService().upsert_user(db_session, "uid-other", "", None, None)
    db_session.flush()

    assert also_empty.display_name == ""
