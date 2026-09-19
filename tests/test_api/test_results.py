"""Black-box tests for ``GET /api/v1/games/{room_code}/results``.

Covers ``15-results-and-export-api.md §4`` acceptance criteria 1-9 and
``§5`` failure modes 1, 3, 7, 8, 11 and 12. Money-as-a-number (failure mode
8) and the driver-error text (failure mode 11) are tested against this
route too, since neither the document nor the frozen surface ties them to
a specific one of the three routes this section owns.

Driven entirely through: the HTTP surface itself; the frozen surfaces of
``app.core.enums`` (``Role``, ``ROLE_ORDER``), ``app.core.stats``, and
``app.services.db_service`` (``DbService``, ``build_snapshot``); the room
document shape (``09-state-service.md §2``); and section 13's and 14's own
test helpers, reused wholesale rather than reinvented
(``tests/test_models/conftest.py``, ``tests/test_services/conftest.py``).

``app/api/v1/games.py``, ``app/api/v1/user_games.py``, ``app/schemas/
results.py`` and ``app/services/export_service.py`` are never opened.

The Testcontainers MySQL fixture chain, ``api_client``, and the
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


def RESULTS_URL(room_code: str) -> str:
    return f"/api/v1/games/{room_code}/results"


CLAIM_URL = "/api/v1/games/claim"

# Config parameters (host teaching knobs) that §3.1 says must never appear in
# a results/export/profile payload: "Costs and delays are omitted
# deliberately."
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
    # every demand generator's parameters (13-db-models-and-migrations.md §2.5)
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

# Identity / secret keys no broadcast or response may ever carry
# (00-conventions.md §2, §3).
FORBIDDEN_IDENTITY_KEYS = {
    "identity",
    "guest_identity",
    "user_id",
    "session_token",
    "host_secret",
}


# ---------------------------------------------------------------------------
# Acceptance criteria 1, 2
# ---------------------------------------------------------------------------


def test_ac1_ac2_all_four_roles_in_order_with_length_matching_weeks_played(
    api_client, session_factory, fake_redis
):
    game = persist_full_game(session_factory, duration_weeks=10, seed=1)

    response = api_client.get(RESULTS_URL(game["room_code"]))

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
# Acceptance criterion 3
# ---------------------------------------------------------------------------


def test_ac3_total_cost_equals_the_final_cumulative_cost(
    api_client, session_factory, fake_redis
):
    game = persist_full_game(session_factory, duration_weeks=8, seed=2)

    payload = api_client.get(RESULTS_URL(game["room_code"])).json()

    for role_result in payload["per_role"]:
        assert role_result["total_cost"] == pytest.approx(
            role_result["cumulative_cost"][-1]
        )


# ---------------------------------------------------------------------------
# Acceptance criterion 4
# ---------------------------------------------------------------------------


def test_ac4_chain_total_cost_is_the_sum_of_the_four_total_costs(
    api_client, session_factory, fake_redis
):
    game = persist_full_game(session_factory, duration_weeks=8, seed=3)

    payload = api_client.get(RESULTS_URL(game["room_code"])).json()

    expected = sum(role_result["total_cost"] for role_result in payload["per_role"])
    assert round(payload["chain_total_cost"], 2) == round(expected, 2)


# ---------------------------------------------------------------------------
# Acceptance criterion 5
# ---------------------------------------------------------------------------


def test_ac5_bullwhip_ratio_is_null_for_constant_demand(
    api_client, session_factory, fake_redis
):
    # make_config's default demand is ConstantDemand -- D12's undefined case.
    game = persist_full_game(session_factory, duration_weeks=8, seed=4)

    payload = api_client.get(RESULTS_URL(game["room_code"])).json()

    assert all(r["bullwhip_ratio"] is None for r in payload["per_role"])


# ---------------------------------------------------------------------------
# Acceptance criterion 6
# ---------------------------------------------------------------------------


def test_ac6_unknown_room_code_is_404_with_string_detail(api_client, fake_redis):
    response = api_client.get(RESULTS_URL("NOSUCH01"))

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert detail == "No finished game found for that code."


# ---------------------------------------------------------------------------
# Acceptance criterion 7
# ---------------------------------------------------------------------------


def test_ac7_reused_room_code_returns_the_more_recent_game(
    api_client, session_factory, fake_redis
):
    room_code = unique_room_code()
    persist_full_game(
        session_factory,
        room_code=room_code,
        duration_weeks=6,
        seed=5,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        finished_at=datetime(2026, 1, 1, 2, tzinfo=timezone.utc),
    )
    persist_full_game(
        session_factory,
        room_code=room_code,
        duration_weeks=9,
        seed=6,
        started_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        finished_at=datetime(2026, 3, 1, 2, tzinfo=timezone.utc),
    )

    payload = api_client.get(RESULTS_URL(room_code)).json()

    assert payload["weeks_played"] == 9
    assert len(payload["demand_series"]) == 9


# ---------------------------------------------------------------------------
# Acceptance criterion 8
# ---------------------------------------------------------------------------


def test_ac8_results_require_no_authentication(api_client, session_factory, fake_redis):
    game = persist_full_game(session_factory, duration_weeks=6, seed=7)

    response = api_client.get(RESULTS_URL(game["room_code"]))

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Acceptance criterion 9 / failure mode 3
# ---------------------------------------------------------------------------


def test_ac9_fm3_payload_carries_no_identity_secret_or_config_keys(
    api_client, session_factory, fake_redis
):
    """A wrong implementation that echoes the joined ``weeks``/``participants``
    row (rather than the documented ``RoleResult``/``ResultsResponse``
    shape) would leak ``identity``, ``guest_identity`` or a cost/delay
    column; this recursive key scan catches that regardless of nesting
    depth (``00-conventions.md §5`` -- recursive assertions only)."""
    game = persist_full_game(session_factory, duration_weeks=6, seed=8)

    payload = api_client.get(RESULTS_URL(game["room_code"])).json()

    keys = keys_recursive(payload)
    leaked_identity_keys = keys & FORBIDDEN_IDENTITY_KEYS
    leaked_config_keys = keys & FORBIDDEN_CONFIG_KEYS
    assert not leaked_identity_keys, leaked_identity_keys
    assert not leaked_config_keys, leaked_config_keys


# ---------------------------------------------------------------------------
# Failure mode 1
# ---------------------------------------------------------------------------


def test_fm1_no_registered_uid_or_guest_string_anywhere_in_results(
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
        duration_weeks=6,
        seed=9,
        host_identity=registered_uid,
        participants=participants,
    )

    response = api_client.get(RESULTS_URL(game["room_code"]))
    payload = response.json()

    assert registered_uid not in response.text
    assert "guest_" not in response.text
    string_values = [v for v in values_recursive(payload) if isinstance(v, str)]
    assert not any(registered_uid in v for v in string_values)
    assert not any("guest_" in v for v in string_values)


# ---------------------------------------------------------------------------
# Failure mode 7
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


def test_fm7_orders_index_zero_is_week_one_per_role(
    api_client, session_factory, fake_redis
):
    """A chain-order-of-iteration bug (e.g. reversing ``ROLE_ORDER`` before
    slicing per-week arrays) would misassign these distinct week-1 values;
    an all-4s game could not catch it."""
    game = persist_full_game(
        session_factory, duration_weeks=8, seed=10, order_fn=_distinct_week1_orders
    )

    payload = api_client.get(RESULTS_URL(game["room_code"])).json()

    by_role = {r["role"]: r for r in payload["per_role"]}
    for role, expected in _WEEK1_ORDERS.items():
        assert by_role[role.value]["orders"][0] == expected


# ---------------------------------------------------------------------------
# Failure mode 8
# ---------------------------------------------------------------------------


def test_fm8_total_cost_is_a_json_number(api_client, session_factory, fake_redis):
    """Parsed straight off the wire, not through a Pydantic model that would
    coerce a ``Decimal``-as-string back into a float and hide the bug."""
    game = persist_full_game(session_factory, duration_weeks=6, seed=11)

    response = api_client.get(RESULTS_URL(game["room_code"]))
    raw = json.loads(response.text)

    assert isinstance(raw["chain_total_cost"], (int, float))
    for role_result in raw["per_role"]:
        assert isinstance(role_result["total_cost"], (int, float))
        for value in role_result["cumulative_cost"]:
            assert isinstance(value, (int, float))


# ---------------------------------------------------------------------------
# Failure mode 11
# ---------------------------------------------------------------------------

_LEAK_HOST = "leaky-mysql-host.example.invalid"
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
                        '(pymysql.err.OperationalError) (1045, "Access denied '
                        f"for user '{_LEAK_USER}'@'{_LEAK_HOST}:{_LEAK_PORT}' "
                        '(using password: YES)")'
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


def test_fm11_driver_error_leaks_no_host_port_or_user(broken_db_client):
    response = broken_db_client.get(RESULTS_URL("ANYCODE1"))

    assert _LEAK_HOST not in response.text
    assert _LEAK_PORT not in response.text
    assert _LEAK_USER not in response.text


# ---------------------------------------------------------------------------
# Failure mode 12
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm12_unfinished_game_in_redis_is_404_not_a_partial_payload(
    api_client, fake_redis
):
    """A room that is ``RUNNING`` live in Redis and has no row in MySQL yet
    must still be a plain 404 -- a wrong implementation that falls back to
    the live room document for a "quick" answer would instead return 200
    (or a half-filled payload) here."""
    room_code = "LIVEONLY"
    await write_live_redis_room(fake_redis, room_code, state="RUNNING")

    response = api_client.get(RESULTS_URL(room_code))

    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)
