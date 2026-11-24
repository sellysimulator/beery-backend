"""Black-box tests for ``POST /api/v1/games/claim`` and the ``/users/me/*``
routes on the profile router (``app/api/v1/user_games.py``, spec §9.9): the
guest claim, the zero-filled stats, the paged match history, a game detail
that hides someone else's game, and the bounded statement count.

Reuses ``persist_full_game``, ``register_user``, ``bearer_for`` and the
``api_client``/``session_factory`` fixtures from ``tests/test_api/conftest.py``.
``app/api/v1/user_games.py`` is never opened.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
HOSTED_URL = "/api/v1/users/me/hosted-games"


def GAME_DETAIL_URL(public_id: str) -> str:
    return f"/api/v1/users/me/games/{public_id}"


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

    A non-guest ``identity`` is registered first, with ``role.value`` as its
    display name, so the profile reads a known name rather than the bare
    ``users`` row the end-of-game write would otherwise create for it.
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
# Claiming guest games
# ---------------------------------------------------------------------------


def test_claim_returns_the_claimed_count(
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


def test_claim_without_a_token_is_401(api_client, fake_redis):
    response = api_client.post(
        CLAIM_URL, json={"guest_identity": "guest_does-not-matter-0000-0000-000011"}
    )

    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)


# ---------------------------------------------------------------------------
# Career stats
# ---------------------------------------------------------------------------


def test_stats_for_a_user_with_no_row_is_zero_filled_200(
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
    assert body["best_game_public_id"] is None
    assert body["games_as_retailer"] == 0
    assert body["games_as_wholesaler"] == 0
    assert body["games_as_distributor"] == 0
    assert body["games_as_factory"] == 0


# ---------------------------------------------------------------------------
# Match history
# ---------------------------------------------------------------------------


def test_match_history_is_own_newest_first_and_clamped(
    api_client, session_factory, firebase_tokens, fake_redis
):
    firebase_uid = "uid-history-owner"
    headers = bearer_for(
        firebase_tokens, firebase_uid=firebase_uid, token="tok-history-owner"
    )
    base = datetime(2026, 1, 1, tzinfo=UTC)
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
    returned_ids = {m["public_id"] for m in first_page["matches"]}
    assert returned_ids == {games[2]["public_id"], games[1]["public_id"]}
    # The match list carries no internal id (D22).
    assert "game_id" not in first_page["matches"][0]
    finished_at_values = [m["finished_at"] for m in first_page["matches"]]
    assert finished_at_values == sorted(finished_at_values, reverse=True)
    # ISO-8601 UTC with a `Z`, so a browser never reads it as local time.
    assert finished_at_values[0] == "2026-01-03T00:00:00Z"

    second_page = api_client.get(
        GAMES_URL, params={"page": 2, "page_size": 2}, headers=headers
    ).json()
    assert second_page["total"] == 3
    assert len(second_page["matches"]) == 1
    assert second_page["matches"][0]["public_id"] == games[0]["public_id"]

    # An oversized page_size is clamped.
    clamped = api_client.get(
        GAMES_URL, params={"page": 1, "page_size": 1000}, headers=headers
    ).json()
    assert clamped["page_size"] == 100


# ---------------------------------------------------------------------------
# Game detail
# ---------------------------------------------------------------------------


def _persist_hosted_game(
    session_factory,
    *,
    host_uid: str,
    seed: int,
    finished_at: datetime,
    player: str | None = None,
    duration_weeks: int = 8,
):
    """A finished game started by ``host_uid``'s verified connection, with
    four seats (one ``player``, the default guests and a bot)."""
    participants = None
    if player is not None:
        participants = {
            "P1": make_participant(
                "P1", identity=player, display_name="P1", role=Role.RETAILER
            )
        }
    return persist_full_game(
        session_factory,
        duration_weeks=duration_weeks,
        seed=seed,
        host_identity=host_uid,
        host_uid=host_uid,
        participants=participants,
        started_at=finished_at,
        finished_at=finished_at,
    )


def test_game_detail_for_someone_elses_game_is_404_like_a_missing_one(
    api_client, session_factory, firebase_tokens, fake_redis
):
    owner_uid = "uid-game-owner"
    game = _persist_game_for_player(session_factory, identity=owner_uid, seed=80)

    owner_headers = bearer_for(
        firebase_tokens, firebase_uid=owner_uid, token="tok-game-owner"
    )
    own_response = api_client.get(
        GAME_DETAIL_URL(game["public_id"]), headers=owner_headers
    )
    assert own_response.status_code == 200
    assert own_response.json()["room_code"] == game["room_code"]

    other_headers = bearer_for(
        firebase_tokens, firebase_uid="uid-not-the-owner", token="tok-not-the-owner"
    )
    other_persons_game = api_client.get(
        GAME_DETAIL_URL(game["public_id"]), headers=other_headers
    )
    truly_missing = api_client.get(GAME_DETAIL_URL("0" * 32), headers=other_headers)
    # The internal row id opens nothing, even for the owner.
    assert (
        api_client.get(
            GAME_DETAIL_URL(str(game["game_id"])), headers=owner_headers
        ).status_code
        == 404
    )

    assert other_persons_game.status_code == 404
    assert truly_missing.status_code == 404
    # Identical, so the endpoint cannot be used as a game-existence oracle.
    assert other_persons_game.json()["detail"] == truly_missing.json()["detail"]
    assert isinstance(other_persons_game.json()["detail"], str)


# ---------------------------------------------------------------------------
# Statement count
# ---------------------------------------------------------------------------


def test_match_history_issues_a_bounded_number_of_statements(
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
    # The match history's bound: a fixed number of statements, not one per game.
    assert log.total <= 3, (
        f"expected at most 3 statements for 25 games, ran {log.total}: "
        f"{[r.statement for r in log.records]}"
    )


# ---------------------------------------------------------------------------
# A game the caller hosted is not one they played (D23)
# ---------------------------------------------------------------------------


def test_game_detail_for_a_game_the_caller_only_hosted_is_404(
    api_client, session_factory, firebase_tokens, fake_redis
):
    """Spec §9.9: `/users/me/games/{id}` answers only for a game the caller
    played. Hosting it opens nothing here, and it stays out of the history
    and the stats."""
    host_uid = "uid-host-not-player"
    register_user(session_factory, firebase_uid=host_uid, display_name="Host")
    headers = bearer_for(firebase_tokens, firebase_uid=host_uid, token="tok-hnp")
    game = _persist_hosted_game(
        session_factory,
        host_uid=host_uid,
        seed=210,
        finished_at=datetime(2026, 3, 1, tzinfo=UTC),
    )

    response = api_client.get(GAME_DETAIL_URL(game["public_id"]), headers=headers)
    assert response.status_code == 404
    assert api_client.get(GAMES_URL, headers=headers).json()["total"] == 0
    assert api_client.get(STATS_URL, headers=headers).json()["games_played"] == 0
    hosted = api_client.get(HOSTED_URL, headers=headers).json()
    assert [g["room_code"] for g in hosted["games"]] == [game["room_code"]]


def test_game_detail_with_two_seats_for_one_account_is_200(
    api_client, session_factory, firebase_tokens, fake_redis
):
    """One account can hold two seats of one game: a guest seat in one tab,
    a signed-in seat in another, then a claim. The detail is a 200, not a
    500 from reading one row of two."""
    uid = "uid-two-seats"
    guest = "guest_twoseat-0000-0000-0000-000000000001"
    register_user(session_factory, firebase_uid=uid, display_name="Two")
    headers = bearer_for(firebase_tokens, firebase_uid=uid, token="tok-two-seats")
    participants = {
        "P1": make_participant(
            "P1", identity=uid, display_name="A", role=Role.RETAILER
        ),
        "P2": make_participant(
            "P2", identity=guest, display_name="B", role=Role.WHOLESALER
        ),
    }
    game = persist_full_game(session_factory, seed=211, participants=participants)
    claim = api_client.post(CLAIM_URL, json={"guest_identity": guest}, headers=headers)
    assert claim.json()["claimed"] == 1

    response = api_client.get(GAME_DETAIL_URL(game["public_id"]), headers=headers)
    assert response.status_code == 200
    assert response.json()["room_code"] == game["room_code"]


# ---------------------------------------------------------------------------
# Games you hosted (D23)
# ---------------------------------------------------------------------------


def test_hosted_games_without_a_token_is_401(api_client, fake_redis):
    response = api_client.get(HOSTED_URL)

    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)


def test_hosted_games_is_own_newest_first_paged_and_shaped(
    api_client, session_factory, firebase_tokens, fake_redis
):
    host_uid = "uid-hosted-owner"
    register_user(session_factory, firebase_uid=host_uid, display_name="Prof")
    headers = bearer_for(firebase_tokens, firebase_uid=host_uid, token="tok-hosted")
    base = datetime(2026, 4, 1, tzinfo=UTC)
    games = [
        _persist_hosted_game(
            session_factory,
            host_uid=host_uid,
            seed=220 + i,
            finished_at=base + timedelta(days=i),
        )
        for i in range(3)
    ]
    # Someone else's hosted game, and a game this account only played.
    register_user(session_factory, firebase_uid="uid-other-host", display_name="X")
    _persist_hosted_game(
        session_factory,
        host_uid="uid-other-host",
        seed=230,
        finished_at=base + timedelta(days=10),
        player=host_uid,
    )

    first = api_client.get(
        HOSTED_URL, params={"page": 1, "page_size": 2}, headers=headers
    )
    assert first.status_code == 200
    body = first.json()
    assert (body["total"], body["page"], body["page_size"]) == (3, 1, 2)
    assert [g["room_code"] for g in body["games"]] == [
        games[2]["room_code"],
        games[1]["room_code"],
    ]
    entry = body["games"][0]
    assert set(entry) == {
        "public_id",
        "room_code",
        "finished_at",
        "player_count",
        "ended_early",
    }
    assert entry["finished_at"] == "2026-04-03T00:00:00Z"
    assert entry["player_count"] == 4
    assert entry["ended_early"] is False
    results = api_client.get(f"/api/v1/games/id/{entry['public_id']}/results")
    assert results.json()["room_code"] == games[2]["room_code"]

    second = api_client.get(
        HOSTED_URL, params={"page": 2, "page_size": 2}, headers=headers
    ).json()
    assert [g["room_code"] for g in second["games"]] == [games[0]["room_code"]]

    clamped = api_client.get(
        HOSTED_URL, params={"page": 0, "page_size": 1000}, headers=headers
    ).json()
    assert (clamped["page"], clamped["page_size"]) == (1, 100)

    # Hosting is not playing: the played history holds only the one game
    # this account played, in someone else's room.
    history = api_client.get(GAMES_URL, headers=headers).json()
    assert history["total"] == 1
    assert history["matches"][0]["room_code"] != games[0]["room_code"]


def test_hosted_games_for_an_account_with_no_users_row_is_empty(
    api_client, session_factory, firebase_tokens, fake_redis
):
    headers = bearer_for(
        firebase_tokens, firebase_uid="uid-never-upserted", token="tok-no-row"
    )

    body = api_client.get(HOSTED_URL, headers=headers).json()

    assert body == {"games": [], "total": 0, "page": 1, "page_size": 20}


def test_hosted_games_issues_a_bounded_number_of_statements(
    api_client, session_factory, firebase_tokens, fake_redis, schema_engine
):
    host_uid = "uid-hosted-25"
    register_user(session_factory, firebase_uid=host_uid, display_name="Prof")
    headers = bearer_for(firebase_tokens, firebase_uid=host_uid, token="tok-h25")
    base = datetime(2026, 5, 1, tzinfo=UTC)
    for i in range(25):
        _persist_hosted_game(
            session_factory,
            host_uid=host_uid,
            seed=300 + i,
            finished_at=base + timedelta(hours=i),
        )

    with count_statements(schema_engine) as log:
        response = api_client.get(
            HOSTED_URL, params={"page": 1, "page_size": 20}, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["total"] == 25
    assert len(response.json()["games"]) == 20
    assert log.total <= 3, [r.statement for r in log.records]


def test_best_game_is_named_by_public_id(
    api_client, session_factory, firebase_tokens, fake_redis
):
    """The stats name the best game by `public_id`, the id the profile opens
    it by; no internal id is on the wire."""
    from app.services.stats_service import StatsService

    uid = "uid-best-game"
    game = _persist_game_for_player(session_factory, identity=uid, seed=240)
    db = session_factory()
    StatsService().recompute_for_game(db, game["game_id"])
    db.close()
    headers = bearer_for(firebase_tokens, firebase_uid=uid, token="tok-best")

    body = api_client.get(STATS_URL, headers=headers).json()

    assert body["best_game_public_id"] == game["public_id"]
    assert "best_game_id" not in body
    detail = api_client.get(
        GAME_DETAIL_URL(body["best_game_public_id"]), headers=headers
    )
    assert detail.status_code == 200
