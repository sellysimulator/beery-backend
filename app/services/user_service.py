"""Profile persistence for Firebase-authenticated users."""

import logging
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models.user import User

logger = logging.getLogger(__name__)

_EMAIL_MAX_LENGTH = 200
_PHOTO_URL_MAX_LENGTH = 500


def _sanitise(value: str | None, max_length: int) -> str | None:
    """Strip control characters from untrusted display data, then truncate.

    Truncation happens after stripping, so a name padded with control characters
    is not silently shortened by them (00-decisions.md §5).
    """
    if value is None:
        return None
    cleaned = "".join(ch for ch in value if unicodedata.category(ch) != "Cc")
    return cleaned[:max_length]


class UserService:
    """Reads and writes the `users` table. Guests never reach it."""

    def upsert_user(
        self,
        db: Session,
        firebase_uid: str,
        display_name: str | None,
        email: str | None,
        photo_url: str | None,
    ) -> User:
        """Create or refresh the profile for `firebase_uid`.

        `firebase_uid` comes from verified token claims, never from a request
        body. The three profile fields are untrusted display data and are
        sanitised before they are stored.
        """
        display_name = _sanitise(display_name, settings.MAX_DISPLAY_NAME_LENGTH)
        email = _sanitise(email, _EMAIL_MAX_LENGTH)
        photo_url = _sanitise(photo_url, _PHOTO_URL_MAX_LENGTH)

        user = self.get_by_firebase_uid(db, firebase_uid)
        if user is None:
            user = User(
                firebase_uid=firebase_uid,
                display_name=display_name,
                email=email,
                photo_url=photo_url,
            )
            db.add(user)
        else:
            user.display_name = display_name
            user.email = email
            user.photo_url = photo_url

        db.commit()
        db.refresh(user)
        return user

    def get_by_firebase_uid(self, db: Session, firebase_uid: str) -> User | None:
        """The profile for a uid, or None when it has never been upserted."""
        return db.execute(
            select(User).where(User.firebase_uid == firebase_uid)
        ).scalar_one_or_none()
