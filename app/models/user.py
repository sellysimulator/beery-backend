"""The `users` table: the profile of a Firebase-authenticated player.

Guests are never written here. A guest exists only in Redis room state and is
attributed by its guest identity string.

Section 13 owns the Alembic revision for this table and imports `User` for the
foreign keys that reference it; the two declarations must agree column for
column.
"""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """A registered user, keyed by the uid of a verified Firebase ID token."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    firebase_uid: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )
    # Capped at MAX_DISPLAY_NAME_LENGTH; `UserService` truncates before writing.
    display_name: Mapped[str | None] = mapped_column(String(24), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
