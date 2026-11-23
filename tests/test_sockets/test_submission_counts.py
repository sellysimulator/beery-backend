"""The process-local resubmission counters cannot grow without bound
(spec §12.1).

A week's counters go when it closes; a room's go when its game finishes,
ended early included (its open week never closes); and counters older than a
room can live without a save -- a room that expired or was deleted mid-week
-- are swept. This file reads ``_submission_counts`` itself: what it checks
is that private map's size.
"""

from __future__ import annotations

import time

import pytest

import app.services.game_service as game_service_module
from app.services.state_service import ROOM_TTL_SECONDS
from app.sockets.handlers.play import end_game_early, submit_order

pytestmark = pytest.mark.asyncio


def _keys_for(room_code: str) -> list[tuple[str, int, str]]:
    return [
        key for key in game_service_module._submission_counts if key[0] == room_code
    ]


async def _submit_retailer(game, state_svc) -> None:
    stored = await state_svc.get_room(game["room_code"])
    week = state_svc.load_engine(stored).week
    await submit_order(
        game["sids_by_role"]["RETAILER"],
        {"room_id": game["room_code"], "week": week, "order": 4},
    )


async def test_ending_a_game_early_drops_its_open_week_s_counters(
    make_room, start_running_game, state_svc, monkeypatch
):
    async def no_persist(room_code, room, engine, stats):
        return None

    monkeypatch.setattr(game_service_module, "persist_finished_game", no_persist)
    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES")
    )
    await _submit_retailer(game, state_svc)
    assert _keys_for(game["room_code"])

    await end_game_early(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )

    assert _keys_for(game["room_code"]) == []


async def test_finishing_one_room_leaves_another_room_s_counters(
    make_room, start_running_game, state_svc, monkeypatch
):
    async def no_persist(room_code, room, engine, stats):
        return None

    monkeypatch.setattr(game_service_module, "persist_finished_game", no_persist)
    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES")
    )
    await _submit_retailer(game, state_svc)
    other = ("ZZZZZZ", 3, "RETAILER")
    game_service_module._submission_counts[other] = (1, time.monotonic())

    await end_game_early(
        game["host_sid"],
        {"room_id": game["room_code"], "host_secret": game["host_secret"]},
    )

    assert other in game_service_module._submission_counts


async def test_counters_of_a_room_that_expired_mid_week_are_swept(
    make_room, start_running_game, state_svc, monkeypatch
):
    """A room that expired (or was deleted) with a week open never closes it,
    so its counters are dropped once older than ``ROOM_TTL_SECONDS``; a fresh
    room's are kept."""
    now = time.monotonic()
    expired = ("YYYYYY", 5, "FACTORY")
    fresh = ("XXXXXX", 2, "RETAILER")
    game_service_module._submission_counts[expired] = (3, now - ROOM_TTL_SECONDS - 1)
    game_service_module._submission_counts[fresh] = (1, now - 60)
    monkeypatch.setattr(game_service_module, "_submissions_swept_at", float("-inf"))

    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES")
    )
    await _submit_retailer(game, state_svc)

    assert expired not in game_service_module._submission_counts
    assert fresh in game_service_module._submission_counts
    assert _keys_for(game["room_code"])
