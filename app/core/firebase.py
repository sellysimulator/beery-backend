"""Firebase Admin bootstrap, used to verify ID tokens minted by the frontend.

The credential is a single environment variable holding the **entire** service
account JSON on one line, which is what platforms like Render make easy; mounting
a credentials file is not.

In a `.env` file that value must be in single quotes or no quotes. With double
quotes python-dotenv applies escape decoding, turns the `\\n` inside `private_key`
into real newlines, then fails to parse the line at all and leaves the variable
silently unset.
"""

import json
import logging

import firebase_admin  # type: ignore[import-untyped]
from firebase_admin import credentials  # type: ignore[import-untyped]

from ..config import settings

logger = logging.getLogger(__name__)

_app: firebase_admin.App | None = None
_credential_failure_logged = False


def init_firebase() -> firebase_admin.App | None:
    """Return the lazily initialised Firebase Admin app, or None.

    Returns None — never raises — when `FIREBASE_SERVICE_ACCOUNT_JSON` is unset
    or will not parse. Every later call returns the same cached app object.

    A failure is deliberately indistinguishable, to a caller, from an invalid
    token: `verify_firebase_id_token` turns both into None and both mean reject.
    """
    global _app, _credential_failure_logged

    if not settings.FIREBASE_SERVICE_ACCOUNT_JSON:
        return None

    if _app is not None:
        return _app

    try:
        if firebase_admin._apps:
            # Another import path already initialised the SDK; adopt that app
            # rather than raising on a duplicate `initialize_app`.
            _app = firebase_admin.get_app()
            return _app
        certificate = credentials.Certificate(
            json.loads(settings.FIREBASE_SERVICE_ACCOUNT_JSON)
        )
        _app = firebase_admin.initialize_app(certificate)
    except Exception as exc:  # noqa: BLE001 — unconfigured must never raise
        if not _credential_failure_logged:
            # Only the exception type is logged: the message of a credential
            # error can quote the service account contents.
            logger.error(
                "FIREBASE_SERVICE_ACCOUNT_JSON could not be loaded (%s). Firebase "
                "ID token verification is disabled.",
                type(exc).__name__,
            )
            _credential_failure_logged = True
        return None

    return _app
