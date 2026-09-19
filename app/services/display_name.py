"""The shared display-name sanitiser (``10-rooms-rest-api.md`` §3.1a).

One implementation, used at every point a display name enters the backend.
The algorithm is ``16-frontend-foundation.md`` §4's, step for step, because
the same rule applied at each of the several collection points is a rule one
of them is guaranteed to get wrong independently -- which is exactly what
happened twice on the frontend before it was written down here.

This is **not** ``app.services.user_service._sanitise``: that function strips
control characters and truncates, with no whitespace collapsing and no
fallback. It is right for a stored profile field and wrong here, and it must
not be reused or modified for this job.
"""

from __future__ import annotations

import re
import unicodedata

from ..config import settings

__all__ = ["sanitise_display_name"]

# Whitespace control characters are *replaced* with a single space rather
# than removed outright, so a tab- or newline-separated paste ("Ana\\nSmith")
# does not have its words silently joined ("AnaSmith") -- the first of the
# two mistakes `16 §4` records two agents making independently.
_WHITESPACE_CONTROL = frozenset("\t\n\r\v\f")

_WHITESPACE_RUN = re.compile(r"\s+")


def sanitise_display_name(raw: str | None, fallback: str) -> str:
    """Clean a host- or player-supplied display name.

    In order:

    1. replace each whitespace control character (``\\t``, ``\\n``, ``\\r``,
       ``\\v``, ``\\f``) with a space, and remove every other control
       character;
    2. collapse runs of whitespace to one space;
    3. trim;
    4. clamp to ``settings.MAX_DISPLAY_NAME_LENGTH``;
    5. trim again -- clamping can land on a trailing space, which step 3
       never saw because it did not exist yet.

    A value that sanitises to empty returns ``fallback`` rather than ``""``.
    That is the one place this differs from the frontend's
    ``setDisplayName``, which *clears* the stored name instead; the server
    always needs something to put in the room document.
    """
    if raw is None:
        return fallback

    cleaned_characters: list[str] = []
    for character in raw:
        if character in _WHITESPACE_CONTROL:
            cleaned_characters.append(" ")
        elif unicodedata.category(character) == "Cc":
            continue
        else:
            cleaned_characters.append(character)
    cleaned = "".join(cleaned_characters)

    cleaned = _WHITESPACE_RUN.sub(" ", cleaned)
    cleaned = cleaned.strip()
    cleaned = cleaned[: settings.MAX_DISPLAY_NAME_LENGTH]
    cleaned = cleaned.strip()

    return cleaned if cleaned else fallback
