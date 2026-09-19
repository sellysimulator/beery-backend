"""Guest -> account claim on the results screen (`14-end-of-game-persistence.md §3.8`).

A guest identity is a credential-shaped value that arrives from the client's
`localStorage` and is therefore attacker-controllable. It is never logged and
never returned in a payload: this module's public method returns only a row
count. Does not touch `app/services/user_service.py`; it calls
`UserService.upsert_user` as a dependency (**D19**).
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from .stats_service import get_stats_service
from .user_service import UserService

logger = logging.getLogger(__name__)

__all__ = ["ClaimService"]

# `user_id IS NULL` makes a second claim of the same guest id a no-op
# (`§3.8` step 2): a row already claimed is never re-attributed even if
# `guest_identity` still happens to match (failure mode 10).
_COUNT_MATCHING_PARTICIPANTS = text("""
    SELECT COUNT(*) FROM participants
    WHERE guest_identity = :guest_identity AND user_id IS NULL
    """)

_CLAIM_PARTICIPANTS = text("""
    UPDATE participants
    SET user_id = :user_id, guest_identity = NULL
    WHERE guest_identity = :guest_identity AND user_id IS NULL
    """)


class ClaimService:
    """Attributes a guest's finished games to the account they just made."""

    def __init__(self, user_service: UserService | None = None) -> None:
        self._user_service = user_service or UserService()

    def claim_guest_results(
        self, db: Session, firebase_uid: str, guest_identity: str
    ) -> int:
        """Attribute every participant row holding `guest_identity` to this
        user. Returns the number of rows claimed. Recomputes that user's
        stats. Returns 0 and writes nothing when the guest identity is
        unknown -- "nothing" includes not creating a `users` row for
        `firebase_uid` either, so checking for a match comes before
        resolving or creating the user.
        """
        match_count = db.execute(
            _COUNT_MATCHING_PARTICIPANTS, {"guest_identity": guest_identity}
        ).scalar_one()
        if not match_count:
            return 0

        user = self._user_service.get_by_firebase_uid(db, firebase_uid)
        if user is None:
            user = self._user_service.upsert_user(
                db,
                firebase_uid=firebase_uid,
                display_name=None,
                email=None,
                photo_url=None,
            )

        result = db.execute(
            _CLAIM_PARTICIPANTS,
            {"user_id": user.id, "guest_identity": guest_identity},
        )
        claimed = int(result.rowcount or 0)
        db.commit()

        if claimed:
            get_stats_service().recompute_user_stats(db, user.id)

        return claimed
