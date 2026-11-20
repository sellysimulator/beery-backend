"""Application settings, loaded from environment variables and `.env`."""

import os
import ssl
import tempfile
from functools import lru_cache
from typing import Any
from urllib.parse import quote

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@lru_cache(maxsize=8)
def _ca_file(ca: str) -> str:
    """A path psycopg can hand to libpq as ``sslrootcert``, for ``DB_SSL_CA``.

    ``ca`` is PEM text -- with real newlines, or ``\\n``-escaped so it fits on
    one ``.env`` line -- or a path to a PEM file. libpq only reads a CA from a
    file, so PEM text is written once per distinct value to a private temp
    file. Either way the certificate is loaded here first, so a value that is
    neither a valid PEM nor a readable PEM file raises when the engine is
    built instead of failing at the first connection.
    """
    ca = ca.strip()
    pem = ca.replace("\\n", "\n")
    try:
        if pem.startswith("-----BEGIN"):
            ssl.create_default_context(cadata=pem)
            handle, path = tempfile.mkstemp(suffix=".pem")
            with os.fdopen(handle, "w") as fh:
                fh.write(pem + "\n")
            return path
        ssl.create_default_context(cafile=ca)
    except (OSError, ValueError) as exc:  # ssl.SSLError is an OSError too
        raise ValueError(
            "DB_SSL_CA is neither a valid PEM certificate nor a readable PEM file"
        ) from exc
    return ca


class Settings(BaseSettings):
    """Environment-driven configuration for the Beery backend."""

    APP_NAME: str = "Beery"
    VERSION: str = "0.1.0"
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8080

    # NEVER ["*"] — the app sets allow_credentials=True and the two together are an
    # invalid, permissive combination. Keep in sync with sockets/manager.py.
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "postgres"
    DB_PASSWORD: str = ""
    DB_DATABASE: str = "beerydb"
    # TLS to PostgreSQL is required unless switched off (local Postgres without
    # TLS). DB_SSL_CA is the CA certificate of the shared Novus Aiven
    # PostgreSQL service (the same file for all four games) -- the PEM text
    # itself, or a path to a PEM file -- and adds
    # certificate and hostname verification (sslmode=verify-full); without it
    # the link is encrypted (sslmode=require) but the server is not
    # authenticated.
    DB_REQUIRE_SSL: bool = True
    DB_SSL_CA: str = ""
    # Connection pool per process: at most DB_POOL_SIZE + DB_MAX_OVERFLOW
    # (2 + 1) connections. The four Novus games share one Aiven Free service
    # (20 connections); the budget is in DEPLOY.md, "Managed PostgreSQL
    # version". Raise both on a bigger plan.
    DB_POOL_SIZE: int = 2
    DB_MAX_OVERFLOW: int = 1

    # Room state lives in this process unless this is switched on. See
    # `app/services/state_backend.py` for what the switch changes and the
    # single-instance constraint it carries.
    REDIS_ENABLED: bool = False
    REDIS_URL: str = ""
    FIREBASE_SERVICE_ACCOUNT_JSON: str = ""

    # Hard ceilings — see spec §12.1.
    #
    # Only the display-name cap is a setting. The rest are deliberately
    # NOT declared here, because nothing would read it: `app/core` is pure and
    # must not import `app.config`, so the order,
    # week, delay, quantity, unit-value and player ceilings live on
    # `core.limits.Limits` / `DEFAULT_LIMITS` and arrive by injection,
    # and the room TTL and lock timeout are module constants in
    # `services/state_service.py`. They were fields here until they were found
    # to be read nowhere: an operator setting `MAX_WEEKS_LIMIT` in `.env` got
    # silence, not a different ceiling. Change a ceiling at the site that owns
    # it, and do not re-add a field here that no code reads.
    MAX_DISPLAY_NAME_LENGTH: int = 24

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=True, extra="ignore"
    )

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _reject_wildcard_origin(cls, value: list[str]) -> list[str]:
        """Never allow ``"*"`` in ``CORS_ORIGINS``.

        The app hardcodes ``allow_credentials=True`` (``app/main.py``), and a
        wildcard origin combined with credentials is an invalid, permissive
        combination that browsers themselves refuse to honour -- but only
        after the server has already advertised it. Reject it at config load
        instead.
        """
        if "*" in value:
            raise ValueError(
                'CORS_ORIGINS must never contain "*": this app sets '
                "allow_credentials=True, and the two together are an "
                "invalid, permissive combination."
            )
        return value

    @property
    def db_url(self) -> str:
        """SQLAlchemy URL for the PostgreSQL database (psycopg 3 driver).

        The credentials are percent-encoded with `quote(..., safe="")`, not
        `quote_plus`: a password containing `@`, `:`, `/`, `#`, `?`, `%` or `+`
        would otherwise split or corrupt the authority section, and `quote_plus`
        would encode a space as `+`, which SQLAlchemy's `unquote` of the userinfo
        component leaves in place — handing the driver a password that is not the
        one configured.
        """
        user = quote(self.DB_USER, safe="")
        password = quote(self.DB_PASSWORD, safe="")
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_DATABASE}"
        )

    @property
    def db_connect_args(self) -> dict[str, Any]:
        """psycopg connect args: a UTC session, plus the TLS mode.

        ``options=-c timezone=UTC`` makes every session read and write
        ``timestamptz`` in UTC whatever the server's own ``TimeZone`` is.
        With ``DB_SSL_CA`` the server certificate and hostname are verified
        (``sslmode=verify-full``); with only ``DB_REQUIRE_SSL`` the link is
        encrypted but not authenticated (``require``), never a silent
        plaintext fallback; with neither, libpq's default (``prefer``) applies.
        ``{}`` for a non-PostgreSQL URL, so a SQLite engine can share the path.
        """
        if not self.db_url.startswith("postgresql"):
            return {}
        args: dict[str, Any] = {"options": "-c timezone=UTC"}
        if self.DB_SSL_CA.strip():
            args |= {"sslmode": "verify-full", "sslrootcert": _ca_file(self.DB_SSL_CA)}
        elif self.DB_REQUIRE_SSL:
            args |= {"sslmode": "require"}
        return args


settings = Settings()
