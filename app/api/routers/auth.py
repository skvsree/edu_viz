from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models import User
from app.services.microsoft_identity import build_oauth, load_identity_config
from app.services.session import sign_session

router = APIRouter(tags=["auth"])


@router.get("/login")
async def login(request: Request):
    cfg = load_identity_config()
    oauth = build_oauth()
    return await oauth.microsoft.authorize_redirect(request, cfg.redirect_uri)


@router.get("/auth/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    cfg = load_identity_config()
    oauth = build_oauth()
    token = await oauth.microsoft.authorize_access_token(request)

    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.microsoft.parse_id_token(request, token)

    if not isinstance(userinfo, dict) or not userinfo.get(cfg.subject_claim):
        raise HTTPException(status_code=400, detail="invalid id token")

    sub = str(userinfo[cfg.subject_claim])
    email = userinfo.get("email")
    if not email and isinstance(userinfo.get("emails"), list) and userinfo["emails"]:
        email = userinfo["emails"][0]

    user = db.execute(select(User).where(User.identity_sub == sub)).scalars().first()
    if user is None:
        user = User(identity_sub=sub, email=email)
        db.add(user)
        db.commit()
    else:
        if email and user.email != email:
            user.email = email
            db.commit()

    resp = RedirectResponse(url="/dashboard", status_code=303)
    resp.set_cookie(
        "session",
        sign_session(user_id=user.id, claims=userinfo),
        httponly=True,
        samesite="lax",
    )
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("session")
    return resp
