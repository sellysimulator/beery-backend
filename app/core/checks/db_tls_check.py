"""Startup check: say how the MySQL connection is protected.

This logs; it never aborts. A `DB_SSL_CA` that cannot be loaded never reaches
this check -- building the engine raises first, so the process does not start.
"""

from __future__ import annotations

import logging

from ...config import settings
from . import register_check

logger = logging.getLogger(__name__)


@register_check
def check_db_tls() -> None:
    """Log at WARNING unless the server is verified against `DB_SSL_CA`."""
    if not settings.DB_REQUIRE_SSL:
        logger.warning(
            "DB_REQUIRE_SSL is false: MySQL connections are not required to use "
            "TLS and fall back to plaintext if the server offers none."
        )
    elif not settings.DB_SSL_CA.strip():
        logger.warning(
            "DB_SSL_CA is not set: MySQL connections are encrypted, but the "
            "server certificate is NOT verified."
        )
    else:
        logger.info("MySQL TLS is required and verified against DB_SSL_CA.")
