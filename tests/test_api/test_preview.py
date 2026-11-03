"""Black-box tests for ``POST /rooms/preview`` (``10-rooms-rest-api.md`` §3.5b)
and the preset cards' generated figures (§3.5, amended 2026-11-03).

The host form used to draw its demand preview with its own copy of the
generators, and its preset cards restated each preset's numbers by hand. The
preview is now the server's: it must return exactly the series a started game
plays and the lead times every player is sent, and refuse a draft the way a
save is refused. The preset descriptions must be written from the preset's own
config.
"""

from __future__ import annotations

from app.core.config_models import DEFAULT_LIMITS, GameConfig
from app.core.enums import ROLE_ORDER
from app.core.game_engine import GameEngine
from app.core.presets import get_preset, scenario_summary

from .conftest import PRESETS

PREVIEW = "/api/v1/rooms/preview"


def classic_payload(**overrides: object) -> dict:
    payload = get_preset("CLASSIC_MIT").to_payload()
    payload.update(overrides)
    return payload


def test_preview_returns_the_series_and_lead_times_the_game_plays(client):
    draft = classic_payload(random_seed=77)

    response = client.post(PREVIEW, json={"config": draft})

    assert response.status_code == 200
    body = response.json()
    config = GameConfig.from_host_input(draft, DEFAULT_LIMITS)
    engine = GameEngine.start(config, 77)
    assert body["demand_series"] == engine.demand_series
    assert body["demand_series"][:6] == [4, 4, 4, 4, 8, 8]
    assert body["demand_is_example"] is False
    assert body["lead_times"] == {
        role.value: engine.player_view(role)["order_arrival_lead_weeks"]
        for role in ROLE_ORDER
    }
    assert body["lead_times"] == {
        "RETAILER": 4,
        "WHOLESALER": 4,
        "DISTRIBUTOR": 4,
        "FACTORY": 2,
    }


def test_preview_needs_no_room_and_no_secret(client):
    response = client.post(PREVIEW, json={"config": classic_payload()})

    assert response.status_code == 200
    assert "X-Host-Secret" not in response.request.headers


def test_preview_flags_an_unseeded_random_series_as_an_example(client):
    draft = classic_payload(
        demand={
            "kind": "STOCHASTIC",
            "distribution": "UNIFORM",
            "mean": 8,
            "stdev": 2,
            "min": 2,
            "max": 12,
        }
    )

    first = client.post(PREVIEW, json={"config": draft}).json()
    again = client.post(PREVIEW, json={"config": draft}).json()

    assert first["demand_is_example"] is True
    assert first["demand_series"] == again["demand_series"]
    assert all(2 <= value <= 12 for value in first["demand_series"])


def test_a_new_kind_with_no_parameters_previews_the_servers_defaults(client):
    """The host form sends only the kind it switched to; the server fills the
    rest, so the preview of ``{"kind": "SEASONAL"}`` is the declared default."""
    response = client.post(
        PREVIEW,
        json={
            "config": classic_payload(duration_weeks=12, demand={"kind": "SEASONAL"})
        },
    )

    assert response.status_code == 200
    assert response.json()["demand_series"] == [8, 10, 11, 12, 11, 10, 8, 6, 5, 4, 5, 6]


def test_a_refused_draft_is_a_422_naming_the_field_and_the_range(client):
    response = client.post(
        PREVIEW, json={"config": classic_payload(duration_weeks=500)}
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert detail.startswith("duration_weeks: ")
    assert "between 8 and 104" in detail


def test_a_short_custom_series_is_refused_with_the_servers_reason(client):
    response = client.post(
        PREVIEW,
        json={"config": classic_payload(demand={"kind": "CUSTOM", "values": [4, 4]})},
    )

    assert response.status_code == 422
    assert response.json()["detail"].startswith("demand.values: ")


def test_preset_descriptions_are_written_from_each_presets_config(client):
    presets = client.get(PRESETS).json()["presets"]

    for entry in presets:
        summary = scenario_summary(get_preset(entry["name"]))
        assert entry["description"].startswith(summary)
        # The tagline after it carries no figure of its own.
        tagline = entry["description"][len(summary) :]
        assert not any(character.isdigit() for character in tagline)

    classic = next(entry for entry in presets if entry["name"] == "CLASSIC_MIT")
    assert classic["description"].startswith(
        "36 weeks, 12 units on hand, 2-week delays, demand steps from 4 to 8 at week 5, "
        "$0.50 holding and $1.00 backlog a unit a week."
    )
