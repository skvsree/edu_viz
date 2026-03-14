import uuid

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.models import User
from app.services.session import unsign_session


def _resolve_user(
    session: str | None,
    db: Session,
) -> User | None:
    if not session:
        return None

    data = unsign_session(session)
    if not data:
        return None

    user_id_raw = data.get("user_id")
    if not user_id_raw:
        return None

    try:
        user_id = uuid.UUID(user_id_raw)
    except ValueError:
        return None

    user = db.get(User, user_id)
    return user


def optional_current_user(
    session: str | None = Cookie(default=None, alias=settings.app_session_cookie_name),
    db: Session = Depends(get_db),
) -> User | None:
    return _resolve_user(session, db)


def current_user(
    session: str | None = Cookie(default=None, alias=settings.app_session_cookie_name),
    db: Session = Depends(get_db),
) -> User:
    user = _resolve_user(session, db)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user
