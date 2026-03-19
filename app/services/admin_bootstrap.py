from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import User
from app.services.access import ROLE_SYSTEM_ADMIN


SYSTEM_ADMIN_BOOTSTRAP_EMAIL = "skv.sree@outlook.com"


def configured_system_admin_email() -> str:
    return (settings.system_admin_bootstrap_email or SYSTEM_ADMIN_BOOTSTRAP_EMAIL).strip().lower()


def ensure_system_admin_role(db: Session, user: User) -> bool:
    if user.role == ROLE_SYSTEM_ADMIN:
        return False
    user.role = ROLE_SYSTEM_ADMIN
    db.add(user)
    return True


def bootstrap_system_admin_by_email(db: Session) -> bool:
    email = configured_system_admin_email()
    if not email:
        return False

    user = db.execute(select(User).where(func.lower(User.email) == email)).scalars().first()
    if user is None:
        return False

    changed = ensure_system_admin_role(db, user)
    if changed:
        db.commit()
    return changed


def bootstrap_system_admin_for_user(db: Session, user: User) -> bool:
    email = configured_system_admin_email()
    if not email or not user.email:
        return False
    if user.email.strip().lower() != email:
        return False

    changed = ensure_system_admin_role(db, user)
    if changed:
        db.commit()
        db.refresh(user)
    return changed
