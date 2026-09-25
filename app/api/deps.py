import uuid

import secrets

from fastapi import Cookie, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.models import User
from app.services.access import ROLE_SYSTEM_ADMIN
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


def _verify_bulk_import_key(x_api_key: str | None) -> None:
    configured_key = (settings.bulk_import_api_key or "").strip()
    if not configured_key:
        raise HTTPException(status_code=503, detail="Bulk import API is not configured")
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing API key")
    if not secrets.compare_digest(x_api_key, configured_key):
        raise HTTPException(status_code=403, detail="Invalid API key")


def require_bulk_import_api_key(x_api_key: str | None = Header(default=None)) -> None:
    _verify_bulk_import_key(x_api_key)


def first_system_admin(db: Session) -> User | None:
    """The oldest system admin, or ``None`` when the install has no admin yet."""
    return (
        db.execute(
            select(User)
            .where(User.role == ROLE_SYSTEM_ADMIN)
            .order_by(User.created_at.asc())
        )
        .scalars()
        .first()
    )


def bulk_import_system_admin(db: Session) -> User:
    """The user an API-key request acts as."""
    user = first_system_admin(db)
    if user is None:
        raise HTTPException(
            status_code=503,
            detail="No system admin is available for bulk import",
        )
    return user


def bulk_import_or_session_user(
    session: str | None = Cookie(default=None, alias=settings.app_session_cookie_name),
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Authenticate a browser session (cookie) or an automation script (``X-Api-Key``).

    A valid session always wins, so nothing changes for the web UI. Without one,
    the bulk-import API key authenticates the caller as the system admin — the
    same identity ``/api/v1/import/*`` uses — so external importers do not have
    to forge a session cookie.
    """
    user = _resolve_user(session, db)
    if user is not None:
        return user

    if x_api_key:
        _verify_bulk_import_key(x_api_key)
        return bulk_import_system_admin(db)

    raise HTTPException(status_code=401, detail="not authenticated")
