"""Health endpoints: a dependency-free probe and a deep backing-service probe."""

from __future__ import annotations

import asyncio
import logging

import anyio.to_thread
import redis.asyncio as aioredis
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from ...config import settings
from ...db.session import engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])

_CHECK_TIMEOUT_SECONDS = 5.0


class HealthResponse(BaseModel):
    """Liveness answer. Deliberately free of any backing-service state."""

    status: str


class DeepHealthResponse(BaseModel):
    """Readiness answer. Always returned with HTTP 200, degraded or not."""

    status: str
    database: bool
    #: Reachability of Redis. `True` when it is switched off, because a
    #: dependency that is not in use cannot be unhealthy -- read it with
    #: `state_backend`, which says whether it is in use at all.
    redis: bool
    #: `"redis"` or `"memory"`. Which store holds live room state.
    state_backend: str


def _ping_database() -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


async def _database_reachable() -> bool:
    try:
        await asyncio.wait_for(
            anyio.to_thread.run_sync(_ping_database), timeout=_CHECK_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 — any failure means "not reachable"
        logger.warning("Deep health: database unreachable (%s).", type(exc).__name__)
        return False
    return True


async def _redis_reachable() -> bool:
    """True when Redis is reachable, or when it is deliberately not in use.

    Returning `False` for "switched off" would pin `/health/deep` to
    `degraded` forever on a single-instance deployment that never wanted
    Redis -- a readiness probe that always says "not ready" is a probe
    nobody reads.
    """
    if not settings.REDIS_ENABLED:
        return True
    if not settings.REDIS_URL:
        logger.warning("Deep health: REDIS_ENABLED is true but REDIS_URL is not set.")
        return False
    client = None
    try:
        client = aioredis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=_CHECK_TIMEOUT_SECONDS,
            socket_timeout=_CHECK_TIMEOUT_SECONDS,
        )
        await asyncio.wait_for(client.ping(), timeout=_CHECK_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — any failure means "not reachable"
        logger.warning("Deep health: Redis unreachable (%s).", type(exc).__name__)
        return False
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 — a probe must not fail on cleanup
                logger.warning("Deep health: closing the Redis probe failed.")
    return True


@router.get("", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe. Touches neither the database nor Redis.

    The frontend polls this as a cold-start wake-up probe, so a health check
    that needed the database would turn a slow database into an app that
    appears dead.
    """
    return HealthResponse(status="ok")


@router.get("/deep", response_model=DeepHealthResponse)
async def health_deep() -> DeepHealthResponse:
    """Readiness probe. Reports each backing service, always with HTTP 200."""
    database_ok = await _database_reachable()
    redis_ok = await _redis_reachable()
    return DeepHealthResponse(
        status="ok" if database_ok and redis_ok else "degraded",
        database=database_ok,
        redis=redis_ok,
        state_backend="redis" if settings.REDIS_ENABLED else "memory",
    )
