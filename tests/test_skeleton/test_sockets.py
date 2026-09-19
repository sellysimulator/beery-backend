"""``app/sockets/manager.py`` -- the Socket.IO server (01 §2, §3.3).

Acceptance criteria 5 and 10.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import application
from app.sockets.manager import SocketManager, sio, socket_manager

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Printed by a fresh interpreter so the DEBUG-true half of AC 10 can be
# observed: python-socketio sets each logger's level only while it is still
# NOTSET, so the first AsyncServer built in a process wins and no second
# server constructed in *this* process would prove anything (§3.3).
LEVEL_PROBE = (
    "import json\n"
    "from app.sockets.manager import sio\n"
    "print(json.dumps([sio.logger.level, sio.eio.logger.level]))\n"
)


def _probe_logger_levels(debug: str) -> list[int]:
    env = dict(os.environ)
    env["DEBUG"] = debug
    proc = subprocess.run(
        [sys.executable, "-c", LEVEL_PROBE],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --- AC 5 -------------------------------------------------------------------


def test_socketio_polling_handshake_completes():
    """AC 5: a socket.io client can complete a handshake against
    ``application``.  This is the engine.io v4 opening handshake, which is the
    first thing any client does."""
    with TestClient(application) as c:
        response = c.get("/socket.io/?EIO=4&transport=polling")
    assert response.status_code == 200
    body = response.text
    assert body.startswith("0{"), body[:120]
    opening = json.loads(body[1:])
    assert opening["sid"]
    assert "websocket" in opening["upgrades"]


def test_socketio_path_is_mounted_where_clients_expect_it():
    """§3.3: ``socketio_path="socket.io"`` -- the exact value the ASGI mount
    needs for a correct HTTP 101 upgrade."""
    with TestClient(application) as c:
        missing = c.get("/socket.io/?EIO=4&transport=polling")
        assert missing.status_code == 200
        elsewhere = c.get("/socketio/?EIO=4&transport=polling")
        assert elsewhere.status_code != 200


# --- AC 10 ------------------------------------------------------------------


def test_socketio_loggers_are_quiet_when_debug_is_false():
    """AC 10 / 00-conventions.md §7: engine.io logs every packet's full
    payload -- including player ids -- at INFO.  With ``DEBUG`` false both
    loggers sit at ``ERROR`` and the socket.io one is not enabled for INFO.

    ``tests/conftest.py`` pins ``DEBUG=False`` for the whole suite, so this is
    the app's own server, in a process where it is the first one built."""
    assert settings.DEBUG is False
    assert sio.logger.level == logging.ERROR
    assert sio.logger.isEnabledFor(logging.INFO) is False
    assert sio.eio.logger.level == logging.ERROR


def test_socketio_loggers_are_verbose_when_debug_is_true():
    """AC 10, second half: in a **fresh interpreter** with ``DEBUG=true`` in
    the environment, both loggers are at INFO.

    A fresh interpreter is required, not a preference: each level is set only
    while it is still ``NOTSET`` (§3.3), so a second ``AsyncServer`` built in
    this process with different flags would change nothing and assert
    nothing.  This is also what fails if the flags are hardcoded rather than
    read from ``settings.DEBUG``."""
    assert _probe_logger_levels("true") == [logging.INFO, logging.INFO]


def test_socketio_loggers_stay_quiet_in_a_fresh_interpreter_by_default():
    """AC 10: the same probe with ``DEBUG=false`` -- the pair of runs is what
    shows the levels *follow* the setting rather than happening to match it."""
    assert _probe_logger_levels("false") == [logging.ERROR, logging.ERROR]


def test_socket_cors_is_not_a_wildcard():
    """AC 11: "Keep in sync with sockets/manager.py" -- the socket server may
    not open up an origin the REST app refuses."""
    eio = getattr(sio, "eio", None)
    origins = getattr(eio, "cors_allowed_origins", None)
    assert origins is not None
    assert origins != "*"
    assert "*" not in origins


# --- frozen public surface (§2) --------------------------------------------


def test_socket_manager_singleton_surface():
    assert isinstance(socket_manager, SocketManager)
    for name in ("sid_to_room", "sid_to_alias", "sid_to_identity"):
        assert isinstance(getattr(socket_manager, name), dict)


@pytest.mark.parametrize(
    "name",
    [
        "connect",
        "disconnect",
        "join_room",
        "leave_room",
        "emit_to_room",
        "emit_to_sid",
    ],
)
def test_socket_manager_methods_are_coroutines(name):
    assert inspect.iscoroutinefunction(getattr(SocketManager, name))
