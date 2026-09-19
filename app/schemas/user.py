"""Request and response models for the user profile routes."""

from pydantic import BaseModel, ConfigDict


class UserUpsertRequest(BaseModel):
    """Profile fields a client may offer when it signs in.

    `firebase_uid` is deliberately absent: it is derived server-side from the
    verified ID token and is never trusted from the body.
    """

    display_name: str | None = None
    email: str | None = None
    photo_url: str | None = None


class UserResponse(BaseModel):
    """A profile as returned to its owner.

    `firebase_uid` is deliberately absent: the identity is server-only and never
    goes on the wire (00-conventions.md §2).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str | None
    email: str | None
    photo_url: str | None
