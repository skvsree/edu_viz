import uuid

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models import User
from app.services.session import unsign_session


def current_user(
    session: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    if not session:
        raise HTTPException(status_code=401, detail="not authenticated")

    user_id: uuid.UUID | None = unsign_session(session)
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid session")

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="user not found")

    return user
