import uuid

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.models import User
from app.services.session import unsign_session


def current_user(
    session: str | None = Cookie(default=None, alias=settings.app_session_cookie_name),
    db: Session = Depends(get_db),
) -> User:
    if not session:
        raise HTTPException(status_code=401, detail="not authenticated")

    data = unsign_session(session)
    if not data:
        raise HTTPException(status_code=401, detail="invalid session")

    user_id_raw = data.get("user_id")
    if not user_id_raw:
        raise HTTPException(status_code=401, detail="invalid session")

    try:
        user_id = uuid.UUID(user_id_raw)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid session")

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="user not found")

    return user
