"""Black-box tests for ``POST /rooms/create``, ``GET /rooms/{code}/status``
and ``GET /rooms/presets`` (``10-rooms-rest-api.md``).

Covers ``§4`` acceptance criteria 1-10 and 19-20, and ``§5`` failure modes 1
(the create-response half), 5, 6 and 9. The config-mutation routes (``PUT``/
``GET /config``) and the criteria and failure modes that are only reachable
through them live in ``test_room_config.py``.

Driven entirely through the frozen public surface of this section plus the
already-shipped surfaces of sections 01, 02, 03 and 09 (``app.core.presets``,
``app.services.state_service``), and the shared ``client``/``fake_redis``/
``firebase_tokens`` fixtures from ``tests/conftest.py``. ``app/api/v1/rooms.py``
is opened only by the one structural test that the document requires (failure
mode 2) and only through ``inspect``/``ast``, never for its private names.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from app.core.config_models import DEFAULT_LIMITS, GameConfig
from app.core.presets import get_preset

from .conftest import (
    CREATE,
    PRESETS,
    create_room,
    keys_recursive,
    status_url,
    values_recursive,
)

# --- AC 1, 2 ------------------------------------------------------------------


def test_ac1_create_with_empty_json_body_succeeds(client, fake_redis):
    response = client.post(CREATE, json={})

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["room_code"], str)
    assert len(body["room_code"]) == 6
    assert isinstance(body["host_secret"], str)
    assert body["host_secret"] != ""
    assert body["state"] == "LOBBY"
    assert body["config"]["preset_name"] == "CLASSIC_MIT"
    assert body["config"]["duration_weeks"] == 36


def test_ac1_create_with_no_body_at_all_also_succeeds(client, fake_redis):
    """ "Empty body" also covers a literally empty POST, not only ``{}``."""
    response = client.post(CREATE)

    assert response.status_code == 200
    assert response.json()["state"] == "LOBBY"


def test_ac2_create_needs_no_authorization_header(client, fake_redis):
    response = client.post(CREATE, json={})

    assert response.status_code == 200
    assert "host_secret" in response.json()


# --- AC 3 ----------------------------------------------------------------


def test_ac3_create_succeeds_with_a_valid_and_with_an_invalid_firebase_token(
    client, fake_redis, firebase_tokens
):
    firebase_tokens.configure()
    firebase_tokens.add("good-token", uid="uid-creator")

    valid = client.post(CREATE, json={}, headers={"Authorization": "Bearer good-token"})
    invalid = client.post(
        CREATE, json={}, headers={"Authorization": "Bearer forged-token"}
    )

    assert valid.status_code == 200
    assert invalid.status_code == 200


# --- AC 4 ----------------------------------------------------------------


def test_ac4_a_known_preset_name_returns_that_presets_config(client, fake_redis):
    response = client.post(CREATE, json={"preset": "CHAOS"})

    assert response.status_code == 200
    assert response.json()["config"]["preset_name"] == "CHAOS"
    assert response.json()["config"]["duration_weeks"] == 52


def test_ac4_an_unknown_preset_name_is_400_not_a_silent_fallback(client, fake_redis):
    response = client.post(CREATE, json={"preset": "NOPE"})

    assert response.status_code == 400
    assert isinstance(response.json()["detail"], str)


# --- AC 5 ----------------------------------------------------------------


def test_ac5_two_creates_return_different_codes_and_secrets(client, fake_redis):
    first = client.post(CREATE, json={}).json()
    second = client.post(CREATE, json={}).json()

    assert first["room_code"] != second["room_code"]
    assert first["host_secret"] != second["host_secret"]


# --- AC 6 ----------------------------------------------------------------


def test_ac6_a_200_character_display_name_is_truncated_to_24(client, fake_redis):
    room_code, _secret = create_room(client, host_display_name="a" * 200)

    status = client.get(status_url(room_code)).json()

    assert status["host_display_name"] == "a" * 24


def test_ac6_a_whitespace_only_display_name_becomes_the_fallback(client, fake_redis):
    room_code, _secret = create_room(client, host_display_name="   ")

    status = client.get(status_url(room_code)).json()

    assert status["host_display_name"] == "Host"


def test_ac6_control_characters_are_stripped_not_left_in_place(client, fake_redis):
    """``\\x00`` (a non-whitespace control) is removed outright; ``\\n`` (a
    whitespace control) becomes a space rather than vanishing and joining the
    words either side of it -- ``16-frontend-foundation.md §4`` steps 1 and 5.
    """
    room_code, _secret = create_room(client, host_display_name="Ana\x00\nSmith")

    status = client.get(status_url(room_code)).json()

    assert status["host_display_name"] == "Ana Smith"


# --- AC 7 ----------------------------------------------------------------


def test_ac7_status_on_a_fresh_room(client, fake_redis):
    room_code, _secret = create_room(client)

    response = client.get(status_url(room_code))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["room_code"] == room_code
    assert body["state"] == "LOBBY"
    assert body["seats_total"] == 4
    assert body["seats_taken"] == 0


# --- AC 8 ----------------------------------------------------------------


def test_ac8_status_on_an_unknown_code_is_http_200_with_ok_false(client, fake_redis):
    response = client.get(status_url("ZZZZZZ"))

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["reason"] == "Room does not exist."


# --- AC 9 / FM 1 (status half) -------------------------------------------


def test_ac9_status_never_carries_config_or_any_secret_or_identity_key(
    client, fake_redis
):
    room_code, secret = create_room(client)

    response = client.get(status_url(room_code)).json()

    forbidden_keys = {
        "config",
        "host_secret",
        "participants",
        "identity",
        "session_token",
    }
    assert keys_recursive(response).isdisjoint(forbidden_keys)
    assert secret not in values_recursive(response)


def test_ac9_status_on_an_unknown_room_also_carries_no_forbidden_key(
    client, fake_redis
):
    response = client.get(status_url("ZZZZZZ")).json()

    forbidden_keys = {
        "config",
        "host_secret",
        "participants",
        "identity",
        "session_token",
    }
    assert keys_recursive(response).isdisjoint(forbidden_keys)


# --- AC 10 ----------------------------------------------------------------


def test_ac10_status_lookup_is_case_insensitive(client, fake_redis):
    room_code, _secret = create_room(client)

    response = client.get(status_url(room_code.lower()))

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["room_code"] == room_code


# --- AC 19 ----------------------------------------------------------------


def test_ac19_presets_have_the_three_names_and_survive_from_host_input(
    client, fake_redis
):
    response = client.get(PRESETS)

    assert response.status_code == 200
    presets = response.json()["presets"]
    assert {entry["name"] for entry in presets} == {
        "CLASSIC_MIT",
        "FAST_GAME",
        "CHAOS",
    }

    for entry in presets:
        assert entry["label"]
        assert entry["description"]
        original = get_preset(entry["name"])
        rebuilt = GameConfig.from_host_input(entry["config"], DEFAULT_LIMITS)
        assert rebuilt == original


# --- FM 1 (the rest of it: create's secret must not reach these routes) --


def test_fm1_host_secret_never_appears_outside_the_create_response(client, fake_redis):
    room_code, secret = create_room(client)

    status = client.get(status_url(room_code)).json()
    presets = client.get(PRESETS).json()

    for payload in (status, presets):
        assert secret not in values_recursive(payload)
        assert "host_secret" not in keys_recursive(payload)


# --- FM 5 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm5_room_code_oracle_missing_and_full_are_both_200_and_ok_false(
    client, fake_redis
):
    """A full room and a missing room must be indistinguishable at the HTTP
    layer -- both 200, both ``ok: false``. A 404 for "does not exist" would
    let a client probe for valid room codes.

    Section 11 (join) has not been built yet, so "full" is produced the only
    way available at this layer: writing four participant records directly
    through section 09's own public ``get_room``/``save_room``, exactly as
    that section's own tests build synthetic participants (see
    ``test_room_schema.py``'s ``_room_with_aliases``).
    """
    from app.services.state_service import get_state_service

    room_code, _secret = create_room(client)
    svc = get_state_service()
    room = await svc.get_room(room_code)
    assert room is not None
    room["participants"] = {
        f"P{n}": {
            "alias": f"P{n}",
            "identity": f"guest_{n}",
            "session_token": "tok",
            "display_name": f"P{n}",
            "role": None,
            "is_bot": False,
            "connected": True,
        }
        for n in range(1, 5)
    }
    await svc.save_room(room_code, room)

    full = client.get(status_url(room_code))
    missing = client.get(status_url("ZZZZZZ"))

    assert full.status_code == 200
    assert missing.status_code == 200
    assert full.json()["ok"] is False
    assert missing.json()["ok"] is False
    assert full.json()["reason"] == "Room is full."


# --- FM 6 -------------------------------------------------------------------


def test_fm6_no_unauthenticated_route_can_return_config():
    """Enumerate this section's own router: any route whose declared response
    model carries a ``config`` field must require the host secret header as
    a parameter of its endpoint function.

    Structural on purpose, per the document's own warning against an
    assertion that cannot fail: this catches a *route* leaking config, not
    just today's two routes that correctly guard it.

    ``POST /rooms/create`` is deliberately excluded: it is the one route the
    document's own frozen surface has return ``config`` unauthenticated,
    because that config is the room the caller just created and specified
    themselves -- not a look at somebody else's room. The leak this failure
    mode names is a stranger reading a room's config, which only the *other*
    routes could ever do.
    """
    from app.api.v1.rooms import router

    checked_any = False
    for route in router.routes:
        if route.path.endswith("/create"):
            continue
        response_model = getattr(route, "response_model", None)
        fields = getattr(response_model, "model_fields", None)
        if not fields or "config" not in fields:
            continue
        checked_any = True
        endpoint = route.endpoint
        param_names = set(inspect.signature(endpoint).parameters)
        assert "x_host_secret" in param_names, (
            f"{sorted(route.methods or [])} {route.path} returns `config` "
            "without requiring the X-Host-Secret header"
        )
    # At least the two /config routes must exist and have been checked --
    # otherwise this test would trivially and silently pass on an empty router.
    assert checked_any


# --- FM 9 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm9_create_room_raises_runtime_error_rather_than_looping_forever(
    fake_redis, monkeypatch
):
    import app.services.state_service as state_service_module
    from app.services.state_service import get_state_service

    svc = get_state_service()
    monkeypatch.setattr(
        state_service_module, "generate_room_code", lambda *a, **k: "SAMECD"
    )
    occupied = await svc.create_room(get_preset("CLASSIC_MIT"), "Host One")
    assert occupied["room_code"] == "SAMECD"

    with pytest.raises(RuntimeError):
        await svc.create_room(get_preset("CLASSIC_MIT"), "Host Two")


# --- FM 2 -------------------------------------------------------------------


def test_fm2_host_secret_authorisation_uses_hmac_compare_digest():
    """A ``==`` comparison passes "a secret differing only in its last
    character is rejected" too, so that assertion has no power here -- the
    document is explicit that only a structural check on the source can
    prove anything. Scans the whole module for a call shaped like
    ``hmac.compare_digest(...)``.
    """
    import app.api.v1.rooms as rooms_module

    tree = ast.parse(inspect.getsource(rooms_module))
    found = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "compare_digest"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "hmac"
        for node in ast.walk(tree)
    )
    assert found, (
        "app/api/v1/rooms.py must authorise host_secret via "
        "hmac.compare_digest(...), not a plain `==`"
    )
