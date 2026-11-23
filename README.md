# Beery — Backend

FastAPI + Socket.IO server for the Beer Distribution Game. Python 3.11, PostgreSQL 18 (psycopg 3)
for the durable record, and an optional Redis for live room state.

The game's rules, data model and API are specified in
`../Beery_Frontend/docs/beer-game-spec.md`; its §15 records the decisions (`D<n>`) that code
comments cite.

---

## Prerequisites

- **Python 3.11** — create the venv with `python3.11 -m venv venv`, never bare `python3`. On a
  machine whose default `python3` has moved on, the bare form silently builds the venv on a
  different interpreter than `python:3.11-slim` runs.
- **Docker**, for the `dbschema` schema and migration suite (Testcontainers) and the image
  build test. Not needed to run the app.
- **PostgreSQL 18** is needed for anything that finishes a game or reads a profile: the
  durable record is written when a game ends, and `/api/v1/users/*` and `/api/v1/games/*` read
  it. Lobbies, configuration and play itself run without it. Locally,
  `docker run --rm -e POSTGRES_PASSWORD=… -e POSTGRES_DB=beerydb -p 127.0.0.1:5432:5432 postgres:18`
  is enough (`DB_HOST=localhost`, `DB_USER=postgres`, `DB_REQUIRE_SSL=false`, empty
  `DB_SSL_CA`). Develop against this, not Beery's Neon database: a local backend on Neon is one
  more instance and breaks the connection budget during a deploy (`DEPLOY.md`). `alembic upgrade head` builds the schema; there is no
  `create_all()`.
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
DB_HOST=127.0.0.1 DB_PORT=15432 DB_REQUIRE_SSL=false DB_SSL_CA= REDIS_URL=redis://127.0.0.1:16379/0 \
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
| `GET` | `/api/v1/health/deep` | none | `{status, database, redis, state_backend}`, always HTTP 200; the answer is reused for 15 s |
| `POST` | `/api/v1/rooms/create` | none (D3) | A new room: `room_code`, the `host_secret` (returned exactly once), `state`, `config` |
| `POST` | `/api/v1/rooms/batch-create` | none (D3) | The multi-game dashboard's create: `{count, preset?, config?, host_display_name?}` → `{rooms: [...]}`, each shaped like `/create`'s answer with its own `host_secret`. `count` is `Limits.min_batch_rooms`–`max_batch_rooms` (2–10). `config` is a patch over the preset, merged and validated once like `PUT /config`; an unknown preset (400) or a refused config (422 naming the field) creates nothing. A store that fills part-way deletes the rooms the batch made and answers 503 |
| `POST` | `/api/v1/rooms/summary` | host secret per room, in the body | `{rooms: [{room_code, host_secret}]}` (1–10) → one `{room_code, exists, state, seats_taken, seats_total, week, duration_weeks, finished, can_start, start_blocked_reason}` per entry, in order. A missing room and a wrong secret both answer `exists: false` with every other field `null`. Read-only (no lock, no save, no TTL refresh); never carries a secret |
| `GET` | `/api/v1/rooms/presets` | none | The three presets with their full configs, for the host UI; each `description`'s figures are written from the preset's own config (`scenario_summary`), and `is_default` marks the one `/create` uses when none is named |
| `GET` | `/api/v1/rooms/limits` | none | Every range the host form checks (`core.limits.Limits`); the frontend keeps no copy |
| `POST` | `/api/v1/rooms/preview` | none | `{config, patch?}` → the `demand_series`, `demand_is_example` and per-role `lead_times` that draft plays, from the engine's own generator and lead-time function (`core.preview`), plus the validated `config`. A `patch` is merged over `config` first by `merge_config_patch`, as a config save merges it (the multi-game setup form edits its local draft this way); a refused draft is the same 422 as `PUT /config` |
| `GET` | `/api/v1/rooms/{code}/status` | none | Is this room joinable? Always HTTP 200 — a 404 would be a room-code oracle. `host_display_name` is always `null` |
| `PUT` | `/api/v1/rooms/{code}/config` | host secret | Applies a config patch while the room is still configurable |
| `GET` | `/api/v1/rooms/{code}/config` | host secret | The current config |
| `GET` | `/api/v1/games/id/{public_id}/results` | none | The full results payload of a finished game, by its permanent `public_id` (D22): shareable, no time limit, 404 for an unknown id. There is no results or export route by room code (a code is short and reissued, so it names no game) |
| `GET` | `/api/v1/games/id/{public_id}/export` | none | The complete week-by-week record, `?format=csv` (default) or `json`, by permanent id, open to anyone who can see the results (D22): a guest host has no account to fall back on. It carries the results screen's figures and no uid, email, user id, guest id, session token, secret **or chosen name**: every player is their seat's `P<N>` (Retailer `P1` … Factory `P4`), in CSV and JSON alike. 404 for an unknown id. Limited to 100 an hour per address (below). A finished export is kept in an in-process LRU (64 files, keyed by `(public_id, format)`, no expiry, 200s only), so a repeat download reads nothing from the database; the cache is per instance and empty after a restart |
| `POST` | `/api/v1/games/claim` | Firebase | Attributes a guest's finished games (the seats that guest id played, never a hosting) to the signed-in caller |
| `POST` | `/api/v1/users/upsert` | Firebase | Creates or refreshes the caller's profile. The uid comes from the token, never the body |
| `GET` | `/api/v1/users/me` | Firebase | The caller's profile, or 404 if it was never upserted |
| `GET` | `/api/v1/users/me/stats` | Firebase | Aggregate statistics (the best game as `best_game_public_id`); a caller with no history gets a zero-filled 200, not a 404 |
| `GET` | `/api/v1/users/me/games` | Firebase | Match history (played seats only), newest first, `?page` / `?page_size` (clamped to 1–100); each row names its game by `public_id` |
| `GET` | `/api/v1/users/me/games/{public_id}` | Firebase | One of the caller's own games in full, by `public_id` (no route takes an internal id); 404 — never 403 — if they did not play a seat in it (hosting it does not count) |
| `GET` | `/api/v1/users/me/hosted-games` | Firebase | The games the caller hosted while signed in (D23), newest first, `?page` / `?page_size` (clamped to 1–100): `{games: [{public_id, room_code, finished_at, player_count, ended_early}], total, page, page_size}`. Read from `games.host_user_id`; it lists and grants nothing |

In `/health/deep`, `status` is `"ok"` only when every backing service in use is reachable;
otherwise `"degraded"`. With `REDIS_ENABLED=false` the unused Redis is not probed and is not
a reason to degrade, and `state_backend` reports `"memory"` rather than `"redis"`.

Every timestamp in a response or a JSON export (`started_at`, `finished_at`) is ISO-8601 UTC
with a `Z` suffix (`2026-11-18T14:00:00Z`, `app/schemas/types.py::UtcDateTime`), and money is
a JSON number, never a string. A `{code}` in a games route is case-insensitive: the code is
upper-cased before the lookup, as the live room lookup does.

### Abuse limits

`app/core/abuse.py` holds every limit as a module constant (none is a setting), counted in
this process's memory per client address: `CF-Connecting-IP` when present, else the
**rightmost** non-empty `X-Forwarded-For` entry (Render's proxy appends it), else the TCP
peer. Every window is 60 s except room creation's and the export's, which are an hour. The first request from each IP source logs `client ip source: …` (never the IP), so the deployment's header can be checked in Render's logs.

| What | Limit | Over it |
|---|---|---|
| Any REST request except `/api/v1/health` | 1200 per address | `429 {"detail": "Too many requests. Please slow down and try again."}`, `Retry-After` = seconds left in the window (60 at most) |
| `POST /api/v1/rooms/create` | 30 rooms per address per **hour** (`ROOM_CREATE_LIMIT_PER_IP`, `ROOM_CREATE_WINDOW_SECONDS`; the standard across the Novus games) | the same 429, `Retry-After` = seconds left in that hour |
| `POST /api/v1/rooms/batch-create` | `count` (at most 10) against the same 30 rooms per address per hour, all or nothing (`room_create_allowed`, charged by the route after validation; the middleware leaves it on the general limit): three full batches, then the next batch or single create is refused | the same 429; a refused batch, or one refused for its preset or config, is charged nothing |
| `GET /api/v1/rooms/{code}/status` | 120 per address | the same 429 |
| `POST /api/v1/rooms/preview` | 300 per address | the same 429 |
| `GET /api/v1/games/id/{public_id}/export` (CSV and JSON together) | 100 per address per **hour** (`GAME_EXPORT_LIMIT_PER_IP`, `GAME_EXPORT_WINDOW_SECONDS`). The export by `public_id` is open, so this is its compute guard; a classroom shares one address, so never lower it | the same 429, `Retry-After` = seconds left in that hour |
| Open sockets | 200 per address | the handshake is refused (`connect_error`, message `Too many connections from your network. Try again in a minute.`; a failed ID token gets the generic refusal) |
| `submit_order` resubmissions | 20 per role per week (`MAX_RESUBMISSIONS_PER_ROLE_PER_WEEK`). The counter is process-local, keyed by (room code, week, role), pruned at the week's close, at the game's finish (ended early too) and, for a room that expired or was deleted mid-week, once older than a room's 24 h lifetime; deliberately not in the room document: a socket is pinned to one worker, and a document field would add a store write per increment | `error {message, code: "TOO_MANY_SUBMISSIONS"}`; the last accepted order stands |
| Socket events | 120 per connection | `error {message, code: "RATE_LIMITED"}` (play) or `join_error {message}` (lobby); the handler does not run |
| `join` + `join_waiting` | 300 per address | as above |
| One REST request body | 64 KB (`MAX_BODY_BYTES`), a declared `Content-Length` or a chunked body as it is read | `413 {"detail": "That request is too large."}`; an oversized `Content-Length` is refused before anything is counted |
| One socket message | 64 KB (`max_http_buffer_size` = `MAX_BODY_BYTES`) | refused by Engine.IO |
| Live rooms, in-memory backend | 500 (`MAX_ROOMS` in `state_backend.py`) | `POST /rooms/create` answers `503`; `POST /rooms/batch-create` deletes the rooms it already made, then answers the same `503` |

The numbers are sized for a classroom of about forty people behind one NAT address. A room
code that `generate_room_code` could not have minted is refused before any lock or read
(`is_room_code`), and a handshake `idToken` over 4096 characters is rejected unverified.

### The socket surface

`/socket.io/`, handled by the auto-discovered modules in `app/sockets/handlers/`. Identity is
resolved once, at `connect`, from an optional Firebase token in the handshake `auth`, and is
**server-side only** thereafter — no payload may carry it.

- **Connection** — `connect`, `disconnect`.
- **Lobby** (`lobby.py`) — in: `join_waiting`, `join`, `leave`, `config_update`,
  `set_role_mode`, `assign_role`, `claim_role`, `release_role`, `start_game`.
- **Play** (`play.py`) — in: `submit_order`, `pause_game`, `resume_game`, `force_close_week`,
  `substitute_bot`, `reclaim_seat`, `end_game_early`, `request_state`.
- **Out** — `joined`, `join_error`, `host_claimed`, `leave_ack`, `lobby_update`,
  `config_updated`, `roles_assigned`, `game_started`, `game_paused`, `game_resumed`,
  `your_state`, `host_state`, `order_submitted`, `your_week_closed`, `week_closed`,
  `participant_disconnected`, `participant_reconnected`, `host_disconnected`,
  `host_reconnected`, `bot_substituted`, `seat_reclaimed`, `game_finished`,
  `game_persisted` (after a successful database write; carries the `public_id`), `error`.
  A failed end-of-game write is retried in the background three times (after 2, 10 and
  30 s), and the host or a player rejoining, or asking for state in, a finished room whose
  game is still unsaved starts it again; `game_persisted` follows whichever write lands.
  Recipients and payloads are in the spec's §11.2.

`your_state` carries the role's whole `own_history` only where a client may have missed a
record: game start, `join` (a reconnect), `request_state`, a stale-week resend and the
refresh after `resume_game` (which then plays any bot seat still owing the open week its
order, so a bot swapped in while paused does not freeze the week). The `your_state` after a
submission or a week close leaves it
out; the week's one new record travels in `your_week_closed` and the client appends it.
Every `request_state` reply carries a fresh `seq`, above every number already sent. The host
gets a fresh `host_state` after every submission (a person's, or a round of bot orders),
including the one that completes the week, so the console's "decided" marks tick as orders
arrive and the final week's console shows all four decided; the close then sends the new
week's `host_state`.

Every event name and payload here is a contract with `Beery_Frontend/src/api/socketHandlers.ts`.
A change to either must ship with the other, backend first, unless the backend stops sending a
field the live frontend reads: then the frontend ships first (see `DEPLOY.md`).

### Not built here

Deliberately out of v1: spectators, turn timers, chat, `clone_room`, per-week write-through to
PostgreSQL, and chains that are not four stages. The spec's §13 Out of scope and §16 Not built
yet list them; check there before "adding" one.

## Tests

```bash
./venv/bin/python -m pytest -m "not dbschema"   # everyday run, no Docker needed
./venv/bin/python -m pytest tests/test_core     # one area
```

`-m "not dbschema"` is the everyday run. The deselected tests are the schema and migration
suite (the migrations, `db/schema.postgres.sql`, the models-match check and the persistence
round trips), which needs a Docker daemon because it starts a real `postgres:18` with
Testcontainers:

```bash
./venv/bin/python -m pytest -m dbschema
```

The schema suite cannot use SQLite: it would pass every constraint, reserved word and
`server_default` check vacuously. `tests/test_models/conftest.py` starts one session-scoped
`postgres:18` container, migrates it once and runs each test in a transaction it rolls back. It
needs no `DB_*` credentials and refuses any target but its own container, so it can never be
aimed at a real database. Without Docker it skips loudly (a skip reason plus a warning), and CI
fails a run in which nothing passed.

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
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_DATABASE` | PostgreSQL (default port 5432, user `postgres`); built into `postgresql+psycopg://…` with the credentials percent-encoded. Every session runs in UTC (`options=-c timezone=UTC`), and a connection attempt gives up after 10 s (`connect_timeout=10`); there is no `statement_timeout` (it would ride in the startup `options`, which a pooled Neon endpoint refuses). |
| `DB_REQUIRE_SSL`, `DB_SSL_CA` | TLS. `DB_REQUIRE_SSL=true` alone is `sslmode=require` (encrypted, server not verified); `DB_SSL_CA` — the Let's Encrypt roots ISRG Root X1 + X2 that Neon's certificates chain to (one public PEM bundle, the same for all four games), as PEM text or a path — is `sslmode=verify-full`. The boot log states which. |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | Connection pool per process, default 7 + 2 = 9: Beery's budget is 20 connections, deploys included (two overlapping instances, the startup migration and one psql session; `DEPLOY.md`). |
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
store, and PostgreSQL is written only when a game finishes (**D4** in the spec's §15). Which store
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
2. **Restarting the process ends every game in progress.** A game reaches PostgreSQL only when it
   finishes, so a deploy or a crash mid-session loses the rooms. Redis survives an app restart;
   it does not survive its own restart or eviction, so this is a narrowing of the window rather
   than a guarantee.

The app says which backend is active every time it starts, at WARNING for the in-memory one.
`GET /api/v1/health/deep` reports it as `state_backend: "memory" | "redis"`.

A room document expires 2 hours after its last save while its game has not started
(`UNSTARTED_ROOM_TTL_SECONDS`; rooms come from an unauthenticated route) and 24 hours after
it once it has (`ROOM_TTL_SECONDS`); every save refreshes it, and `POST /rooms/summary`
never does.

The in-memory store refuses a new room past `MAX_ROOMS` (500) live ones -- `POST /rooms/create`
answers 503 -- and sweeps expired rooms from the write path at most once a minute. Rewriting
a room it already holds is never refused. With Redis on there is no such ceiling here; bound
Redis with its own `maxmemory`.

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
│       ├── user_games.py       /users/me/stats, /users/me/games[/{id}], /users/me/hosted-games
│       ├── rooms.py            /rooms/create, /rooms/batch-create, /rooms/summary, /rooms/presets, /rooms/limits, /rooms/preview, /rooms/{code}/status|config
│       └── games.py            /games/id/{public_id} results and export, /games/claim
├── core/                       pure domain logic — no I/O, no clock, no global RNG
│   ├── enums.py                Role, RoomState, DemandKind, Distribution, …
│   ├── config_models.py        GameConfig; validation (out of range is refused, never clamped)
│   ├── limits.py               Limits/DEFAULT_LIMITS, the hard ceilings
│   ├── presets.py              CLASSIC_MIT, FAST_GAME, CHAOS; scenario_summary() writes their card figures
│   ├── preview.py              what a draft config plays (demand series, lead times), for the host form
│   ├── demand.py               the customer demand generator
│   ├── pipeline.py             the shipment and order delay pipelines
│   ├── agents.py               one station's settle-and-order step
│   ├── game_engine.py          the week loop; the only thing that mutates game state
│   ├── bot.py                  the Sterman anchor-and-adjust bot
│   ├── records.py              the week/game record shapes the engine emits
│   ├── stats.py                end-of-game statistics, including the bullwhip ratio
│   ├── firebase.py             credential loading (not a domain module — see below)
│   ├── abuse.py                rate limits and socket caps (not a domain module — see below)
│   └── checks/                 ← auto-discovered startup-check package
│       ├── __init__.py         register_check(), run_startup_checks()
│       ├── db_tls_check.py
│       ├── firebase_check.py
│       └── state_backend_check.py
├── db/
│   ├── base.py                 re-exports Base — `from ..models.base import Base`
│   └── session.py              engine, SessionLocal, get_db
├── models/                     SQLAlchemy 2.0; the package imports every module in it
│   ├── base.py                 Base, TimestampMixin, utcnow()
│   ├── user.py                 users, user_stats
│   └── game.py                 games, configs, participants, weeks
├── schemas/                    Pydantic request and response models (user, room, results; types.UtcDateTime)
├── services/                   the stateful layer over the state store and PostgreSQL
│   ├── state_backend.py        the REDIS_ENABLED switch and the in-memory backend
│   ├── state_service.py        room documents, the lock, TTL
│   ├── room_service.py         lobby and seating rules
│   ├── game_service.py         driving the engine through a week
│   ├── db_service.py           the end-of-game write; the only money-rounding point
│   ├── stats_service.py        user_stats maintenance
│   ├── claim_service.py        attributing a guest's games to an account
│   ├── export_service.py       CSV and JSON export rows
│   ├── export_cache.py         the in-process LRU of finished exports
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

alembic/                        env.py reads settings.db_url; versions/0001_postgres_baseline, 0002_games_host_user_id (head)
db/schema.postgres.sql          the generated DDL of head (`Novus/scripts/db/gen_ddl.py`)
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
handler (D19). Each package also works while empty.

**`app/core/` is pure, with three named exceptions.** The domain modules — `enums`,
`config_models`, `limits`, `presets`, `preview`, `demand`, `pipeline`, `agents`, `game_engine`,
`bot`, `records` and `stats` — import nothing from `app.config`, `app.services`, `app.db`, `app.sockets` or `app.api`,
perform no I/O, and read no clock and no global RNG. The hard ceilings arrive as an injected
`Limits`, never as an import of `settings`. `app/core/checks/`,
`app/core/firebase.py` and `app/core/abuse.py` are *not* domain modules (they perform I/O or
read the clock); the rule that matters is that **no domain module may import any of them**.

**`app/db/base.py` must read `from ..models.base import Base`.** Writing `from .base import Base`
inside that file is a self-import that raises `ImportError` on any use.

## Docker

```bash
docker build -t beery-backend .
docker run --rm -p 8080:8080 --env-file .env beery-backend
```

`python:3.11-slim`, plain uvicorn with `--http h11 --proxy-headers`. `.dockerignore` excludes
`.env`, `tests/`, `venv/` and `.git/`.
