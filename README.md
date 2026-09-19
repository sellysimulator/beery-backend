# Beery — Backend

FastAPI + Socket.IO server for the Beer Distribution Game. Python 3.11, MySQL for the durable
record, Redis for live room state.

Built section by section against `../docs/plan/`. Each section document is the contract; the
plan's `BUILD-LOG.md` records what is done and every specification defect found along the way.

---

## Prerequisites

- **Python 3.11** — create the venv with `python3.11 -m venv venv`, never bare `python3`. On a
  machine whose default `python3` has moved on, the bare form silently builds the venv on a
  different interpreter than `python:3.11-slim` runs.
- **Docker**, for the section 13 migration suite (Testcontainers) and the image build test. Not
  needed to run the app.
- MySQL and Redis are **not** needed to run the app or the tests. Nothing implemented so far
  writes to either, the suite fakes Redis, and `/api/v1/health` never touches them.

## Setup

```bash
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env    # then fill it in — see Configuration
```

## Running

```bash
./venv/bin/uvicorn app.main:application --host 127.0.0.1 --port 8080 --http h11 --proxy-headers
```

`app.main:application` is the **only** supported entry point — it is the `socketio.ASGIApp`
wrapping the FastAPI app, and importing `app.main:app` instead gives you REST with no sockets.
`--http h11` is required for a reliable WebSocket 101 upgrade.

Port 8080 is what the frontend's dev proxy expects (`vite.config.ts`).

To run without touching the database and Redis your `.env` points at:

```bash
DB_HOST=127.0.0.1 DB_PORT=13306 REDIS_URL=redis://127.0.0.1:16379/0 \
  ./venv/bin/uvicorn app.main:application --host 127.0.0.1 --port 8080 --http h11 --proxy-headers
```

### What answers today

| Method | Path | Response |
|---|---|---|
| `GET` | `/` | `{"app": "Beery", "version": "0.1.0", "status": "ok"}` |
| `GET` | `/api/v1/health` | `{"status": "ok"}` — never touches the database or Redis |
| `GET` | `/api/v1/health/deep` | `{"status": "ok"\|"degraded", "database": bool, "redis": bool}`, always HTTP 200 |
| `POST` | `/api/v1/users/upsert` | Creates or refreshes the caller's profile. Requires a Firebase ID token. |
| `GET` | `/api/v1/users/me` | The caller's own profile. |
| — | `/socket.io/` | Handshake; establishes identity and nothing else yet. |

`status` is `"ok"` only when **both** backing services are reachable; one of two is `"degraded"`.

### Not built yet

Rooms, the game engine, gameplay sockets and the results API. `POST /rooms/create` is section 10,
the lobby is 11, play is 12. The socket layer accepts a handshake and establishes identity, but
there is no room to join. The database has no tables either — section 13 writes the first Alembic
revision, so `alembic upgrade head` currently creates only `alembic_version`.

## Tests

```bash
./venv/bin/python -m pytest                    # 858 passed, 1 skipped
./venv/bin/python -m pytest tests/test_core    # one area
```

The skip is the Docker image build, which is opt-in because a cold build is minutes:

```bash
BEERY_DOCKER_BUILD_TEST=1 ./venv/bin/python -m pytest -k docker
```

Quality gates, all of which must be clean:

```bash
./venv/bin/ruff check app
./venv/bin/black --check app
./venv/bin/mypy app/core
```

**The suite never reaches a real database or Redis.** `tests/conftest.py` pins `DB_*` and
`REDIS_URL` to unreachable local values before anything imports `app`, because `Settings` reads
`.env` relative to the working directory and a developer's `.env` points at live managed services.
Without that, a deep-health assertion would pass or fail depending on whether a cloud service
happened to be up, and from section 13 onward a migration would run against real data.

`asyncio_mode` is `strict`, so every `async def test_*` must carry `@pytest.mark.asyncio`. An
unmarked async test fails loudly rather than being silently skipped.

## Configuration

`.env`, read by `app/config.py`. See `.env.example` for the full list.

| Variable | Notes |
|---|---|
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_DATABASE` | MySQL. `DB_REQUIRE_SSL` and `DB_SSL_CA` for managed instances. |
| `REDIS_URL` | Live room state, and the Socket.IO client manager. |
| `FIREBASE_SERVICE_ACCOUNT_JSON` | The **entire** service account JSON on one line. |
| `CORS_ORIGINS` | Never `["*"]` — the app sets `allow_credentials=True` and the two together are an invalid, permissive combination. |
| `DEBUG` | Drives log level and the Socket.IO packet loggers. Never hardcode those to `True`: engine.io logs every packet's full payload at INFO. |

**Setting `FIREBASE_SERVICE_ACCOUNT_JSON`:** one line, in **single quotes** or no quotes. With
double quotes, python-dotenv applies escape decoding, turns the `\n` inside `private_key` into
real newlines, then fails to parse the line at all and leaves the variable silently unset.

When it is unset the app still starts and logs at CRITICAL:

```
FIREBASE_SERVICE_ACCOUNT_JSON is not set. Firebase ID token verification is DISABLED:
signed-in users will be REJECTED at the Socket.IO handshake and every /api/v1/users/* route
will return 503. Only guest play will work.
```

That is deliberate. Guest play is fully functional without Firebase, so refusing to boot would
turn a partial degradation into a total one — but the failure is confusing enough to deserve a
CRITICAL line rather than silence.

## Layout

```
app/
├── main.py                     the FastAPI app, lifespan, and the ASGI entry point
├── config.py                   Settings, and the hard limits from 00-decisions.md §5
├── api/
│   ├── deps.py                 Firebase token verification for REST
│   └── v1/                     ← auto-discovered router package
│       ├── __init__.py         all_routers(): every module's `router`, in module-name order
│       ├── health.py           /health, /health/deep
│       └── users.py            /users/upsert, /users/me
├── core/                       pure domain logic — no I/O, no clock, no global RNG
│   ├── enums.py                Role, RoomState, DemandKind, Distribution, …
│   ├── config_models.py        GameConfig and everything under it; validation and clamping
│   ├── presets.py              CLASSIC_MIT, FAST_GAME, CHAOS
│   ├── firebase.py             credential loading (not a domain module — see below)
│   └── checks/                 ← auto-discovered startup-check package
│       ├── __init__.py         register_check(), run_startup_checks()
│       └── firebase_check.py
├── db/
│   ├── base.py                 re-exports Base — `from ..models.base import Base`
│   └── session.py              engine, SessionLocal, get_db
├── models/
│   ├── base.py                 Base, TimestampMixin, utcnow()
│   └── user.py
├── schemas/                    Pydantic request and response models
├── services/                   stateful services over Redis and MySQL
└── sockets/
    ├── manager.py              the AsyncServer and SocketManager
    └── handlers/               ← auto-discovered handler package
        ├── __init__.py
        └── connection.py       connect / disconnect; identity, once, at the handshake

alembic/                        migration scaffold; the first revision is section 13's
tests/
├── conftest.py                 FakeRedis, firebase_tokens, FakeSocketManager, the env pinning
├── test_skeleton/              section 01
├── test_auth/                  section 02
└── test_core/                  sections 03–08
```

### Three things about this layout that are deliberate

**The three `__init__.py` packages discover their own members.** `app/api/v1/`,
`app/sockets/handlers/` and `app/core/checks/` each import every module beside them at import
time, so a section adds behaviour by *dropping in a file* — `handlers/lobby.py`, `v1/rooms.py` —
and nothing upstream is edited. `app/main.py` names no individual router and no individual
handler, and has exactly one author. Each package also works while empty, which is what let
section 01 ship and pass its own tests before any member existed.

**`app/core/` is pure, with two named exceptions.** The domain modules — `enums`,
`config_models`, `presets`, and the demand, pipeline, agent, engine, bot and stats modules still
to come — import nothing from `app.config`, `app.services`, `app.db`, `app.sockets` or `app.api`,
perform no I/O, and read no clock and no global RNG. `app/core/checks/` and `app/core/firebase.py`
are *not* domain modules and do perform I/O; the rule that matters is that **no domain module may
import either**.

**`app/db/base.py` must read `from ..models.base import Base`.** Writing `from .base import Base`
inside that file is a self-import that raises `ImportError` on any use.

## Docker

```bash
docker build -t beery-backend .
docker run --rm -p 8080:8080 --env-file .env beery-backend
```

`python:3.11-slim`, plain uvicorn with `--http h11 --proxy-headers`. `.dockerignore` excludes
`.env`, `tests/`, `venv/` and `.git/`.
