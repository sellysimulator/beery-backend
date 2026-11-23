"""Black-box tests for ``GET /api/v1/games/id/{public_id}/results``, the
only results route (D22): there is none by room code.

Covers the payload's shape and figures, unknown ids, reused room codes, open
access, and what the payload must never carry (identities, secrets, config
keys). Money-as-a-number and the driver-error text are tested against this
route too, since neither is tied to a specific one of the results, export and
profile routes.

Driven entirely through: the HTTP surface itself; the frozen surfaces of
``app.core.enums`` (``Role``, ``ROLE_ORDER``), ``app.core.stats``, and
``app.services.db_service`` (``DbService``, ``build_snapshot``); the room
document shape (spec §11.3); and the model and service suites' own test
helpers, reused wholesale rather than reinvented
(``tests/test_models/conftest.py``, ``tests/test_services/conftest.py``).

``app/api/v1/games.py``, ``app/api/v1/user_games.py``, ``app/schemas/
results.py`` and ``app/services/export_service.py`` are never opened.

The Testcontainers PostgreSQL fixture chain, ``api_client``, and the
``persist_full_game``/``register_user``/``bearer_for``/``write_live_redis_room``
helpers ``test_export.py`` and ``test_profile.py`` also use all live in
``tests/test_api/conftest.py`` -- a conftest's fixtures resolve for every
module in this directory without an import, which is why they are not
defined or re-imported here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from app.core.enums import ROLE_ORDER, Role

from .conftest import (
    keys_recursive,
    make_participant,
    persist_full_game,
    unique_room_code,
    values_recursive,
    write_live_redis_room,
)

pytestmark = pytest.mark.dbschema


def REMOVED_RESULTS_BY_CODE_URL(room_code: str) -> str:
    return f"/api/v1/games/{room_code}/results"


def RESULTS_BY_ID_URL(public_id: str) -> str:
    return f"/api/v1/games/id/{public_id}/results"


CLAIM_URL = "/api/v1/games/claim"

# Config parameters (host teaching knobs) that must never appear in a
# results/export/profile payload: costs and delays are omitted deliberately.
FORBIDDEN_CONFIG_KEYS = {
    "holding_cost_per_unit_week",
    "backlog_cost_per_unit_week",
    "fixed_order_cost",
    "unit_purchase_cost",
    "starting_capital",
    "shipping_delay_weeks",
    "information_delay_weeks",
    "production_delay_weeks",
    "production_capacity_per_week",
    # every demand generator's parameters
    "value",
    "initial_value",
    "step_week",
    "step_value",
    "slope_per_week",
    "start_week",
    "cap",
    "base",
    "amplitude",
    "period_weeks",
    "phase",
    "distribution",
    "mean",
    "stdev",
    "min_value",
    "max_value",
}

# Identity / secret keys no broadcast or response may ever carry.
FORBIDDEN_IDENTITY_KEYS = {
    "identity",
    "guest_identity",
    "user_id",
    "session_token",
    "host_secret",
}


# ---------------------------------------------------------------------------
# Roles and lengths
# ---------------------------------------------------------------------------


def test_all_four_roles_in_order_with_length_matching_weeks_played(
    api_client, session_factory, fake_redis
):
    game = persist_full_game(session_factory, duration_weeks=10, seed=1)

    response = api_client.get(RESULTS_BY_ID_URL(game["public_id"]))

    assert response.status_code == 200
    payload = response.json()
    assert [r["role"] for r in payload["per_role"]] == [
        role.value for role in ROLE_ORDER
    ]

    weeks_played = payload["weeks_played"]
    assert weeks_played == 10
    assert len(payload["demand_series"]) == weeks_played
    for role_result in payload["per_role"]:
        assert len(role_result["orders"]) == weeks_played
        assert len(role_result["inventory"]) == weeks_played
        assert len(role_result["backlog"]) == weeks_played
        assert len(role_result["cumulative_cost"]) == weeks_played


# ---------------------------------------------------------------------------
# Total cost
# ---------------------------------------------------------------------------


def test_total_cost_equals_the_final_cumulative_cost(
    api_client, session_factory, fake_redis
):
    game = persist_full_game(session_factory, duration_weeks=8, seed=2)

    payload = api_client.get(RESULTS_BY_ID_URL(game["public_id"])).json()

    for role_result in payload["per_role"]:
        assert role_result["total_cost"] == pytest.approx(
            role_result["cumulative_cost"][-1]
        )


# ---------------------------------------------------------------------------
# Chain total cost
# ---------------------------------------------------------------------------


def test_chain_total_cost_is_the_sum_of_the_four_total_costs(
    api_client, session_factory, fake_redis
):
    game = persist_full_game(session_factory, duration_weeks=8, seed=3)

    payload = api_client.get(RESULTS_BY_ID_URL(game["public_id"])).json()

    expected = sum(role_result["total_cost"] for role_result in payload["per_role"])
    assert round(payload["chain_total_cost"], 2) == round(expected, 2)


# ---------------------------------------------------------------------------
# Bullwhip ratio
# ---------------------------------------------------------------------------


def test_bullwhip_ratio_is_null_for_constant_demand(
    api_client, session_factory, fake_redis
):
    # make_config's default demand is ConstantDemand -- D12's undefined case.
    game = persist_full_game(session_factory, duration_weeks=8, seed=4)

    payload = api_client.get(RESULTS_BY_ID_URL(game["public_id"])).json()

    assert all(r["bullwhip_ratio"] is None for r in payload["per_role"])


# ---------------------------------------------------------------------------
# No route by room code
# ---------------------------------------------------------------------------


def test_there_is_no_results_route_by_room_code(
    api_client, session_factory, fake_redis
):
    """Results are addressed by `public_id` only (D22). The room-code route is
    gone: a finished game's code, in either case, is a 404 like any unknown
    path, and the payload is still there by its id."""
    game = persist_full_game(session_factory, duration_weeks=8, seed=8)

    for code in (game["room_code"], game["room_code"].lower(), "NOSUCH01"):
        response = api_client.get(REMOVED_RESULTS_BY_CODE_URL(code))
        assert response.status_code in (404, 405), code
        assert "public_id" not in response.text

    assert api_client.get(RESULTS_BY_ID_URL(game["public_id"])).status_code == 200


def test_timestamps_are_utc_with_a_z_suffix(api_client, session_factory, fake_redis):
    """``started_at`` / ``finished_at`` are ISO-8601 UTC ending in ``Z`` --
    never a naive string a browser would read as local time, and never
    ``+00:00``."""
    game = persist_full_game(
        session_factory,
        duration_weeks=8,
        seed=9,
        started_at=datetime(2026, 1, 1, 8, 0, 0, 750_000, tzinfo=timezone.utc),
        finished_at=datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc),
    )

    payload = api_client.get(RESULTS_BY_ID_URL(game["public_id"])).json()

    # Truncated to whole seconds on the way in: the idempotency key.
    assert payload["started_at"] == "2026-01-01T08:00:00Z"
    assert payload["finished_at"] == "2026-01-01T10:30:00Z"


# ---------------------------------------------------------------------------
# No authentication
# ---------------------------------------------------------------------------


def test_results_require_no_authentication(api_client, session_factory, fake_redis):
    game = persist_full_game(session_factory, duration_weeks=8, seed=7)

    response = api_client.get(RESULTS_BY_ID_URL(game["public_id"]))

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Forbidden keys
# ---------------------------------------------------------------------------


def test_payload_carries_no_identity_secret_or_config_keys(
    api_client, session_factory, fake_redis
):
    """A wrong implementation that echoes the joined ``weeks``/``participants``
    row (rather than the documented ``RoleResult``/``ResultsResponse``
    shape) would leak ``identity``, ``guest_identity`` or a cost/delay
    column; this recursive key scan catches that regardless of nesting
    depth (recursive assertions only)."""
    game = persist_full_game(session_factory, duration_weeks=8, seed=8)

    payload = api_client.get(RESULTS_BY_ID_URL(game["public_id"])).json()

    keys = keys_recursive(payload)
    leaked_identity_keys = keys & FORBIDDEN_IDENTITY_KEYS
    leaked_config_keys = keys & FORBIDDEN_CONFIG_KEYS
    assert not leaked_identity_keys, leaked_identity_keys
    assert not leaked_config_keys, leaked_config_keys


# ---------------------------------------------------------------------------
# No uid or guest identity
# ---------------------------------------------------------------------------


def test_no_registered_uid_or_guest_string_anywhere_in_results(
    api_client, session_factory, fake_redis
):
    """Play with one registered user and one guest, and check that neither
    identity string appears anywhere -- as a key, as a value, or embedded in
    the raw response text (e.g. inside an unexpectedly stringified blob a
    key-only scan would miss)."""
    registered_uid = "uid-should-never-leak-99887766"
    guest_identity = "guest_ffffffff-1111-2222-3333-444444444444"
    participants = {
        "P1": make_participant(
            "P1", identity=registered_uid, display_name="Ana", role=Role.RETAILER
        ),
        "P2": make_participant(
            "P2", identity=guest_identity, display_name="Bea", role=Role.WHOLESALER
        ),
        "P3": make_participant(
            "P3",
            identity="guest_00000000-0000-0000-0000-000000000009",
            display_name="Cid",
            role=Role.DISTRIBUTOR,
        ),
        "P4": make_participant(
            "P4", identity=None, display_name="Bot", role=Role.FACTORY, is_bot=True
        ),
    }
    game = persist_full_game(
        session_factory,
        duration_weeks=8,
        seed=9,
        host_identity=registered_uid,
        host_uid=registered_uid,
        participants=participants,
    )

    response = api_client.get(RESULTS_BY_ID_URL(game["public_id"]))
    payload = response.json()

    assert registered_uid not in response.text
    assert "guest_" not in response.text
    string_values = [v for v in values_recursive(payload) if isinstance(v, str)]
    assert not any(registered_uid in v for v in string_values)
    assert not any("guest_" in v for v in string_values)


# ---------------------------------------------------------------------------
# Order indexing
# ---------------------------------------------------------------------------

_WEEK1_ORDERS = {
    Role.RETAILER: 5,
    Role.WHOLESALER: 6,
    Role.DISTRIBUTOR: 7,
    Role.FACTORY: 8,
}


def _distinct_week1_orders(role: Role, week: int) -> int:
    if week == 1:
        return _WEEK1_ORDERS[role]
    return 4


def test_orders_index_zero_is_week_one_per_role(
    api_client, session_factory, fake_redis
):
    """A chain-order-of-iteration bug (e.g. reversing ``ROLE_ORDER`` before
    slicing per-week arrays) would misassign these distinct week-1 values;
    an all-4s game could not catch it."""
    game = persist_full_game(
        session_factory, duration_weeks=8, seed=10, order_fn=_distinct_week1_orders
    )

    payload = api_client.get(RESULTS_BY_ID_URL(game["public_id"])).json()

    by_role = {r["role"]: r for r in payload["per_role"]}
    for role, expected in _WEEK1_ORDERS.items():
        assert by_role[role.value]["orders"][0] == expected


# ---------------------------------------------------------------------------
# Money as a number
# ---------------------------------------------------------------------------


def test_total_cost_is_a_json_number(api_client, session_factory, fake_redis):
    """Parsed straight off the wire, not through a Pydantic model that would
    coerce a ``Decimal``-as-string back into a float and hide the bug."""
    game = persist_full_game(session_factory, duration_weeks=8, seed=11)

    response = api_client.get(RESULTS_BY_ID_URL(game["public_id"]))
    raw = json.loads(response.text)

    assert isinstance(raw["chain_total_cost"], (int, float))
    for role_result in raw["per_role"]:
        assert isinstance(role_result["total_cost"], (int, float))
        for value in role_result["cumulative_cost"]:
            assert isinstance(value, (int, float))


# ---------------------------------------------------------------------------
# Driver errors
# ---------------------------------------------------------------------------

_LEAK_HOST = "leaky-postgres-host.example.invalid"
_LEAK_PORT = "45219"
_LEAK_USER = "leaky_db_user_marker"


@pytest.fixture()
def broken_db_client():
    """``get_db`` overridden with a session that raises a realistic driver
    error -- containing a host, a port and a user -- on first use, so a
    route that does ``except Exception as e: raise HTTPException(..., str(e))``
    is caught red-handed while a route that logs and returns a generic
    message is not."""
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app

    class _ExplodingSession:
        def close(self) -> None:
            return None

        def __getattr__(self, _name: str):
            def _raise(*_args: Any, **_kwargs: Any) -> None:
                raise OperationalError(
                    "SELECT 1",
                    {},
                    Exception(
                        "(psycopg.OperationalError) connection failed: "
                        f'connection to server at "{_LEAK_HOST}", port '
                        f"{_LEAK_PORT} failed: FATAL:  password authentication "
                        f'failed for user "{_LEAK_USER}"'
                    ),
                )

            return _raise

    def _override():
        yield _ExplodingSession()

    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_driver_error_leaks_no_host_port_or_user(broken_db_client):
    response = broken_db_client.get(RESULTS_BY_ID_URL("0" * 32))

    assert _LEAK_HOST not in response.text
    assert _LEAK_PORT not in response.text
    assert _LEAK_USER not in response.text


# ---------------------------------------------------------------------------
# Unfinished game
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unfinished_game_in_redis_is_404_not_a_partial_payload(
    api_client, fake_redis
):
    """A room that is ``RUNNING`` live has no results: nothing reads the
    live room document for a "quick" answer, and its code opens nothing."""
    room_code = "LIVEONLY"
    await write_live_redis_room(fake_redis, room_code, state="RUNNING")

    response = api_client.get(REMOVED_RESULTS_BY_CODE_URL(room_code))

    assert response.status_code in (404, 405)
    assert "demand_series" not in response.text


# ---------------------------------------------------------------------------
# Stable results URL: GET /games/id/{public_id}/results
# ---------------------------------------------------------------------------


def test_public_id_is_an_opaque_id_not_the_row_id(
    api_client, session_factory, fake_redis
):
    game = persist_full_game(session_factory, duration_weeks=8, seed=21)

    response = api_client.get(RESULTS_BY_ID_URL(game["public_id"]))

    assert response.status_code == 200
    public_id = response.json()["public_id"]
    assert public_id == game["public_id"]
    assert isinstance(public_id, str) and len(public_id) == 32
    assert public_id != str(game["game_id"])
    assert api_client.get(RESULTS_BY_ID_URL(str(game["game_id"]))).status_code == 404


def test_public_id_keeps_resolving_the_older_game_after_its_code_is_reused(
    api_client, session_factory, fake_redis
):
    room_code = unique_room_code()
    older_id = persist_full_game(
        session_factory,
        room_code=room_code,
        duration_weeks=8,
        seed=22,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        finished_at=datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
    )["public_id"]
    newer_id = persist_full_game(
        session_factory,
        room_code=room_code,
        duration_weeks=10,
        seed=23,
        started_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        finished_at=datetime(2026, 3, 1, 2, tzinfo=timezone.utc),
    )["public_id"]

    newer = api_client.get(RESULTS_BY_ID_URL(newer_id)).json()
    older = api_client.get(RESULTS_BY_ID_URL(older_id)).json()

    assert newer["public_id"] != older_id
    assert newer["weeks_played"] == 10
    assert older["weeks_played"] == 8
    assert older["public_id"] == older_id


def test_unknown_public_id_is_404_with_string_detail(api_client, fake_redis):
    response = api_client.get(RESULTS_BY_ID_URL("0" * 32))

    assert response.status_code == 404
    assert response.json()["detail"] == "No finished game found for that id."


def test_public_id_results_require_no_authentication_and_leak_nothing(
    api_client, session_factory, fake_redis
):
    game = persist_full_game(session_factory, duration_weeks=8, seed=24)
    public_id = game["public_id"]

    response = api_client.get(RESULTS_BY_ID_URL(public_id))

    assert response.status_code == 200
    keys = keys_recursive(response.json())
    assert not keys & FORBIDDEN_IDENTITY_KEYS
    assert not keys & FORBIDDEN_CONFIG_KEYS
