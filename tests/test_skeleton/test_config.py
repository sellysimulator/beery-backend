"""``app/config.py`` -- the frozen ``Settings`` surface.

Covers the database URL and TLS arguments, CORS origins, unknown variables and
the hard ceilings.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.config import Settings, settings
from app.core.checks import db_tls_check

# A throwaway self-signed CA (its key was discarded). Only its parseability
# matters: no connection is ever verified against it.
_TEST_CA_PEM = """\
-----BEGIN CERTIFICATE-----
MIIBmDCCAT2gAwIBAgIUGXD542pZtQa9SLnAxCsSm1qTBHwwCgYIKoZIzj0EAwIw
GDEWMBQGA1UEAwwNTm92dXMgdGVzdCBDQTAgFw0yNjEwMDIyMDU2NTZaGA8yMTI2
MDkwODIwNTY1NlowGDEWMBQGA1UEAwwNTm92dXMgdGVzdCBDQTBZMBMGByqGSM49
AgEGCCqGSM49AwEHA0IABHCAFZW6AwimQZTLzv3AWFqneF3rOhSXVy6aK0h6Fo5U
J20/SnI5CKMQwuqY6YSC2kuzF8UHqaEHiCLfyL7WFXyjYzBhMB0GA1UdDgQWBBQ8
FdhkXQM4IflDyllXsNm5kjFtMDAfBgNVHSMEGDAWgBQ8FdhkXQM4IflDyllXsNm5
kjFtMDAPBgNVHRMBAf8EBTADAQH/MA4GA1UdDwEB/wQEAwIBBjAKBggqhkjOPQQD
AgNJADBGAiEAiY0rLhkAr086mBgEi/LBgn23cS3BLxLUsjLph2QblP8CIQCkPZEJ
KZ781IJHaBK1rqXuL8CPgjD3c3nx86yO47mExA==
-----END CERTIFICATE-----
"""


def _isolated(monkeypatch, **overrides) -> Settings:
    """A ``Settings`` built from declared defaults only.

    Neither the developer's ``.env`` nor an exported ``DB_HOST`` may decide
    what this suite thinks the defaults are.
    """
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **overrides)


# --- identity ---------------------------------------------------------------


def test_app_name_and_version_defaults(monkeypatch):
    """``GET /`` answers with these two values."""
    s = _isolated(monkeypatch)
    assert s.APP_NAME == "Beery"
    assert s.VERSION == "0.1.0"


def test_debug_is_off_by_default(monkeypatch):
    """Socket logging is driven from DEBUG, and DEBUG is off unless a
    deployment says otherwise."""
    assert _isolated(monkeypatch).DEBUG is False


def test_module_level_settings_singleton_exists():
    """``settings`` is a module-level singleton of type ``Settings``."""
    assert isinstance(settings, Settings)


# --- db_url -----------------------------------------------------------------


def test_db_url_shape(monkeypatch):
    """``postgresql+psycopg://user:password@host:port/database``."""
    s = _isolated(
        monkeypatch,
        DB_USER="beeruser",
        DB_PASSWORD="s3cret",
        DB_HOST="db.example.com",
        DB_PORT=5433,
        DB_DATABASE="beery_test",
    )
    assert (
        s.db_url
        == "postgresql+psycopg://beeruser:s3cret@db.example.com:5433/beery_test"
    )


def test_db_url_uses_the_psycopg_driver(monkeypatch):
    """psycopg 3 (``psycopg[binary]``), the one PostgreSQL driver."""
    assert _isolated(monkeypatch).db_url.startswith("postgresql+psycopg://")


@pytest.mark.parametrize(
    "password",
    [
        pytest.param("p@ssw0rd", id="at"),
        pytest.param("colon:pass", id="colon"),
        pytest.param("slash/pass", id="slash"),
        pytest.param("hash#pass", id="hash"),
        pytest.param("query?pass", id="question"),
        pytest.param("percent%pass", id="percent"),
        pytest.param("plus+pass", id="plus"),
        pytest.param("correct horse battery", id="space"),
        pytest.param("a@b:c/d#e?f%g+h i", id="all-of-them"),
    ],
)
def test_db_password_with_url_metacharacters_round_trips(monkeypatch, password):
    """[HARD-WON] A ``DB_PASSWORD`` containing ``@``, ``:``, ``/``,
    ``#``, ``?``, ``%``, ``+`` or a space still produces a valid ``db_url``,
    and that URL round-trips back to the original password.

    The user and password want ``urllib.parse.quote(value, safe="")``, not
    ``quote_plus``: ``quote_plus`` encodes a space as ``+``, SQLAlchemy
    unquotes the userinfo component with ``unquote``, the ``+`` therefore
    survives literally, and the driver is handed a password that is not the
    one configured -- an authentication failure with no clue in it.
    """
    s = _isolated(
        monkeypatch,
        DB_USER="beeruser",
        DB_PASSWORD=password,
        DB_HOST="db.example.com",
        DB_PORT=5432,
        DB_DATABASE="beery",
    )
    url = make_url(s.db_url)
    assert url.drivername == "postgresql+psycopg"
    assert url.username == "beeruser"
    assert url.password == password
    assert url.host == "db.example.com"
    assert url.port == 5432
    assert url.database == "beery"
    # Exactly one unescaped '@' -- the credentials/host separator.
    assert s.db_url.count("@") == 1


def test_db_user_with_url_metacharacters_round_trips(monkeypatch):
    """The same encoding applies to the user half of the userinfo
    component, which is the other thing a managed provider hands out with a
    ``@`` in it."""
    s = _isolated(
        monkeypatch,
        DB_USER="beery@tenant",
        DB_PASSWORD="plain",
        DB_HOST="db.example.com",
        DB_PORT=5432,
        DB_DATABASE="beery",
    )
    url = make_url(s.db_url)
    assert url.username == "beery@tenant"
    assert url.password == "plain"
    assert url.host == "db.example.com"


# --- db_connect_args --------------------------------------------------------

_UTC = {"options": "-c timezone=UTC"}


def test_db_connect_args_always_pin_the_session_to_utc(monkeypatch):
    """Every session reads and writes ``timestamptz`` in UTC, whatever the
    server's ``TimeZone`` -- the connect option replaces a per-connection
    ``SET`` listener."""
    for require in (True, False):
        args = _isolated(monkeypatch, DB_REQUIRE_SSL=require).db_connect_args
        assert args["options"] == "-c timezone=UTC"


def test_db_connect_args_without_ssl_or_ca_use_libpq_defaults(monkeypatch):
    """With ``DB_REQUIRE_SSL`` false and no CA, no ``sslmode`` is set
    (libpq's ``prefer``); only the UTC option is passed."""
    s = _isolated(monkeypatch, DB_REQUIRE_SSL=False)
    assert s.db_connect_args == _UTC


def test_db_connect_args_encrypt_without_verifying_when_no_ca(monkeypatch):
    """TLS with no ``DB_SSL_CA`` is ``sslmode=require`` -- encrypted,
    never a plaintext fallback, but the server is not authenticated."""
    s = _isolated(monkeypatch, DB_REQUIRE_SSL=True)
    assert s.db_connect_args == {**_UTC, "sslmode": "require"}


# Every form DB_SSL_CA accepts: a path, the PEM text as Render stores it, and
# the one-line ``\n``-escaped text a ``.env`` holds.
_CA_FORMS = ["path", "pem", "pem-one-line"]


def _ca_value(form, tmp_path) -> str:
    if form == "path":
        ca = tmp_path / "ca.pem"
        ca.write_text(_TEST_CA_PEM)
        return str(ca)
    if form == "pem":
        return _TEST_CA_PEM
    if form == "pem-one-line":
        return _TEST_CA_PEM.strip().replace("\n", "\\n")
    return ""


@pytest.mark.parametrize("require", [True, False])
@pytest.mark.parametrize("form", _CA_FORMS)
def test_db_connect_args_verify_against_the_ca(monkeypatch, tmp_path, form, require):
    """With ``DB_SSL_CA`` -- in any accepted form -- the server
    certificate and hostname are verified (``sslmode=verify-full``) against a
    readable file holding exactly that CA. libpq reads a CA only from a file,
    so PEM text is written to one; a path is passed through untouched."""
    value = _ca_value(form, tmp_path)
    s = _isolated(monkeypatch, DB_REQUIRE_SSL=require, DB_SSL_CA=value)
    args = s.db_connect_args
    assert args["sslmode"] == "verify-full"
    assert args["options"] == "-c timezone=UTC"
    root = Path(args["sslrootcert"])
    assert root.is_file() and os.access(root, os.R_OK)
    assert root.read_text().strip() == _TEST_CA_PEM.strip()
    if form == "path":
        assert args["sslrootcert"] == value


def test_db_connect_args_write_a_pem_ca_once(monkeypatch):
    """The PEM-text CA is written to one temp file per distinct value, not
    one per engine (``alembic/env.py`` and the app each build one)."""
    s = _isolated(monkeypatch, DB_SSL_CA=_TEST_CA_PEM)
    assert s.db_connect_args["sslrootcert"] == s.db_connect_args["sslrootcert"]


@pytest.mark.parametrize(
    "bad",
    [
        "/nonexistent/ca.pem",
        "-----BEGIN CERTIFICATE-----\\nnot a certificate\\n-----END CERTIFICATE-----",
    ],
)
def test_db_connect_args_fail_loudly_on_an_unloadable_ca(monkeypatch, bad):
    """A wrong ``DB_SSL_CA`` is an error, not a silent fallback."""
    s = _isolated(monkeypatch, DB_REQUIRE_SSL=True, DB_SSL_CA=bad)
    with pytest.raises(ValueError, match="DB_SSL_CA"):
        _ = s.db_connect_args


def test_db_connect_args_are_empty_for_a_non_postgres_url(monkeypatch):
    """A SQLite engine (the fast unit suites) never gets libpq options."""
    s = _isolated(monkeypatch, DB_SSL_CA=_TEST_CA_PEM)
    monkeypatch.setattr(Settings, "db_url", property(lambda self: "sqlite://"))
    assert s.db_connect_args == {}


@pytest.mark.parametrize(
    ("require", "ca", "level", "text"),
    [
        (False, "", logging.WARNING, "DB_REQUIRE_SSL is false"),
        (True, "", logging.WARNING, "NOT verified"),
        (True, _TEST_CA_PEM, logging.INFO, "verified against DB_SSL_CA"),
    ],
)
def test_boot_log_says_how_the_db_link_is_protected(
    monkeypatch, caplog, require, ca, level, text
):
    """The startup check states the TLS mode; it never aborts."""
    monkeypatch.setattr(db_tls_check.settings, "DB_REQUIRE_SSL", require)
    monkeypatch.setattr(db_tls_check.settings, "DB_SSL_CA", ca)
    with caplog.at_level(logging.INFO, logger=db_tls_check.logger.name):
        db_tls_check.check_db_tls()
    [record] = [r for r in caplog.records if r.name == db_tls_check.logger.name]
    assert record.levelno == level
    assert text in record.getMessage()


# --- CORS -------------------------------------------------------------------


def test_cors_origins_default_is_not_a_wildcard(monkeypatch):
    """The app sets ``allow_credentials=True``; combined with
    ``["*"]`` that is an invalid, permissive configuration."""
    s = _isolated(monkeypatch)
    assert s.CORS_ORIGINS != ["*"]
    assert "*" not in s.CORS_ORIGINS
    assert s.CORS_ORIGINS == ["http://localhost:5173"]


def test_live_cors_origins_is_not_a_wildcard():
    """Nor may the *running* configuration be a wildcard."""
    assert "*" not in settings.CORS_ORIGINS
    assert settings.CORS_ORIGINS != ["*"]


# --- unknown variables are ignored ------------------------------------------


def test_unknown_environment_variable_is_ignored(monkeypatch):
    """``extra="ignore"`` -- an unrelated variable in the environment
    must not stop the app booting."""
    monkeypatch.setenv("BEERY_SOMETHING_NOBODY_DECLARED", "1")
    monkeypatch.setenv("PATH_TO_NOWHERE", "x")
    s = _isolated(monkeypatch)
    assert s.APP_NAME == "Beery"


def test_unknown_keyword_is_ignored(monkeypatch):
    """The same guarantee reached through construction."""
    s = _isolated(monkeypatch, SOMETHING_NOBODY_DECLARED="1")
    assert s.APP_NAME == "Beery"
    assert not hasattr(s, "SOMETHING_NOBODY_DECLARED")


# --- hard ceilings (spec §12.1) ---------------------------------------------


def test_hard_limit_defaults(monkeypatch):
    """The hard ceilings, asserted at the place that actually enforces each one.

    Only ``MAX_DISPLAY_NAME_LENGTH`` is a setting. The other ten were fields
    on ``Settings`` and were read by nothing -- ``app/core`` is pure and
    cannot import ``app.config``, so the game ceilings are injected as
    ``Limits`` and the store's ceilings are module constants. Asserting them
    here, at their owners, keeps every ceiling covered without a ``.env``
    variable that silently does nothing.
    """
    from app.core.config_models import DEFAULT_LIMITS
    from app.services import state_service

    s = _isolated(monkeypatch)
    assert s.MAX_DISPLAY_NAME_LENGTH == 24

    assert DEFAULT_LIMITS.max_order_quantity == 9_999
    assert DEFAULT_LIMITS.max_weeks == 104
    assert DEFAULT_LIMITS.min_weeks == 8
    assert DEFAULT_LIMITS.max_delay_weeks == 8
    assert DEFAULT_LIMITS.min_delay_weeks == 1
    assert DEFAULT_LIMITS.max_initial_quantity == 9_999
    assert DEFAULT_LIMITS.max_unit_value == 1_000_000.0

    assert state_service.ROOM_TTL_SECONDS == 86_400
    assert state_service.LOCK_TIMEOUT_SECONDS == 10

    # The dead fields stay deleted: re-declaring one makes `.env` lie again.
    for dead in (
        "MAX_ORDER_QUANTITY",
        "MAX_WEEKS_LIMIT",
        "MIN_WEEKS",
        "MAX_DELAY_WEEKS",
        "MIN_DELAY_WEEKS",
        "MAX_INITIAL_QUANTITY",
        "MAX_UNIT_VALUE",
        "MAX_PLAYERS",
        "ROOM_TTL_SECONDS",
        "LOCK_TIMEOUT_SECONDS",
    ):
        assert dead not in Settings.model_fields


def test_transport_and_service_defaults(monkeypatch):
    s = _isolated(monkeypatch)
    assert s.HOST == "0.0.0.0"
    assert s.PORT == 8080
    assert s.DB_HOST == "localhost"
    assert s.DB_PORT == 5432
    assert s.DB_USER == "postgres"
    assert s.DB_PASSWORD == ""
    assert s.DB_DATABASE == "beerydb"
    assert s.DB_REQUIRE_SSL is True
    assert s.DB_SSL_CA == ""
    assert s.REDIS_ENABLED is False
    assert s.REDIS_URL == ""
    assert s.FIREBASE_SERVICE_ACCOUNT_JSON == ""


def test_pool_defaults_fit_the_aiven_free_plan(monkeypatch):
    """2 + 1 per instance on the Aiven Free service the four Novus games share:
    Selly, Pulky and Beery 3 each, Lemony 4, plus one game's second instance
    during its deploy (at most 4), the startup migration (1) and one psql
    session (1) stay within 20."""
    s = _isolated(monkeypatch)
    assert (s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW) == (2, 1)
    beery = s.DB_POOL_SIZE + s.DB_MAX_OVERFLOW
    assert 3 + 3 + beery + 4 + 4 + 2 <= 20


def test_pool_size_is_overridable_from_the_environment(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_POOL_SIZE", "12")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "6")
    s = Settings(_env_file=None)
    assert (s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW) == (12, 6)


def test_the_engine_uses_the_pool_settings():
    """`app/db/session.py` reads the two settings and bounds the wait."""
    from app.db.session import engine

    assert engine.pool.size() == settings.DB_POOL_SIZE
    assert engine.pool._max_overflow == settings.DB_MAX_OVERFLOW
    assert engine.pool._timeout == 10


def test_settings_is_case_sensitive(monkeypatch):
    """``case_sensitive=True`` -- a lower-cased variable is not picked up."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("app_name", "NotBeery")
    assert Settings(_env_file=None).APP_NAME == "Beery"
