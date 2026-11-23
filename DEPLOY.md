# Deploying Beery_Backend

Mirrors the Tequila Game (D17 in `../Beery_Frontend/docs/beer-game-spec.md` §15): a
Docker container on Render, the database `beerydb` on the Aiven PostgreSQL service all
four Novus games share, and — **optionally** — a managed Redis instance (*Redis is
optional*, below).

## Topology

```
Browser --HTTPS--> Firebase Hosting (Beery_Frontend, static)
Browser --HTTPS--> Render: Beery_Backend  (/api/v1/*)
Browser --WSS----> Render: Beery_Backend  (/socket.io)  -- direct, NOT via Hosting
                       |
                       +--> Aiven PostgreSQL 17 (durable record, written at game end)
                       +--> managed Redis   (live room state + Socket.IO pub/sub)
                            OPTIONAL: only when REDIS_ENABLED=true. With it
                            off, that state lives inside the one container.
```

Firebase Hosting does not proxy WebSocket upgrades, so the frontend's
`VITE_SOCKET_URL` must point directly at this service's Render host in
production, never at the Hosting origin.

## Build and run

- The service is a plain Docker web service on Render's **free** plan,
  configured in the Render dashboard; `render.yaml` records its settings but
  is not linked as a Blueprint. It builds the committed `Dockerfile` (plain
  uvicorn, `--http h11 --proxy-headers`, no gunicorn).
- **Migrations run at container startup.** Free instances have no pre-deploy
  command, so the Dockerfile `CMD` is `alembic upgrade head && exec uvicorn
  ...`: the container migrates before it serves -- never `create_all()`. On a
  database already at head it is a no-op. A failed migration exits the
  container non-zero, the new instance never becomes healthy, and Render keeps
  the previous deploy live; the deploy log shows the Alembic error. Run
  exactly **one** instance: two containers starting together would race the
  same migration.
- The image bakes in a **code snapshot**: there is no bind mount. **A source
  change needs a rebuild** -- a running container will happily keep serving
  the old code, which looks exactly like "my fix didn't work". Always confirm
  a new image was actually built (the Render deploy log shows a fresh build,
  not a cache hit) before concluding a fix did not work.
- For local iteration, run `uvicorn app.main:application --reload` instead of
  the container.

## Environment (set on the platform, never only in a local `.env`)

```
DEBUG=false
CORS_ORIGINS=["https://beersim.web.app","http://localhost:5173"]
DB_HOST= DB_PORT= DB_USER= DB_PASSWORD= DB_DATABASE=
DB_REQUIRE_SSL=true
DB_SSL_CA=
REDIS_ENABLED=false
REDIS_URL=
FIREBASE_SERVICE_ACCOUNT_JSON=
```

- `CORS_ORIGINS` is **never** `["*"]` -- the app sets `allow_credentials=True`
  and rejects the combination at startup (`Settings` raises).
- `FIREBASE_SERVICE_ACCOUNT_JSON` is the whole service account JSON **on one
  line**, in single quotes or unquoted. With double quotes, python-dotenv
  decodes the `\n` inside `private_key` into real newlines, the value stops
  being valid JSON, and the variable is effectively unusable -- the app still
  starts (guests can still play) but logs a CRITICAL warning at boot rather
  than failing silently. Never commit or log this value; rotate it if it is
  ever printed or pasted anywhere.
- `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_DATABASE` are the
  connection details of the shared Novus Aiven PostgreSQL service, with
  the user `avnadmin` and the database `beerydb` (Aiven's port, not 5432, is
  usual). Credentials may hold any character: the app percent-encodes them
  into `postgresql+psycopg://…`.
- `DB_REQUIRE_SSL=true` with `DB_SSL_CA` set is the production setting: the
  CA certificate of **the shared Novus Aiven PostgreSQL service** (Aiven
  console → service → *CA certificate*; the same file for all four games),
  pasted as PEM text or given as a path (locally e.g.
  `<Novus>/novus-secrets/aiven-pg-ca.pem`). It
  turns on `sslmode=verify-full` (certificate and hostname verified). Without
  it the link is `sslmode=require`: encrypted, but the server is not
  authenticated, and the boot log says so at WARNING.
- `REDIS_ENABLED` decides whether this deployment has a Redis dependency at
  all. See *Redis is optional* below. It is `false` by default.
- `REDIS_URL` is read **only when `REDIS_ENABLED` is `true`**, and is then
  required: the app refuses to start with the flag on and an empty URL,
  naming the setting rather than complaining about URL schemes. When it is
  used it must point at a **live**, non-free-tier instance. A reclaimed
  free-tier Redis returns `NXDOMAIN` on its hostname and every room operation
  fails -- this takes the whole game down, not a feature of it.

## Redis is optional

Live room state -- lobbies, seating, the running `GameEngine` -- is
authoritative in a key-value store, and PostgreSQL is written only when a game
finishes (**D4**). Which store holds it is one environment
variable, and **the default is no Redis at all**.

**`REDIS_ENABLED=false` (the default).** Room documents live in a dict inside
the process; `app/sockets/manager.py` uses the in-process Socket.IO manager.
Nothing is provisioned, nothing can be misconfigured, and `/api/v1/health/deep`
reports `state_backend: "memory"` and `redis: true` (an unused dependency is
not a degradation). Two deployment constraints come with it, neither of which
the process can detect from the inside:

1. **Exactly one instance, and no `--workers`.** Two instances means two
   players in one room land on different processes and never see each other
   -- no error, no log, just a lobby that will not fill. Scaling past one
   instance and turning Redis on must be the same change.
2. **A restart ends every game in progress.** A game reaches PostgreSQL only when
   it finishes, so a deploy or a crash mid-session loses the live rooms.
   Warn the class, or finish the session first.

The app logs which backend is active at every boot, at WARNING for the
in-memory one.

The abuse limits (`README.md`, "Abuse limits") are counted in the same
process memory, so they share constraint 1: at N instances each becomes N
times looser. They key on `CF-Connecting-IP`, else the **rightmost**
`X-Forwarded-For` entry, which assumes Render's edge sets the first or is the
last hop appending the second. If the service is ever reachable without that
edge, `CF-Connecting-IP` becomes client-chosen; if a second proxy is put in
front, every visitor shares one budget. Revisit
`app/core/abuse.py::client_ip` in the same change.

**`REDIS_ENABLED=true`.** Set `REDIS_URL` in the same change; the two are
required together. Room state moves to Redis and the Socket.IO client manager
becomes `AsyncRedisManager`, which is what makes cross-instance fan-out work
and is the one thing the in-memory backend cannot replace. `/health/deep`
then reports `state_backend: "redis"` and a real `redis` reachability probe.
Redis survives an app restart; it does not survive its own restart or an
eviction, so this narrows the window rather than closing it.

For a classroom deployment on one Render instance, `false` is the right
setting. Turn it on before the first moment there is a second instance.

## Managed PostgreSQL version

The database is `beerydb` on the **Aiven PostgreSQL 17** service that all four
Novus games (Selly, Lemony, Pulky, Beery) share: each game has its own
database on it, every game connects as the service's `avnadmin` user, and
one CA certificate serves all four. It **must run PostgreSQL 17**:
`tests/test_models/conftest.py` pins the CI schema suite's Testcontainers image
to `postgres:17` for exactly this reason -- proving the schema against a
different major version proves the wrong engine.

- **Schema.** Alembic owns it: `alembic/versions/0001_postgres_baseline.py`
  builds the whole schema, later revisions follow it. The reviewed DDL is
  committed as `db/schema.postgres.sql`, generated by
  `python3 scripts/db/gen_ddl.py --backend Beery/Beery_Backend` (from
  `Novus/`; it runs `alembic upgrade head` in a throwaway `postgres:17`
  container). The schema job fails if the migrations and that file drift.
  Regenerate it in the same change as any migration.
- **Time.** Every timestamp column is `timestamptz`; every session runs in UTC
  (`options=-c timezone=UTC`), and the API returns times as ISO-8601 UTC with
  a `Z` suffix.
- **Connections.** All four games share one Aiven PostgreSQL 17 service
  (Free plan, 20 connections), each with its own database and role. Per
  instance at most `DB_POOL_SIZE + DB_MAX_OVERFLOW`: 2 + 1 = **3** for Selly,
  Pulky and Beery, 3 + 1 = 4 for Lemony (a request waits up to 10 s for a
  connection, then fails). Normal running: 3 + 3 + 3 + 4 = 13. During a
  deploy Render runs the old and new instance of that game side by side (at
  most +4 = 17), the new container's startup `alembic upgrade head` takes 1
  and one operator `psql` session 1: 19 of 20. So **deploy one game at a time**. A
  backend run locally against the shared service is one more instance;
  prefer a local Docker PostgreSQL for development. On a bigger plan raise
  both variables (they are not in `render.yaml`; set them in the Render
  dashboard).

## The shared wire protocol -- ship together

Every Socket.IO event name and payload (`README.md`, *The socket surface*) is a
contract between two separately deployed artefacts (this repository and
`Beery_Frontend`). An old frontend against a new backend means nobody can
play.

**When a change touches anything under `app/sockets/handlers/` or
`src/api/socketHandlers.ts`, deploy `Beery_Backend` and `Beery_Frontend` in
the same window, backend first.**

**The exception: a backend change that drops a field the live frontend reads
ships frontend first.** Backend first would leave the old frontend reading a
field that no longer arrives, so the new frontend has to tolerate the field's
absence and be live before the backend stops sending it. Leaving
`own_history` out of the per-round `your_state` is one such change: the old frontend stores `your_state` as it arrives, and its
`OwnHistoryChart` throws on the missing list.

**A REST route the live frontend still calls is removed frontend first**, for the
same reason: the new frontend must stop calling it before the backend drops it.
The public_id-only results change (spec D22) is one: the previous frontend fetched
`GET /games/{room_code}/results`, which the backend no longer serves, while the new
frontend calls only routes the previous backend already had for its results flow
(`GET /games/id/{public_id}/results`, `/export`, `game_persisted`). The new profile
needs the new backend (`/users/me/hosted-games`, `/users/me/games/{public_id}`,
`public_id` on history rows, `best_game_public_id`), so deploy the backend right
after the frontend, in the same window: in between, only the profile's links and
its hosted list are degraded.

## Deploy checklist

Run this before a class, every time.

- [ ] Frontend and backend deployed from the same commit window (see above).
- [ ] CI built the frontend -- confirm the Hosting release timestamp moved.
- [ ] Backend environment set **on the platform**: `FIREBASE_SERVICE_ACCOUNT_JSON`,
      `CORS_ORIGINS`, `REDIS_ENABLED`, all `DB_*`, and `REDIS_URL` if Redis is on.
- [ ] `REDIS_ENABLED` matches the instance count: `false` only while the
      service runs as exactly **one** instance with no `--workers`.
- [ ] If `REDIS_ENABLED=true`: `REDIS_URL` points at a **live** instance.
      Resolve the hostname and connect.
- [ ] The deploy log shows the startup `alembic upgrade head` completing
      before uvicorn starts; `alembic_version` exists and matches head.
- [ ] Startup logs show *"PostgreSQL TLS is required and verified against
      DB_SSL_CA (sslmode=verify-full)."* -- `DB_SSL_CA` is the CA of the
      shared Novus Aiven PostgreSQL service.
- [ ] Startup logs show *"Firebase ID token verification is configured and
      active."* and **not** the CRITICAL warning.
- [ ] `GET /api/v1/health` returns 200; `GET /api/v1/health/deep` reports the
      backing services up and the `state_backend` you expect.
- [ ] `VITE_SOCKET_URL` (frontend) points directly at this service's host, not
      at Firebase Hosting.
- [ ] Smoke test: sign in, create a room, join from a second browser, play a
      full week, **finish a game** -- that last step is the only one that
      exercises the database write path and account attribution at all.
- [ ] Smoke test as a guest: create a room as a guest and finish a game -- a
      separate identity path, and, since guests may host (D3), a separate hosting
      path too.
- [ ] Results load at `/results/:publicId` (the link the end screen shows once
      the game is saved) after the room's 24-hour state TTL would have expired --
      i.e. from PostgreSQL, not from the live state store.
- [ ] CSV export downloads and opens in a spreadsheet.
- [ ] Secrets: `.env` not committed; service account not printed anywhere.

## CI

`.github/workflows/ci.yml` runs, on push to `main` and on pull requests:

1. `ruff check app tests`, `black --check app tests`, `mypy app/core`.
2. `pytest -m "not dbschema" --cov=app --cov-fail-under=80`, plus a
   `coverage report` scoped to `app/core` enforcing `--fail-under=95`.
3. A separate schema job: `pytest -m dbschema` against an ephemeral
   PostgreSQL 17 (`postgres:17`) started by Testcontainers -- migrations,
   the DDL snapshot, the models-match check and the persistence round
   trips. It also catches migration drift: it upgrades an empty database to
   head, asserts that autogenerate against it is empty and that the revision
   chain is linear, so a model change shipped without its migration fails. It needs a Docker daemon, no repository secret
   and no database credential, and fails outright if every test in it
   skipped (an all-skipped run looks identical to a passing one otherwise).
4. `docker build .`

There is no deploy step in the workflow. Render's Auto-Deploy is set to
"After CI checks pass" in the dashboard (recorded as `autoDeployTrigger:
checksPass` in `render.yaml`): a push to `main` deploys only once every check above has
passed. Do not add a deploy hook back alongside it -- a passing push would
deploy twice.
