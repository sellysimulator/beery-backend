"""Black-box tests for deployment and CI (``DEPLOY.md``).

The frontend's env-surface checks live in
``Beery_Frontend/src/__tests__/envSurface.test.ts``.

Deployment is almost entirely configuration, not code, so most of these
tests read committed files -- ``Dockerfile``, ``.dockerignore``,
``pyproject.toml``, ``render.yaml``, ``DEPLOY.md``, ``.github/workflows/ci.yml``
-- rather than importing application symbols. Two things are exercised for
real: a real ``docker build`` of the committed ``Dockerfile``, because what
the image holds is asserted by inspecting the built image's file list, and
the actual ``Settings``/``check_auth_config`` runtime behaviour for the CORS
and double-quoted-secret failures, because those are properties of running
code, not of a text file.

A Docker daemon is required for the ``docker build``-based tests. Exactly
like ``tests/test_models/conftest.py``'s Testcontainers guard, an unreachable
daemon skips those tests loudly (a warning plus a printed reason) rather than
silently, so an all-skipped run does not read as a passing one.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Iterator
from pathlib import Path

import pydantic
import pytest
from dotenv import dotenv_values

BACKEND_ROOT = Path(__file__).resolve().parents[2]

DOCKERFILE = BACKEND_ROOT / "Dockerfile"
DOCKERIGNORE = BACKEND_ROOT / ".dockerignore"
REQUIREMENTS = BACKEND_ROOT / "requirements.txt"
REQUIREMENTS_DEV = BACKEND_ROOT / "requirements-dev.txt"
PYPROJECT = BACKEND_ROOT / "pyproject.toml"
RENDER_YAML = BACKEND_ROOT / "render.yaml"
DEPLOY_MD = BACKEND_ROOT / "DEPLOY.md"
CI_WORKFLOW = BACKEND_ROOT / ".github" / "workflows" / "ci.yml"
SCHEMA_CONFTEST = BACKEND_ROOT / "tests" / "test_models" / "conftest.py"
MIGRATION_TEST = BACKEND_ROOT / "tests" / "test_models" / "test_migration.py"

IMAGE_TAG = "beery-backend-section23-test"

# The Dockerfile CMD migrates, then serves. Free Render instances have no
# pre-deploy step, so the container runs ``alembic upgrade head`` itself.
STARTUP_MIGRATION = "alembic upgrade head && "


def _read(path: Path) -> str:
    assert path.exists(), f"required file is missing: {path}"
    return path.read_text()


def _dockerfile_cmd() -> list[str]:
    """The Dockerfile's exec-form ``CMD``, with line continuations joined the
    way Docker joins them."""
    text = _read(DOCKERFILE).replace("\\\n", "")
    match = re.search(r"^CMD\s*(\[.*\])\s*$", text, re.MULTILINE)
    assert match, "Dockerfile has no exec-form CMD"
    return json.loads(match.group(1))


def _serve_only_cmd() -> list[str]:
    """The Dockerfile's ``CMD`` without its startup migration: the container
    tests have no database, so they run exactly the committed uvicorn command
    and nothing before it."""
    argv = _dockerfile_cmd()
    assert argv[-1].startswith(STARTUP_MIGRATION)
    return [*argv[:-1], argv[-1][len(STARTUP_MIGRATION) :]]


# --- Docker daemon guard (mirrors tests/test_models/conftest.py) -----------


class DockerUnavailableWarning(UserWarning):
    """An all-skipped Docker-dependent run must not look like a pass."""


def _docker_unavailable_reason() -> str | None:
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
            check=True,
        )
    except Exception as exc:  # noqa: BLE001 - any failure means "no daemon"
        return f"No Docker daemon is reachable: {type(exc).__name__}: {exc}"
    return None


def _require_docker() -> None:
    reason = _docker_unavailable_reason()
    if reason is not None:
        warnings.warn(reason, DockerUnavailableWarning, stacklevel=2)
        print(f"\nSKIPPING SECTION 23 DOCKER SUITE: {reason}", file=sys.stderr)
        pytest.skip(reason)


@pytest.fixture(scope="module")
def built_image() -> Iterator[str]:
    """Build the committed ``Dockerfile`` once and remove it afterwards."""
    _require_docker()
    subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, "."],
        cwd=BACKEND_ROOT,
        check=True,
        capture_output=True,
        timeout=600,
    )
    try:
        yield IMAGE_TAG
    finally:
        subprocess.run(
            ["docker", "rmi", "-f", IMAGE_TAG], capture_output=True, check=False
        )


@pytest.fixture()
def running_container(built_image: str) -> Iterator[tuple[str, int]]:
    """A container from ``built_image``, listening on a free host port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    name = f"beery-section23-{port}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            f"{port}:8080",
            "-e",
            "PORT=8080",
            # Required: app/services/state_service.py builds a redis client
            # at import time from REDIS_URL, and an empty string is not a
            # valid redis:// URL. This is a syntactically valid but
            # unreachable target, not a real dependency.
            "-e",
            "REDIS_URL=redis://127.0.0.1:16379/0",
            built_image,
            *_serve_only_cmd(),
        ],
        check=True,
        capture_output=True,
    )
    try:
        yield name, port
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _wait_for_http_ok(port: int, path: str, attempts: int = 30) -> int:
    url = f"http://127.0.0.1:{port}{path}"
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return response.status
        except (urllib.error.URLError, ConnectionError) as exc:
            last_error = exc
            time.sleep(1)
    raise AssertionError(f"{url} never came up: {last_error}")


def _docker_exec(name: str, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", name, *cmd],
        capture_output=True,
        text=True,
        check=False,
    )


# --- the image builds and serves ---------------------------------------


def test_docker_build_succeeds_and_serves_root_and_health(
    running_container: tuple[str, int],
) -> None:
    """``docker build .`` succeeds; the container serves ``GET /`` and
    ``GET /api/v1/health``."""
    _name, port = running_container

    assert _wait_for_http_ok(port, "/") == 200
    assert _wait_for_http_ok(port, "/api/v1/health") == 200


# --- the uvicorn command -----------------------------------------------


def test_uvicorn_command_includes_h11_and_proxy_headers() -> None:
    """The container's uvicorn command includes ``--http h11`` and
    ``--proxy-headers``."""
    text = _read(DOCKERFILE)
    cmd_match = re.search(r"CMD\s*\[.*?\]", text, re.DOTALL)
    assert cmd_match, "Dockerfile has no CMD"
    cmd_text = cmd_match.group(0)

    assert "--http h11" in cmd_text
    assert "--proxy-headers" in cmd_text
    assert "uvicorn" in cmd_text
    assert "gunicorn" not in cmd_text


# --- Startup migration --------------------------------------------------------


def test_container_migrates_at_startup_before_serving() -> None:
    """Free Render instances have no pre-deploy command, so the Dockerfile
    CMD runs ``alembic upgrade head`` and only then ``exec``s uvicorn; and
    ``render.yaml`` declares no ``preDeployCommand`` that would never run."""
    argv = _dockerfile_cmd()
    assert argv[:2] == ["sh", "-c"]
    assert argv[-1].startswith("alembic upgrade head && exec uvicorn ")

    render_text = _read(RENDER_YAML)
    assert "preDeployCommand" not in render_text
    assert re.search(r"^\s+plan: free$", render_text, re.MULTILINE)
    assert re.search(r"^\s+autoDeployTrigger: checksPass$", render_text, re.MULTILINE)

    dockerignore_lines = _read(DOCKERIGNORE).split()
    assert not {"alembic", "alembic/", "alembic.ini"} & set(dockerignore_lines)


def test_failed_startup_migration_exits_before_serving(built_image: str) -> None:
    """A migration that fails exits the container non-zero and uvicorn never
    starts, so the new instance never becomes healthy and Render keeps the
    previous deploy live."""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            "DB_HOST=127.0.0.1",
            "-e",
            "DB_PORT=1",
            "-e",
            "DB_REQUIRE_SSL=false",
            built_image,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "OperationalError" in output, "the startup migration did not run"
    assert "Uvicorn running" not in output


# --- what the image leaves out ------------------------------------------


def test_dockerignore_excludes_env_and_tests(
    running_container: tuple[str, int],
) -> None:
    """``.dockerignore`` excludes ``.env`` and ``tests/``; asserted by
    inspecting the built image's file list, not by reading the ignore file."""
    name, _port = running_container

    dockerignore_text = _read(DOCKERIGNORE)
    assert ".env" in dockerignore_text
    assert "tests/" in dockerignore_text

    env_check = _docker_exec(name, "test", "-e", "/app/.env")
    assert env_check.returncode != 0, "/app/.env is present in the built image"

    tests_check = _docker_exec(name, "test", "-d", "/app/tests")
    assert tests_check.returncode != 0, "/app/tests is present in the built image"


def test_secrets_are_absent_from_the_built_image(
    running_container: tuple[str, int],
) -> None:
    """``.env`` is absent from the built image."""
    name, _port = running_container

    result = _docker_exec(name, "find", "/app", "-maxdepth", "1", "-name", ".env")
    assert result.stdout.strip() == "", "a .env file was found inside the image"


# --- no gunicorn ---------------------------------------------------------


def test_no_gunicorn_anywhere(running_container: tuple[str, int]) -> None:
    """No ``gunicorn`` in any requirements file or command."""
    name, _port = running_container

    assert "gunicorn" not in _read(REQUIREMENTS).lower()
    assert "gunicorn" not in _read(REQUIREMENTS_DEV).lower()
    assert "gunicorn" not in _read(DOCKERFILE).lower()

    pip_show = _docker_exec(name, "pip", "show", "gunicorn")
    assert pip_show.returncode != 0, "gunicorn is installed in the built image"


# --- no test tooling in the image ------------------------------------------


def test_test_tooling_is_absent_from_the_built_image(
    running_container: tuple[str, int],
) -> None:
    """``pytest`` is absent from the built image."""
    name, _port = running_container

    result = _docker_exec(name, "python", "-c", "import pytest")
    assert result.returncode != 0, "pytest imports successfully inside the image"


# --- CORS ------------------------------------------------------------------


def test_settings_default_cors_origins_is_never_wildcard() -> None:
    """``Settings`` never *defaults* to ``CORS_ORIGINS == ["*"]``."""
    from app.config import Settings as SettingsClass

    default_origins = SettingsClass.model_fields["CORS_ORIGINS"].default
    assert default_origins != ["*"]
    assert "*" not in default_origins


def test_cors_wildcard_with_credentials_is_rejected() -> None:
    """``CORS_ORIGINS=["*"]`` is rejected outright.

    The app hardcodes ``allow_credentials=True`` in ``app/main.py``, so the
    two together are always the invalid, permissive combination -- there is
    no configuration in which "*" is ever safe here.
    """
    from app.config import Settings as SettingsClass

    with pytest.raises(pydantic.ValidationError):
        SettingsClass(CORS_ORIGINS=["*"])

    with pytest.raises(pydantic.ValidationError):
        SettingsClass(CORS_ORIGINS=["https://beersim.web.app", "*"])


def test_main_always_sets_allow_credentials_true() -> None:
    """The rejection matters only because credentials are always allowed."""
    main_source = _read(BACKEND_ROOT / "app" / "main.py")
    assert "allow_credentials=True" in main_source


# --- the double-quoted service account --------------------------------------


def _corrupted_double_quoted_service_account(tmp_path: Path) -> str:
    """Reproduce the ``[HARD-WON]`` double-quote bug and return the value
    ``Settings`` would end up with.

    A real service account JSON's ``private_key`` contains literal ``\\n``
    two-character escapes. Wrapped in double quotes in a ``.env`` file,
    python-dotenv decodes those into real newline characters, and the result
    is no longer valid JSON -- ``json.loads`` raises ``JSONDecodeError``
    ("Invalid control character").
    """
    inner = (
        '{"type": "service_account", "private_key": '
        '"-----BEGIN PRIVATE KEY-----\\nFAKEKEYDATA\\n'
        '-----END PRIVATE KEY-----\\n", '
        '"client_email": "x@y.iam.gserviceaccount.com"}'
    ).replace('"', '\\"')
    env_path = tmp_path / "double_quoted.env"
    env_path.write_text(f'FIREBASE_SERVICE_ACCOUNT_JSON="{inner}"\n')
    values = dotenv_values(env_path)
    value = values.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    assert value, "the .env fixture itself failed to produce a value to corrupt"
    return value


def test_double_quoting_reproduces_the_documented_corruption(tmp_path: Path) -> None:
    """Sanity check on the reproduction itself: the corrupted value must not
    parse as JSON, or this test proves nothing about the double-quote bug."""
    corrupted = _corrupted_double_quoted_service_account(tmp_path)
    with pytest.raises(json.JSONDecodeError):
        json.loads(corrupted)


def test_double_quoted_service_account_logs_critical_but_app_stays_healthy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    client,
) -> None:
    """A double-quoted ``FIREBASE_SERVICE_ACCOUNT_JSON``
    is detected -- the app keeps serving (health stays 200) but logs CRITICAL,
    so the partial outage is visible instead of silent."""
    import importlib

    import app.core.firebase as firebase_module
    from app.config import settings
    from app.core.checks.firebase_check import check_auth_config

    corrupted = _corrupted_double_quoted_service_account(tmp_path)

    monkeypatch.setattr(settings, "FIREBASE_SERVICE_ACCOUNT_JSON", corrupted)
    importlib.reload(firebase_module)
    try:
        with caplog.at_level(logging.CRITICAL, logger="app.core.checks.firebase_check"):
            check_auth_config()

        assert any(
            record.levelno == logging.CRITICAL
            and "FIREBASE_SERVICE_ACCOUNT_JSON" in record.message
            for record in caplog.records
        ), "no CRITICAL log about the unusable Firebase credential"

        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    finally:
        monkeypatch.setattr(settings, "FIREBASE_SERVICE_ACCOUNT_JSON", "")
        importlib.reload(firebase_module)


# --- a dead Redis ------------------------------------------------------


def test_dead_redis_health_ok_deep_degraded(
    monkeypatch: pytest.MonkeyPatch, client
) -> None:
    """A dead ``REDIS_URL`` still answers ``GET /api/v1/health`` with 200,
    while ``/health/deep`` reports Redis down.

    Points at a closed local port rather than an unresolvable hostname: both
    are "Redis unreachable" as far as ``_redis_reachable`` is concerned, but a
    bogus hostname risks a slow, sandbox-dependent DNS timeout, while a
    connection refused on localhost fails immediately and deterministically.
    """
    from app.config import settings

    # Redis has to be switched **on** for its reachability to be asked
    # about: with `REDIS_ENABLED` false — the default since the state
    # backend became switchable — an unused dependency reports healthy,
    # and this test is about a dependency that is in use and dead.
    monkeypatch.setattr(settings, "REDIS_ENABLED", True)
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")

    shallow = client.get("/api/v1/health")
    assert shallow.status_code == 200
    assert shallow.json()["status"] == "ok"

    deep = client.get("/api/v1/health/deep")
    assert deep.status_code == 200
    body = deep.json()
    assert body["redis"] is False
    assert body["status"] == "degraded"


# --- CI workflow parsing helpers -------------------------------------------


def _job_blocks(workflow_text: str) -> dict[str, str]:
    """Split a ``jobs:`` mapping into ``{job_name: raw_block_text}``.

    A small, deliberately literal parser (in the style of
    ``test_migration.py``'s regex reading of generated Alembic scripts)
    rather than a YAML library dependency this repository does not otherwise
    need: every workflow in this repository uses a flat two-space job
    indent, so a regex on that exact shape is reliable and adds nothing to
    ``requirements-dev.txt``.
    """
    after_jobs = workflow_text.split("\njobs:\n", 1)
    assert len(after_jobs) == 2, "workflow has no top-level `jobs:` mapping"
    body = after_jobs[1]
    job_starts = list(re.finditer(r"^  ([A-Za-z0-9_-]+):\n", body, re.MULTILINE))
    assert job_starts, "no jobs found under `jobs:`"
    blocks: dict[str, str] = {}
    for index, match in enumerate(job_starts):
        start = match.end()
        end = (
            job_starts[index + 1].start() if index + 1 < len(job_starts) else len(body)
        )
        blocks[match.group(1)] = body[start:end]
    return blocks


def _run_steps(job_block: str) -> list[str]:
    """Every ``run:`` step body (single-line or ``|`` block) in a job, in
    document order."""
    steps = []
    for match in re.finditer(
        r"run:\s*\|?\n?(.*?)(?=\n\s{2,8}- (?:uses|run|name):|\Z)",
        job_block,
        re.DOTALL,
    ):
        steps.append(match.group(1))
    # Also catch single-line `run: <cmd>` forms the block regex above skips.
    for match in re.finditer(r"run:\s*([^\n|][^\n]*)", job_block):
        steps.append(match.group(1))
    return steps


# --- the schema job: migrations and drift ------------------------------


def test_ci_schema_job_runs_the_migration_upgrade_suite() -> None:
    """``alembic upgrade head`` on a clean database succeeds in CI --
    exercised by the schema job running ``pytest -m dbschema``, which
    includes ``test_migration.py``'s upgrade-head test."""
    jobs = _job_blocks(_read(CI_WORKFLOW))
    schema_job = jobs.get("dbschema")
    assert schema_job, "ci.yml has no `dbschema` job"
    assert "-m dbschema" in schema_job

    migration_source = _read(MIGRATION_TEST)
    assert "def test_upgrade_head_on_an_empty_database_creates_exactly_nine_tables" in (
        migration_source
    )


def test_ci_schema_job_runs_the_autogenerate_drift_detector() -> None:
    """The autogenerate-drift check fails CI when a model changes
    without a migration."""
    jobs = _job_blocks(_read(CI_WORKFLOW))
    schema_job = jobs.get("dbschema")
    assert schema_job and "-m dbschema" in schema_job

    migration_source = _read(MIGRATION_TEST)
    assert (
        "def test_autogenerate_against_head_produces_an_empty_revision"
        in migration_source
    )


def test_the_drift_detector_actually_asserts_emptiness() -> None:
    """Changing a model without a migration must fail CI --
    i.e. the drift test really does assert the generated revision is empty,
    not merely that it runs."""
    migration_source = _read(MIGRATION_TEST)
    assert "assert operations == []" in migration_source


# --- coverage floors -------------------------------------------------------


def test_ci_enforces_both_coverage_floors() -> None:
    """The backend CI enforces ``--cov-fail-under=80`` overall, and
    95% on ``app/core``."""
    jobs = _job_blocks(_read(CI_WORKFLOW))
    test_job = jobs.get("test")
    assert test_job, "ci.yml has no `test` job"
    steps = _run_steps(test_job)
    joined = "\n".join(steps)

    assert re.search(r"--cov=app\b", joined)
    assert "--cov-fail-under=80" in joined
    assert re.search(r'--include[= ]["\']?app/core', joined)
    assert "--fail-under=95" in joined


# --- the schema job: no secrets, no silent skip ------------------------


def test_schema_job_needs_no_secret_or_db_credential() -> None:
    """The schema job needs no repository secret and no database
    credential."""
    jobs = _job_blocks(_read(CI_WORKFLOW))
    schema_job = jobs.get("dbschema")
    assert schema_job

    assert "secrets." not in schema_job
    for forbidden in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_DATABASE"):
        assert forbidden not in schema_job, (
            f"schema job sets {forbidden}, but Testcontainers supplies its own "
            "throwaway coordinates"
        )


def test_schema_job_fails_when_every_test_in_it_skipped() -> None:
    """The job must fail on an all-skipped run -- with no
    Docker daemon the suite skips, and pytest alone exits 0 for that."""
    jobs = _job_blocks(_read(CI_WORKFLOW))
    schema_job = jobs.get("dbschema")
    assert schema_job

    joined = "\n".join(_run_steps(schema_job))
    assert "-m dbschema" in joined
    # A guard that inspects the result for at least one passed test and
    # fails the step otherwise.
    assert re.search(r"passed", joined)
    assert "exit 1" in joined


# --- the Testcontainers image ----------------------------------------


def test_testcontainers_image_matches_the_documented_managed_engine() -> None:
    """The Testcontainers PostgreSQL image tag matches the major
    version of the deployed managed instance, as DEPLOY.md and render.yaml
    both document it."""
    schema_conftest_source = _read(SCHEMA_CONFTEST)
    match = re.search(r'POSTGRES_IMAGE\s*=\s*"postgres:(\d+)"', schema_conftest_source)
    assert match, "could not find POSTGRES_IMAGE in tests/test_models/conftest.py"
    pinned_version = match.group(1)

    documented = re.compile(rf"PostgreSQL {pinned_version}\b")
    for name, text in (
        ("DEPLOY.md", _read(DEPLOY_MD)),
        ("render.yaml", _read(RENDER_YAML)),
    ):
        assert documented.search(text), (
            f"{name} does not document the managed PostgreSQL version "
            f"({pinned_version}) that the Testcontainers image in "
            "tests/test_models/conftest.py is pinned to"
        )


# --- lint tooling ----------------------------------------------------------


def test_pyproject_exists_and_lint_tools_are_exactly_pinned() -> None:
    """``ruff check`` and ``black --check`` reproduce the same result
    from a clean checkout with no configuration outside the repository --
    asserted by a committed ``pyproject.toml`` existing, and by
    ``requirements-dev.txt`` pinning exact versions of both tools."""
    assert PYPROJECT.exists(), "pyproject.toml is missing"

    dev_requirements = _read(REQUIREMENTS_DEV)
    ruff_pin = re.search(r"^ruff==([\w.]+)$", dev_requirements, re.MULTILINE)
    black_pin = re.search(r"^black==([\w.]+)$", dev_requirements, re.MULTILINE)
    assert ruff_pin, "ruff is not pinned to an exact version in requirements-dev.txt"
    assert black_pin, "black is not pinned to an exact version in requirements-dev.txt"


def test_pyproject_does_not_restate_ruff_defaults() -> None:
    """No ``[tool.ruff]`` section pinning one reading of the
    defaults, which would silently drift on the next ruff upgrade."""
    pyproject_text = _read(PYPROJECT)
    assert "[tool.ruff]" not in pyproject_text


# --- DEPLOY.md --------------------------------------------------------


def test_backend_deploy_md_states_the_ship_together_rule() -> None:
    """``DEPLOY.md`` states the ship-together rule."""
    deploy_text = _read(DEPLOY_MD).lower()
    assert "sockets/handlers" in deploy_text or "socketio" in deploy_text.lower()
    assert "same window" in deploy_text or "ship" in deploy_text
    assert "backend first" in deploy_text


def test_deploy_md_documents_the_stale_container_risk() -> None:
    """Not automatable, but documented -- a change with no
    rebuild serves old code."""
    deploy_text = _read(DEPLOY_MD).lower()
    assert "rebuild" in deploy_text
    assert "snapshot" in deploy_text or "stale" in deploy_text
