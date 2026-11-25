"""Every page gets a room's events in ``seq`` order (spec §11.2), so it drops none.

The wire here takes a different time for every send, as a real one does, and a page
applies an event only when its ``seq`` is newer than the last one it applied. The
Socket.IO rooms are the real ones (``sio.manager``), so who hears a broadcast is
whoever was in its room when it was sent.
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from typing import Any

import pytest

import app.services.game_service as game_service_module
from app.sockets.handlers.connection import disconnect
from app.sockets.handlers.lobby import join, join_waiting
from app.sockets.handlers.play import end_game_early, submit_order
from app.sockets.manager import sio
from tests.test_sockets.conftest import new_guest

pytestmark = pytest.mark.asyncio
SEEDS = range(12)
PLAYERS = 4


class Wire:
    """Delivers each emit, after a random number of loop turns, to the pages that were
    in the room when it was sent."""

    def __init__(self, sockets: Any) -> None:
        self.sockets = sockets
        self.random = random.Random(0)
        self.arrived: dict[str, list[tuple[str, int]]] = defaultdict(list)
        self.broadcast: list[tuple[str, int]] = []
        self.sent: list[int] = []
        self.dead: set[str] = set()

    async def _find_dead(self, sids: list[str]) -> list[str]:
        """Socket.IO runs ``disconnect`` inside the send that finds one."""
        for sid in self.dead.intersection(sids):
            self.dead.discard(sid)
            await drop(sid)
        return [sid for sid in sids if sio.rooms(sid)]

    async def _deliver(self, sids: list[str], event: str, data: dict) -> None:
        sids = await self._find_dead(sids)
        for _ in range(self.random.randrange(6)):
            await asyncio.sleep(0)
        if "seq" not in data:
            return
        self.sent.append(data["seq"])
        for sid in sids:
            self.arrived[sid].append((event, data["seq"]))

    async def emit_to_room(self, room_id: str, event: str, data: dict) -> None:
        self.broadcast.append((event, data["seq"]))
        sids = [sid for sid, _ in sio.manager.get_participants("/", room_id)]
        await self._deliver(sids, event, data)

    async def emit_to_sid(self, sid: str, event: str, data: dict) -> None:
        await self._deliver([sid], event, data)

    async def join_room(self, sid: str, room_id: str, alias: str) -> None:
        self.sockets.sid_to_room[sid] = room_id
        self.sockets.sid_to_alias[sid] = alias
        await sio.enter_room(sid, room_id)

    def out_of_order(self) -> dict[str, list[tuple[str, int]]]:
        """Per page, the events its ``seq`` gate would drop."""
        dropped: dict[str, list[tuple[str, int]]] = {}
        for sid, events in self.arrived.items():
            last = 0
            for event, seq in events:
                if seq <= last:
                    dropped.setdefault(sid, []).append((event, seq))
                else:
                    last = seq
        return dropped

    def missed(self) -> dict[str, list[tuple[str, int]]]:
        """Per page still connected, the room events later than its first event
        that never came."""
        missed: dict[str, list[tuple[str, int]]] = {}
        for sid, events in self.arrived.items():
            if not sio.rooms(sid):
                continue
            first = min(seq for _, seq in events)
            for event, seq in self.broadcast:
                if seq > first and (event, seq) not in events:
                    missed.setdefault(sid, []).append((event, seq))
        return missed

    def forget(self) -> None:
        self.arrived.clear()
        self.broadcast.clear()
        self.sent.clear()

    def got(self, sid: str) -> set[str]:
        return {event for event, _ in self.arrived[sid]}


@pytest.fixture
def wire(fake_socket_manager, monkeypatch) -> Wire:
    wire = Wire(fake_socket_manager)
    for name in ("emit_to_room", "emit_to_sid", "join_room"):
        monkeypatch.setattr(fake_socket_manager, name, getattr(wire, name))
    return wire


async def drop(sid: str) -> None:
    """The connection is gone: the handler runs, then Socket.IO forgets the sid."""
    await disconnect(sid)
    await sio.manager.disconnect(sid, "/")


def claim(game: dict) -> dict:
    return {"room_id": game["room_code"], "host_secret": game["host_secret"]}


async def new_pages(game: dict, connect_identity: Any) -> list[str]:
    """Every player's connection dropped and a new one is open."""
    pages: list[str] = []
    for role, sid in game["sids_by_role"].items():
        await drop(sid)
        pages.append(f"again-{role.lower()}")
        await connect_identity(pages[-1], game["identities_by_role"][role])
    return pages


@pytest.mark.parametrize("seed", SEEDS)
async def test_players_whose_pages_rejoin_together_all_get_their_state(
    make_room, start_running_game, connect_identity, wire, seed
):
    """Fails when the events are sent in any order: a later ``participant_reconnected``
    reaches a page before its own ``your_state``, which the page then drops."""
    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES")
    )
    pages = await new_pages(game, connect_identity)
    wire.forget()
    wire.random.seed(seed)
    await asyncio.gather(*(join(sid, {"room_id": game["room_code"]}) for sid in pages))
    assert wire.out_of_order() == {} and wire.missed() == {}
    assert all({"game_started", "your_state"} <= wire.got(sid) for sid in pages)


@pytest.mark.parametrize("seed", SEEDS)
async def test_players_joining_the_lobby_together_all_see_who_is_in_it(
    make_room, connect_identity, state_svc, wire, seed
):
    """Fails when a page enters the room after its join's turn: an update in between,
    its own included, never reaches it."""
    code = (await make_room())["room_code"]
    pages = [f"p{n}" for n in range(1, PLAYERS + 1)]
    for sid in pages:
        await connect_identity(sid, new_guest())
    wire.random.seed(seed)
    await asyncio.gather(*(join(sid, {"room_id": code}) for sid in pages))
    assert wire.out_of_order() == {} and wire.missed() == {}
    last = (await state_svc.get_room(code))["seq"]
    assert all(wire.arrived[sid][-1] == ("lobby_update", last) for sid in pages)


@pytest.mark.parametrize("seed", SEEDS)
async def test_a_host_claiming_a_finished_game_among_rejoins_gets_its_view(
    make_room, start_running_game, connect_identity, monkeypatch, wire, seed
):
    """Fails when the host's page enters the room after its claim's turn: the claim's
    own ``lobby_update`` never reaches it."""

    async def stored(*args):
        return "a" * 32

    monkeypatch.setattr(game_service_module, "persist_finished_game", stored)
    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES")
    )
    await end_game_early(game["host_sid"], claim(game))
    pages = await new_pages(game, connect_identity)
    await connect_identity("host-2", new_guest())
    wire.forget()
    wire.random.seed(seed)
    await asyncio.gather(
        join_waiting("host-2", claim(game)),
        *(join(sid, {"room_id": game["room_code"]}) for sid in pages),
    )
    assert wire.out_of_order() == {} and wire.missed() == {}
    host = wire.arrived["host-2"]
    [config_seq] = [seq for event, seq in host if event == "config_updated"]
    assert ("lobby_update", config_seq - 1) in host
    assert {"host_state", "game_persisted"} <= wire.got("host-2")
    assert all({"your_state", "game_persisted"} <= wire.got(sid) for sid in pages)


@pytest.mark.parametrize("seed", SEEDS)
async def test_a_week_closing_among_other_events_reaches_every_page(
    make_room, start_running_game, connect_identity, wire, seed
):
    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES")
    )
    players = list(game["sids_by_role"].values())
    await connect_identity("host-2", new_guest())
    wire.forget()
    wire.random.seed(seed)
    order = {"room_id": game["room_code"], "week": 1, "order": 4}
    await asyncio.gather(
        *(submit_order(sid, order) for sid in players),
        join_waiting("host-2", claim(game)),
        drop(game["host_sid"]),
    )
    assert wire.out_of_order() == {} and wire.missed() == {}
    for sid in players:
        assert {"week_closed", "your_week_closed", "your_state"} <= wire.got(sid)
    assert {"week_closed", "host_state"} <= wire.got("host-2")


@pytest.mark.parametrize("seed", SEEDS)
async def test_the_last_order_of_a_game_is_heard_before_its_end(
    make_room, start_running_game, connect_identity, monkeypatch, wire, seed
):
    """Every page is told of the last week and then of the end, with the store's answer
    after both, while another page is rejoining."""

    async def stored(*args):
        return "a" * 32

    monkeypatch.setattr(game_service_module, "persist_finished_game", stored)
    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES", duration_weeks=8)
    )
    players = list(game["sids_by_role"].values())
    for week in range(1, 8):
        for sid in players:
            await submit_order(
                sid, {"room_id": game["room_code"], "week": week, "order": 4}
            )
    wire.forget()
    wire.random.seed(seed)
    last = {"room_id": game["room_code"], "week": 8, "order": 4}
    await asyncio.gather(
        *(submit_order(sid, last) for sid in players),
        join(players[0], {"room_id": game["room_code"]}),
    )
    assert wire.out_of_order() == {} and wire.missed() == {}
    for sid in players:
        events = [event for event, _ in wire.arrived[sid]]
        told = ["your_week_closed", "game_finished", "game_persisted"]
        assert sorted(told, key=events.index) == told


async def test_a_submission_leaves_in_seq_order(make_room, start_running_game, wire):
    """The player's events and the host's share one order, whoever gets each."""
    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES")
    )
    wire.forget()
    await submit_order(
        game["sids_by_role"]["RETAILER"],
        {"room_id": game["room_code"], "week": 1, "order": 4},
    )
    assert len(wire.sent) == 3 and wire.sent == sorted(wire.sent)


async def test_a_connection_found_dead_during_a_send_does_not_block_the_room(
    make_room, start_running_game, wire
):
    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES")
    )
    sids = game["sids_by_role"]
    wire.forget()
    wire.dead.add(sids["FACTORY"])
    async with asyncio.timeout(1):
        await submit_order(
            sids["RETAILER"], {"room_id": game["room_code"], "week": 1, "order": 4}
        )
    assert wire.out_of_order() == {} and wire.missed() == {}
    told = wire.got(sids["RETAILER"])
    assert {"participant_disconnected", "game_paused"} <= told


async def test_a_refusal_to_a_connection_found_dead_does_not_block_the_room(
    make_room, start_running_game, wire
):
    """A refusal sent under the room's lock would wait for its own ``disconnect``."""
    game = await start_running_game(
        await make_room(role_assignment_mode="PLAYER_CHOOSES")
    )
    sids = game["sids_by_role"]
    wire.forget()
    wire.dead.add(sids["FACTORY"])
    async with asyncio.timeout(1):
        await submit_order(
            sids["FACTORY"], {"room_id": game["room_code"], "week": 99, "order": 4}
        )
    assert "participant_disconnected" in wire.got(sids["RETAILER"])
