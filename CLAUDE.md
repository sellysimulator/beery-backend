# Beery_Backend — agent orientation

## What this app is

**Beery** is a browser implementation of the **Beer Distribution Game**: a supply-chain
teaching simulation. Four players hold one station each in a linear chain —
`customer → RETAILER → WHOLESALER → DISTRIBUTOR → FACTORY → production`. Each simulated
**week** every role settles (receives a shipment, fulfils its downstream neighbour's
order, pays holding/backlog cost) and then decides exactly one integer: how much to order
upstream. Shipments and orders are both delayed. Only the Retailer sees true customer
demand. The point of the exercise is the **bullwhip effect**, which the results screen
measures.

- A **host** creates a room (6-char code), configures everything, assigns roles, runs the
  session, and sees all four seats. The host does not play and pays no costs.
- **Players** join by code/link, signed in with Google (Firebase) or as a guest.
- Empty or abandoned roles are filled by a **bot** (Sterman anchor-and-adjust).
- Games are 8–104 weeks (default 36), with three presets: `CLASSIC_MIT`, `FAST_GAME`,
  `CHAOS`.

This repo is the **backend**: FastAPI + Socket.IO on Python 3.11, MySQL for the durable
record, an optional Redis for live room state.

### Where it sits in the Novus stack

`/Users/humberto/Code/Novus/` holds a family of browser business games built from one
shared template, described in `/Users/humberto/Code/Novus/game_stack.md` (the canonical
copy; the per-game copies, including `Beery/game_stack.md`, are generated from it — do not
edit those in place).

- `Selly/` — the Tequila Game, the worked reference implementation the template came from.
- `Beery/` — this game: `Beery_Backend/` (this repo) + `Beery_Frontend/` + `docs/`.
  The two code directories are **separate git repos** (`origin` here is
  `github.com/sellysimulator/beery-backend`).
- `Lemony/`, `Pulky/` — scaffolds only, not built.
- `Supply/` — an unrelated Firebase/Supabase app, not part of the game template.

`Beery_Frontend` is React 19 + Vite 7 + TS + Tailwind 4 + Zustand + `socket.io-client` +
Firebase Auth, hosted on **Firebase Hosting** (`https://beersim.web.app`). It talks to this
service over **REST `/api/v1/*`** (Axios, `Authorization: Bearer <firebase id token>`) and
**Socket.IO at `/socket.io`**. In dev, `Beery_Frontend/vite.config.ts` proxies `/api` and
`/socket.io` (with `ws: true`) to `http://localhost:8080` — which is why this service runs
on port 8080 locally. Firebase Hosting does **not** proxy WebSocket upgrades, so in
production the frontend's `VITE_SOCKET_URL` points directly at the Render host.

## Read these instead of re-deriving

- **`README.md` (here)** — prerequisites, venv setup, the exact run command, the test
  commands, the full env-var table, the complete HTTP and socket surface, the directory
  layout, and a long, good section on **"Room state and Redis"** (what the `REDIS_ENABLED`
  switch changes, its two deployment constraints, measured round-trip costs, and how to
  smoke-test a real Redis). Open it before touching `app/services/state_backend.py` or
  anything about deployment topology. It was re-derived from the routers on 2026-09-21 and
  is current.
- **`DEPLOY.md` (here)** — production topology diagram, platform env vars, **"Redis is
  optional"** (what `REDIS_ENABLED=false` costs and when it stops being allowed), the
  managed MySQL 8.4 requirement, the "ship frontend and backend together" rule, a pre-class
  deploy checklist, and what CI runs.
- **`../docs/plan/`** — the build plan this code was written against. Open when you need
  the *why*:
  - `00-decisions.md` — the **append-only** decision log D1–D19 and the hard limits (§5). It
    overrides `beer-game-spec.md`, `game_stack.md` and Selly wherever they disagree. D4 and
    D17 carry dated amendment notes recording that Redis became optional in `b56edaa`; amend
    by appending a dated note, never by rewriting a decision.
  - `00-conventions.md` — vocabulary, the three identifiers, payload/error/event shapes,
    redaction, idempotency, locking, code and test conventions.
  - `01..23-*.md` — one document per buildable section; each is the contract for the files
    it owns. Module docstrings in `app/` cite these by section and §.
  - `BUILD-LOG.md` (146K) / `HANDOFF.md` — what was built, every spec defect found, and
    where the work stopped. `ORCHESTRATOR.md` is the two-agent build protocol (historical).
  - `99-phase-2-backlog.md` — deliberately-excluded features. Check before "adding" one.
- `/Users/humberto/Code/Novus/game_stack.md` — the shared template: §0's seven security
  invariants, §2's pins and Dockerfile, §4's per-game vs never-changes split.

## Directory map

```
app/
  main.py            FastAPI app + CORS + lifespan; `application` = socketio.ASGIApp wrapper
  config.py          pydantic-settings `Settings`, `settings` singleton, db_url, db_connect_args
  api/
    deps.py          THE single Firebase token verifier + get_current/optional_firebase_user
    v1/              auto-discovered routers: health, users, user_games, rooms, games
  core/              PURE domain: enums, config_models, presets, demand, pipeline, records,
                     agents, game_engine, bot, stats
    checks/          auto-discovered startup checks (NOT pure — does I/O)
    firebase.py      Firebase Admin bootstrap (NOT pure — does I/O)
  db/                base.py (re-export of Base), session.py (engine, SessionLocal, get_db)
  models/            SQLAlchemy 2.0 models; package __init__ auto-imports every module
  schemas/           Pydantic request/response models (user, room, results)
  services/          stateful/impure layer over the state store and MySQL
  sockets/
    manager.py       AsyncServer `sio` + `socket_manager` (sid → room/alias/identity)
    errors.py        `@guarded(...)` decorator
    handlers/        auto-discovered: connection.py, lobby.py, play.py
alembic/             env.py reads the URL from settings; versions/0001, 0002
tests/               test_skeleton, test_auth, test_core, test_services, test_api,
                     test_sockets, test_models (dbschema), test_deploy
```

**Naming:** `app/api/v1/<area>.py` exports a module-level `router: APIRouter`.
`app/sockets/handlers/<area>.py` registers `@sio.event async def <event_name>(sid, data=None)`.
Services are `app/services/<thing>_service.py` with a `get_<thing>_service()` singleton
(the pure ones — `config_merge`, `display_name`, `export_service` — are plain functions or
stateless classes).

**Three packages self-discover their members** (`pkgutil.iter_modules` + `importlib`):
`app/api/v1/`, `app/sockets/handlers/`, `app/core/checks/`, plus `app/models/`. You add
behaviour by **dropping a file in**. `app/main.py` names no individual router or handler —
do not edit it to register one.

## Key files and configuration

| Concern | Where |
|---|---|
| Settings | `app/config.py` → `settings`. `SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")`. |
| Env vars | `.env.example` is the complete list and is kept complete: `DEBUG HOST PORT CORS_ORIGINS DB_HOST DB_PORT DB_USER DB_PASSWORD DB_DATABASE DB_REQUIRE_SSL DB_SSL_CA REDIS_ENABLED REDIS_URL FIREBASE_SERVICE_ACCOUNT_JSON`, plus `MAX_DISPLAY_NAME_LENGTH`, `APP_NAME` and `VERSION` which have sane defaults. Every `Settings` field is read somewhere — do not add one that is not. |
| DB connection | `settings.db_url` → `mysql+pymysql://...`, credentials percent-encoded with `quote(..., safe="")`. Engine in `app/db/session.py`: `pool_pre_ping`, `pool_recycle=3600`, `pool_size=10`, `max_overflow=20`, and a `connect` listener forcing `SET time_zone='+00:00'`. |
| Auth | Firebase Admin only. `app/core/firebase.py::init_firebase()` returns `None` (never raises) when unconfigured. `app/api/deps.py::verify_firebase_id_token` is the **one** verifier, shared by REST and the socket handshake. No JWTs are minted here. |
| CORS | `app/main.py` sets `allow_credentials=True`, so `Settings` **rejects `"*"` in `CORS_ORIGINS` at load**. `sockets/manager.py` passes the same list to `cors_allowed_origins`. |
| Alembic | `alembic.ini` has **no `sqlalchemy.url`** — `alembic/env.py` reads `settings.db_url` and imports `app.models` so `Base.metadata` is populated. Head is `0002`. |
| Deployment | `render.yaml`: one Docker web service, `healthCheckPath: /api/v1/health`, `preDeployCommand: alembic upgrade head`, `REDIS_ENABLED=false`. MySQL is **not** declared there — provisioned externally and wired in via env. Redis is **optional** and likewise not declared; with the flag off the deployment has no Redis at all, which is only valid at exactly one instance. |
| Docker | `python:3.11-slim`, plain uvicorn (no gunicorn), `--http h11 --proxy-headers`, `CMD` binds `${PORT:-8080}`. `.dockerignore` excludes `.env`, `tests/`, `venv/`, `.git/`. |
| CI | `.github/workflows/ci.yml`, runs at repo root (this repo IS `Beery_Backend`; no `working-directory`). jobs: `test` (ruff + black + mypy app/core + pytest `-m "not dbschema"` at ≥80% + `app/core` at ≥95% + docker build), `dbschema` (Testcontainers MySQL, fails if nothing passed). No deploy job: Render auto-deploys `main` after checks pass (`autoDeployTrigger: checksPass`). |

## Commands

All of these are run from `/Users/humberto/Code/Novus/Beery/Beery_Backend`.

```bash
# setup — 3.11 exactly; a bare `python3` venv silently diverges from the image
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env

# run (the ONLY supported entry point — `app.main:app` is REST with no sockets)
./venv/bin/uvicorn app.main:application --host 127.0.0.1 --port 8080 --http h11 --proxy-headers
./venv/bin/uvicorn app.main:application --reload          # local iteration

# tests
./venv/bin/python -m pytest -m "not dbschema"             # 1557 passed, 1 skipped (~37s)
./venv/bin/python -m pytest tests/test_core               # one area
./venv/bin/python -m pytest -m dbschema                   # needs a Docker daemon (mysql:8.4)
BEERY_DOCKER_BUILD_TEST=1 ./venv/bin/python -m pytest -k docker   # the opt-in image build

# gates (all must be clean; CI runs them over `app tests`)
./venv/bin/ruff check app tests
./venv/bin/black --check app tests
./venv/bin/mypy app/core

# migrations
./venv/bin/alembic heads
./venv/bin/alembic upgrade head
./venv/bin/alembic revision --autogenerate -m "description"   # needs a reachable DB
./venv/bin/alembic downgrade -1

# docker
docker build -t beery-backend .
docker run --rm -p 8080:8080 --env-file .env beery-backend
```

**Deploy** is not a local command: push to `main` → CI → Render auto-deploy once all checks pass, which builds
the Dockerfile and runs `alembic upgrade head` as a pre-deploy step. There is no bind
mount in the image, so a source change needs a rebuild.

## Architecture that matters

- **MySQL + SQLAlchemy 2.0, fully synchronous.** `Mapped[...] / mapped_column(...)`
  declarative style, `Session` via the `get_db` FastAPI dependency. PyMySQL, never
  `mysql-connector-python` (it pins `protobuf<=4.21.12` and conflicts with `firebase-admin`).
  There is **no `Base.metadata.create_all()` anywhere** — Alembic revision `0001` is the
  only thing that builds the schema.
- **Async/sync split.** Socket handlers, services over the state store, and the routes are
  `async def`; everything touching SQLAlchemy is sync and runs on the threadpool FastAPI
  gives a sync dependency. The deep-health DB ping explicitly uses
  `anyio.to_thread.run_sync`. Do not call a sync `Session` from inside an `async def` without
  that.
- **D4 — the state store is authoritative for a live game; MySQL is written only when a
  game finishes.** The room is a JSON document at `room:{CODE}` with a 24 h TTL. The
  `GameEngine` is rehydrated per operation (`to_payload`/`from_payload`), never held in
  process memory between handlers.
- **`REDIS_ENABLED` (default `false`)** switches `app/services/state_backend.py` between
  `redis.asyncio.Redis` and `InMemoryBackend` behind one five-method protocol, and
  `sockets/manager.py` between `AsyncRedisManager` and the in-process manager. The two
  backends are **deliberately semantically identical** — both store JSON strings, both
  require an explicit `save_room`. With Redis off, the app must run as exactly one
  instance and no `--workers`.
- **Locking.** Every read-modify-write of room state is inside
  `async with state_svc.lock(room_code):`. No exceptions. Never hold the lock across a
  database write — `db_service.build_snapshot` copies out under the lock, everything below
  it is lock-free.
- **Model / schema separation.** `app/models/` is SQLAlchemy only; `app/schemas/` is
  Pydantic only. The response shapes in `app/schemas/results.py` are **frozen** — the
  shipped frontend expects those exact names. Money fields there are declared `float`, never
  `Decimal`, because a `Decimal` on the wire breaks the frontend charts.
- **Money.** `float` in the engine, in the room document and in payloads; `DECIMAL(12,2)`
  in MySQL. Rounding (`ROUND_HALF_UP`) happens **only** at the persistence boundary, in
  `db_service.round_money`. Quantities are `int` everywhere.
- **Purity of `app/core/`.** Domain modules import nothing from `app.config`,
  `app.services`, `app.db`, `app.sockets`, `app.api`; no I/O, no clock, no global `random`.
  Hard limits arrive as an injected `Limits` (`DEFAULT_LIMITS`), not by importing settings.
  `app/core/checks/` and `app/core/firebase.py` are the two non-domain exceptions inside
  that tree, and **no domain module may import either**.
- **Service layer.** One rule, one owner. `merge_config_patch` (`config_merge.py`) and
  `sanitise_display_name` (`display_name.py`) each exist once and are called from both the
  REST and socket collection points — do not reimplement them locally. `user_service._sanitise`
  is a *different* function for stored profile fields and must not be reused for display names.
- **API/socket contract with the frontend.** REST: `/api/v1/health`, `/health/deep`,
  `/users/upsert`, `/users/me`, `/users/me/stats`, `/users/me/games`,
  `/users/me/games/{game_id}`, `/rooms/create`, `/rooms/presets`, `/rooms/{code}/status`,
  `GET|PUT /rooms/{code}/config`, `/games/{code}/results`, `/games/{code}/export`,
  `POST /games/claim`.
  Socket in: `connect`, `disconnect`, `join_waiting`, `join`, `leave`, `config_update`,
  `set_role_mode`, `assign_role`, `claim_role`, `release_role`, `start_game`, `submit_order`,
  `pause_game`, `resume_game`, `force_close_week`, `substitute_bot`, `end_game_early`,
  `request_state`.
  Socket out: `joined`, `join_error`, `leave_ack`, `lobby_update`, `config_updated`,
  `roles_assigned`, `game_started`, `game_paused`, `game_resumed`, `your_state`, `host_state`,
  `order_submitted`, `your_week_closed`, `error`.
  Any change here must ship with `Beery_Frontend/src/api/socketHandlers.ts` in the same
  window, backend first.
- **Error handling.** REST: `HTTPException` with a **string** `detail`, never `str(e)`;
  log with `logger.exception` and return a generic message. Sockets: apply `@guarded()`
  *under* `@sio.event` — `guarded("error")` for play (`{message, code}`), `guarded("join_error")`
  for the lobby (`{message}`). It logs the traceback and the event name, never the payload.
- **Startup never hard-fails on a missing credential.** An unset
  `FIREBASE_SERVICE_ACCOUNT_JSON` logs CRITICAL and guest play still works; the state-backend
  check logs the in-memory constraint at WARNING. A check that raises is logged and does not
  abort boot.

## Gotchas and conventions

- **Auth fails closed and is deliberately ambiguous.** `verify_firebase_id_token` returns
  `None` both for "unconfigured server" and "bad token", and `None` always means reject.
  Never fall back to trusting a raw value, and never add a second verification path.
- **`identity` is server-only.** It is resolved once at `connect` and read thereafter from
  `socket_manager.sid_to_identity[sid]` — never accepted from a client payload. A broadcast
  may carry `alias`; it may **never** carry `identity`, `session_token` or `host_secret`.
  Redact server-side before sending: build payloads key by key, never spread a room document.
- **Authority is a capability**: `host_secret`, compared with `hmac.compare_digest`
  (D3). D18 also accepts a connection whose verified identity equals `host_identity`.
- **Alias allocation scans for the lowest unused `P<N>`** (`next_free_alias`), never
  `len(participants) + 1` — that reissues a live participant's alias after a `leave`.
- **`app/db/base.py` must read `from ..models.base import Base`.** `from .base import Base`
  there is a self-import that raises `ImportError`.
- **The §5 hard limits are not settings, on purpose.** `Settings` declares exactly one of
  them, `MAX_DISPLAY_NAME_LENGTH` (consumed by `display_name.py` and `user_service.py`).
  The game ceilings live on `core.config_models.Limits` / `DEFAULT_LIMITS` and are
  injected, because `app/core` may not import `app.config`; `ROOM_TTL_SECONDS` and
  `LOCK_TIMEOUT_SECONDS` are module constants in `services/state_service.py`. The other ten
  §5 names *were* `Settings` fields, were read by nothing, and were deleted on 2026-09-21
  so that `.env` stops lying — a variable the app ignores is worse than no variable. Change
  a ceiling at the site that owns it; `01-backend-skeleton.md` §2 still shows the old,
  larger `Settings` surface, and the code is right and the plan is stale on that point.
- **`FIREBASE_SERVICE_ACCOUNT_JSON` must be one line, single-quoted or unquoted.** Double
  quotes make python-dotenv decode `\n` inside `private_key`, break the line, and leave the
  variable silently unset.
- **`CORS_ORIGINS` must never contain `"*"`** — `Settings` raises at load.
- **Never hardcode the Socket.IO `logger`/`engineio_logger` to `True`** — they are driven
  by `settings.DEBUG`; engine.io logs every packet payload at INFO.
- **Never log a `host_secret`, `session_token`, guest identity, Firebase uid, or the
  service account.** Use `logging.getLogger(__name__)`; no `print`.
- **`asyncio_mode = strict`** — every `async def test_*` needs `@pytest.mark.asyncio` or it
  fails loudly.
- **The suite never reaches a real DB or Redis.** `tests/conftest.py` overwrites `DB_*` and
  `REDIS_URL` with unreachable values *before* `app` is imported, and monkeypatches
  `app.services.state_service.redis_client` with a `FakeRedis`. A change to
  `state_backend.py` therefore needs a manual smoke test against a real Redis (recipe in
  README).
- **`state_service.redis_client` is read by name at every call site** — never capture it in
  a constructor, or the test monkeypatch stops working.
- **Never add `asyncio` to `requirements.txt`** (the PyPI package shadows the stdlib module).
- Deps are exactly pinned (`==`). There is no `ruff`/`mypy`/`black` config section anywhere —
  they run on defaults; keep it that way unless you have a reason.
- English only, no i18n layer (D13). Do not port Selly's Spanish strings.
- Before "adding a feature", check `00-decisions.md` §3 (superseded spec parts) and
  `99-phase-2-backlog.md` — spectators, timers, chat, `clone_room`, per-week write-through
  and non-4-stage chains are all deliberately out of v1.
