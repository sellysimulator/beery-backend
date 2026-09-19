"""Black-box tests for section 09 -- the room-document shape and the pure,
non-Redis helpers of ``app/services/state_service.py``: ``new_room_document``,
``generate_room_code`` and ``next_free_alias``.

Covers ``09-state-service.md §5`` acceptance criteria 2 and 4, and ``§6``
failure modes 1 and 5.  Criteria and failure modes that need the Redis-backed
``StateService`` (creation, lookup, locking, persistence) live in
``test_state_service.py`` instead.

Only the frozen public surface of section 09 (``§3``) plus the public
surfaces of sections 01, 03, 07 and 08 are imported. No private name and no
internal data structure is asserted on.
"""

from __future__ import annotations

import random

from app.core.config_models import (
    DEFAULT_LIMITS,
    ConstantDemand,
    FactoryConfig,
    GameConfig,
    RoleConfig,
)
from app.core.enums import Role
from app.services.state_service import (
    LOCK_TIMEOUT_SECONDS,
    ROOM_CODE_ALPHABET,
    ROOM_CODE_LENGTH,
    ROOM_TTL_SECONDS,
    SCHEMA_VERSION,
    generate_room_code,
    new_room_document,
    next_free_alias,
)

# The exact top-level key set of the §2 room document.
EXPECTED_TOP_LEVEL_KEYS = {
    "schema_version",
    "room_code",
    "state",
    "created_at",
    "started_at",
    "finished_at",
    "host_secret",
    "host_sid",
    "host_identity",
    "host_display_name",
    "participants",
    "sid_to_alias",
    "role_to_alias",
    "seed",
    "config",
    "engine",
    "bots",
    "seq",
    "paused_reason",
    "persisted",
}


def make_config(duration_weeks: int = 8) -> GameConfig:
    """A minimal, valid ``GameConfig`` built only through section 03's frozen
    surface (``from_host_input``), mirroring the helper pattern used by
    section 07's own tests."""
    roles = {
        Role.RETAILER.value: RoleConfig(),
        Role.WHOLESALER.value: RoleConfig(),
        Role.DISTRIBUTOR.value: RoleConfig(),
        Role.FACTORY.value: FactoryConfig(),
    }
    payload = {
        "roles": roles,
        "demand": ConstantDemand(value=4),
        "duration_weeks": duration_weeks,
    }
    return GameConfig.from_host_input(payload, DEFAULT_LIMITS)


def _role_to_alias_value(role_to_alias: dict, role: Role):
    """Look the role up whether it is keyed by the ``Role`` enum member or
    by its plain string value -- the frozen surface promises only the
    *shape*, not which of the two equal representations is used."""
    if role in role_to_alias:
        return role_to_alias[role]
    return role_to_alias[role.value]


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


def test_module_constants_match_00_decisions_section_5():
    assert SCHEMA_VERSION == 1
    assert ROOM_CODE_LENGTH == 6
    assert ROOM_TTL_SECONDS == 86_400
    assert LOCK_TIMEOUT_SECONDS == 10


def test_room_code_alphabet_excludes_ambiguous_characters():
    # FM 5 -- the alphabet itself must exclude the five confusable
    # characters, and it must be upper-case only.
    for ambiguous in "0O1IL":
        assert ambiguous not in ROOM_CODE_ALPHABET
    assert ROOM_CODE_ALPHABET == ROOM_CODE_ALPHABET.upper()
    assert len(ROOM_CODE_ALPHABET) == len(set(ROOM_CODE_ALPHABET))


# ---------------------------------------------------------------------------
# AC 2 -- new_room_document
# ---------------------------------------------------------------------------


def test_new_room_document_top_level_key_set_matches_schema():
    room = new_room_document("ABC234", make_config(), "Host")
    assert set(room) == EXPECTED_TOP_LEVEL_KEYS


def test_new_room_document_fresh_lobby_defaults():
    room = new_room_document("ABC234", make_config(), "Host")

    assert room["schema_version"] == SCHEMA_VERSION
    assert room["state"] == "LOBBY"
    assert room["seq"] == 0
    assert room["engine"] is None
    assert room["persisted"] is False
    for role in (Role.RETAILER, Role.WHOLESALER, Role.DISTRIBUTOR, Role.FACTORY):
        assert _role_to_alias_value(room["role_to_alias"], role) is None

    # §1's illustrative values are shown populated only to document shape;
    # a fresh room's collections are empty.
    assert room["participants"] == {}
    assert room["sid_to_alias"] == {}
    assert room["bots"] == {}

    assert room["room_code"] == "ABC234"
    assert room["host_display_name"] == "Host"


def test_new_room_document_mints_a_fresh_secret_each_call():
    room_a = new_room_document("ABC234", make_config(), "Host")
    room_b = new_room_document("ABC234", make_config(), "Host")

    assert isinstance(room_a["host_secret"], str)
    assert room_a["host_secret"] != ""
    # secrets.token_urlsafe(32) -- never the same value twice.
    assert room_a["host_secret"] != room_b["host_secret"]


# ---------------------------------------------------------------------------
# AC 4 / FM 5 -- generate_room_code
# ---------------------------------------------------------------------------


def test_generate_room_code_length_and_alphabet_over_many_generations():
    for _ in range(10_000):
        code = generate_room_code()
        assert len(code) == ROOM_CODE_LENGTH
        assert all(character in ROOM_CODE_ALPHABET for character in code)
        assert not any(character in code for character in "0O1IL")


def test_generate_room_code_accepts_an_explicit_random_for_determinism():
    # The docstring's whole point: a caller can force a specific code (and,
    # in a caller that retries on collision, force a collision) by passing
    # its own seeded Random.
    first = generate_room_code(random.Random(20260919))
    second = generate_room_code(random.Random(20260919))

    assert first == second
    assert len(first) == ROOM_CODE_LENGTH
    assert all(character in ROOM_CODE_ALPHABET for character in first)


# ---------------------------------------------------------------------------
# AC 13 -- next_free_alias
# ---------------------------------------------------------------------------


def _room_with_aliases(aliases: set[str]) -> dict:
    return {"participants": {alias: {"alias": alias} for alias in aliases}}


def test_next_free_alias_returns_the_lowest_unused_slot():
    assert next_free_alias(_room_with_aliases({"P1", "P3"})) == "P2"
    assert next_free_alias(_room_with_aliases(set())) == "P1"
    assert next_free_alias(_room_with_aliases({"P1", "P2", "P3", "P4"})) == "P5"


# ---------------------------------------------------------------------------
# FM 1 -- alias reuse after a leave must not clobber a survivor
# ---------------------------------------------------------------------------


def test_fm1_alias_reuse_after_leave_reuses_the_freed_slot_not_the_count():
    room = _room_with_aliases({"P1", "P2", "P3"})
    survivor_record = {"alias": "P3", "display_name": "Cid", "role": None}
    room["participants"]["P3"] = survivor_record

    del room["participants"]["P2"]  # P2 leaves, freeing that slot

    new_alias = next_free_alias(room)

    # A count-based `len(participants) + 1` implementation computes
    # len({"P1", "P3"}) + 1 -> "P3" here, which collides with the survivor
    # and would overwrite their record the moment a caller wrote the new
    # participant in. The correct, lowest-unused-slot answer is "P2".
    assert new_alias == "P2"
    assert room["participants"]["P3"] == survivor_record
