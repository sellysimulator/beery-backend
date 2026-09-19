"""Black-box tests for ``POST /api/v1/games/claim`` and the ``/users/me/*``
routes on the second router ``15-results-and-export-api.md`` owns
(``app/api/v1/user_games.py``).

Covers ``§4`` acceptance criteria 18-22 and ``§5`` failure modes 5 and 6.

Reuses ``persist_full_game``, ``register_user``, ``bearer_for`` and the
``api_client``/``session_factory`` fixtures from ``tests/test_api/conftest.py``.
``app/api/v1/user_games.py`` is never opened.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.enums import Role

from .conftest import (
    bearer_for,
    count_statements,
    make_participant,
    persist_full_game,
    register_user,
)

pytestmark = pytest.mark.dbschema

CLAIM_URL = "/api/v1/games/claim"
STATS_URL = "/api/v1/users/me/stats"
GAMES_URL = "/api/v1/users/me/games"


def GAME_DETAIL_URL(game_id: int) -> str:
    return f"/api/v1/users/me/games/{game_id}"


def _persist_game_for_player(
    session_factory,
    *,
    identity: str | None,
    role: Role = Role.RETAILER,
    room_code: str | None = None,
    duration_weeks: int = 8,
    seed: int = 0,
    finished_at: datetime | None = None,
):
    """Persist a finished game with ``identity`` playing ``role``.

    A non-guest ``identity`` must already have a ``users`` row *before* the
    game is persisted, or ``DbService._resolve_attribution`` cannot find it
    and the row is stored with ``user_id`` NULL (indistinguishable from an
    unregistered uid, failure mode 11 of ``14-end-of-game-persistence.md``)
    -- which would make every "my games" query below correctly find
    nothing, for the wrong reason.
    """
    if identity is not None and not identity.startswith("guest_"):
        register_user(session_factory, firebase_uid=identity, display_name=role.value)
    participants = {
        "P1": make_participant("P1", identity=identity, display_name="P1", role=role)
    }
    return persist_full_game(
        session_factory,
        room_code=room_code,
        duration_weeks=duration_weeks,
        seed=seed,
        host_identity=None,
        participants=participants,
        started_at=finished_at,
        finished_at=finished_at,
    )


# ---------------------------------------------------------------------------
# Acceptance criterion 18
# ---------------------------------------------------------------------------


def test_ac18_claim_returns_the_claimed_count(
    api_client, session_factory, firebase_tokens, fake_redis
):
    guest_identity = "guest_claim-target-0000-0000-0000-000000000010"
    _persist_game_for_player(
        session_factory, identity=guest_identity, role=Role.RETAILER, seed=50
    )

    headers = bearer_for(
        firebase_tokens, firebase_uid="uid-claimer-1", token="tok-claimer-1"
    )
    response = api_client.post(
        CLAIM_URL, json={"guest_identity": guest_identity}, headers=headers
    )

    assert response.status_code == 200
    assert response.json()["claimed"] == 1

    stats_response = api_client.get(STATS_URL, headers=headers)
    assert stats_response.json()["games_played"] == 1


def test_ac18_claim_without_a_token_is_401(api_client, fake_redis):
    response = api_client.post(
        CLAIM_URL, json={"guest_identity": "guest_does-not-matter-0000-0000-000011"}
    )

    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)


# ---------------------------------------------------------------------------
# Acceptance criterion 19
# ---------------------------------------------------------------------------


def test_ac19_stats_for_a_user_with_no_row_is_zero_filled_200(
    api_client, session_factory, firebase_tokens, fake_redis
):
    firebase_uid = "uid-brand-new-account"
    register_user(session_factory, firebase_uid=firebase_uid, display_name="New")
    headers = bearer_for(
        firebase_tokens, firebase_uid=firebase_uid, token="tok-brand-new"
    )

    response = api_client.get(STATS_URL, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["games_played"] == 0
    assert body["weeks_played"] == 0
    assert body["total_cost"] == 0
    assert body["avg_cost_per_week"] == 0
    assert body["bullwhip_avg"] is None
    assert body["best_game_id"] is None
    assert body["games_as_retailer"] == 0
    assert body["games_as_wholesaler"] == 0
    assert body["games_as_distributor"] == 0
    assert body["games_as_factory"] == 0


# ---------------------------------------------------------------------------
# Acceptance criteria 20, 21
# ---------------------------------------------------------------------------


def test_ac20_ac21_match_history_is_own_newest_first_and_clamped(
    api_client, session_factory, firebase_tokens, fake_redis
):
    firebase_uid = "uid-history-owner"
    headers = bearer_for(
        firebase_tokens, firebase_uid=firebase_uid, token="tok-history-owner"
    )
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    games = [
        _persist_game_for_player(
            session_factory,
            identity=firebase_uid,
            seed=60 + i,
            finished_at=base + timedelta(days=i),
        )
        for i in range(3)
    ]
    # A game belonging to someone else must never show up in A's history.
    _persist_game_for_player(
        session_factory,
        identity="uid-someone-else",
        seed=70,
        finished_at=base + timedelta(days=10),
    )

    first_page = api_client.get(
        GAMES_URL, params={"page": 1, "page_size": 2}, headers=headers
    ).json()
    assert first_page["total"] == 3
    assert first_page["page"] == 1
    assert first_page["page_size"] == 2
    assert len(first_page["matches"]) == 2
    # Newest first: the two most recently finished of the three games.
    returned_ids = {m["game_id"] for m in first_page["matches"]}
    assert returned_ids == {games[2]["game_id"], games[1]["game_id"]}
    finished_at_values = [m["finished_at"] for m in first_page["matches"]]
    assert finished_at_values == sorted(finished_at_values, reverse=True)

    second_page = api_client.get(
        GAMES_URL, params={"page": 2, "page_size": 2}, headers=headers
    ).json()
    assert second_page["total"] == 3
    assert len(second_page["matches"]) == 1
    assert second_page["matches"][0]["game_id"] == games[0]["game_id"]

    # AC 21
    clamped = api_client.get(
        GAMES_URL, params={"page": 1, "page_size": 1000}, headers=headers
    ).json()
    assert clamped["page_size"] == 100


# ---------------------------------------------------------------------------
# Acceptance criterion 22 / failure mode 5
# ---------------------------------------------------------------------------


def test_ac22_fm5_game_detail_for_someone_elses_game_is_404_like_a_missing_one(
    api_client, session_factory, firebase_tokens, fake_redis
):
    owner_uid = "uid-game-owner"
    game = _persist_game_for_player(session_factory, identity=owner_uid, seed=80)

    owner_headers = bearer_for(
        firebase_tokens, firebase_uid=owner_uid, token="tok-game-owner"
    )
    own_response = api_client.get(
        GAME_DETAIL_URL(game["game_id"]), headers=owner_headers
    )
    assert own_response.status_code == 200
    assert own_response.json()["room_code"] == game["room_code"]

    other_headers = bearer_for(
        firebase_tokens, firebase_uid="uid-not-the-owner", token="tok-not-the-owner"
    )
    other_persons_game = api_client.get(
        GAME_DETAIL_URL(game["game_id"]), headers=other_headers
    )
    nonexistent_id = game["game_id"] + 999_999
    truly_missing = api_client.get(
        GAME_DETAIL_URL(nonexistent_id), headers=other_headers
    )

    assert other_persons_game.status_code == 404
    assert truly_missing.status_code == 404
    # Identical, so the endpoint cannot be used as a game-existence oracle.
    assert other_persons_game.json()["detail"] == truly_missing.json()["detail"]
    assert isinstance(other_persons_game.json()["detail"], str)


# ---------------------------------------------------------------------------
# Failure mode 6
# ---------------------------------------------------------------------------


def test_fm6_match_history_issues_a_bounded_number_of_statements(
    api_client, session_factory, firebase_tokens, fake_redis, schema_engine
):
    firebase_uid = "uid-25-games-profile"
    headers = bearer_for(
        firebase_tokens, firebase_uid=firebase_uid, token="tok-25-games"
    )
    for seed in range(25):
        _persist_game_for_player(
            session_factory, identity=firebase_uid, seed=seed + 100
        )

    with count_statements(schema_engine) as log:
        response = api_client.get(
            GAMES_URL, params={"page": 1, "page_size": 20}, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["total"] == 25
    # 15-results-and-export-api.md §5 failure mode 6's own bound.
    assert log.total <= 3, (
        f"expected at most 3 statements for 25 games, ran {log.total}: "
        f"{[r.statement for r in log.records]}"
    )
