"""Startup check: say how the PostgreSQL connection is protected.

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
    """Log at WARNING unless the server is verified against `DB_SSL_CA`.

    Mirrors `Settings.db_connect_args`: a CA means `verify-full` whatever
    `DB_REQUIRE_SSL` says, so the CA is checked first.
    """
    if settings.DB_SSL_CA.strip():
        logger.info(
            "PostgreSQL TLS is required and verified against DB_SSL_CA "
            "(sslmode=verify-full)."
        )
    elif settings.DB_REQUIRE_SSL:
        logger.warning(
            "DB_SSL_CA is not set: PostgreSQL connections are encrypted "
            "(sslmode=require), but the server certificate is NOT verified."
        )
    else:
        logger.warning(
            "DB_REQUIRE_SSL is false: PostgreSQL connections are not required "
            "to use TLS (sslmode=prefer) and fall back to plaintext if the "
            "server offers none."
        )
