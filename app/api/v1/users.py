"""User profile routes.

There is deliberately no route that accepts a uid as a parameter. If one is ever
added it must assert that the path uid equals the caller's own.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ...db.session import get_db
from ...schemas.user import UserResponse, UserUpsertRequest
from ...services.user_service import UserService
from ..deps import get_current_firebase_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])

_user_service = UserService()

_INTERNAL_ERROR_DETAIL = "Internal server error."


def _or_claim(value: str | None, claims: dict, key: str) -> str | None:
    """A body field, falling back to the corresponding verified claim.

    The fallback fires only when the field is absent or None. The claim is
    verified data, so it is at least as trustworthy as the body, and this is what
    gives a Google user a profile without the client having to echo one back. An
    empty string is a value the caller sent, not an absent field, so it is kept.
    """
    if value is not None:
        return value
    claim = claims.get(key)
    return claim if isinstance(claim, str) else None


@router.post("/upsert", response_model=UserResponse)
def upsert_user(
    payload: UserUpsertRequest,
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
) -> UserResponse:
    """Create or refresh the caller's own profile.

    Called by the frontend straight after a successful Google sign-in. The uid is
    taken from the verified token; a uid-shaped value in the body has no effect
    whatsoever.
    """
    try:
        user = _user_service.upsert_user(
            db,
            firebase_uid=claims["uid"],
            display_name=_or_claim(payload.display_name, claims, "name"),
            email=_or_claim(payload.email, claims, "email"),
            photo_url=_or_claim(payload.photo_url, claims, "picture"),
        )
    except Exception:
        # The driver's own message names host, port and user, so it is logged
        # and never returned.
        logger.exception("Failed to upsert a user profile.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None
    return UserResponse.model_validate(user)


@router.get("/me", response_model=UserResponse)
def get_me(
    db: Annotated[Session, Depends(get_db)],
    claims: Annotated[dict, Depends(get_current_firebase_user)],
) -> UserResponse:
    """The caller's own profile, or 404 when it has never been upserted."""
    try:
        user = _user_service.get_by_firebase_uid(db, claims["uid"])
    except Exception:
        logger.exception("Failed to read a user profile.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_INTERNAL_ERROR_DETAIL,
        ) from None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found."
        )
    return UserResponse.model_validate(user)
