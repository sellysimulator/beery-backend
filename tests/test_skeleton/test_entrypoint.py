"""``uvicorn app.main:application`` -- the only supported entry point (01 §2).

Acceptance criteria 2 and 5, exercised against a real server process rather
than an in-process transport, because that is what the criteria say: the app
*starts*, and a socket.io client *completes a handshake* against it.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOOT_TIMEOUT = 45.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """A real ``uvicorn app.main:application`` process."""
    if shutil.which(sys.executable) is None:  # pragma: no cover - defensive
        pytest.skip("no interpreter to launch")
    pytest.importorskip("uvicorn")

    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:application",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--http",
            "h11",
            "--proxy-headers",
        ],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + BOOT_TIMEOUT
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                pytest.fail(
                    "uvicorn app.main:application exited during startup:\n" + output
                )
            try:
                with urllib.request.urlopen(base + "/", timeout=2):
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)
        else:
            pytest.fail(f"uvicorn did not serve / within {BOOT_TIMEOUT}s")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=15)


# --- AC 2 -------------------------------------------------------------------


def test_uvicorn_entrypoint_serves_the_root_route(live_server):
    """AC 2: ``uvicorn app.main:application`` starts and serves ``GET /``."""
    from app.config import settings

    with urllib.request.urlopen(live_server + "/", timeout=10) as response:
        assert response.status == 200
        body = json.loads(response.read().decode())
    assert body == {
        "app": settings.APP_NAME,
        "version": settings.VERSION,
        "status": "ok",
    }


def test_uvicorn_entrypoint_serves_health(live_server):
    """AC 3, through the real server."""
    with urllib.request.urlopen(live_server + "/api/v1/health", timeout=10) as response:
        assert response.status == 200
        assert json.loads(response.read().decode()) == {"status": "ok"}


# --- AC 5 -------------------------------------------------------------------


def test_socketio_client_completes_a_handshake(live_server):
    """AC 5: a socket.io client can complete a handshake against
    ``application``."""
    import socketio

    client = socketio.Client()
    try:
        client.connect(live_server, wait_timeout=20)
        assert client.connected
        assert client.get_sid()
    finally:
        if client.connected:
            client.disconnect()
