"""The socket handshake -- identity is established once, by the server.

Covers acceptance criteria 1-6, 15 and 17 (socket half), and failure modes 1,
2, 3, 5, 6 and 7 (``02-identity-and-auth.md §5``, ``§6``).

``identity`` is the server-only third identifier of ``00-conventions.md §2``:
never on the wire, established at the handshake, and never afterwards accepted
from the client.  Conflating it with the public ``alias`` or with the secret
``session_token`` produced three separate critical findings in the Tequila
Game -- impersonation, account-state forgery and UID broadcast -- which is why
this file is as suspicious as it is.

``firebase_tokens`` is section 01's shared fake, unconfigured until a test says
``configure()``; ``verify_firebase_id_token`` and ``resolve_identity`` run
un-mocked on top of it.
"""

from __future__ import annotations

import re
import uuid

import pytest

from app.api.deps import verify_firebase_id_token
from app.sockets.handlers.connection import (
    GUEST_ID_RE,
    connect,
    disconnect,
    resolve_identity,
)

# The shape §3.3 specifies, written out independently of the implementation's
# own constant so that a loosened regex is caught rather than followed.
GUEST_RE = re.compile(r"^guest_[0-9a-f-]{36}$")

# What a real Firebase uid looks like: 28 mixed-case alphanumerics.  It can
# never satisfy GUEST_RE, which is the property that stops a guest claiming a
# Firebase identity.
FIREBASE_UID_SHAPED = "aZ3kQ9pLmN2bV7cX1yT4uR6wE0sD"


# --- AC 1 --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_id_token_becomes_the_identity(sock_mgr, firebase_tokens):
    """AC 1: a verified token's ``uid`` is the identity for that sid."""
    firebase_tokens.configure()
    firebase_tokens.add("good-token", uid="firebase-uid-123", email="a@example.com")

    result = await connect("sid-1", {}, {"idToken": "good-token"})

    assert result is not False
    assert sock_mgr.sid_to_identity["sid-1"] == "firebase-uid-123"


# --- AC 2 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    ["forged-token", "expired-token"],
    ids=["invalid", "expired"],
)
@pytest.mark.asyncio
async def test_unverifiable_id_token_is_rejected_and_stores_nothing(
    sock_mgr, firebase_tokens, token
):
    """AC 2: the handshake returns ``False`` and writes no identity.

    Returning anything other than ``False`` from a Socket.IO ``connect``
    handler *accepts* the connection, so "reject" has exactly one spelling.
    """
    firebase_tokens.configure()
    firebase_tokens.add("good-token", uid="firebase-uid-123")

    result = await connect("sid-1", {}, {"idToken": token})

    assert result is False
    assert "sid-1" not in sock_mgr.sid_to_identity


# --- AC 3 --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_well_formed_guest_id_is_accepted_verbatim(sock_mgr, firebase_tokens):
    """AC 3: a ``guest_<uuid4>`` is taken exactly as presented.

    That stability across reloads is what makes guest reconnection work
    (§3.5); it carries no authority of any kind.
    """
    guest_id = f"guest_{uuid.uuid4()}"

    result = await connect("sid-1", {}, {"guestId": guest_id})

    assert result is not False
    assert sock_mgr.sid_to_identity["sid-1"] == guest_id


# --- AC 4 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "supplied",
    [
        "not-a-real-guest-id",
        "guest_short",
        "guest_" + "g" * 36,  # right length, characters outside [0-9a-f-]
        "GUEST_" + str(uuid.uuid4()),  # wrong case on the prefix
        f"prefix_guest_{uuid.uuid4()}",  # unanchored match attempt
        f"guest_{uuid.uuid4()}_suffix",
        "",
    ],
    ids=[
        "arbitrary-string",
        "too-short",
        "bad-characters",
        "uppercase-prefix",
        "leading-junk",
        "trailing-junk",
        "empty",
    ],
)
@pytest.mark.asyncio
async def test_malformed_guest_id_is_replaced_by_a_fresh_one(
    sock_mgr, firebase_tokens, supplied
):
    """AC 4: a malformed ``guestId`` yields a fresh ``guest_<uuid4>``.

    The supplied value is never honoured -- otherwise ``guestId`` would be a
    free-text identity field, which is the client-asserted identity bug in a
    different hat.
    """
    result = await connect("sid-1", {}, {"guestId": supplied})

    assert result is not False
    identity = sock_mgr.sid_to_identity["sid-1"]
    assert identity != supplied
    assert GUEST_RE.match(identity)


@pytest.mark.parametrize(
    "supplied",
    [12345, 3.5, True, None, ["guest"], {"guest": "id"}],
    ids=["int", "float", "bool", "none", "list", "dict"],
)
@pytest.mark.asyncio
async def test_a_non_string_guest_id_is_replaced_rather_than_crashing(
    sock_mgr, firebase_tokens, supplied
):
    """§3.3: "a non-string ``guestId`` is replaced rather than crashing".

    ``GUEST_ID_RE.match(12345)`` raises ``TypeError``, and the truth table is
    exhaustive over client-supplied payloads -- a client can put anything in a
    handshake, and an unhandled exception there is a crash any visitor can
    trigger.
    """
    result = await connect("sid-1", {}, {"guestId": supplied})

    assert result is not False
    assert GUEST_RE.match(sock_mgr.sid_to_identity["sid-1"])


# --- AC 5 --------------------------------------------------------------------


@pytest.mark.parametrize(
    "auth",
    [None, {}, "not-a-dict", ["also", "not", "a", "dict"], 7],
    ids=["none", "empty-dict", "string", "list", "int"],
)
@pytest.mark.asyncio
async def test_absent_or_unusable_auth_yields_a_fresh_guest(
    sock_mgr, firebase_tokens, auth
):
    """AC 5: no ``auth`` at all is a guest, not a rejection.

    Guest play is a first-class path (**D2**, **D3**), so the handshake must
    mint an identity rather than refuse the connection.
    """
    result = await connect("sid-1", {}, auth)

    assert result is not False
    assert GUEST_RE.match(sock_mgr.sid_to_identity["sid-1"])


# --- AC 6 --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_anonymous_handshakes_get_different_identities(
    sock_mgr, firebase_tokens
):
    """AC 6: fresh guest ids are distinct.

    A shared or constant guest id would put every anonymous visitor into one
    identity, so two browsers would fight over a single seat per room.
    """
    await connect("sid-1", {}, None)
    await connect("sid-2", {}, None)

    assert sock_mgr.sid_to_identity["sid-1"] != sock_mgr.sid_to_identity["sid-2"]


# --- the rest of the §3.3 truth table ---------------------------------------


@pytest.mark.asyncio
async def test_a_verified_token_wins_over_a_supplied_guest_id(
    sock_mgr, firebase_tokens
):
    """§3.3: both present and the token verifies -> the token's uid."""
    guest_id = f"guest_{uuid.uuid4()}"
    firebase_tokens.configure()
    firebase_tokens.add("good-token", uid="firebase-uid-123")

    await connect("sid-1", {}, {"idToken": "good-token", "guestId": guest_id})

    assert sock_mgr.sid_to_identity["sid-1"] == "firebase-uid-123"


def test_resolve_identity_matches_the_truth_table(firebase_tokens):
    """The pure helper, exercised directly: §3.3 row by row."""
    firebase_tokens.configure()
    firebase_tokens.add("good-token", uid="firebase-uid-123")
    guest_id = f"guest_{uuid.uuid4()}"

    assert resolve_identity({"idToken": "good-token"}) == "firebase-uid-123"
    assert resolve_identity({"idToken": "forged"}) is None
    assert resolve_identity({"guestId": guest_id}) == guest_id
    assert GUEST_RE.match(resolve_identity({"guestId": "nope"}) or "")
    assert GUEST_RE.match(resolve_identity({"guestId": 12345}) or "")
    assert GUEST_RE.match(resolve_identity({}) or "")
    assert GUEST_RE.match(resolve_identity(None) or "")
    assert (
        resolve_identity({"idToken": "good-token", "guestId": guest_id})
        == "firebase-uid-123"
    )
    assert resolve_identity({"idToken": "forged", "guestId": guest_id}) is None


def test_the_guest_id_pattern_cannot_match_a_firebase_uid():
    """§3.3: "a Firebase uid can never satisfy" ``GUEST_ID_RE``.

    The frozen public constant is asserted directly, because everything else
    in this file rests on it.
    """
    assert GUEST_ID_RE.match(f"guest_{uuid.uuid4()}")
    assert not GUEST_ID_RE.match(FIREBASE_UID_SHAPED)
    assert not GUEST_ID_RE.match(f"guest_{FIREBASE_UID_SHAPED}")


# --- AC 15 / failure mode 6 --------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_clears_all_three_sid_maps(sock_mgr, firebase_tokens):
    """AC 15: ``disconnect`` removes the sid from all three maps (§3.4).

    ``sid_to_room`` and ``sid_to_alias`` are filled by section 11's ``join``,
    which does not exist yet; both are declared public attributes of the
    section 01 manager, so the test puts in the two entries ``disconnect`` is
    required to take out.  A map that keeps growing is a leak in a
    long-running process, and a stale ``sid_to_identity`` entry is worse than
    a leak: a recycled sid would inherit somebody else's identity.
    """
    firebase_tokens.configure()
    firebase_tokens.add("good-token", uid="firebase-uid-123")
    await connect("sid-1", {}, {"idToken": "good-token"})
    sock_mgr.sid_to_room["sid-1"] = "ROOM42"
    sock_mgr.sid_to_alias["sid-1"] = "P1"

    # A second, unrelated connection that must survive untouched.
    await connect("sid-2", {}, None)
    sock_mgr.sid_to_room["sid-2"] = "ROOM42"
    sock_mgr.sid_to_alias["sid-2"] = "P2"
    survivor = sock_mgr.sid_to_identity["sid-2"]

    await disconnect("sid-1")

    assert "sid-1" not in sock_mgr.sid_to_room
    assert "sid-1" not in sock_mgr.sid_to_alias
    assert "sid-1" not in sock_mgr.sid_to_identity

    assert sock_mgr.sid_to_room["sid-2"] == "ROOM42"
    assert sock_mgr.sid_to_alias["sid-2"] == "P2"
    assert sock_mgr.sid_to_identity["sid-2"] == survivor


@pytest.mark.asyncio
async def test_disconnect_does_not_leak_the_identity_map(sock_mgr, firebase_tokens):
    """Failure mode 6: after ``disconnect``, ``sid_to_identity`` has no entry.

    Stated separately from AC 15 because this is the map whose staleness is a
    security problem rather than a memory one.
    """
    firebase_tokens.configure()
    firebase_tokens.add("good-token", uid="firebase-uid-123")
    await connect("sid-1", {}, {"idToken": "good-token"})
    assert "sid-1" in sock_mgr.sid_to_identity

    await disconnect("sid-1")

    assert "sid-1" not in sock_mgr.sid_to_identity
    assert "firebase-uid-123" not in sock_mgr.sid_to_identity.values()


@pytest.mark.asyncio
async def test_disconnect_of_an_unknown_sid_is_harmless(sock_mgr, firebase_tokens):
    """A rejected handshake never reaches ``disconnect`` with state to clear,
    and a client can drop at any point; neither may raise."""
    await disconnect("never-connected")

    assert "never-connected" not in sock_mgr.sid_to_identity


# --- failure mode 1: THE test ------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_token_with_valid_guest_id_is_rejected_not_downgraded(
    sock_mgr, firebase_tokens
):
    """**Failure mode 1 -- the single most important test in the section.**

    ``{"idToken": "<invalid>", "guestId": "<valid>"}`` must be **rejected**.

    The bug this catches is a silent downgrade: code shaped like

        identity = verify(auth.get("idToken")) or guest_from(auth)

    falls through to the guest branch the moment verification fails.  The
    connection is then accepted, and an attacker holding a forged, expired or
    revoked token is quietly admitted as a guest of their own choosing.  It
    looks harmless -- they are "only" a guest -- but it converts the one
    unforgeable credential in the system into an optional field: presenting a
    bad token becomes indistinguishable from presenting none, and the
    handshake stops being the place where identity is decided.  Every later
    section trusts ``sid_to_identity`` precisely because this cannot happen.
    """
    guest_id = f"guest_{uuid.uuid4()}"
    firebase_tokens.configure()
    firebase_tokens.add("real-token", uid="firebase-uid-123")

    auth = {"idToken": "forged-or-expired-token", "guestId": guest_id}

    assert resolve_identity(auth) is None

    result = await connect("sid-1", {}, auth)

    assert result is False
    assert "sid-1" not in sock_mgr.sid_to_identity
    assert guest_id not in sock_mgr.sid_to_identity.values()
    assert sock_mgr.sid_to_identity == {}


# --- failure mode 2 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_client_asserted_identity_key_has_no_effect(sock_mgr, firebase_tokens):
    """Failure mode 2: ``{"identity": "SOMEONE_ELSE_UID"}`` is inert.

    ``identity`` is server-only (``00-conventions.md §2`` rule 2).  There are
    exactly two inputs the handshake reads -- ``idToken`` and ``guestId`` --
    and a payload naming the field directly must not be one of them.
    """
    firebase_tokens.configure()
    firebase_tokens.add("good-token", uid="firebase-uid-123")

    await connect("sid-1", {}, {"identity": "SOMEONE_ELSE_UID"})
    minted = sock_mgr.sid_to_identity["sid-1"]
    assert minted != "SOMEONE_ELSE_UID"
    assert GUEST_RE.match(minted)

    # And it cannot override either of the two fields that *are* read.
    guest_id = f"guest_{uuid.uuid4()}"
    await connect("sid-2", {}, {"identity": "SOMEONE_ELSE_UID", "guestId": guest_id})
    assert sock_mgr.sid_to_identity["sid-2"] == guest_id

    await connect(
        "sid-3", {}, {"identity": "SOMEONE_ELSE_UID", "idToken": "good-token"}
    )
    assert sock_mgr.sid_to_identity["sid-3"] == "firebase-uid-123"

    assert "SOMEONE_ELSE_UID" not in sock_mgr.sid_to_identity.values()


@pytest.mark.asyncio
async def test_neighbouring_identity_fields_are_ignored_too(sock_mgr, firebase_tokens):
    """Failure mode 2, adjacent spellings: ``uid``, ``user_id`` and ``sub`` are
    the names a decoded token uses, and none of them is an input."""
    await connect(
        "sid-1",
        {},
        {
            "uid": "SOMEONE_ELSE_UID",
            "user_id": "SOMEONE_ELSE_UID",
            "sub": "SOMEONE_ELSE_UID",
        },
    )

    assert GUEST_RE.match(sock_mgr.sid_to_identity["sid-1"])
    assert "SOMEONE_ELSE_UID" not in sock_mgr.sid_to_identity.values()


# --- failure mode 3 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_guest_cannot_claim_a_firebase_uid(sock_mgr, firebase_tokens):
    """Failure mode 3: a uid-shaped ``guestId`` fails the regex.

    ``guest_<uuid4>`` and a Firebase uid are disjoint by construction, so a
    guest can never end up holding a string that a signed-in user's results,
    statistics or host claim would be attributed to.
    """
    firebase_tokens.configure()
    firebase_tokens.add("victims-token", uid=FIREBASE_UID_SHAPED)

    await connect("sid-1", {}, {"guestId": FIREBASE_UID_SHAPED})

    identity = sock_mgr.sid_to_identity["sid-1"]
    assert identity != FIREBASE_UID_SHAPED
    assert GUEST_RE.match(identity)

    # The same attempt dressed up with the guest prefix fares no better.
    await connect("sid-2", {}, {"guestId": f"guest_{FIREBASE_UID_SHAPED}"})
    identity_2 = sock_mgr.sid_to_identity["sid-2"]
    assert identity_2 != f"guest_{FIREBASE_UID_SHAPED}"
    assert GUEST_RE.match(identity_2)


# --- failure mode 5 ----------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfigured_firebase_rejects_tokens_but_still_admits_guests(
    sock_mgr, firebase_tokens
):
    """Failure mode 5: no service account is not "trust everyone".

    ``firebase_tokens`` is left unconfigured while holding a token that *would*
    verify.  A missing ``FIREBASE_SERVICE_ACCOUNT_JSON`` degrades the app to
    guest-only play -- which is why the startup check logs at CRITICAL rather
    than aborting (§3.7).  What it must never do is accept an unverifiable
    token, because with nothing to verify against, "trust the raw value" hands
    out any uid on request.
    """
    firebase_tokens.add("would-verify-if-configured", uid="firebase-uid-123")

    rejected = await connect("sid-1", {}, {"idToken": "would-verify-if-configured"})
    assert rejected is False
    assert "sid-1" not in sock_mgr.sid_to_identity

    guest_id = f"guest_{uuid.uuid4()}"
    accepted = await connect("sid-2", {}, {"guestId": guest_id})
    assert accepted is not False
    assert sock_mgr.sid_to_identity["sid-2"] == guest_id

    anonymous = await connect("sid-3", {}, None)
    assert anonymous is not False
    assert GUEST_RE.match(sock_mgr.sid_to_identity["sid-3"])


# --- AC 17 (socket half) / failure mode 7 ------------------------------------


@pytest.mark.asyncio
async def test_decoded_claims_without_a_uid_are_rejected(sock_mgr, firebase_tokens):
    """Failure mode 7: ``None`` is not the same as falsy.

    A token that verifies to ``{}`` is a *successful* decode, so
    ``verify_firebase_id_token`` returns ``{}`` and not ``None`` -- the two
    are different answers and §3.2 says so.  A caller written as
    ``if decoded:`` cannot tell them apart; one written as
    ``if decoded is not None:`` can, and must then find that there is no
    ``uid`` to use.  Either way the handshake rejects (§3.3, row 3).
    """
    firebase_tokens.configure()
    firebase_tokens.add("verifies-but-empty")

    assert verify_firebase_id_token("verifies-but-empty") == {}

    assert resolve_identity({"idToken": "verifies-but-empty"}) is None

    result = await connect("sid-1", {}, {"idToken": "verifies-but-empty"})
    assert result is False
    assert "sid-1" not in sock_mgr.sid_to_identity


@pytest.mark.parametrize(
    "uid",
    ["", None, 0, False, 12345, ["firebase-uid-123"], {"uid": "firebase-uid-123"}],
    ids=["empty", "none", "zero", "false", "int", "list", "dict"],
)
@pytest.mark.asyncio
async def test_a_falsy_or_non_string_uid_is_rejected(sock_mgr, firebase_tokens, uid):
    """AC 17, socket half: the check is on the **value**, not on the key.

    An implementation testing ``"uid" not in claims`` accepts ``""`` as an
    identity, and every connection presenting such a token would then share
    one identity -- one seat, one set of results, one host claim, for
    everybody.  A non-string uid is no better: it would be written into
    ``sid_to_identity`` and compared against stored strings for the rest of
    the room's life.
    """
    firebase_tokens.configure()
    firebase_tokens.add("bad-uid-token", uid=uid, email="a@example.com")

    assert resolve_identity({"idToken": "bad-uid-token"}) is None

    result = await connect("sid-1", {}, {"idToken": "bad-uid-token"})
    assert result is False
    assert "sid-1" not in sock_mgr.sid_to_identity
    assert sock_mgr.sid_to_identity == {}


@pytest.mark.asyncio
async def test_a_falsy_uid_does_not_downgrade_to_a_supplied_guest_id(
    sock_mgr, firebase_tokens
):
    """AC 17 meets failure mode 1: a token that verifies to a falsy uid is a
    rejection, not a licence to fall back on the ``guestId`` beside it."""
    firebase_tokens.configure()
    firebase_tokens.add("bad-uid-token", uid="")
    guest_id = f"guest_{uuid.uuid4()}"

    result = await connect(
        "sid-1", {}, {"idToken": "bad-uid-token", "guestId": guest_id}
    )

    assert result is False
    assert guest_id not in sock_mgr.sid_to_identity.values()
    assert sock_mgr.sid_to_identity == {}
