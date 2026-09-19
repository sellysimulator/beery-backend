"""Black-box tests for ``PUT``/``GET /rooms/{code}/config``
(``10-rooms-rest-api.md``).

Covers ``§4`` acceptance criteria 11-18 and 20, and ``§5`` failure modes 3,
4, 7, 8 and 10. Failure modes 1, 2, 5, 6 and 9, and criteria 1-10 and 19,
live in ``test_rooms.py``.

Driven entirely through the frozen public surface of this section plus the
already-shipped surfaces of sections 01, 03 and 09, and the shared
``client``/``fake_redis`` fixtures from ``tests/conftest.py``.
"""

from __future__ import annotations

import pytest

from .conftest import config_url, create_room, status_url


def _headers(secret: str) -> dict[str, str]:
    return {"X-Host-Secret": secret}


# --- AC 11 -----------------------------------------------------------------


def test_ac11_put_without_the_secret_header_is_403(client, fake_redis):
    room_code, _secret = create_room(client)

    response = client.put(
        config_url(room_code), json={"config": {"duration_weeks": 10}}
    )

    assert response.status_code == 403
    assert isinstance(response.json()["detail"], str)


# --- AC 12 -----------------------------------------------------------------


def test_ac12_put_with_the_wrong_secret_is_403_and_changes_nothing(client, fake_redis):
    room_code, secret = create_room(client)
    before = client.get(config_url(room_code), headers=_headers(secret)).json()

    response = client.put(
        config_url(room_code),
        json={"config": {"duration_weeks": 10}},
        headers=_headers(secret + "x"),
    )

    assert response.status_code == 403

    after = client.get(config_url(room_code), headers=_headers(secret)).json()
    assert after["config"] == before["config"]


# --- AC 13 -----------------------------------------------------------------


def test_ac13_put_merges_a_partial_update_and_keeps_untouched_fields(
    client, fake_redis
):
    room_code, secret = create_room(client, preset="CLASSIC_MIT")

    response = client.put(
        config_url(room_code),
        json={"config": {"duration_weeks": 20}},
        headers=_headers(secret),
    )

    assert response.status_code == 200
    config = response.json()["config"]
    assert config["duration_weeks"] == 20
    # Untouched fields keep their previous (preset) values.
    assert config["currency_symbol"] == "$"
    assert config["role_assignment_mode"] == "HOST_ASSIGNS"
    assert config["demand"]["kind"] == "STEP"
    assert config["demand"]["step_week"] == 5


# --- AC 14 -----------------------------------------------------------------


def test_ac14_out_of_range_values_are_clamped_not_rejected(client, fake_redis):
    room_code, secret = create_room(client)

    response = client.put(
        config_url(room_code),
        json={"config": {"duration_weeks": 500}},
        headers=_headers(secret),
    )

    assert response.status_code == 200
    assert response.json()["config"]["duration_weeks"] == 104


# --- AC 15 -----------------------------------------------------------------


def test_ac15_a_too_short_custom_demand_is_422_naming_demand_values(client, fake_redis):
    room_code, secret = create_room(client)

    response = client.put(
        config_url(room_code),
        json={
            "config": {
                "duration_weeks": 10,
                "demand": {"kind": "CUSTOM", "values": [1, 2, 3]},
            }
        },
        headers=_headers(secret),
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "demand.values" in detail


# --- AC 16 / FM 4 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_ac16_and_fm4_put_on_a_running_room_rejects_every_field_and_changes_nothing(
    client, fake_redis
):
    """AC 16 plus failure mode 4: the most important test in this file.
    Every field family is attempted against a ``RUNNING`` room; each must
    400, and the stored config (and the untouched ``engine`` slot) must be
    byte-for-byte the same afterwards.
    """
    from app.services.state_service import get_state_service

    room_code, secret = create_room(client, preset="CLASSIC_MIT")

    svc = get_state_service()
    room = await svc.get_room(room_code)
    assert room is not None
    room["state"] = "RUNNING"
    await svc.save_room(room_code, room)
    before = await svc.get_room(room_code)
    assert before is not None

    attempts = [
        {"duration_weeks": 20},
        {"currency_symbol": "€"},
        {"pause_on_disconnect": False},
        {"bot_fill_empty_roles": True},
        {"random_seed": 42},
        {"role_assignment_mode": "RANDOM"},
        {"roles": {"RETAILER": {"initial_inventory": 1}}},
        {"visibility": {"show_all_inventories": True}},
        {"bot": {"theta": 0.9}},
        {"demand": {"kind": "SEASONAL"}},
    ]
    for patch in attempts:
        response = client.put(
            config_url(room_code), json={"config": patch}, headers=_headers(secret)
        )
        assert response.status_code == 400, patch
        assert isinstance(response.json()["detail"], str)

    after = await svc.get_room(room_code)
    assert after is not None
    assert after["config"] == before["config"]
    assert after["engine"] == before["engine"]
    assert after["state"] == "RUNNING"


# --- AC 17 -----------------------------------------------------------------


def test_ac17_switching_demand_kind_replaces_the_block_with_no_leftover_key(
    client, fake_redis
):
    room_code, secret = create_room(client, preset="CLASSIC_MIT")
    before = client.get(config_url(room_code), headers=_headers(secret)).json()
    assert before["config"]["demand"]["kind"] == "STEP"
    assert "step_week" in before["config"]["demand"]

    response = client.put(
        config_url(room_code),
        json={"config": {"demand": {"kind": "SEASONAL"}}},
        headers=_headers(secret),
    )

    assert response.status_code == 200
    demand = response.json()["config"]["demand"]
    assert demand["kind"] == "SEASONAL"
    assert "step_week" not in demand
    assert demand["period_weeks"] == 12  # SeasonalDemand's own default


# --- FM 8 -------------------------------------------------------------------


def test_fm8_demand_kind_round_trip_does_not_leak_the_earlier_step_week(
    client, fake_redis
):
    """``STEP`` (customised) -> ``SEASONAL`` -> ``STEP`` must land back on the
    ``STEP`` *default* ``step_week`` (5), not the 9 set on the first hop.
    A merge that treats ``demand`` like any other deep-mergeable block would
    carry the old value across the two kind changes.
    """
    room_code, secret = create_room(client, preset="CLASSIC_MIT")

    client.put(
        config_url(room_code),
        json={"config": {"demand": {"kind": "STEP", "step_week": 9}}},
        headers=_headers(secret),
    )
    client.put(
        config_url(room_code),
        json={"config": {"demand": {"kind": "SEASONAL"}}},
        headers=_headers(secret),
    )
    back = client.put(
        config_url(room_code),
        json={"config": {"demand": {"kind": "STEP"}}},
        headers=_headers(secret),
    )

    assert back.status_code == 200
    demand = back.json()["config"]["demand"]
    assert demand["kind"] == "STEP"
    assert demand["step_week"] == 5


# --- AC 18 -----------------------------------------------------------------


def test_ac18_get_without_the_secret_is_403(client, fake_redis):
    room_code, _secret = create_room(client)

    response = client.get(config_url(room_code))

    assert response.status_code == 403
    assert isinstance(response.json()["detail"], str)


def test_ac18_get_with_the_secret_returns_exactly_what_put_stored(client, fake_redis):
    room_code, secret = create_room(client)

    put_response = client.put(
        config_url(room_code),
        json={"config": {"duration_weeks": 15}},
        headers=_headers(secret),
    )
    get_response = client.get(config_url(room_code), headers=_headers(secret))

    assert get_response.status_code == 200
    assert get_response.json()["config"] == put_response.json()["config"]


# --- 404s (§3.3 step 1 / §3.4) and AC 20 -----------------------------------


def test_put_and_get_config_404_on_an_unknown_room_code(client, fake_redis):
    put_response = client.put(
        config_url("ZZZZZZ"),
        json={"config": {"duration_weeks": 10}},
        headers=_headers("whatever"),
    )
    get_response = client.get(config_url("ZZZZZZ"), headers=_headers("whatever"))

    assert put_response.status_code == 404
    assert get_response.status_code == 404
    assert isinstance(put_response.json()["detail"], str)
    assert isinstance(get_response.json()["detail"], str)


# --- FM 3 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fm3_an_empty_stored_secret_and_an_empty_header_are_rejected(
    client, fake_redis
):
    """``host_secret == ""`` should be impossible in practice, but a naive
    ``stored == provided`` authorisation would let an empty header through
    against it. Both must still be rejected.
    """
    from app.services.state_service import get_state_service

    room_code, _secret = create_room(client)
    svc = get_state_service()
    room = await svc.get_room(room_code)
    assert room is not None
    room["host_secret"] = ""
    await svc.save_room(room_code, room)

    with_empty_header = client.get(config_url(room_code), headers=_headers(""))
    without_header_at_all = client.get(config_url(room_code))

    assert with_empty_header.status_code == 403
    assert without_header_at_all.status_code == 403


# --- FM 7 -------------------------------------------------------------------


def test_fm7_deep_merge_does_not_clobber_untouched_role_fields(client, fake_redis):
    room_code, secret = create_room(client, preset="CLASSIC_MIT")
    before = client.get(config_url(room_code), headers=_headers(secret)).json()[
        "config"
    ]

    response = client.put(
        config_url(room_code),
        json={"config": {"roles": {"RETAILER": {"initial_inventory": 20}}}},
        headers=_headers(secret),
    )

    assert response.status_code == 200
    roles = response.json()["config"]["roles"]
    assert roles["RETAILER"]["initial_inventory"] == 20
    # A shallow merge would reset the other eight RETAILER fields to their
    # model defaults; they must survive untouched from the CLASSIC_MIT preset.
    assert (
        roles["RETAILER"]["holding_cost_per_unit_week"]
        == before["roles"]["RETAILER"]["holding_cost_per_unit_week"]
    )
    assert (
        roles["RETAILER"]["backlog_cost_per_unit_week"]
        == before["roles"]["RETAILER"]["backlog_cost_per_unit_week"]
    )
    assert roles["WHOLESALER"] == before["roles"]["WHOLESALER"]
    assert roles["DISTRIBUTOR"] == before["roles"]["DISTRIBUTOR"]
    assert roles["FACTORY"] == before["roles"]["FACTORY"]


# --- Sanity: PUT does not emit anything and moves LOBBY -> CONFIGURING ----


def test_put_config_advances_lobby_to_configuring_and_status_reflects_it(
    client, fake_redis
):
    room_code, secret = create_room(client)

    client.put(
        config_url(room_code),
        json={"config": {"duration_weeks": 12}},
        headers=_headers(secret),
    )

    status = client.get(status_url(room_code)).json()
    assert status["state"] == "CONFIGURING"
