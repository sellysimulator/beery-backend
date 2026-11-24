"""The end-of-game write runs off the event loop (spec §10.2).

``persist_finished_game`` is synchronous SQLAlchemy behind an ``async`` seam.
Run on the loop, a Neon compute waking from suspend would freeze every room
in the process until the database answered. No database is needed here: the
snapshot builder, the write, the session and the stats recompute are faked
at the module names the seam reads at call time.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.game_service as game_service_module
from app.db import session as db_session

pytestmark = pytest.mark.asyncio


class _FakeSession:
    def execute(self, _statement: Any) -> SimpleNamespace:
        return SimpleNamespace(scalar_one=lambda: "f" * 32)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


async def test_the_write_runs_in_a_worker_thread_while_the_loop_keeps_going(
    monkeypatch,
):
    """The write blocks its thread until the loop releases it. Were the write
    on the loop, nothing could set ``release`` until it gave up waiting, and
    the persist would fail instead of returning the ``public_id``."""
    started = threading.Event()
    release = threading.Event()
    seen: dict[str, Any] = {}

    def persist_game(_db: Any, _snapshot: Any) -> int:
        seen["thread"] = threading.get_ident()
        started.set()
        seen["released"] = release.wait(timeout=5)
        return 7

    monkeypatch.setattr(game_service_module, "build_snapshot", lambda *a: object())
    monkeypatch.setattr(
        game_service_module,
        "get_db_service",
        lambda: SimpleNamespace(persist_game=persist_game),
    )
    monkeypatch.setattr(
        game_service_module,
        "get_stats_service",
        lambda: SimpleNamespace(recompute_for_game=lambda _db, _id: None),
    )
    monkeypatch.setattr(db_session, "SessionLocal", _FakeSession)

    task = asyncio.create_task(
        game_service_module.persist_finished_game(
            "ABCDEF", {}, SimpleNamespace(config=None), None  # type: ignore[arg-type]
        )
    )
    assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=6)
    release.set()  # only reachable while the write is waiting in its thread

    assert await asyncio.wait_for(task, timeout=6) == "f" * 32
    assert seen["released"] is True
    assert seen["thread"] != threading.get_ident()
