"""Startup check: report whether Firebase ID token verification is available.

Registered purely by being importable — section 01's discovery loop imports this
module and `register_check` wires it into the lifespan, so `app/main.py` is not
edited (00-decisions.md D19).

This logs; it never aborts. Guest play is fully functional without Firebase, so
refusing to boot would turn a partial degradation into a total outage.
"""

import logging

from ..firebase import init_firebase
from . import register_check

logger = logging.getLogger(__name__)


@register_check
def check_auth_config() -> None:
    """Log at CRITICAL when Firebase is unconfigured; at INFO when it is not."""
    if init_firebase() is None:
        logger.critical(
            "FIREBASE_SERVICE_ACCOUNT_JSON is not set. Firebase ID token "
            "verification is DISABLED: signed-in users will be REJECTED at the "
            "Socket.IO handshake and every /api/v1/users/* route will return 503. "
            "Only guest play will work."
        )
    else:
        logger.info("Firebase ID token verification is configured and active.")
