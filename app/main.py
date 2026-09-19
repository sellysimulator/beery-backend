"""FastAPI + Socket.IO entry point.

Run with:  uvicorn app.main:application --host 0.0.0.0 --port 8080

This module is an assembly point and names no individual endpoint module and no
individual socket handler module (00-decisions.md D19): both packages discover
their own members, so a later section adds behaviour by dropping in a file.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .api.v1 import all_routers
from .config import settings
from .core.checks import run_startup_checks
from .sockets import handlers  # noqa: F401  — discovery registers socket events
from .sockets.manager import sio

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class RootResponse(BaseModel):
    """Service banner returned by `GET /`."""

    app: str
    version: str
    status: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown hook.

    Replaces `@app.on_event("startup")`, which FastAPI has deprecated and which
    emits a DeprecationWarning on every boot. It names no individual check: a
    section registers one by dropping a module into `app/core/checks/`.
    """
    logger.info("Starting %s version %s.", settings.APP_NAME, settings.VERSION)
    run_startup_checks()
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    debug=settings.DEBUG,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for _router in all_routers():
    app.include_router(_router, prefix="/api/v1")


@app.get("/", response_model=RootResponse)
async def root() -> RootResponse:
    return RootResponse(
        app=settings.APP_NAME,
        version=settings.VERSION,
        status="ok",
    )


# The ASGI mount needs this exact socketio_path for a correct HTTP 101 upgrade.
application = socketio.ASGIApp(
    socketio_server=sio,
    other_asgi_app=app,
    socketio_path="socket.io",
)
