# Deploying Beery_Backend

Mirrors the Tequila Game (`00-decisions.md` D17): a Docker container on
Render, a managed MySQL instance, and — **optionally** — a managed Redis
instance. See `docs/plan/23-deployment-and-ci.md` for the full specification
this file implements, and the D4/D17 amendment notes in
`docs/plan/00-decisions.md` for why Redis stopped being mandatory.

## Topology

```
Browser --HTTPS--> Firebase Hosting (Beery_Frontend, static)
Browser --HTTPS--> Render: Beery_Backend  (/api/v1/*)
Browser --WSS----> Render: Beery_Backend  (/socket.io)  -- direct, NOT via Hosting
                       |
                       +--> managed MySQL   (durable record, written at game end)
                       +--> managed Redis   (live room state + Socket.IO pub/sub)
                            OPTIONAL: only when REDIS_ENABLED=true. With it
                            off, that state lives inside the one container.
```

Firebase Hosting does not proxy WebSocket upgrades, so the frontend's
`VITE_SOCKET_URL` must point directly at this service's Render host in
production, never at the Hosting origin.

## Build and run

- `render.yaml` builds the committed `Dockerfile` (plain uvicorn, `--http h11
  --proxy-headers`, no gunicorn) and runs `alembic upgrade head` as a
  `preDeployCommand` before traffic switches to the new instance -- not
  `create_all()`, and not at application startup, where a migration racing N
  container instances is a lock-contention problem.
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
DB_HOST= DB_PORT= DB_USER= DB_PASSWORD= DB_DATABASE= DB_REQUIRE_SSL=
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
authoritative in a key-value store, and MySQL is written only when a game
finishes (`00-decisions.md` **D4**). Which store holds it is one environment
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
2. **A restart ends every game in progress.** A game reaches MySQL only when
   it finishes, so a deploy or a crash mid-session loses the live rooms.
   Warn the class, or finish the session first.

The app logs which backend is active at every boot, at WARNING for the
in-memory one.

**`REDIS_ENABLED=true`.** Set `REDIS_URL` in the same change; the two are
required together. Room state moves to Redis and the Socket.IO client manager
becomes `AsyncRedisManager`, which is what makes cross-instance fan-out work
and is the one thing the in-memory backend cannot replace. `/health/deep`
then reports `state_backend: "redis"` and a real `redis` reachability probe.
Redis survives an app restart; it does not survive its own restart or an
eviction, so this narrows the window rather than closing it.

For a classroom deployment on one Render instance, `false` is the right
setting. Turn it on before the first moment there is a second instance.

## Managed MySQL engine version

The managed MySQL instance **must run engine 8.4**. `tests/test_models/conftest.py`
pins the CI schema suite's Testcontainers image to `mysql:8.4` for exactly this
reason -- proving the schema against a different major version proves the
wrong engine.

## The shared wire protocol -- ship together

Every Socket.IO event name and payload in sections 11 and 12 is a contract
between two separately deployed artefacts (this repository and
`Beery_Frontend`). An old frontend against a new backend means nobody can
play.

**When a change touches anything under `app/sockets/handlers/` or
`src/api/socketHandlers.ts`, deploy `Beery_Backend` and `Beery_Frontend` in
the same window, backend first.**

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
- [ ] `alembic upgrade head` has run against the application database;
      `alembic_version` exists and matches head.
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
      separate identity path, and after D3 a separate hosting path too.
- [ ] Results load at `/results/:roomCode` after the room's 24-hour state TTL
      would have expired -- i.e. from MySQL, not from the live state store.
- [ ] CSV export downloads and opens in a spreadsheet.
- [ ] Secrets: `.env` not committed; service account not printed anywhere.

## CI

`.github/workflows/ci.yml` runs, on every push and pull request:

1. `ruff check app tests`, `black --check app tests`, `mypy app/core`.
2. `pytest -m "not dbschema" --cov=app --cov-fail-under=80`, plus a
   `coverage report` scoped to `app/core` enforcing `--fail-under=95`.
3. A separate schema job: `pytest -m dbschema` against an ephemeral MySQL
   started by Testcontainers -- needs a Docker daemon, no repository secret
   and no database credential, and fails outright if every test in it
   skipped (an all-skipped run looks identical to a passing one otherwise).
4. `docker build .`
5. On `main`: trigger a Render deploy.
