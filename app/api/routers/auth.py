from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.models import User
from app.services.azure_b2c import build_oauth, load_b2c_config
from app.services.session import sign_session

router = APIRouter(tags=["auth"])

oauth = build_oauth()


@router.get("/login")
async def login(request: Request):
    _ = load_b2c_config()
    return await oauth.azureb2c.authorize_redirect(request, settings.azure_b2c_redirect_uri)


@router.get("/auth/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    token = await oauth.azureb2c.authorize_access_token(request)

    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.azureb2c.parse_id_token(request, token)

    if not isinstance(userinfo, dict) or not userinfo.get("sub"):
        raise HTTPException(status_code=400, detail="invalid id token")

    sub = str(userinfo["sub"])
    email = userinfo.get("email")
    if not email and isinstance(userinfo.get("emails"), list) and userinfo["emails"]:
        email = userinfo["emails"][0]

    user = db.execute(select(User).where(User.b2c_sub == sub)).scalars().first()
    if user is None:
        user = User(b2c_sub=sub, email=email)
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
