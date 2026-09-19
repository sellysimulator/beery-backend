"""The play loop (`12-socket-play.md`).

Owned solely by section 12 (**D19**), alongside `app/sockets/handlers/play.py`.
This module holds the two rules that dominate the section: **the server is
authoritative** (`§3.9`) and **redaction happens before send** (`§3.4`) --
every broadcast built here is assembled key by key, never by spreading a
room document or a view into an `emit_to_room` payload.

The locking contract (`§2.1`) is obeyed throughout: every method that reads,
mutates and saves room state does all three inside one
``state_svc.lock(room_code)`` acquisition, then returns to its caller, which
emits and chains into any further ``GameService`` call only after that lock
has been released. The bot cascade -- a submit closing a week, which plays
the bots, which may close the next week, and so on -- is driven by the
private ``_drive`` loop, a plain ``while``, never by two public methods
calling each other. That is what keeps a 104-week all-bot game from
recursing (`§2.1`, `§3.2`, acceptance criterion 13).
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from ..core.bot import bot_for
from ..core.enums import ROLE_ORDER, Role, RoomState
from ..core.game_engine import GameEngine, GamePhase, WeekRecord
from ..core.stats import GameStats, compute_stats
from ..db import session as db_session
from ..sockets.manager import socket_manager
from .db_service import build_snapshot, get_db_service
from .state_service import StateService, get_state_service
from .stats_service import get_stats_service

logger = logging.getLogger(__name__)

__all__ = [
    "GameService",
    "get_game_service",
    "persist_finished_game",
    "stats_to_payload",
]

MAX_RESUBMISSIONS_PER_ROLE_PER_WEEK = 20

_ROOM_NOT_FOUND = "Room does not exist."
_GAME_PAUSED_MSG = "The game is paused."
_GAME_FINISHED_MSG = "The game has finished."
_NOT_RUNNING_MSG = "The game is not running."
_NOT_IN_ROOM_MSG = "You are not seated in this room."
_NO_ROLE_MSG = "You do not hold a role."
_BOT_SEATED_MSG = "That role is now played by a bot."
_STALE_WEEK_MSG = "That week is no longer open."
_INVALID_ORDER_MSG = "Order must be a whole number."
_TOO_MANY_MSG = "Too many submissions for this week."

# Resubmission counters, per (room_code, week, role). Deliberately
# process-local, not part of the frozen room document (`09-state-service.md
# §2` -- no new top-level key may be added without a schema bump, which is
# section 09's call, not this section's). Socket.IO connections are sticky
# for their lifetime, so every `submit_order` for one sid is handled by the
# same worker process that would have to enforce this cap anyway; the guard
# this exists for is a single client looping submissions, not a
# cross-process invariant (`§3.1`, failure mode 7). Entries are dropped for
# a week as soon as it closes, so this cannot grow without bound.
_submission_counts: dict[tuple[str, int, str], int] = {}


def _forget_submission_counts(room_code: str, week: int) -> None:
    stale = [
        key for key in _submission_counts if key[0] == room_code and key[1] == week
    ]
    for key in stale:
        _submission_counts.pop(key, None)


def _coerce_order(value: Any) -> tuple[bool, int]:
    """`§3.1` step 5: accept an `int`, or a `float` that is exactly integral.

    Reject a `bool` (a `bool` is a subclass of `int` in Python but is never
    a valid order), a string, `None`, `NaN` and `inf`. Never coerces an
    invalid value to 0 -- that would silently place an order nobody made.
    """
    if isinstance(value, bool):
        return False, 0
    if isinstance(value, int):
        return True, value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value) or not value.is_integer():
            return False, 0
        return True, int(value)
    return False, 0


def _sid_for_alias(room: dict, alias: str | None) -> str | None:
    """The connected sid currently mapped to `alias`, or `None`."""
    if not alias:
        return None
    for sid, mapped_alias in room["sid_to_alias"].items():
        if mapped_alias == alias:
            return sid
    return None


def _week_record_payload(record: WeekRecord) -> dict:
    """A JSON-safe `WeekRecord`, for the one sid entitled to see it."""
    payload = asdict(record)
    payload["role"] = record.role.value
    return payload


def stats_to_payload(stats: GameStats) -> dict:
    """The one serialiser for `GameStats` (`§3.8` step 3).

    `GameStats` is a frozen dataclass keyed by the `Role` enum; every field
    that carries a `Role` becomes its string value, and `per_role` is keyed
    by that same value. `bullwhip_ratio` and `fill_rate` pass through as
    `None`, which the JSON encoder renders as `null`.
    """
    return {
        "weeks_played": stats.weeks_played,
        "demand_variance": stats.demand_variance,
        "chain_total_cost": stats.chain_total_cost,
        "per_role": {
            role.value: {
                "role": role_stats.role.value,
                "total_cost": role_stats.total_cost,
                "peak_inventory": role_stats.peak_inventory,
                "peak_backlog": role_stats.peak_backlog,
                "weeks_in_backlog": role_stats.weeks_in_backlog,
                "order_variance": role_stats.order_variance,
                "bullwhip_ratio": role_stats.bullwhip_ratio,
                "fill_rate": role_stats.fill_rate,
                "average_order": role_stats.average_order,
            }
            for role, role_stats in stats.per_role.items()
        },
    }


async def persist_finished_game(
    room_code: str, room: dict, engine: GameEngine, stats: GameStats
) -> bool:
    """Persistence seam filled by `14-end-of-game-persistence.md §3.1`.

    Called by `finish_game`, after the Redis lock has been released and
    after `game_finished` has already been emitted -- the debrief never
    waits on the database. Everything here is lock-free: `room` and `engine`
    are values already copied out of Redis by the caller.

    Never raises. A failure -- building the snapshot, writing it, or
    recomputing stats afterwards -- is logged at ERROR naming `room_code`
    and this returns `False`, so `finish_game` records `persisted: false`
    and the debrief the clients already received is untouched (`§3.4`).

    `stats` is accepted only because section 12 froze this signature; the
    snapshot's own `GameStats` is `compute_stats` run again over the same
    `history` and `demand_series` (`§3.5b`) -- the same function, over the
    same data, deliberately not threaded through here to avoid a second copy
    to keep in sync.

    Called and read as a bare module-level name, exactly as
    `state_service.redis_client` is (`09-state-service.md §3`), so a test can
    monkeypatch `app.services.game_service.persist_finished_game` without
    this module having captured a stale reference. The database session is
    opened the same way: through `db_session.SessionLocal()`, read off the
    `app.db.session` module at call time rather than captured by a bare
    `from ... import SessionLocal`, so a test that monkeypatches
    `app.db.session.SessionLocal` reaches this call too.
    """
    try:
        snapshot = build_snapshot(room, engine.config, engine)
    except Exception:
        logger.exception(
            "Building the persistence snapshot for room %s failed.", room_code
        )
        return False

    db = db_session.SessionLocal()
    try:
        try:
            game_id = get_db_service().persist_game(db, snapshot)
        except Exception:
            db.rollback()
            logger.exception("Persisting finished game for room %s failed.", room_code)
            return False

        try:
            get_stats_service().recompute_for_game(db, game_id)
        except Exception:
            # The game itself is already committed; a stats failure here
            # does not undo it, and the next finished game recomputes these
            # users' stats from scratch anyway.
            logger.exception(
                "Recomputing stats after persisting game %s (room %s) failed.",
                game_id,
                room_code,
            )
        return True
    finally:
        db.close()


class GameService:
    """The play loop. See `12-socket-play.md §3`."""

    # --- submission ---------------------------------------------------- #

    async def submit(self, room_code: str, sid: str, week: Any, order: Any) -> None:
        """`submit_order` (`§3.1`). Acquires the lock once; every emit, and
        the cascade into `close_week_if_ready`, happens only after it is
        released (`§2.1`)."""
        state_svc = get_state_service()
        room_code_out = room_code
        order_submitted_payload: dict | None = None
        your_state_target: tuple[str, dict] | None = None

        async with state_svc.lock(room_code):
            room = await state_svc.get_room(room_code)
            if room is None:
                await socket_manager.emit_to_sid(
                    sid, "error", {"message": _ROOM_NOT_FOUND, "code": "NOT_IN_ROOM"}
                )
                return
            room_code_out = room["room_code"]

            if room["state"] == RoomState.PAUSED.value:
                await socket_manager.emit_to_sid(
                    sid, "error", {"message": _GAME_PAUSED_MSG, "code": "GAME_PAUSED"}
                )
                return
            if room["state"] == RoomState.FINISHED.value:
                await socket_manager.emit_to_sid(
                    sid,
                    "error",
                    {"message": _GAME_FINISHED_MSG, "code": "GAME_FINISHED"},
                )
                return
            if room["state"] != RoomState.RUNNING.value:
                await socket_manager.emit_to_sid(
                    sid,
                    "error",
                    {"message": _NOT_RUNNING_MSG, "code": "GAME_NOT_RUNNING"},
                )
                return

            alias = room["sid_to_alias"].get(sid)
            participant = room["participants"].get(alias) if alias else None
            if participant is None:
                await socket_manager.emit_to_sid(
                    sid, "error", {"message": _NOT_IN_ROOM_MSG, "code": "NOT_IN_ROOM"}
                )
                return

            role_raw = participant.get("role")
            if role_raw is None:
                await socket_manager.emit_to_sid(
                    sid, "error", {"message": _NO_ROLE_MSG, "code": "NO_ROLE"}
                )
                return
            if participant.get("is_bot"):
                await socket_manager.emit_to_sid(
                    sid, "error", {"message": _BOT_SEATED_MSG, "code": "NO_ROLE"}
                )
                return
            role = Role(role_raw)

            engine = state_svc.load_engine(room)
            if engine is None or engine.phase is not GamePhase.DECISION:
                await socket_manager.emit_to_sid(
                    sid,
                    "error",
                    {"message": _NOT_RUNNING_MSG, "code": "GAME_NOT_RUNNING"},
                )
                return

            if week != engine.week:
                await socket_manager.emit_to_sid(
                    sid, "error", {"message": _STALE_WEEK_MSG, "code": "STALE_WEEK"}
                )
                await socket_manager.emit_to_sid(
                    sid,
                    "your_state",
                    {"seq": state_svc.next_seq(room), **engine.player_view(role)},
                )
                await state_svc.save_room(room_code_out, room)
                return

            valid, coerced = _coerce_order(order)
            if not valid:
                await socket_manager.emit_to_sid(
                    sid,
                    "error",
                    {"message": _INVALID_ORDER_MSG, "code": "INVALID_ORDER"},
                )
                return

            count_key = (room_code_out, engine.week, role.value)
            count = _submission_counts.get(count_key, 0) + 1
            if count > MAX_RESUBMISSIONS_PER_ROLE_PER_WEEK:
                await socket_manager.emit_to_sid(
                    sid,
                    "error",
                    {"message": _TOO_MANY_MSG, "code": "TOO_MANY_SUBMISSIONS"},
                )
                return
            _submission_counts[count_key] = count

            engine.submit_order(role, coerced)
            state_svc.store_engine(room, engine)

            order_submitted_payload = {
                "seq": state_svc.next_seq(room),
                "week": engine.week,
                "role": role.value,
                "display_name": participant.get("display_name"),
                "is_bot": bool(participant.get("is_bot")),
            }
            your_state_target = (
                sid,
                {"seq": state_svc.next_seq(room), **engine.player_view(role)},
            )

            await state_svc.save_room(room_code_out, room)

        if order_submitted_payload is not None:
            await socket_manager.emit_to_room(
                room_code_out, "order_submitted", order_submitted_payload
            )
        if your_state_target is not None:
            target_sid, payload = your_state_target
            await socket_manager.emit_to_sid(target_sid, "your_state", payload)
            await self.close_week_if_ready(room_code_out)

    # --- bots ------------------------------------------------------------ #

    async def run_bot_decisions(self, room_code: str) -> None:
        """`§3.2`. Plays every bot role due a decision for the currently
        open week, and keeps going through any further weeks an all-bot
        room closes on its own -- iteratively (`§2.1`, acceptance
        criterion 13)."""
        await self._drive(room_code)

    async def _play_bot_round(self, room_code: str) -> bool:
        """One lock cycle: a decision from every bot role that has not yet
        submitted for the open week. Returns whether any bot played.
        Emits `order_submitted` for each, after the lock is released."""
        state_svc = get_state_service()
        room_code_out = room_code
        events: list[dict] = []

        async with state_svc.lock(room_code):
            room = await state_svc.get_room(room_code)
            if room is None:
                return False
            room_code_out = room["room_code"]
            if room["state"] != RoomState.RUNNING.value:
                return False

            engine = state_svc.load_engine(room)
            if engine is None or engine.phase is not GamePhase.DECISION:
                return False

            config = state_svc.load_config(room)
            bots = state_svc.load_bots(room)
            changed = False

            for role in ROLE_ORDER:
                if role in engine.pending_orders:
                    continue
                alias = room["role_to_alias"].get(role.value)
                participant = room["participants"].get(alias) if alias else None
                if participant is None or not participant.get("is_bot"):
                    continue

                bot = bots.get(role)
                if bot is None:
                    bot = bot_for(role, config)

                view = engine.player_view(role)
                bot.observe(view["incoming_order"])
                qty = bot.decide(
                    view["inventory"], view["backlog"], view["supply_line"]
                )
                engine.submit_order(role, qty)
                bots[role] = bot
                changed = True

                events.append(
                    {
                        "seq": state_svc.next_seq(room),
                        "week": engine.week,
                        "role": role.value,
                        "display_name": participant.get("display_name"),
                        "is_bot": True,
                    }
                )

            if changed:
                state_svc.store_engine(room, engine)
                state_svc.store_bots(room, bots)
                await state_svc.save_room(room_code_out, room)

        for payload in events:
            await socket_manager.emit_to_room(room_code_out, "order_submitted", payload)
        return bool(events)

    async def _drive(self, room_code: str) -> None:
        """The iterative bot cascade shared by `run_bot_decisions` and
        `close_week_if_ready`. A plain loop of short lock acquisitions
        (`§2.1`): play whatever bots are due, try to close, and stop the
        moment a round plays nothing or a close does not happen."""
        while True:
            played = await self._play_bot_round(room_code)
            if not played:
                return
            closed, finished, room_after = await self._try_close_week(room_code)
            if not closed:
                return
            if finished:
                if room_after is not None:
                    await self.finish_game(room_code, room_after)
                return

    # --- closing a week ---------------------------------------------------#

    async def close_week_if_ready(self, room_code: str, force: bool = False) -> bool:
        """`§3.3`. Closes the open week once every role has a decision (or
        `force`), emits, and -- if the closed week was not the last --
        drives any further all-bot weeks through the same private loop
        `run_bot_decisions` uses, never by calling that public method
        (`§2.1`: nothing in this section recurses)."""
        closed, finished, room_after = await self._try_close_week(
            room_code, force=force
        )
        if not closed:
            return False
        if finished:
            if room_after is not None:
                await self.finish_game(room_code, room_after)
            return True
        await self._drive(room_code)
        return True

    async def _try_close_week(
        self, room_code: str, force: bool = False
    ) -> tuple[bool, bool, dict | None]:
        """One lock cycle. Returns `(closed, finished, room)`; `room` is the
        document already saved inside this call, handed back so the caller
        can pass it straight to `finish_game` without a second lock or a
        second read (`§2.1`)."""
        state_svc = get_state_service()
        room_code_out = room_code
        week_closed_payload: dict | None = None
        your_week_closed_events: list[tuple[str, dict]] = []
        your_state_events: list[tuple[str, dict]] = []
        host_state_event: tuple[str, dict] | None = None
        closed = False
        finished = False
        saved_room: dict | None = None

        async with state_svc.lock(room_code):
            room = await state_svc.get_room(room_code)
            if room is None:
                return False, False, None
            room_code_out = room["room_code"]
            if room["state"] != RoomState.RUNNING.value:
                return False, False, None

            engine = state_svc.load_engine(room)
            if engine is None or engine.phase is not GamePhase.DECISION:
                return False, False, None
            if not engine.all_orders_in() and not force:
                return False, False, None

            closed_week = engine.week
            records = engine.close_week(force=force)
            closed = True
            finished = engine.phase is GamePhase.FINISHED
            state_svc.store_engine(room, engine)

            next_week = None if finished else engine.week
            awaiting_roles = (
                []
                if finished
                else [r.value for r in ROLE_ORDER if r not in engine.pending_orders]
            )
            week_closed_payload = {
                "seq": state_svc.next_seq(room),
                "week": closed_week,
                "next_week": next_week,
                "awaiting_roles": awaiting_roles,
            }

            for record in records:
                alias = room["role_to_alias"].get(record.role.value)
                target_sid = _sid_for_alias(room, alias)
                if target_sid is not None:
                    your_week_closed_events.append(
                        (
                            target_sid,
                            {
                                "seq": state_svc.next_seq(room),
                                "week": closed_week,
                                "record": _week_record_payload(record),
                            },
                        )
                    )

            if finished:
                room["state"] = RoomState.FINISHED.value
                if not room.get("finished_at"):
                    room["finished_at"] = datetime.now(timezone.utc).isoformat()
            else:
                for role in ROLE_ORDER:
                    alias = room["role_to_alias"].get(role.value)
                    target_sid = _sid_for_alias(room, alias)
                    if target_sid is None:
                        continue
                    your_state_events.append(
                        (
                            target_sid,
                            {
                                "seq": state_svc.next_seq(room),
                                **engine.player_view(role),
                            },
                        )
                    )
                host_sid = room.get("host_sid")
                if isinstance(host_sid, str) and host_sid:
                    host_state_event = (
                        host_sid,
                        {"seq": state_svc.next_seq(room), **engine.host_view()},
                    )

            await state_svc.save_room(room_code_out, room)
            saved_room = room
            _forget_submission_counts(room_code_out, closed_week)

        await socket_manager.emit_to_room(
            room_code_out, "week_closed", week_closed_payload
        )
        for target_sid, payload in your_week_closed_events:
            await socket_manager.emit_to_sid(target_sid, "your_week_closed", payload)
        for target_sid, payload in your_state_events:
            await socket_manager.emit_to_sid(target_sid, "your_state", payload)
        if host_state_event is not None:
            target_sid, payload = host_state_event
            await socket_manager.emit_to_sid(target_sid, "host_state", payload)

        return closed, finished, saved_room

    # --- resync ------------------------------------------------------------#

    async def broadcast_state(self, room_code: str) -> None:
        """`§2.1`: acquires, allocates its sequence numbers, saves, releases,
        then emits. Refreshes every seated human's `your_state` and the
        host's `host_state` -- `resume_game`'s use after lifting a pause.

        It re-reads the room under the lock rather than saving a snapshot the
        caller read earlier. `next_seq` mutates `room["seq"]`, so this method
        has to write, and writing a document that was read before the
        caller released its lock silently reverts anything committed in
        between -- a substitution, a disconnect flag, another week. The
        window is small and the failure leaves no trace, which is exactly the
        combination this section runs two agents for.
        """
        state_svc = get_state_service()
        pending: list[tuple[str, str, dict]] = []

        async with state_svc.lock(room_code):
            room = await state_svc.get_room(room_code)
            if room is None:
                return
            engine = state_svc.load_engine(room)
            if engine is None:
                return
            pending = self._state_refresh_events(room, engine, state_svc)
            await state_svc.save_room(room["room_code"], room)

        for target_sid, event, payload in pending:
            await socket_manager.emit_to_sid(target_sid, event, payload)

    def _state_refresh_events(
        self, room: dict, engine: GameEngine, state_svc: StateService
    ) -> list[tuple[str, str, dict]]:
        """Allocate one `seq` per recipient and build their refresh payload.

        Called inside the lock; the caller saves before emitting any of them.
        """
        events: list[tuple[str, str, dict]] = []
        for role in ROLE_ORDER:
            alias = room["role_to_alias"].get(role.value)
            participant = room["participants"].get(alias) if alias else None
            if participant is None or participant.get("is_bot"):
                continue
            target_sid = _sid_for_alias(room, alias)
            if target_sid is None:
                continue
            events.append(
                (
                    target_sid,
                    "your_state",
                    {"seq": state_svc.next_seq(room), **engine.player_view(role)},
                )
            )

        host_sid = room.get("host_sid")
        if isinstance(host_sid, str) and host_sid:
            events.append(
                (
                    host_sid,
                    "host_state",
                    {"seq": state_svc.next_seq(room), **engine.host_view()},
                )
            )
        return events

    # --- the end -------------------------------------------------------- #

    async def finish_game(self, room_code: str, room: dict) -> None:
        """`§3.8`. Never acquires the lock; persistence never runs under
        one either. A persistence failure must not break the results
        screen (step 5): it is logged, `persisted` is `False`, and the
        clients keep the `game_finished` payload they already received."""
        state_svc = get_state_service()
        engine = state_svc.load_engine(room)
        if engine is None:
            logger.error("finish_game called for room %s with no engine.", room_code)
            return

        room["state"] = RoomState.FINISHED.value
        if not room.get("finished_at"):
            room["finished_at"] = datetime.now(timezone.utc).isoformat()

        weeks_played = engine.weeks_played
        stats = compute_stats(engine.history, engine.demand_series, weeks_played)

        orders_by_role: dict[str, list[int]] = {role.value: [] for role in ROLE_ORDER}
        for record in engine.history:
            orders_by_role[record.role.value].append(record.order)

        finished_payload = {
            "seq": state_svc.next_seq(room),
            "weeks_played": weeks_played,
            "stats": stats_to_payload(stats),
            "demand_series": list(engine.demand_series),
            "orders_by_role": orders_by_role,
        }

        # The debrief goes out BEFORE persistence, not after (§3.8 step 5).
        # Persistence is a database round trip that may be slow and may fail;
        # the results screen must not wait on it and must not be lost with it.
        await socket_manager.emit_to_room(
            room["room_code"], "game_finished", finished_payload
        )

        persisted = False
        try:
            persisted = await persist_finished_game(
                room["room_code"], room, engine, stats
            )
        except Exception:
            logger.exception("Persisting finished game %s failed.", room["room_code"])
            persisted = False

        room["persisted"] = bool(persisted)
        await state_svc.save_room(room["room_code"], room)


_game_service: GameService | None = None


def get_game_service() -> GameService:
    """Module-level singleton, mirroring `state_service.get_state_service()`."""
    global _game_service
    if _game_service is None:
        _game_service = GameService()
    return _game_service
