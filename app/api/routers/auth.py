from __future__ import annotations

import logging
from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import optional_current_user
from app.core.config import settings
from app.core.db import get_db
from app.models import User
from app.services.admin_bootstrap import bootstrap_system_admin_for_user
from app.services.google_identity import (
    build_google_oauth,
    load_google_identity_config,
)
from app.services.microsoft_identity import (
    build_claims_options,
    build_oauth,
    load_identity_config,
    validate_userinfo_issuer,
)
from app.services.session import sign_session

router = APIRouter(tags=["auth"])
logger = logging.getLogger(__name__)


def _resolve_user_from_oidc_userinfo(
    db: Session, cfg, userinfo: dict[str, object]
) -> User:
    sub = str(userinfo[cfg.subject_claim])
    email = userinfo.get("email")
    if not email and isinstance(userinfo.get("emails"), list) and userinfo["emails"]:
        email = userinfo["emails"][0]
    if not email and userinfo.get("preferred_username"):
        email = userinfo.get("preferred_username")

    user = db.execute(select(User).where(User.identity_sub == sub)).scalars().first()
    if user is None and email:
        user = db.execute(select(User).where(User.email == email)).scalars().first()
        if user is not None:
            user.identity_sub = sub
            db.commit()
    if user is None:
        user = User(
            identity_sub=sub, email=email, is_test_enabled=settings.test_enabled_default
        )
        db.add(user)
        db.commit()
    else:
        if email and user.email != email:
            user.email = email
            db.commit()
    return user


@router.get("/login")
async def login(
    request: Request,
    user: User | None = Depends(optional_current_user),
):
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=303)

    return RedirectResponse(url="/login/providers", status_code=303)


@router.get("/login/microsoft")
async def login_microsoft(
    request: Request, user: User | None = Depends(optional_current_user)
):
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=303)

    cfg = load_identity_config()
    oauth = build_oauth()
    return await oauth.microsoft.authorize_redirect(request, cfg.redirect_uri)


@router.get("/login/google")
async def login_google(
    request: Request, user: User | None = Depends(optional_current_user)
):
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=303)

    cfg = load_google_identity_config()
    oauth = build_google_oauth()
    return await oauth.google.authorize_redirect(request, cfg.redirect_uri)


@router.get("/auth/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    cfg = load_identity_config()
    oauth = build_oauth()
    metadata = await oauth.microsoft.load_server_metadata()
    claims_options = build_claims_options(metadata.get("issuer"))

    try:
        token = await oauth.microsoft.authorize_access_token(
            request, claims_options=claims_options
        )
    except OAuthError as exc:
        logger.warning("OIDC callback failed: %s (%s)", exc.error, exc.description)
        raise HTTPException(status_code=400, detail="login failed") from exc

    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.microsoft.parse_id_token(request, token)

    if not isinstance(userinfo, dict) or not userinfo.get(cfg.subject_claim):
        raise HTTPException(status_code=400, detail="invalid id token")

    if not validate_userinfo_issuer(metadata.get("issuer"), userinfo):
        raise HTTPException(status_code=400, detail="invalid issuer")

    user = _resolve_user_from_oidc_userinfo(db, cfg, userinfo)
    bootstrap_system_admin_for_user(db, user)

    resp = RedirectResponse(url="/dashboard", status_code=303)
    resp.set_cookie(
        settings.app_session_cookie_name,
        sign_session(user_id=user.id, claims=userinfo),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.app_session_max_age_seconds,
        expires=settings.app_session_max_age_seconds,
    )
    return resp


@router.get("/auth/google/callback")
async def auth_google_callback(request: Request, db: Session = Depends(get_db)):
    logger.info("Google callback reached: %s", request.url)
    cfg = load_google_identity_config()
    oauth = build_google_oauth()

    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        logger.warning("Google callback failed: %s (%s)", exc.error, exc.description)
        raise HTTPException(status_code=400, detail="login failed") from exc

    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.google.parse_id_token(request, token)

    if not isinstance(userinfo, dict) or not userinfo.get(cfg.subject_claim):
        raise HTTPException(status_code=400, detail="invalid id token")

    user = _resolve_user_from_oidc_userinfo(db, cfg, userinfo)
    bootstrap_system_admin_for_user(db, user)

    resp = RedirectResponse(url="/dashboard", status_code=303)
    resp.set_cookie(
        settings.app_session_cookie_name,
        sign_session(user_id=user.id, claims=userinfo),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.app_session_max_age_seconds,
        expires=settings.app_session_max_age_seconds,
    )
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie(settings.app_session_cookie_name)
    return resp
