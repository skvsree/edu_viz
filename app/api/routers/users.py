"""User lookup API."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from app.api.deps import current_user
from app.models import User

router = APIRouter(prefix="/api/v1/users", tags=["users"])


@router.get("/by-email")
def get_user_by_email(
    email: str,
    user: User = Depends(current_user),
):
    """Look up a user by email. Only available to authenticated users."""
    result = select(User).where(User.email == email.lower().strip())
    from app.core.db import engine

    from sqlalchemy.orm import Session

    with Session(engine) as db:
        target = db.execute(result).scalars().first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    return {"id": str(target.id), "email": target.email}


@router.get("/{target_user_id}")
def get_user(
    target_user_id: uuid.UUID,
    user: User = Depends(current_user),
):
    """Get a user by ID."""
    from app.core.db import engine
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        target = db.get(User, target_user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    return {"id": str(target.id), "email": target.email}
