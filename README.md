# Beery — Backend

FastAPI + Socket.IO server for the Beer Distribution Game. Python 3.11, MySQL for the durable
record, and an optional Redis for live room state.

Built section by section against `../docs/plan/`. Each section document is the contract; the
plan's `BUILD-LOG.md` records what is done and every specification defect found along the way.

---

## Prerequisites

- **Python 3.11** — create the venv with `python3.11 -m venv venv`, never bare `python3`. On a
  machine whose default `python3` has moved on, the bare form silently builds the venv on a
  different interpreter than `python:3.11-slim` runs.
- **Docker**, for the `dbschema` schema and migration suite (Testcontainers) and the image
  build test. Not needed to run the app.
- **MySQL** is needed for anything that finishes a game or reads a profile: the durable
  record is written when a game ends, and `/api/v1/users/*` and `/api/v1/games/*` read it.
  Lobbies, configuration and play itself run without it. `alembic upgrade head` builds the
  schema; there is no `create_all()`.
- **Redis is optional and off by default** (`REDIS_ENABLED=false`) — live room state then
  lives in this process. See *Room state and Redis* below before changing that.
- Neither is needed to run the **tests**: the suite fakes the state store and never opens a
  database connection, and `/api/v1/health` never touches either.

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

### The HTTP surface

Every router under `app/api/v1/` is auto-discovered, so this table is the whole of it.
"Auth" is what the server actually checks: **Firebase** means a verified
`Authorization: Bearer <id token>`, **host secret** means the `X-Host-Secret` header
(`hmac.compare_digest`), **none** means the route is deliberately open.

| Method | Path | Auth | Answers |
|---|---|---|---|
| `GET` | `/` | none | `{"app": "Beery", "version": "0.1.0", "status": "ok"}` |
| `GET` | `/api/v1/health` | none | `{"status": "ok"}` — liveness; touches neither backing service |
| `GET` | `/api/v1/health/deep` | none | `{status, database, redis, state_backend}`, always HTTP 200 |
| `POST` | `/api/v1/rooms/create` | none (D3) | A new room: `room_code`, the `host_secret` (returned exactly once), `state`, `config` |
| `GET` | `/api/v1/rooms/presets` | none | The three presets with their full configs, for the host UI |
| `GET` | `/api/v1/rooms/{code}/status` | none | Is this room joinable? Always HTTP 200 — a 404 would be a room-code oracle |
| `PUT` | `/api/v1/rooms/{code}/config` | host secret | Applies a config patch while the room is still configurable |
| `GET` | `/api/v1/rooms/{code}/config` | host secret | The current config |
| `GET` | `/api/v1/games/{code}/results` | none | The full results payload for the latest finished game in that room — a results URL is shareable |
| `GET` | `/api/v1/games/{code}/export` | host secret *or* the host's Firebase identity | The complete week-by-week record, `?format=csv` (default) or `json` |
| `POST` | `/api/v1/games/claim` | Firebase | Attributes a guest's finished games to the signed-in caller |
| `POST` | `/api/v1/users/upsert` | Firebase | Creates or refreshes the caller's profile. The uid comes from the token, never the body |
| `GET` | `/api/v1/users/me` | Firebase | The caller's profile, or 404 if it was never upserted |
| `GET` | `/api/v1/users/me/stats` | Firebase | Aggregate statistics; a caller with no history gets a zero-filled 200, not a 404 |
| `GET` | `/api/v1/users/me/games` | Firebase | Match history, newest first, `?page` / `?page_size` (clamped to 1–100) |
| `GET` | `/api/v1/users/me/games/{game_id}` | Firebase | One of the caller's own games in full; 404 — never 403 — if they did not play in it |

In `/health/deep`, `status` is `"ok"` only when every backing service in use is reachable;
otherwise `"degraded"`. With `REDIS_ENABLED=false` the unused Redis is not probed and is not
a reason to degrade, and `state_backend` reports `"memory"` rather than `"redis"`.

### The socket surface

`/socket.io/`, handled by the auto-discovered modules in `app/sockets/handlers/`. Identity is
resolved once, at `connect`, from an optional Firebase token in the handshake `auth`, and is
**server-side only** thereafter — no payload may carry it.

- **Connection** — `connect`, `disconnect`.
- **Lobby** (`lobby.py`) — in: `join_waiting`, `join`, `leave`, `config_update`,
  `set_role_mode`, `assign_role`, `claim_role`, `release_role`, `start_game`.
- **Play** (`play.py`) — in: `submit_order`, `pause_game`, `resume_game`, `force_close_week`,
  `substitute_bot`, `end_game_early`, `request_state`.
- **Out** — `joined`, `join_error`, `leave_ack`, `lobby_update`, `config_updated`,
  `roles_assigned`, `game_started`, `game_paused`, `game_resumed`, `your_state`, `host_state`,
  `order_submitted`, `your_week_closed`, `error`.

Every event name and payload here is a contract with `Beery_Frontend/src/api/socketHandlers.ts`.
A change to either must ship with the other, backend first.

### Not built here

Deliberately out of v1 and listed in `../docs/plan/99-phase-2-backlog.md`: spectators, turn
timers, chat, `clone_room`, per-week write-through to MySQL, and chains that are not four
stages. Check that file and `00-decisions.md` §3 before "adding" one.

## Tests

```bash
./venv/bin/python -m pytest -m "not dbschema"   # 1557 passed, 1 skipped, 124 deselected
./venv/bin/python -m pytest tests/test_core     # one area
```

`-m "not dbschema"` is the everyday run. The deselected tests are the schema and migration
suite, which needs a Docker daemon because it starts a real `mysql:8.4` with Testcontainers:

```bash
./venv/bin/python -m pytest -m dbschema
```

The skip is the Docker image build, which is opt-in because a cold build is minutes:

```bash
BEERY_DOCKER_BUILD_TEST=1 ./venv/bin/python -m pytest -k docker
```

Quality gates, all of which must be clean (CI runs them over `app tests`):

```bash
./venv/bin/ruff check app tests
./venv/bin/black --check app tests
./venv/bin/mypy app/core
```

**The suite never reaches a real database or Redis.** `tests/conftest.py` pins `DB_*` and
`REDIS_URL` to unreachable local values before anything imports `app`, because `Settings` reads
`.env` relative to the working directory and a developer's `.env` points at live managed services.
Without that, a deep-health assertion would pass or fail depending on whether a cloud service
happened to be up, and the migration suite would run against real data.

`asyncio_mode` is `strict`, so every `async def test_*` must carry `@pytest.mark.asyncio`. An
unmarked async test fails loudly rather than being silently skipped.

## Configuration

`.env`, read by `app/config.py`. `.env.example` is the full list — every variable the app
reads is there, and nothing that is not read is declared.

| Variable | Notes |
|---|---|
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_DATABASE` | MySQL. `DB_REQUIRE_SSL` and `DB_SSL_CA` for managed instances. |
| `REDIS_ENABLED` | **`false` by default.** When false, live room state is held in this process and there is no Redis dependency at all. See *Room state and Redis* below. |
| `REDIS_URL` | Only read when `REDIS_ENABLED` is true. Live room state, and the Socket.IO client manager. |
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

## Room state and Redis

Live room state — lobbies, seating, the running `GameEngine` — is authoritative in a key-value
store, and MySQL is written only when a game finishes (`00-decisions.md` **D4**). Which store
holds it is a one-line switch.

**`REDIS_ENABLED` is `false` by default**, so out of the box the app has no Redis dependency:
room documents live in a dict in this process. This is the right setting for a single
instance, which is what a classroom deployment is.

### What you give up, and what you get

Two constraints come with the in-memory backend, both of them **deployment** facts that no
process can detect from the inside:

1. **This must be the only instance serving the app.** Two instances means two players in one
   room land on different processes and never see each other — no error, no log, just a lobby
   that will not fill. If you scale past one instance, or add `--workers`, you must turn Redis
   on in the same change.
2. **Restarting the process ends every game in progress.** A game reaches MySQL only when it
   finishes, so a deploy or a crash mid-session loses the rooms. Redis survives an app restart;
   it does not survive its own restart or eviction, so this is a narrowing of the window rather
   than a guarantee.

The app says which backend is active every time it starts, at WARNING for the in-memory one.
`GET /api/v1/health/deep` reports it as `state_backend: "memory" | "redis"`.

What you get in exchange, measured on this build: a player's order submission costs **5** state
round trips and a full week costs **23**, so a 36-week game is around 830 — invisible in
memory, and 1.7 s of added latency against a same-region Redis, 12 s against a distant one,
spread across the session. The sharper case is an all-bot demo, which runs **220** round trips
inside a single `start_game` call: 199 ms in memory, up to 3.3 s against a distant Redis.

### Turning Redis on

```bash
REDIS_ENABLED=true
REDIS_URL=redis://:password@host:6379/0
```

Both are required together. `REDIS_ENABLED=true` with an empty `REDIS_URL` refuses to start,
with a message naming the setting rather than `from_url`'s complaint about URL schemes.

Nothing else changes. `app/services/state_backend.py` chooses between
`redis.asyncio.Redis` and `InMemoryBackend`, both satisfying the same five-method protocol;
`app/sockets/manager.py` switches the Socket.IO client manager to `AsyncRedisManager` on the
same flag, which is what makes cross-instance fan-out work and is the one thing the in-memory
backend cannot replace.

**The two backends are deliberately identical in semantics, not merely similar.** Both store
JSON strings and both require an explicit `save_room`. It is tempting to have the in-memory one
hold live `GameEngine` objects and skip serialisation — worth about 3.4 ms of CPU per handler at
week 36 — and it must not, because a handler's mutations would then take effect without saving,
which works in memory and breaks the moment anyone sets `REDIS_ENABLED=true`. Keep the switch a
deployment choice and never a behavioural one.

### What the suite does and does not prove

The suite passes with `REDIS_ENABLED` either way, and it is worth being clear about why that
is weaker evidence than it looks: `tests/conftest.py` monkeypatches `state_service.redis_client`
with an in-memory fake in both modes, so **neither run talks to a real Redis**. What the flag
changes there is only which client the app builds at import.

- `tests/test_services/test_state_backend.py` covers the switch itself and the in-memory
  backend's semantics — TTL expiry, lock serialisation under a forced suspension, and the
  refusal to start with `REDIS_ENABLED=true` and no URL.
- Everything above it is exercised against the fake.

So a change to `state_backend.py` needs a **manual smoke test against a real Redis** before you
trust it:

```bash
docker run --rm -p 6379:6379 redis:7-alpine
REDIS_ENABLED=true REDIS_URL=redis://localhost:6379/0 \
  ./venv/bin/uvicorn app.main:application --reload
curl localhost:8080/api/v1/health/deep    # expect state_backend "redis", redis true
```

Then create a room, join from a second browser and play a week.

## Layout

```
app/
├── main.py                     the FastAPI app, lifespan, and the ASGI entry point
├── config.py                   Settings and the `settings` singleton
├── api/
│   ├── deps.py                 the one Firebase token verifier, shared by REST and sockets
│   └── v1/                     ← auto-discovered router package
│       ├── __init__.py         all_routers(): every module's `router`, in module-name order
│       ├── health.py           /health, /health/deep
│       ├── users.py            /users/upsert, /users/me
│       ├── user_games.py       /users/me/stats, /users/me/games[/{id}]
│       ├── rooms.py            /rooms/create, /rooms/presets, /rooms/{code}/status|config
│       └── games.py            /games/{code}/results, /games/{code}/export, /games/claim
├── core/                       pure domain logic — no I/O, no clock, no global RNG
│   ├── enums.py                Role, RoomState, DemandKind, Distribution, …
│   ├── config_models.py        GameConfig, Limits/DEFAULT_LIMITS; validation and clamping
│   ├── presets.py              CLASSIC_MIT, FAST_GAME, CHAOS
│   ├── demand.py               the customer demand generator
│   ├── pipeline.py             the shipment and order delay pipelines
│   ├── agents.py               one station's settle-and-order step
│   ├── game_engine.py          the week loop; the only thing that mutates game state
│   ├── bot.py                  the Sterman anchor-and-adjust bot
│   ├── records.py              the week/game record shapes the engine emits
│   ├── stats.py                end-of-game statistics, including the bullwhip ratio
│   ├── firebase.py             credential loading (not a domain module — see below)
│   └── checks/                 ← auto-discovered startup-check package
│       ├── __init__.py         register_check(), run_startup_checks()
│       ├── firebase_check.py
│       └── state_backend_check.py
├── db/
│   ├── base.py                 re-exports Base — `from ..models.base import Base`
│   └── session.py              engine, SessionLocal, get_db
├── models/                     SQLAlchemy 2.0; the package imports every module in it
│   ├── base.py                 Base, TimestampMixin, utcnow()
│   ├── user.py                 users, user_stats
│   └── game.py                 games, configs, participants, weeks
├── schemas/                    Pydantic request and response models (user, room, results)
├── services/                   the stateful layer over the state store and MySQL
│   ├── state_backend.py        the REDIS_ENABLED switch and the in-memory backend
│   ├── state_service.py        room documents, the lock, TTL
│   ├── room_service.py         lobby and seating rules
│   ├── game_service.py         driving the engine through a week
│   ├── db_service.py           the end-of-game write; the only money-rounding point
│   ├── stats_service.py        user_stats maintenance
│   ├── claim_service.py        attributing a guest's games to an account
│   ├── export_service.py       CSV and JSON export rows
│   ├── config_merge.py         merge_config_patch — one implementation, two callers
│   ├── display_name.py         sanitise_display_name — likewise
│   └── user_service.py         profile upsert and lookup
└── sockets/
    ├── manager.py              the AsyncServer and SocketManager (sid → room/alias/identity)
    ├── errors.py               the @guarded(...) decorator
    └── handlers/               ← auto-discovered handler package
        ├── __init__.py
        ├── connection.py       connect / disconnect; identity, once, at the handshake
        ├── lobby.py            joining, seating, config, start_game
        └── play.py             orders, pause/resume, bots, ending a game

alembic/                        env.py reads settings.db_url; versions 0001 and 0002 (head)
tests/
├── conftest.py                 FakeRedis, firebase_tokens, FakeSocketManager, the env pinning
├── test_skeleton/              the app, config, packaging and assembly
├── test_auth/                  identity, token verification, the handshake, profiles
├── test_core/                  the pure domain
├── test_services/              the state store, persistence, stats, guest claims
├── test_api/                   the REST routers
├── test_sockets/               the lobby and play handlers, redaction, host authority
├── test_models/                the schema and migrations (`dbschema` — needs Docker)
└── test_deploy/                Dockerfile, render.yaml, DEPLOY.md and CI as committed
```

### Three things about this layout that are deliberate

**The three `__init__.py` packages discover their own members.** `app/api/v1/`,
`app/sockets/handlers/` and `app/core/checks/` each import every module beside them at import
time (as does `app/models/`), so behaviour is added by *dropping in a file* — `handlers/lobby.py`,
`v1/rooms.py` — and nothing upstream is edited. `app/main.py` names no individual router and no individual
handler, and has exactly one author. Each package also works while empty, which is what let
section 01 ship and pass its own tests before any member existed.

**`app/core/` is pure, with two named exceptions.** The domain modules — `enums`,
`config_models`, `presets`, `demand`, `pipeline`, `agents`, `game_engine`, `bot`, `records` and
`stats` — import nothing from `app.config`, `app.services`, `app.db`, `app.sockets` or `app.api`,
perform no I/O, and read no clock and no global RNG. The hard ceilings of `00-decisions.md` §5
arrive as an injected `Limits`, never as an import of `settings`. `app/core/checks/` and `app/core/firebase.py`
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
