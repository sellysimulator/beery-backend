"""Profile persistence for Firebase-authenticated users."""

import logging
import unicodedata

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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

        user = self.get_or_create(db, firebase_uid)
        user.display_name = display_name
        user.email = email
        user.photo_url = photo_url

        db.commit()
        db.refresh(user)
        return user

    def get_by_firebase_uid(
        self, db: Session, firebase_uid: str, *, for_update: bool = False
    ) -> User | None:
        """The profile for a uid, or None when it has never been upserted."""
        query = select(User).where(User.firebase_uid == firebase_uid)
        if for_update:
            query = query.with_for_update()
        return db.execute(query).scalar_one_or_none()

    def get_or_create(self, db: Session, firebase_uid: str) -> User:
        """The row for a uid, inserting it if missing, safe against a concurrent insert.

        Does not commit: the row joins the caller's transaction. Two requests
        for the same uid (a doubled `/users/upsert` at sign-in, an upsert
        racing a guest claim, or a sign-in racing the end-of-game write) can
        both miss the lookup and both INSERT. The INSERT runs in a savepoint: the loser rolls back only that
        savepoint, keeping the caller's transaction, and re-reads the winner's
        row. The re-read is a locking read because MySQL's REPEATABLE READ
        snapshot would otherwise still show no row.
        """
        user = self.get_by_firebase_uid(db, firebase_uid)
        if user is not None:
            return user
        try:
            with db.begin_nested():
                user = User(firebase_uid=firebase_uid)
                db.add(user)
        except IntegrityError:
            user = self.get_by_firebase_uid(db, firebase_uid, for_update=True)
            if user is None:
                raise
        return user
