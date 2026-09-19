"""Black-box tests for ``GET /api/v1/games/{room_code}/export``.

Covers ``15-results-and-export-api.md §4`` acceptance criteria 10-17 and
``11a`` and ``§5`` failure modes 2, 4, 9 and 10.

Reuses ``persist_full_game``, ``register_user``, ``bearer_for``,
``write_live_redis_room`` and the ``api_client``/``session_factory``
fixtures from ``tests/test_api/conftest.py``. ``app/api/v1/games.py`` and
``app/services/export_service.py`` are never opened.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import pytest

from app.core.enums import ROLE_ORDER, Role
from app.core.game_engine import WeekRecord
from app.core.stats import compute_stats
from app.services.db_service import DbService, GameSnapshot, ParticipantSnapshot

from .conftest import (
    bearer_for,
    make_config,
    make_participant,
    persist_full_game,
    register_user,
    unique_room_code,
    write_live_redis_room,
)

pytestmark = pytest.mark.dbschema


def EXPORT_URL(room_code: str, fmt: str | None = None) -> str:
    if fmt is None:
        return f"/api/v1/games/{room_code}/export"
    return f"/api/v1/games/{room_code}/export?format={fmt}"


# §3.2, column for column. ``was_bot`` sits between ``is_bot`` and
# ``was_forced`` -- a late addition (00-decisions.md/15's own §3.1 already
# named ``WeekRecord.was_bot`` as carried by the export; §3.2's column list
# had simply omitted it).
EXPECTED_HEADER = [
    "room_code",
    "week",
    "role",
    "display_name",
    "is_bot",
    "was_bot",
    "was_forced",
    "customer_demand",
    "opening_inventory",
    "opening_backlog",
    "arrived",
    "incoming_order",
    "obligation",
    "shipped",
    "unfulfilled",
    "closing_inventory",
    "closing_backlog",
    "supply_line_after",
    "orders_in_flight_after",
    "order_qty",
    "holding_cost",
    "backlog_cost",
    "fixed_order_cost",
    "purchase_cost",
    "week_cost",
    "cumulative_cost",
    "production_started",
    "production_queued",
]

# A display name beginning "=" must be written with a leading "'" (failure
# mode 9); the value that round-trips is that *sanitised* string, not the
# original (the note under failure mode 10).
INJECTION_NAME = "=cmd|'/c calc'!A1"
SANITISED_INJECTION_NAME = "'" + INJECTION_NAME

# Comma, quote, a real embedded newline, and a non-ASCII character, all in
# one field -- and, at 6 characters, comfortably under the 24-character cap
# (16 §4) so it is a realistic display name rather than a truncation edge
# case. This is also the AC10 trap: the embedded newline makes the file's
# *physical* line count larger than its row count.
UNICODE_NAME = 'A,"é\nZ'


def _persist_export_game(
    session_factory,
    firebase_tokens,
    *,
    duration_weeks: int = 8,
    seed: int = 30,
    participants=None,
):
    host_uid = f"uid-export-host-{seed}"
    register_user(session_factory, firebase_uid=host_uid, display_name="Host")
    headers = bearer_for(
        firebase_tokens, firebase_uid=host_uid, token=f"tok-export-{seed}"
    )
    if participants is None:
        participants = {
            "P1": make_participant(
                "P1",
                identity="uid-export-retailer",
                display_name=UNICODE_NAME,
                role=Role.RETAILER,
            ),
            "P2": make_participant(
                "P2",
                identity="guest_export-wholesaler-0000-0000-0000-000000000006",
                display_name=INJECTION_NAME,
                role=Role.WHOLESALER,
            ),
            "P3": make_participant(
                "P3",
                identity="guest_export-distributor-0000-0000-0000-000000000007",
                display_name="Distri",
                role=Role.DISTRIBUTOR,
            ),
            "P4": make_participant(
                "P4",
                identity=None,
                display_name="FactoryBot",
                role=Role.FACTORY,
                is_bot=True,
            ),
        }
    finished_at = datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc)
    game = persist_full_game(
        session_factory,
        duration_weeks=duration_weeks,
        seed=seed,
        host_identity=host_uid,
        participants=participants,
        started_at=datetime(2026, 5, 17, 8, 0, tzinfo=timezone.utc),
        finished_at=finished_at,
    )
    game["headers"] = headers
    game["finished_at"] = finished_at
    game["host_uid"] = host_uid
    return game


# ---------------------------------------------------------------------------
# Acceptance criteria 10, 11, 12
# ---------------------------------------------------------------------------


def test_ac10_ac11_ac12_csv_row_count_header_and_customer_demand(
    api_client, session_factory, firebase_tokens, fake_redis
):
    game = _persist_export_game(session_factory, firebase_tokens, seed=31)

    response = api_client.get(
        EXPORT_URL(game["room_code"], "csv"), headers=game["headers"]
    )

    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.text)))
    header, data_rows = rows[0], rows[1:]

    # AC 11
    assert header == EXPECTED_HEADER

    # AC 10 -- counted with csv.reader, never splitlines(): the embedded
    # newline in UNICODE_NAME makes the physical line count larger than the
    # true row count once it is correctly quoted.
    assert len(data_rows) == 8 * 4
    assert response.text.count("\n") > len(data_rows)  # the trap is real

    # AC 12
    demand_series = game["engine"].demand_series
    week_index = header.index("week")
    demand_index = header.index("customer_demand")
    by_week: dict[int, set[str]] = {}
    for row in data_rows:
        by_week.setdefault(int(row[week_index]), set()).add(row[demand_index])
    for week, values in by_week.items():
        assert values == {str(demand_series[week - 1])}, week

    # Row order: week ascending, then ROLE_ORDER within a week, so two
    # exports of the same game are byte-identical.
    role_index = header.index("role")
    actual_order = [(int(row[week_index]), row[role_index]) for row in data_rows]
    expected_order = [
        (week, role.value) for week in range(1, 8 + 1) for role in ROLE_ORDER
    ]
    assert actual_order == expected_order

    # Byte-identical across two fetches of the same game.
    second_response = api_client.get(
        EXPORT_URL(game["room_code"], "csv"), headers=game["headers"]
    )
    assert second_response.text == response.text


# ---------------------------------------------------------------------------
# Acceptance criterion 11a
# ---------------------------------------------------------------------------


def _build_manual_history(
    duration_weeks: int, substituted_role: Role, switch_week: int
) -> list[WeekRecord]:
    """A ``WeekRecord`` list built by hand rather than played, so
    ``was_bot`` can flip partway through one role's rows without driving
    section 12's ``substitute_bot`` (out of this section's reading list).
    ``persist_game`` takes a snapshot and does not care how the history was
    produced -- exactly the pattern section 14's own ``test_fm7_money_
    rounds_half_up_to_two_decimals_at_the_boundary`` uses.
    """
    history: list[WeekRecord] = []
    for week in range(1, duration_weeks + 1):
        for role in ROLE_ORDER:
            is_factory = role is Role.FACTORY
            history.append(
                WeekRecord(
                    role=role,
                    week=week,
                    opening_inventory=12,
                    opening_backlog=0,
                    arrived=4,
                    incoming_order=4,
                    obligation=4,
                    shipped=4,
                    unfulfilled=0,
                    closing_inventory=12,
                    closing_backlog=0,
                    supply_line_after=4,
                    orders_in_flight_after=4,
                    order=4,
                    was_bot=(role is substituted_role and week >= switch_week),
                    was_forced=False,
                    holding_cost=0.5,
                    backlog_cost=0.0,
                    fixed_order_cost=0.0,
                    purchase_cost=0.0,
                    week_cost=0.5,
                    cumulative_cost=round(0.5 * week, 2),
                    production_started=4 if is_factory else None,
                    production_queued=4 if is_factory else None,
                )
            )
    return history


def _persist_manual_game_with_substitution(
    session_factory,
    firebase_tokens,
    *,
    duration_weeks: int = 6,
    switch_week: int = 4,
    substituted_role: Role = Role.WHOLESALER,
    seed: int = 90,
):
    """A finished game whose ``substituted_role`` seat is a bot at game end
    (``is_bot`` true on every one of its rows) but was only actually bot-run
    from ``switch_week`` onward (``was_bot`` flips partway) -- the shape a
    real ``substitute_bot`` mid-game leaves behind.
    """
    host_uid = f"uid-substitution-host-{seed}"
    register_user(session_factory, firebase_uid=host_uid, display_name="Host")
    headers = bearer_for(
        firebase_tokens, firebase_uid=host_uid, token=f"tok-sub-{seed}"
    )

    history = _build_manual_history(duration_weeks, substituted_role, switch_week)
    demand_series = [4] * duration_weeks
    config = make_config(duration_weeks=duration_weeks)
    stats = compute_stats(history, demand_series, duration_weeks)

    participants = [
        ParticipantSnapshot(
            alias=f"P{index + 1}",
            display_name=f"{role.value.title()}Seat",
            role=role,
            participant_type="PLAYER",
            is_bot=(role is substituted_role),
            identity=(
                None
                if role is substituted_role
                else f"guest_sub-{role.value.lower()}-{seed:04d}"
            ),
        )
        for index, role in enumerate(ROLE_ORDER)
    ]

    finished_at = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    snapshot = GameSnapshot(
        room_code=unique_room_code(),
        host_identity=host_uid,
        host_display_name="Host",
        seed=seed,
        started_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
        finished_at=finished_at,
        weeks_played=duration_weeks,
        ended_early=False,
        config=config,
        demand_series=demand_series,
        history=history,
        participants=participants,
        stats=stats,
    )
    db = session_factory()
    DbService().persist_game(db, snapshot)
    db.commit()
    return {
        "room_code": snapshot.room_code,
        "headers": headers,
        "duration_weeks": duration_weeks,
        "switch_week": switch_week,
        "substituted_role": substituted_role,
    }


def _parse_csv_bool(cell: str) -> bool:
    """Accepts any of the common boolean spellings a CSV writer might use --
    the document specifies the two columns' semantics, not one literal
    string encoding for ``True``/``False``."""
    return cell.strip().lower() in ("true", "1", "yes")


def test_ac11a_was_bot_differs_from_is_bot_around_a_mid_game_substitution(
    api_client, session_factory, firebase_tokens, fake_redis
):
    game = _persist_manual_game_with_substitution(
        session_factory,
        firebase_tokens,
        duration_weeks=6,
        switch_week=4,
        substituted_role=Role.WHOLESALER,
        seed=91,
    )

    response = api_client.get(
        EXPORT_URL(game["room_code"], "csv"), headers=game["headers"]
    )
    assert response.status_code == 200

    rows = list(csv.reader(io.StringIO(response.text)))
    header, data_rows = rows[0], rows[1:]
    role_index = header.index("role")
    week_index = header.index("week")
    is_bot_index = header.index("is_bot")
    was_bot_index = header.index("was_bot")

    substituted_rows = {
        int(row[week_index]): row
        for row in data_rows
        if row[role_index] == game["substituted_role"].value
    }
    assert len(substituted_rows) == game["duration_weeks"]

    for week, row in substituted_rows.items():
        # is_bot: the seat is a bot at game end, on every one of its rows.
        assert _parse_csv_bool(row[is_bot_index]) is True
        # was_bot: false before the substitution, true from switch_week on.
        expected_was_bot = week >= game["switch_week"]
        assert _parse_csv_bool(row[was_bot_index]) is expected_was_bot, week

    # Every other role's rows are never bot-run and never a bot seat.
    other_rows = [
        row for row in data_rows if row[role_index] != game["substituted_role"].value
    ]
    assert other_rows
    for row in other_rows:
        assert _parse_csv_bool(row[is_bot_index]) is False
        assert _parse_csv_bool(row[was_bot_index]) is False


# ---------------------------------------------------------------------------
# Acceptance criterion 13
# ---------------------------------------------------------------------------


def test_ac13_json_export_has_the_same_number_of_week_rows_as_the_csv(
    api_client, session_factory, firebase_tokens, fake_redis
):
    game = _persist_export_game(session_factory, firebase_tokens, seed=33)

    csv_response = api_client.get(
        EXPORT_URL(game["room_code"], "csv"), headers=game["headers"]
    )
    json_response = api_client.get(
        EXPORT_URL(game["room_code"], "json"), headers=game["headers"]
    )

    assert json_response.status_code == 200
    assert json_response.headers["content-type"].startswith("application/json")

    csv_data_rows = list(csv.reader(io.StringIO(csv_response.text)))[1:]
    payload = json_response.json()

    assert len(payload["weeks"]) == len(csv_data_rows) == 8 * 4
    assert payload["room_code"] == game["room_code"]
    assert "per_role" in payload


# ---------------------------------------------------------------------------
# Acceptance criterion 14 / failure mode 4
# ---------------------------------------------------------------------------


def test_ac14_fm4_no_credentials_is_403_with_no_partial_body(
    api_client, session_factory, fake_redis
):
    game = persist_full_game(session_factory, duration_weeks=6, seed=34)

    response = api_client.get(EXPORT_URL(game["room_code"], "csv"))

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "room_code" not in response.text  # no CSV header row leaked
    assert not response.headers.get("content-type", "").startswith("text/csv")


def test_export_for_a_room_with_no_finished_game_is_403(api_client, fake_redis):
    """§3.2's evaluation order: "no finished games row for the code" -> 403,
    never 404 -- an unauthorised caller must not learn the code is unused."""
    response = api_client.get(EXPORT_URL("NOSUCH02", "csv"))

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Acceptance criterion 15
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac15_wrong_host_secret_on_a_live_room_is_403(
    api_client, session_factory, fake_redis
):
    game = persist_full_game(session_factory, duration_weeks=6, seed=35)
    await write_live_redis_room(
        fake_redis, game["room_code"], host_secret="the-real-secret"
    )

    response = api_client.get(
        EXPORT_URL(game["room_code"], "csv"), headers={"X-Host-Secret": "wrong-one"}
    )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_export_with_matching_host_secret_on_a_live_room_succeeds(
    api_client, session_factory, fake_redis
):
    game = persist_full_game(session_factory, duration_weeks=6, seed=40)
    await write_live_redis_room(
        fake_redis, game["room_code"], host_secret="matching-secret-xyz"
    )

    response = api_client.get(
        EXPORT_URL(game["room_code"], "csv"),
        headers={"X-Host-Secret": "matching-secret-xyz"},
    )

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Acceptance criterion 16
# ---------------------------------------------------------------------------


def test_ac16_after_room_expired_bearer_host_succeeds_others_get_403(
    api_client, session_factory, firebase_tokens, fake_redis
):
    game = _persist_export_game(session_factory, firebase_tokens, seed=36)
    # No live Redis room is written for this room_code: "expired".

    own_response = api_client.get(
        EXPORT_URL(game["room_code"], "csv"), headers=game["headers"]
    )
    assert own_response.status_code == 200

    other_headers = bearer_for(
        firebase_tokens, firebase_uid="uid-not-the-host", token="tok-other-991"
    )
    other_response = api_client.get(
        EXPORT_URL(game["room_code"], "csv"), headers=other_headers
    )
    assert other_response.status_code == 403


# ---------------------------------------------------------------------------
# Acceptance criterion 17
# ---------------------------------------------------------------------------


def test_ac17_content_disposition_names_a_csv_file_with_the_room_code(
    api_client, session_factory, firebase_tokens, fake_redis
):
    game = _persist_export_game(session_factory, firebase_tokens, seed=37)

    response = api_client.get(
        EXPORT_URL(game["room_code"], "csv"), headers=game["headers"]
    )

    disposition = response.headers.get("content-disposition", "")
    expected_name = f"beery-{game['room_code']}-{game['finished_at']:%Y%m%d}.csv"
    assert "attachment" in disposition
    assert expected_name in disposition


# ---------------------------------------------------------------------------
# Failure mode 2
# ---------------------------------------------------------------------------


def test_fm2_no_identity_leak_in_csv_or_json_export(
    api_client, session_factory, firebase_tokens, fake_redis
):
    registered_uid = "uid-fm2-should-not-leak"
    guest_identity = "guest_fm2-guest-0000-0000-0000-000000000008"
    participants = {
        "P1": make_participant(
            "P1", identity=registered_uid, display_name="Ana", role=Role.RETAILER
        ),
        "P2": make_participant(
            "P2", identity=guest_identity, display_name="Bea", role=Role.WHOLESALER
        ),
        "P3": make_participant(
            "P3",
            identity="guest_fm2-other-0000-0000-0000-000000000009",
            display_name="Cid",
            role=Role.DISTRIBUTOR,
        ),
        "P4": make_participant(
            "P4", identity=None, display_name="Bot", role=Role.FACTORY, is_bot=True
        ),
    }
    game = _persist_export_game(
        session_factory, firebase_tokens, seed=38, participants=participants
    )

    csv_response = api_client.get(
        EXPORT_URL(game["room_code"], "csv"), headers=game["headers"]
    )
    json_response = api_client.get(
        EXPORT_URL(game["room_code"], "json"), headers=game["headers"]
    )

    for response in (csv_response, json_response):
        assert registered_uid not in response.text
        assert game["host_uid"] not in response.text
        assert "guest_" not in response.text


# ---------------------------------------------------------------------------
# Failure mode 9
# ---------------------------------------------------------------------------


def test_fm9_csv_injection_display_name_is_sanitised(
    api_client, session_factory, firebase_tokens, fake_redis
):
    game = _persist_export_game(session_factory, firebase_tokens, seed=32)

    response = api_client.get(
        EXPORT_URL(game["room_code"], "csv"), headers=game["headers"]
    )

    rows = list(csv.reader(io.StringIO(response.text)))
    header = rows[0]
    role_index = header.index("role")
    name_index = header.index("display_name")
    wholesaler_names = {
        row[name_index] for row in rows[1:] if row[role_index] == Role.WHOLESALER.value
    }
    assert wholesaler_names == {SANITISED_INJECTION_NAME}
    assert INJECTION_NAME not in wholesaler_names


# ---------------------------------------------------------------------------
# Failure mode 10
# ---------------------------------------------------------------------------


def test_fm10_unicode_display_name_round_trips_through_csv_reader(
    api_client, session_factory, firebase_tokens, fake_redis
):
    game = _persist_export_game(session_factory, firebase_tokens, seed=39)

    response = api_client.get(
        EXPORT_URL(game["room_code"], "csv"), headers=game["headers"]
    )

    rows = list(csv.reader(io.StringIO(response.text)))
    header = rows[0]
    role_index = header.index("role")
    name_index = header.index("display_name")
    retailer_names = {
        row[name_index] for row in rows[1:] if row[role_index] == Role.RETAILER.value
    }
    assert retailer_names == {UNICODE_NAME}
