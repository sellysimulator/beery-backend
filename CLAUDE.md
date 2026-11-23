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
- Bots (Sterman anchor-and-adjust) play a seat in exactly two cases: an **empty** seat at
  start when the host turned on `bot_fill_empty_roles` (bots take only the roles nobody holds:
  `RoomService.can_start` refuses while any person in the room has no role, naming them, so
  nobody is left seatless; RANDOM deals everyone a role first), and a seat the host explicitly
  **swaps** to a bot ("Swap in a bot", `substitute_bot`). A player who drops is never replaced
  automatically — the room pauses (D7) and the host decides. A swapped seat keeps its player's
  identity, so the finished game still counts for them (spec §10.4); a bot swapped in while
  paused plays the moment the host resumes. The swapped player can take the seat back
  (`reclaim_seat`, their own seat only, running or paused); rejoining alone never does.
  `WeekRecord.was_bot` is who placed the week's standing order, not who held the seat at the
  close (`GameEngine.pending_by_bot`).
- Games are 8–104 weeks (default 36), with three presets: `CLASSIC_MIT`, `FAST_GAME`,
  `CHAOS`.

This repo is the **backend**: FastAPI + Socket.IO on Python 3.11, PostgreSQL 18 for the durable
record, an optional Redis for live room state.

### Where it sits in the Novus stack

`/Users/humberto/Code/Novus/` holds a family of browser business games built from one
shared template, described in `/Users/humberto/Code/Novus/game_stack.md` (the canonical
copy; the per-game copies, including `Beery/game_stack.md`, are generated from it — do not
edit those in place).

- `Selly/` — the Tequila Game, the worked reference implementation the template came from.
- `Beery/` — this game: `Beery_Backend/` (this repo) + `Beery_Frontend/`, whose `docs/`
  holds the Beer Game spec and manual. The two code directories are **separate git repos**
  (`origin` here is `github.com/sellysimulator/beery-backend`).
- `Lemony/` (the Lemonade Game) and `Pulky/` (the Pulque Game) — the other games built from
  the same template.
- `Supply/` — **Selly**, the catalog site that lists the games (Supabase); not built from the
  template.

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
  anything about deployment topology.
- **`DEPLOY.md` (here)** — production topology diagram, platform env vars, **"Redis is
  optional"** (what `REDIS_ENABLED=false` costs and when it stops being allowed), the
  managed PostgreSQL 18 requirement (Neon, direct endpoint, TLS, connection budget, DDL snapshot), the "ship frontend and backend together" rule, a pre-class
  deploy checklist, and what CI runs.
- **`../Beery_Frontend/docs/beer-game-spec.md`** — the product contract: the week's
  sequence (§7), the bot (§8.5), the engine and its redaction (§8.6, §8.8), persistence
  (§10), the REST, socket and room-document surface (§11), the abuse limits (§12.1), the
  vocabulary (§14), **§15 Decisions** (the `D<n>` that code comments cite) and **§16 Not
  built yet**. `beer-game-manual.md` beside it is the product voice. `game_stack.md` wins on
  shared plumbing except where §15 records a deliberate departure. Git history holds the
  build record.
- `/Users/humberto/Code/Novus/game_stack.md` — the shared template: §0's seven security
  invariants, §2's pins and Dockerfile, §4's per-game vs never-changes split.

## Directory map

```
app/
  main.py            FastAPI app + CORS + lifespan; `application` = socketio.ASGIApp wrapper
  config.py          pydantic-settings `Settings`, `settings` singleton, db_url, db_connect_args
  api/
    deps.py          THE single Firebase token verifier + get_current_firebase_user
    v1/              auto-discovered routers: health, users, user_games, rooms, games
  core/              PURE domain: enums, config_models, limits, presets, preview, demand,
                     pipeline, records, agents, game_engine, bot, stats
    checks/          auto-discovered startup checks (NOT pure — does I/O)
    firebase.py      Firebase Admin bootstrap (NOT pure — does I/O)
    abuse.py         rate limits, socket caps, client_ip (NOT pure — reads the clock)
  db/                base.py (re-export of Base), session.py (engine, SessionLocal, get_db)
  models/            SQLAlchemy 2.0 models; package __init__ auto-imports every module
  schemas/           Pydantic request/response models (user, room, results)
  services/          stateful/impure layer over the state store and PostgreSQL
  sockets/
    manager.py       AsyncServer `sio` + `socket_manager` (sid → room/alias/identity)
    errors.py        `@guarded(...)` decorator
    handlers/        auto-discovered: connection.py, lobby.py, play.py
alembic/             env.py reads the URL from settings; versions/0001_postgres_baseline, 0002_games_host_user_id
db/                  schema.postgres.sql — generated DDL of head (scripts/db/gen_ddl.py)
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
| Env vars | `.env.example` is the complete list and is kept complete: `DEBUG HOST PORT CORS_ORIGINS DB_HOST DB_PORT DB_USER DB_PASSWORD DB_DATABASE DB_REQUIRE_SSL DB_SSL_CA DB_POOL_SIZE DB_MAX_OVERFLOW REDIS_ENABLED REDIS_URL FIREBASE_SERVICE_ACCOUNT_JSON`, plus `MAX_DISPLAY_NAME_LENGTH`, `APP_NAME` and `VERSION` which have sane defaults. Every `Settings` field is read somewhere — do not add one that is not. |
| DB connection | `settings.db_url` → `postgresql+psycopg://...` (psycopg 3), credentials percent-encoded with `quote(..., safe="")`. `settings.db_connect_args`: always `options=-c timezone=UTC` (every session is UTC); `DB_SSL_CA` (the Let's Encrypt roots ISRG Root X1 + X2 that Neon's certificates chain to, one public PEM bundle, PEM text or path; PEM text is written once to a temp file because libpq reads `sslrootcert` from a file) → `sslmode=verify-full`, else `DB_REQUIRE_SSL` → `sslmode=require`; `{}` for a non-Postgres URL. Engine in `app/db/session.py`: `pool_pre_ping`, `pool_recycle=3600`, `pool_size=settings.DB_POOL_SIZE` (7), `max_overflow=settings.DB_MAX_OVERFLOW` (2), `pool_timeout=10`: 9 per instance within Beery's budget of 20 connections, deploys included (2 × 9 overlapping instances + 1 startup migration + 1 psql; `DEPLOY.md`). `DB_HOST` is Neon's **direct** endpoint (no `-pooler`): the pooled endpoint rejects the `options` startup parameter. |
| Auth | Firebase Admin only. `app/core/firebase.py::init_firebase()` returns `None` (never raises) when unconfigured. `app/api/deps.py::verify_firebase_id_token` is the **one** verifier, shared by REST and the socket handshake. No JWTs are minted here. |
| CORS | `app/main.py` sets `allow_credentials=True`, so `Settings` **rejects `"*"` in `CORS_ORIGINS` at load**. `sockets/manager.py` passes the same list to `cors_allowed_origins`. |
| Alembic | `alembic.ini` has **no `sqlalchemy.url`** — `alembic/env.py` reads `settings.db_url` and imports `app.models` so `Base.metadata` is populated. One baseline, `0001_postgres_baseline` (autogenerated from the models against an empty PostgreSQL, hand-reviewed), builds the whole schema; later revisions chain from it. `Base.metadata` has a naming convention, so unnamed constraints/indexes get deterministic names. The DDL of head is committed as `db/schema.postgres.sql`, regenerated with `python3 scripts/db/gen_ddl.py --backend Beery/Beery_Backend` (from `Novus/`) in the same change as any migration; the `dbschema` suite fails on drift (`test_ddl_snapshot_matches_migrations`, `test_models_match_migrations`). |
| Deployment | One plain Docker web service on Render's free plan, configured in the dashboard; `render.yaml` records it (not linked as a Blueprint): `plan: free`, `healthCheckPath: /api/v1/health`, `autoDeployTrigger: checksPass`, `REDIS_ENABLED=false`. Migrations run at container startup: the Dockerfile `CMD` is `alembic upgrade head && exec uvicorn ...` (free instances have no pre-deploy command; a failed migration exits, so Render keeps the previous deploy live; one instance only). The database is **not** declared there — the database `beerydb` (user `neondb_owner`) on Beery's own Neon PostgreSQL 18 project (Free plan, AWS US East 1, the Render service's region), wired in via the `DB_*` env vars. Redis is **optional** and likewise not declared; with the flag off the deployment has no Redis at all, which is only valid at exactly one instance. |
| Docker | `python:3.11-slim`, plain uvicorn (no gunicorn), `--http h11 --proxy-headers`, `CMD` runs `alembic upgrade head` then binds `${PORT:-8080}`. `.dockerignore` excludes `.env`, `tests/`, `venv/`, `.git/`. |
| CI | `.github/workflows/ci.yml`, runs at repo root (this repo IS `Beery_Backend`; no `working-directory`). jobs: `test` (ruff + black + mypy app/core + pytest `-m "not dbschema"` at ≥80% + `app/core` at ≥95% + docker build), `dbschema` (Testcontainers `postgres:18`, fails if nothing passed). No deploy job: Render auto-deploys `main` after checks pass (`autoDeployTrigger: checksPass`). |

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
./venv/bin/python -m pytest -m "not dbschema"             # everyday run, no Docker
./venv/bin/python -m pytest tests/test_core               # one area
./venv/bin/python -m pytest -m dbschema                   # needs a Docker daemon (postgres:18)
BEERY_DOCKER_BUILD_TEST=1 ./venv/bin/python -m pytest -k docker   # the opt-in image build

# gates (all must be clean; CI runs them over `app tests` only: `ruff check .` also lints
# alembic/, where env.py has two old findings, so don't widen the scope without fixing them)
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

**Deploy** is not a local command: push to `main` → CI → Render auto-deploy once all checks pass
("After CI checks pass" in the dashboard), which builds the Dockerfile; the container runs
`alembic upgrade head` at startup, before uvicorn. There is no bind mount in the image, so a
source change needs a rebuild.

## Architecture that matters

- **PostgreSQL + SQLAlchemy 2.0, fully synchronous.** `Mapped[...] / mapped_column(...)`
  declarative style, `Session` via the `get_db` FastAPI dependency. psycopg 3
  (`psycopg[binary]`). There is **no `Base.metadata.create_all()` anywhere** — Alembic
  (`0001_postgres_baseline` and its successors) is the only thing that builds the schema.
- **PostgreSQL rules the code relies on.** Timestamps are `timestamptz`, written as aware UTC
  and read back aware; every response serialises them through `schemas/types.py::UtcDateTime`
  (ISO-8601 UTC with `Z`). `games.started_at` is still truncated to whole seconds in
  `db_service._parse_timestamp`: `(room_code, started_at)` is the persistence idempotency key
  (`uq_games_room_code_started_at`) and `timestamptz` would otherwise keep microseconds.
  String comparison is case-sensitive, so a client-supplied room code is upper-cased before a
  database lookup (`games.find_latest_finished_game`), never left to a collation. Raw SQL binds
  Python `bool`s to boolean columns (no `0`/`1`), takes new ids with `RETURNING`, and upserts with
  `ON CONFLICT ... DO UPDATE` (`stats_service`; SQLite parses the same statement). `order` is a
  reserved word, hence `weeks.order_qty`; `min_value`/`max_value` likewise avoid aggregate
  names. A failed statement aborts the transaction: catch inside `db.begin_nested()` if the
  transaction must go on (as `user_service.get_or_create` does). `server_default` exists only on
  `created_at`/`updated_at` (`now()`); every fact column (`ended_early`, `is_bot`, …) is written
  explicitly by the app, and `test_db.py` / `test_schema.py` fail on any other default.
- **Async/sync split.** Socket handlers, services over the state store, and the routes are
  `async def`; everything touching SQLAlchemy is sync and runs on the threadpool FastAPI
  gives a sync dependency. The deep-health DB ping explicitly uses
  `anyio.to_thread.run_sync`. Do not call a sync `Session` from inside an `async def` without
  that.
- **D4 — the state store is authoritative for a live game; PostgreSQL is written only when a
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
  database write — `db_service.build_snapshot` works on a room document already read under
  the lock and runs after it is released; everything below it is lock-free. The lock is
  **not reentrant**: re-acquiring it deadlocks the room for
  `LOCK_TIMEOUT_SECONDS` (10). Read, mutate and save inside it; build payloads and mint
  `seq` inside it; emit and call any other `GameService` method only after it is released.
  `submit`, `run_bot_decisions`, `close_week_if_ready` and `broadcast_state` each take their
  own lock. `finish_game` takes two short ones around a lock-free persistence call: one to
  re-read, mint `game_finished`'s `seq` and save, one to re-read, set `persisted`, mint
  `game_persisted`'s `seq` and save — never saving the caller's copy, so a mutation made
  while the database write runs survives. Its callers (`_drive`, `close_week_if_ready`,
  `end_game_early`) call it only after their own lock block. The all-bot cascade is a loop of short acquisitions
  (`GameService._drive`), not recursion, and a double close is prevented by re-reading the
  room inside `close_week_if_ready`'s own lock. `start_game`'s `run_bot_decisions` call sits
  after its lock block.
- **Model / schema separation.** `app/models/` is SQLAlchemy only; `app/schemas/` is
  Pydantic only. The response shapes in `app/schemas/results.py` are **frozen** — the
  shipped frontend expects those exact names. Money fields there are declared `float`, never
  `Decimal`, because a `Decimal` on the wire breaks the frontend charts.
- **Money.** `float` in the engine, in the room document and in payloads; `NUMERIC(12,2)`
  in PostgreSQL (read back as `Decimal`, converted to `float` by the response schemas and
  `games.py`'s export rows). Rounding (`ROUND_HALF_UP`) happens **only** at the persistence boundary, in
  `db_service.round_money`. Quantities are `int` everywhere.
- **Purity of `app/core/`.** Domain modules import nothing from `app.config`,
  `app.services`, `app.db`, `app.sockets`, `app.api`; no I/O, no clock, no global `random`.
  Hard limits arrive as an injected `Limits` (`DEFAULT_LIMITS`), not by importing settings.
  `app/core/checks/`, `app/core/firebase.py` and `app/core/abuse.py` are the three
  non-domain exceptions inside that tree, and **no domain module may import any of them**.
- **Service layer.** One rule, one owner. `merge_config_patch` (`config_merge.py`) and
  `sanitise_display_name` (`display_name.py`) each exist once and are called from both the
  REST and socket collection points — do not reimplement them locally. `user_service._sanitise`
  is a *different* function for stored profile fields and must not be reused for display names.
- **API/socket contract with the frontend.** REST: `/api/v1/health`, `/health/deep`,
  `/users/upsert`, `/users/me`, `/users/me/stats`, `/users/me/games`,
  `/users/me/games/{public_id}`, `/users/me/hosted-games`, `/rooms/create`, `/rooms/presets`, `/rooms/limits`,
  `POST /rooms/preview`, `POST /rooms/batch-create`, `POST /rooms/summary`,
  `/rooms/{code}/status`,
  `GET|PUT /rooms/{code}/config`,
  `/games/id/{public_id}/results`, `/games/id/{public_id}/export`, `POST /games/claim`.
  Socket in: `connect`, `disconnect`, `join_waiting`, `join`, `leave`, `config_update`,
  `set_role_mode`, `assign_role`, `claim_role`, `release_role`, `start_game`, `submit_order`,
  `pause_game`, `resume_game`, `force_close_week`, `substitute_bot`, `reclaim_seat`,
  `end_game_early`, `request_state`.
  Socket out: `joined`, `join_error`, `host_claimed`, `leave_ack`, `lobby_update`,
  `config_updated`, `roles_assigned`, `game_started`, `game_paused`, `game_resumed`,
  `your_state`, `host_state`, `order_submitted`, `your_week_closed`, `week_closed`,
  `participant_disconnected`, `participant_reconnected`, `host_disconnected`,
  `host_reconnected`, `bot_substituted`, `seat_reclaimed`, `game_finished`,
  `game_persisted`, `error`.
  Any change here must ship with `Beery_Frontend/src/api/socketHandlers.ts` in the same
  window, backend first, unless the backend stops sending a field the live frontend reads
  (e.g. `own_history` leaving the per-round `your_state`): then frontend first, per
  `DEPLOY.md`.
- **`host_view()` carries `order_history`** (spec §9.6): `{role.value: [order per CLOSED
  week]}` in `ROLE_ORDER`, every key present and
  `[]` before the first close, **derived** from `history` (not in `to_payload`). It is every
  order quantity of the game so far, so `host_state` must stay `emit_to_sid` to the host. It
  is on spec §8.8's list of keys no room broadcast may carry, and `test_redaction.py` checks
  it. The frontend reads it as optional, so a frontend deployed ahead of this backend renders
  as before, but the intended order is still backend first.
- **Error handling.** REST: `HTTPException` with a **string** `detail`, never `str(e)`;
  log with `logger.exception` and return a generic message. Sockets: apply `@guarded()`
  *under* `@sio.event` — `guarded("error")` for play (`{message, code}`), `guarded("join_error")`
  for the lobby (`{message}`). It logs the traceback and the event name, never the payload.
  It is also the one place socket events are rate-limited (`core/abuse.py`, code
  `RATE_LIMITED`): never add a per-handler limit. REST is limited by
  `RateLimitMiddleware`, with one exception: `POST /rooms/batch-create` charges the
  room-create budget itself, `abuse.room_create_allowed(scope, count)` (all or nothing,
  `RateLimiter.allow(key, cost)`), after its preset and config have validated, so the
  middleware leaves that path on the general limit. The room-create budget is **30 rooms per
  address per hour** (`ROOM_CREATE_WINDOW_SECONDS`, the standard across the Novus games), shared by `/create` and `/batch-create` (≤ 10 a request): three full batches, then
  429. Both export routes share an **hourly**
  per-address budget (`GAME_EXPORT_LIMIT_PER_IP = 100` per `GAME_EXPORT_WINDOW_SECONDS`,
  CSV and JSON together), applied by the middleware; its 429's `Retry-After` is the seconds
  left in that hour (`RateLimiter.retry_after`). A classroom shares one address: never lower
  it. The first request from each IP source logs `client ip source: …` (never the IP), so the deployment's header can be checked in Render's logs.
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
  may carry `alias`; it may **never** carry `identity`, `host_uid`, `session_token` or `host_secret`.
  Redact server-side before sending: build payloads key by key, never spread a room document.
- **Authority is a capability**: `host_secret`, compared with `hmac.compare_digest`
  (D3). D18 also accepts a connection whose verified identity equals `host_identity`.
- **The host record (D23) is a list entry, never a credential.** `start_game`, past the
  secret check and `can_start`, writes `room["host_uid"]` from
  `socket_manager.sid_to_verified_uid[sid]` — set by the handshake only for an ID token that
  verified, never inferred from a missing `guest_` prefix — so a guest host is never recorded
  and the dashboard's "Start all" (no `join_waiting`) is. `host_uid` is server-only (in no
  payload; `test_redaction.py`, `test_host_record.py`). `persist_game` resolves it to
  `games.host_user_id` by lookup only (no `users` row → NULL, none created); the host has no
  `participants` row, and `host_identity` (D18's live key, possibly a guest id) is never
  persisted. Its one reader is `GET /users/me/hosted-games`. Nothing else may read it: no
  export, claim, results or authority path. History, stats and `/users/me/games/{id}` stay
  played-only.
  `room_service.check_host_identity` must return `False` when the stored `host_identity` is
  null, and only a secret claim may write `host_identity` — otherwise the D18 recovery path
  hands a fresh room to the first stranger.
- **`room['sid_to_alias']` in the room document is authoritative**;
  `socket_manager.sid_to_alias` is per-process bookkeeping. Read the room's, inside the
  lock. A reconnect moves the mapping (the stale sid is dropped), and the host is never in
  either map (`join_waiting` uses `sio.enter_room`, not `socket_manager.join_room`).
- **`connection.py`'s `disconnect` calls `play.on_participant_disconnected(sid)` before
  `socket_manager.disconnect(sid)`**, which pops the sid maps; reversed, the game silently
  never pauses.
- **Vocabulary** is the spec's §14: no synonyms ("round" is not a week) and never a bare
  `token` as an identifier.
- **Socket payload conventions.** Event names are snake_case; every client → server payload
  carries `room_id`, privileged ones also `host_secret`; every server → client game event
  carries the room's monotonic `seq`. Every mutating handler is idempotent: `join` by
  identity (`find_alias_for_reconnect` / `find_alias_for_identity`), `submit_order` by
  (room, week, role), replacing a pre-close resubmission and refusing a closed week, and
  `start_game` on a running room is a silent no-op.
- **Alias allocation scans for the lowest unused `P<N>`** (`next_free_alias`), never
  `len(participants) + 1` — that reissues a live participant's alias after a `leave`.
- **`app/db/base.py` must read `from ..models.base import Base`.** `from .base import Base`
  there is a self-import that raises `ImportError`.
- **Every range lives once, on `core.limits.Limits`, and is refused, never clamped**. `GameConfig.from_host_input` raises `ConfigValidationError` naming the field
  and the range for any number outside it; `GameEngine.submit_order` raises
  `OrderOutOfRangeError` for an order outside `order_bounds()` (sent as `INVALID_ORDER`). The
  host form reads the ranges from `GET /rooms/limits` and the order form from `your_state`
  (`min_order_quantity`, `max_order_quantity`) — the frontend keeps no copy, so add a bound to
  `Limits`, never to a form. The only repairs left: a cap of 0 means unlimited, an inverted
  stochastic range is swapped. There are no negative orders (a stored config still carrying an
  `allow_negative_orders` key loads and ignores it).
- **The server owns every game figure, not only the ranges** (spec §15). The frontend
  computes nothing a rule defines; when it needs a figure, add it
  to a payload or a route that calls **the function the engine itself uses**:
  - `POST /rooms/preview` (`core/preview.py`): a draft config → `demand_series`
    (`generate_demand_series`, with the host's `random_seed` or `EXAMPLE_SEED`),
    `demand_is_example` (`demand.uses_seed` and no seed) and per-role `lead_times`
    (`GameConfig.order_arrival_lead_weeks`, which the engine's `_order_arrival_lead_weeks`
    delegates to). No auth, nothing stored; a refusal is the `PUT /config` 422. It also
    returns the validated `config`, and takes an optional `patch` merged first by
    `merge_config_patch`: that is how the multi-game setup form edits a draft no
    room holds without copying the merge rule or a kind's defaults. `/rooms/presets` marks
    the `/create` default with `is_default`.
  - `/rooms/presets` `description` = `scenario_summary(preset)` + a figure-free tagline.
  - A demand-kind change may send `{kind}` alone: `from_host_input` fills the defaults, so the
    form keeps none.
  - `player_view`: `order_arrival_week`, `supply_line_slot_weeks`,
    `orders_in_flight_slot_weeks` (`Pipeline.weeks_until_out`); `settlement.inventory_after_arrival`
    (a stored settlement without it is rebuilt with `opening_inventory + arrived` in
    `_settlement_from_payload`). `host_view`: `weeks_played` (in `test_host_view_order_history`'s
    and `test_game_engine`'s frozen key sets).
  - The bot is milder at β=1, **not steady** (on flat demand of 4 it orders 9–14 first): its
    target is 3× opening stock, it has no desired-supply-line term and its supply line is
    shipments in transit only. Don't let a docstring or label promise otherwise
    (`test_bot.py` asserts it).
- **Multi-game hosting** (spec §9.10) is two REST routes over the ordinary room,
  nothing more: the rooms are plain rooms, and each is still claimed and run from its own
  host screen. `POST /rooms/batch-create` resolves the preset with `/create`'s
  `_preset_config` and validates the patch with the config PUT's `_validated_config`
  (`merge_config_patch` + `GameConfig.from_host_input`), **before** it charges the limiter or
  makes a room; a `StoreFullError` part-way is rolled back with `state_svc.delete_room` for
  every room already made, then answered 503. `POST /rooms/summary` must stay read-only (no
  `lock`, no `save_room`: polling must not refresh the 24 h TTL) and must answer a missing
  room and a wrong secret identically; its `can_start` is `RoomService.can_start`, never a
  copy. The batch bounds are `Limits.min_batch_rooms` / `max_batch_rooms`, served by
  `/rooms/limits`. The dashboard's "Start all" is the ordinary `start_game` emit:
  `start_game` is authorised by `host_secret` alone, and its `host_state` goes to
  `host_sid` (the claimed host tab) or, when none is claimed, to the caller. **Every refusal
  of `start_game` names its room** (identical in every Novus game): `join_error`
  `{message, room_code}` (`_reject_start`, and `@guarded("join_error", echo_room_code=True)`
  for its rate-limit and server-error replies), so the dashboard puts it on that game's card.
  Additive: listeners reading `message` are unaffected.
- **Results and export are by `public_id` only** (D22), the same in every Novus game.
  `GET /games/id/{public_id}/results` and `/export` take no credential, never expire and
  answer 404 for an unknown id; anyone who can open the results may download them (a guest
  host has no account to fall back on). **There is no route by room code** — a six-character,
  reissued code names no game and could be enumerated — so never add one back, and no route
  takes or returns an internal `games.id` either (`MatchSummary.public_id`,
  `UserStatsResponse.best_game_public_id`, `/users/me/games/{public_id}`). The export may carry only what the results
  screen shows: never add a uid, email, `user_id`, guest identity, session token or secret to
  `build_week_export_rows` or `ResultsResponse` (`tests/test_api/test_export_open.py` checks
  both formats without Docker). The frontend's once-per-session download button is UX only;
  the hourly budget above is the guard. **No chosen name is in the file**: every
  player is their seat's `P<N>` (`export_service.seat_alias`, Retailer `P1` … Factory `P4`)
  in `display_name`, rows and the JSON's `per_role` alike; the screens keep the names. **A
  finished export is cached** in `services/export_cache.py`: an in-process LRU of 64 files
  keyed by `(public_id, format)`, final bytes, no expiry (a stored game never changes), 200s
  only. No Redis or DB, so it is per instance and empty after a restart. The rate limit is
  charged by the middleware before the route, so a cached download still counts.
- **No screen shows a single "week cost".** `WeekRecord.total_week_cost` (and the
  `weeks.total_week_cost` column, the export's `total_week_cost`) is holding + backlog + order
  cost; `player_view` sends `week_holding_backlog_cost` and `week_order_cost` (`null` until the
  week closes). `end_early()` rolls the abandoned week's cost back out of every live total.
- **The hard limits are not settings, on purpose.** `Settings` declares exactly one of
  them, `MAX_DISPLAY_NAME_LENGTH` (consumed by `display_name.py` and `user_service.py`).
  The game ceilings live on `core.limits.Limits` / `DEFAULT_LIMITS` (re-exported by
  `core.config_models`) and are
  injected, because `app/core` may not import `app.config`; `ROOM_TTL_SECONDS` and
  `LOCK_TIMEOUT_SECONDS` are module constants in `services/state_service.py`, `MAX_ROOMS` is
  one in `services/state_backend.py`, and every abuse limit is one in `core/abuse.py`
  (README "Abuse limits"). No other limit is a `Settings` field: a variable the app ignores
  is worse than no variable, because `.env` then lies. Change a ceiling at the site that
  owns it.
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
- **Writing tests.** Every `tests/test_<area>/` has an `__init__.py` and `tests/` itself has
  none, or a second `conftest.py` makes `from conftest import ...` bind to the wrong module.
  A throwaway mapped class gets its own `DeclarativeBase`, never the app's `Base` (it would
  pollute `Base.metadata` for the session and break the `dbschema` comparisons). A
  concurrency test against the fake store proves nothing unless the critical section forces
  a suspension point (`await asyncio.sleep(0)`): confirm it goes red with the lock removed.
  Likewise break the code once to see any new regression test fail, and when a database
  constraint is named as a backstop, check it is reachable from the race (`persist_game`'s
  is `uq_games_room_code_started_at`, not the `weeks` key). Assert against injectable
  inputs, not live wiring; never let a test skip itself once later code lands; no two file
  names may differ only by case.
- **`app/core/checks.register_check` has no unregister.** A check a test registers stays for
  the whole session, so tests that need isolation reload the `app.core.checks` package. A
  raising check is logged and never aborts boot, so a leaked one can't break startup.
- **Migrations.** A schema change is a new revision on the single linear chain from
  `0001_postgres_baseline` (`test_migration.py` asserts contiguous numbers and one head),
  never an edit to an applied revision, and it regenerates `db/schema.postgres.sql` in the
  same change. `downgrade()` drops in reverse dependency order. A foreign-key column that is
  queried gets an explicitly named index in the model (`ix_user_stats_best_game_id`):
  PostgreSQL never creates one for a foreign key.
- **A known unexplained flake:** `tests/test_api/test_profile.py` (`dbschema`) once failed
  three tests ("expected 3, got 0" and a 404), then passed every rerun. If it fails again,
  suspect commit visibility between the test session and the request's session before
  suspecting the route.
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
- Before "adding a feature", check the spec's §13 Out of scope and §16 Not built yet —
  spectators, timers, chat, `clone_room`, per-week write-through and non-4-stage chains are
  all deliberately out of v1.
