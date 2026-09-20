"""Startup check: say out loud which store holds live room state.

Which backend is in use decides a deployment constraint that nothing inside
a process can verify, so the only defence is to state it every time the app
starts and to have `README.md` say the same thing.
"""

from __future__ import annotations

import logging

from ...config import settings
from . import register_check

logger = logging.getLogger(__name__)


@register_check
def check_state_backend() -> None:
    """Log the active backend, and the constraint the in-memory one carries."""
    if settings.REDIS_ENABLED:
        if not settings.REDIS_URL:
            logger.critical(
                "REDIS_ENABLED is true but REDIS_URL is empty. Room state cannot "
                "be stored. Set REDIS_URL, or set REDIS_ENABLED=false."
            )
            return
        logger.info(
            "Room state: Redis. Multiple instances of this app may serve the "
            "same room."
        )
        return

    logger.warning(
        "Room state: THIS PROCESS's memory (REDIS_ENABLED is false). Two "
        "consequences, both of which are deployment constraints and neither "
        "of which this process can detect: (1) this must be the ONLY instance "
        "serving the app, because two players in one room landing on "
        "different instances would never see each other, with no error "
        "anywhere; (2) restarting this process ends every game in progress, "
        "because a game reaches MySQL only when it finishes. See README.md, "
        "'Room state and Redis'."
    )
