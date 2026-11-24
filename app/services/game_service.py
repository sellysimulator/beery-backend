"""The play loop.

It works alongside `app/sockets/handlers/play.py`. This module holds the two
rules that dominate the play loop: **the server is authoritative** and
**redaction happens before send** (spec §8.8) -- every broadcast built here is
assembled key by key, never by spreading a room document or a view into an
`emit_to_room` payload.

The locking contract is obeyed throughout: every method that reads,
mutates and saves room state does all three inside one
``state_svc.lock(room_code)`` acquisition, then returns to its caller, which
emits and chains into any further ``GameService`` call only after that lock
has been released. The bot cascade -- a submit closing a week, which plays
the bots, which may close the next week, and so on -- is driven by the
private ``_drive`` loop, a plain ``while``, never by two public methods
calling each other. That is what keeps a 104-week all-bot game from
recursing.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ..core.bot import bot_for
from ..core.enums import ROLE_ORDER, Role, RoomState
from ..core.game_engine import (
    GameEngine,
    GamePhase,
    OrderOutOfRangeError,
    WeekRecord,
)
from ..core.stats import GameStats, compute_stats
from ..db import session as db_session
from ..models.game import Game
from ..sockets.manager import socket_manager
from .db_service import build_snapshot, get_db_service
from .state_service import ROOM_TTL_SECONDS, StateService, get_state_service
from .stats_service import get_stats_service

logger = logging.getLogger(__name__)

__all__ = [
    "PERSIST_RETRY_DELAYS_SECONDS",
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
_BOT_SEATED_MSG = "A bot is playing your role. Take it back to order again."
_STALE_WEEK_MSG = "That week is no longer open."
_INVALID_ORDER_MSG = "Order must be a whole number."
_TOO_MANY_MSG = "Too many submissions for this week."

#: Seconds before each retry of a failed end-of-game write: three more
#: attempts after the first, in the background, each off the event loop and
#: with no room lock held while it writes or sleeps.
PERSIST_RETRY_DELAYS_SECONDS: tuple[float, ...] = (2.0, 10.0, 30.0)

# Rooms whose end-of-game write is running or waiting for a retry, in this
# process -- the "already in flight" test that keeps a rejoin from starting a
# second write beside the first. Process-local, like the counters below.
_persisting: set[str] = set()
# The event loop keeps only a weak reference to a task; these are the strong
# ones that keep a pending retry alive until it ends.
_retry_tasks: set[asyncio.Task[None]] = set()

# Resubmission counters, per (room_code, week, role): the count and the
# `time.monotonic()` of that week's first counted submission. Deliberately
# process-local, not part of the frozen room document (no new top-level key
# may be added without a schema bump). Socket.IO connections are sticky
# for their lifetime, so every `submit_order` for one sid is handled by the
# same worker process that would have to enforce this cap anyway; the guard
# this exists for is a single client looping submissions, not a
# cross-process invariant (spec §12.1). Entries are dropped for a week as
# soon as it closes and for a whole room when its game finishes (ended early
# included). A room that expires or is deleted mid-week closes nothing, so
# entries older than a room can live without a save are swept as well
# (`_sweep_submission_counts`): this cannot grow without bound.
_submission_counts: dict[tuple[str, int, str], tuple[int, float]] = {}
_SUBMISSION_SWEEP_EVERY_SECONDS = 600.0
_submissions_swept_at = float("-inf")


def _forget_submission_counts(room_code: str, week: int | None = None) -> None:
    """Drop the counters of one week of a room, or of every week when
    `week` is None."""
    stale = [
        key
        for key in _submission_counts
        if key[0] == room_code and (week is None or key[1] == week)
    ]
    for key in stale:
        _submission_counts.pop(key, None)


def _sweep_submission_counts(now: float) -> None:
    """At most once per `_SUBMISSION_SWEEP_EVERY_SECONDS`, drop every counter
    whose first submission is older than `ROOM_TTL_SECONDS`.

    Such a week belongs to a room that expired or was deleted while the week
    was open; if the room is somehow still alive, forgetting its counter only
    restarts its cap."""
    global _submissions_swept_at
    if now - _submissions_swept_at < _SUBMISSION_SWEEP_EVERY_SECONDS:
        return
    _submissions_swept_at = now
    stale = [
        key
        for key, (_count, first) in _submission_counts.items()
        if now - first > ROOM_TTL_SECONDS
    ]
    for key in stale:
        _submission_counts.pop(key, None)


def _record_submission(key: tuple[str, int, str], count: int) -> None:
    now = time.monotonic()
    _sweep_submission_counts(now)
    first = _submission_counts.get(key, (0, now))[1]
    _submission_counts[key] = (count, first)


def _coerce_order(value: Any) -> tuple[bool, int]:
    """Accept an `int`, or a `float` that is exactly integral.

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


def _host_refresh_after_submission(
    room: dict, engine: GameEngine, state_svc: StateService
) -> tuple[str, dict] | None:
    """A fresh `host_state` for the host's submission tracker, or `None`.

    The host console reads every "decided" mark from `host_state`, so without
    this one the tracker shows nobody as decided until the week closes.

    It is sent even when this submission was the last one the week needed.
    Do not leave it out then on the reasoning that the close which follows
    sends the host a state for the new week anyway: the final week's close
    finishes the game and sends no new week, so the host's last state would
    be the one from before the last submission and the console would end the
    game showing that player as "Still deciding". One extra `host_state` a
    week is the price of a rule with no exception in it. Called inside the
    lock, so the `seq` is allocated with the others; the caller emits it
    after the lock.
    """
    host_sid = room.get("host_sid")
    if not (isinstance(host_sid, str) and host_sid):
        return None
    return host_sid, {"seq": state_svc.next_seq(room), **engine.host_view()}


def _week_record_payload(record: WeekRecord) -> dict:
    """A JSON-safe `WeekRecord`, for the one sid entitled to see it."""
    payload = asdict(record)
    payload["role"] = record.role.value
    return payload


def stats_to_payload(stats: GameStats) -> dict:
    """The one serialiser for `GameStats`.

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
) -> str | None:
    """Persistence seam: writes the finished game to PostgreSQL (spec §10.2).

    Called by `finish_game`, after the Redis lock has been released and
    after `game_finished` has already been emitted -- the debrief never
    waits on the database. Everything here is lock-free: `room` and `engine`
    are values already copied out of Redis by the caller.

    Never raises. A failure -- building the snapshot, writing it, or
    recomputing stats afterwards -- is logged at ERROR naming `room_code`
    and this returns `None`, so `finish_game` records `persisted: false`
    and the debrief the clients already received is untouched.
    On success it returns the game's `public_id`, which `finish_game`
    broadcasts so clients can link to the permanent results URL.

    `stats` is accepted only because this signature is frozen; the
    snapshot's own `GameStats` is `compute_stats` run again over the same
    `history` and `demand_series` -- the same function, over the
    same data, deliberately not threaded through here to avoid a second copy
    to keep in sync.

    The work is synchronous SQLAlchemy, so it runs in a worker thread
    (`asyncio.to_thread`), never on the event loop: a Neon compute waking from
    suspend, or a slow write, would otherwise freeze every room in the
    process for as long as it takes.

    Called and read as a bare module-level name, exactly as
    `state_service.redis_client` is, so a test can
    monkeypatch `app.services.game_service.persist_finished_game` without
    this module having captured a stale reference. The database session is
    opened the same way: through `db_session.SessionLocal()`, read off the
    `app.db.session` module at call time rather than captured by a bare
    `from ... import SessionLocal`, so a test that monkeypatches
    `app.db.session.SessionLocal` reaches this call too.
    """
    return await asyncio.to_thread(_persist_finished_game_sync, room_code, room, engine)


def _persist_finished_game_sync(
    room_code: str, room: dict, engine: GameEngine
) -> str | None:
    """`persist_finished_game`'s body, run in a worker thread. Never raises."""
    try:
        snapshot = build_snapshot(room, engine.config, engine)
    except Exception:
        logger.exception(
            "Building the persistence snapshot for room %s failed.", room_code
        )
        return None

    db = db_session.SessionLocal()
    try:
        try:
            game_id = get_db_service().persist_game(db, snapshot)
            # Read before the stats recompute, whose failure could leave the
            # session needing a rollback.
            public_id = db.execute(
                select(Game.public_id).where(Game.id == game_id)
            ).scalar_one()
        except Exception:
            db.rollback()
            logger.exception("Persisting finished game for room %s failed.", room_code)
            return None

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
        return public_id
    finally:
        db.close()


class GameService:
    """The play loop (spec §7, §11.2)."""

    # --- submission ---------------------------------------------------- #

    async def submit(self, room_code: str, sid: str, week: Any, order: Any) -> None:
        """`submit_order`. Acquires the lock once; every emit, and
        the cascade into `close_week_if_ready`, happens only after it is
        released."""
        state_svc = get_state_service()
        room_code_out = room_code
        order_submitted_payload: dict | None = None
        your_state_target: tuple[str, dict] | None = None
        host_state_target: tuple[str, dict] | None = None

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
            # Refused, never clamped: an order outside the game's bounds is
            # sent back with the bounds in the message (the order form reads
            # the same two numbers from `your_state`), records nothing and
            # spends no resubmission.
            low, high = engine.order_bounds()
            if not low <= coerced <= high:
                await socket_manager.emit_to_sid(
                    sid,
                    "error",
                    {
                        "message": str(OrderOutOfRangeError(low, high)),
                        "code": "INVALID_ORDER",
                    },
                )
                return

            count_key = (room_code_out, engine.week, role.value)
            count = _submission_counts.get(count_key, (0, 0.0))[0] + 1
            if count > MAX_RESUBMISSIONS_PER_ROLE_PER_WEEK:
                await socket_manager.emit_to_sid(
                    sid,
                    "error",
                    {"message": _TOO_MANY_MSG, "code": "TOO_MANY_SUBMISSIONS"},
                )
                return
            _record_submission(count_key, count)

            engine.submit_order(role, coerced)
            state_svc.store_engine(room, engine)

            order_submitted_payload = {
                "seq": state_svc.next_seq(room),
                "week": engine.week,
                "role": role.value,
                "display_name": participant.get("display_name"),
                "is_bot": bool(participant.get("is_bot")),
            }
            # Per-round: no history. Nothing was added to it by a submission,
            # and the client already holds every closed week's record.
            your_state_target = (
                sid,
                {
                    "seq": state_svc.next_seq(room),
                    **engine.player_view(role, with_history=False),
                },
            )
            host_state_target = _host_refresh_after_submission(room, engine, state_svc)

            await state_svc.save_room(room_code_out, room)

        if order_submitted_payload is not None:
            await socket_manager.emit_to_room(
                room_code_out, "order_submitted", order_submitted_payload
            )
        if host_state_target is not None:
            host_sid, host_payload = host_state_target
            await socket_manager.emit_to_sid(host_sid, "host_state", host_payload)
        if your_state_target is not None:
            target_sid, payload = your_state_target
            await socket_manager.emit_to_sid(target_sid, "your_state", payload)
            await self.close_week_if_ready(room_code_out)

    # --- bots ------------------------------------------------------------ #

    async def run_bot_decisions(self, room_code: str) -> None:
        """Plays every bot role due a decision for the currently
        open week, and keeps going through any further weeks an all-bot
        room closes on its own -- iteratively.

        Only a `RUNNING` room plays: a call while the room is paused does
        nothing, which is why `resume_game` calls `resume_play` below."""
        await self._drive(room_code)

    async def resume_play(self, room_code: str) -> None:
        """What a room owes the moment it is running again.

        Plays every bot seat that has no order for the open week, then closes
        the week if that completed it. A bot swapped in while the room was
        paused (the usual sequence: a player drops, the room auto-pauses, the
        host swaps in a bot, then resumes) is asked to play at the swap, but
        `run_bot_decisions` does nothing in a paused room -- so without this
        the week waited on a bot nobody would ever ask again, forever.

        The close after the drive covers the one case the drive cannot see: a
        week whose four orders were all in before the pause but which never
        closed. Both calls take their own short locks, like every other
        cascade here, and both are no-ops for a room with nothing
        due.
        """
        await self._drive(room_code)
        await self.close_week_if_ready(room_code)

    async def _play_bot_round(self, room_code: str) -> bool:
        """One lock cycle: a decision from every bot role that has not yet
        submitted for the open week. Returns whether any bot played.
        Emits `order_submitted` for each, after the lock is released, then a
        fresh `host_state` while the week is still waiting on a human."""
        state_svc = get_state_service()
        room_code_out = room_code
        events: list[dict] = []
        host_state_target: tuple[str, dict] | None = None

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

                view = engine.player_view(role, with_history=False)
                bot.observe(view["incoming_order"])
                qty = bot.decide(
                    view["inventory"], view["backlog"], view["supply_line"]
                )
                # The engine refuses an out-of-range order rather than
                # clamping it, and a bot has nobody to show a refusal to: its
                # own figure is capped here, the one place it is.
                # `decide()` already floors at 0.
                qty = min(qty, engine.order_bounds()[1])
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
                host_state_target = _host_refresh_after_submission(
                    room, engine, state_svc
                )
                await state_svc.save_room(room_code_out, room)

        for payload in events:
            await socket_manager.emit_to_room(room_code_out, "order_submitted", payload)
        if host_state_target is not None:
            host_sid, host_payload = host_state_target
            await socket_manager.emit_to_sid(host_sid, "host_state", host_payload)
        return bool(events)

    async def _drive(self, room_code: str) -> None:
        """The iterative bot cascade shared by `run_bot_decisions` and
        `close_week_if_ready`. A plain loop of short lock acquisitions:
        play whatever bots are due, try to close, and stop the
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
        """Closes the open week once every role has a decision (or
        `force`), emits, and -- if the closed week was not the last --
        drives any further all-bot weeks through the same private loop
        `run_bot_decisions` uses, never by calling that public method
        (nothing in the play loop recurses)."""
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
        can pass it to `finish_game` (after this lock is released) as the
        snapshot it persists; `finish_game` takes its own lock for its saves."""
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
                    # The closed week's record went out in `your_week_closed`
                    # just above; this state leaves the history to it.
                    your_state_events.append(
                        (
                            target_sid,
                            {
                                "seq": state_svc.next_seq(room),
                                **engine.player_view(role, with_history=False),
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
        """Acquires, allocates its sequence numbers, saves, releases,
        then emits. Refreshes every seated human's `your_state` and the
        host's `host_state` -- `resume_game`'s use after lifting a pause.

        It re-reads the room under the lock rather than saving a snapshot the
        caller read earlier. `next_seq` mutates `room["seq"]`, so this method
        has to write, and writing a document that was read before the
        caller released its lock silently reverts anything committed in
        between -- a substitution, a disconnect flag, another week. The
        window is small and the failure leaves no trace.
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
            pending = self.state_refresh_events(room, engine, state_svc)
            await state_svc.save_room(room["room_code"], room)

        for target_sid, event, payload in pending:
            await socket_manager.emit_to_sid(target_sid, event, payload)

    def state_refresh_events(
        self, room: dict, engine: GameEngine, state_svc: StateService
    ) -> list[tuple[str, str, dict]]:
        """Allocate one `seq` per recipient and build their refresh payload:
        `your_state` for every connected human seat, `host_state` for the host.

        Called inside the lock; the caller saves before emitting any of them.
        `broadcast_state` and `end_game_early` both use it.
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
        """Two short lock cycles around a lock-free persistence call.

        The caller must already have released the room lock (it is not
        reentrant): `room` is the document it saved under that lock, and is
        used here only as the read-only snapshot handed to persistence.

        1. Under the lock: re-read the room, mint `game_finished`'s `seq`
           and save. Emit after release.
        2. No lock: persist (a database write that may be slow or fail).
        3. Under the lock: re-read the room again, set `persisted`, mint
           `game_persisted`'s `seq` and save. Emit after release.

        Each save is of the document just read under the lock, never of the
        caller's copy, so a mutation made while persistence runs (a
        `request_state` minting a `seq`, a disconnect) is kept, not
        overwritten, and no two events share a `seq`.

        A persistence failure must not break the results screen: it is
        logged, `persisted` is `False`, the clients keep the `game_finished`
        payload they already received, and the write is retried in the
        background (`PERSIST_RETRY_DELAYS_SECONDS`)."""
        state_svc = get_state_service()
        engine = state_svc.load_engine(room)
        if engine is None:
            logger.error("finish_game called for room %s with no engine.", room_code)
            return
        room_code_out = room["room_code"]

        weeks_played = engine.weeks_played
        stats = compute_stats(engine.history, engine.demand_series, weeks_played)

        orders_by_role: dict[str, list[int]] = {role.value: [] for role in ROLE_ORDER}
        for record in engine.history:
            orders_by_role[record.role.value].append(record.order)

        async with state_svc.lock(room_code_out):
            current = await state_svc.get_room(room_code_out)
            if current is None:
                logger.error(
                    "finish_game: room %s vanished before game_finished.",
                    room_code_out,
                )
                return
            # The callers already saved FINISHED; this only guards a caller
            # that did not.
            current["state"] = RoomState.FINISHED.value
            if not current.get("finished_at"):
                current["finished_at"] = datetime.now(timezone.utc).isoformat()
            finished_payload = {
                "seq": state_svc.next_seq(current),
                "weeks_played": weeks_played,
                "stats": stats_to_payload(stats),
                "demand_series": list(engine.demand_series),
                "orders_by_role": orders_by_role,
            }
            await state_svc.save_room(room_code_out, current)
            snapshot = current
        # No week of this game will close again, so none of its counters will
        # be dropped at a close (an ended-early game abandons its open week).
        _forget_submission_counts(room_code_out)

        # The debrief goes out BEFORE persistence, not after (spec §10.2).
        # Persistence is a database round trip that may be slow and may fail;
        # the results screen must not wait on it and must not be lost with it.
        await socket_manager.emit_to_room(
            room_code_out, "game_finished", finished_payload
        )

        if room_code_out in _persisting:
            return
        _persisting.add(room_code_out)
        await self._persist(room_code_out, snapshot, engine, stats)

    async def persist_if_unsaved(self, room_code: str) -> None:
        """Write a finished game nobody has saved yet (spec §10.2).

        Called after the host or a player (re)joins, or asks for state in, a
        FINISHED room: when the room is still `persisted: false` and no write
        for it is running or waiting to retry in this process, the write is
        started again -- one attempt now, then the background retries. A no-op
        otherwise. `room_code` is the room's canonical (upper-case) code.
        """
        if room_code in _persisting:
            return
        found = await self._unsaved_finished_game(room_code)
        if found is None or room_code in _persisting:
            return
        _persisting.add(room_code)
        await self._persist(room_code, *found)

    async def _unsaved_finished_game(
        self, room_code: str
    ) -> tuple[dict, GameEngine, GameStats] | None:
        """The room, engine and stats of a FINISHED game still unsaved, or
        None. A plain read: nothing is written, so no lock."""
        state_svc = get_state_service()
        room = await state_svc.get_room(room_code)
        if (
            room is None
            or room["state"] != RoomState.FINISHED.value
            or room.get("persisted")
        ):
            return None
        engine = state_svc.load_engine(room)
        if engine is None:
            return None
        stats = compute_stats(engine.history, engine.demand_series, engine.weeks_played)
        return room, engine, stats

    async def _persist(
        self, room_code: str, snapshot: dict, engine: GameEngine, stats: GameStats
    ) -> None:
        """One attempt now; after a failure, the retries in the background.

        The caller has put `room_code` in `_persisting` before this first
        attempt; `_persist_once` takes it out when the write succeeds or the
        last attempt fails, before either outcome is announced."""
        try:
            saved = await self._persist_once(
                room_code,
                snapshot,
                engine,
                stats,
                last=not PERSIST_RETRY_DELAYS_SECONDS,
            )
        except BaseException:
            _persisting.discard(room_code)
            raise
        if saved or not PERSIST_RETRY_DELAYS_SECONDS:
            return
        task = asyncio.create_task(self._retry_persist(room_code))
        _retry_tasks.add(task)
        task.add_done_callback(_retry_tasks.discard)

    async def _retry_persist(self, room_code: str) -> None:
        """The background retries: sleep, re-read the room, write. Stops early
        once the game is saved or the room is gone.

        The room stays in `_persisting` through every sleep; the last attempt
        takes it out before its outcome is announced (`_persist_once`), so a
        rejoin arriving meanwhile can start a new attempt. Nothing here
        removes it after that: by then it may be that new attempt's."""
        try:
            for attempt, delay in enumerate(PERSIST_RETRY_DELAYS_SECONDS, start=1):
                await asyncio.sleep(delay)
                found = await self._unsaved_finished_game(room_code)
                if found is None:
                    _persisting.discard(room_code)
                    return
                last = attempt == len(PERSIST_RETRY_DELAYS_SECONDS)
                if await self._persist_once(room_code, *found, last=last):
                    return
        except BaseException:
            _persisting.discard(room_code)
            raise
        logger.error(
            "Gave up persisting the finished game of room %s after %d retries.",
            room_code,
            len(PERSIST_RETRY_DELAYS_SECONDS),
        )

    async def _persist_once(
        self,
        room_code: str,
        snapshot: dict,
        engine: GameEngine,
        stats: GameStats,
        *,
        last: bool,
    ) -> bool:
        """One write, with no lock held, then one short lock to record the
        outcome and, on success, `game_persisted`. True when saved.

        When the write succeeded or this was the `last` attempt, the room
        leaves `_persisting` BEFORE the outcome is announced, so a rejoin
        arriving while it goes out is not mistaken for one beside a write in
        flight.

        The write is idempotent on (`room_code`, `started_at`), so a retry of
        a write that did land returns the same game's `public_id`."""
        state_svc = get_state_service()
        public_id: str | None = None
        try:
            public_id = await persist_finished_game(room_code, snapshot, engine, stats)
        except Exception:
            logger.exception("Persisting finished game %s failed.", room_code)
            public_id = None

        persisted = isinstance(public_id, str)
        persisted_payload: dict | None = None
        async with state_svc.lock(room_code):
            current = await state_svc.get_room(room_code)
            if current is None:
                # Expired or deleted while the write ran: nothing to flag,
                # and saving the stale snapshot would resurrect the room.
                logger.warning(
                    "finish_game: room %s vanished before persisted was saved.",
                    room_code,
                )
            else:
                current["persisted"] = persisted
                if persisted:
                    persisted_payload = {
                        "seq": state_svc.next_seq(current),
                        "game_id": public_id,
                    }
                await state_svc.save_room(room_code, current)

        if persisted or last:
            _persisting.discard(room_code)
        # Only now does the permanent results URL exist. Clients already hold
        # the debrief from `game_finished`; this just lets them link to it.
        if persisted_payload is not None:
            await socket_manager.emit_to_room(
                room_code, "game_persisted", persisted_payload
            )
        return persisted


_game_service: GameService | None = None


def get_game_service() -> GameService:
    """Module-level singleton, mirroring `state_service.get_state_service()`."""
    global _game_service
    if _game_service is None:
        _game_service = GameService()
    return _game_service
