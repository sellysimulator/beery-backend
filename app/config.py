"""Application settings, loaded from environment variables and `.env`."""

from typing import Any
from urllib.parse import quote

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""
    DB_DATABASE: str = "beery"
    DB_REQUIRE_SSL: bool = False
    DB_SSL_CA: str = ""

    REDIS_URL: str = ""
    FIREBASE_SERVICE_ACCOUNT_JSON: str = ""

    # Hard ceilings — see 00-decisions.md §5
    MAX_ORDER_QUANTITY: int = 9_999
    MAX_WEEKS_LIMIT: int = 104
    MIN_WEEKS: int = 8
    MAX_DELAY_WEEKS: int = 8
    MIN_DELAY_WEEKS: int = 1
    MAX_INITIAL_QUANTITY: int = 9_999
    MAX_UNIT_VALUE: float = 1_000_000.0
    MAX_PLAYERS: int = 4
    MAX_DISPLAY_NAME_LENGTH: int = 24
    ROOM_TTL_SECONDS: int = 86_400
    LOCK_TIMEOUT_SECONDS: int = 10

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
        instead (23-deployment-and-ci.md AC 5, failure mode 2).
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
        """SQLAlchemy URL for the MySQL database.

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
            f"mysql+pymysql://{user}:{password}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_DATABASE}"
        )

    @property
    def db_connect_args(self) -> dict:
        """PyMySQL connect args; empty unless TLS is explicitly required."""
        if not self.DB_REQUIRE_SSL:
            return {}
        ssl_opts: dict[str, Any] = {}
        if self.DB_SSL_CA:
            ssl_opts["ca"] = self.DB_SSL_CA
        return {"ssl": ssl_opts}


settings = Settings()
