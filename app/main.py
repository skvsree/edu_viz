from __future__ import annotations

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.api.deps import current_user
from app.core.config import settings
from app.core.db import get_db
from app.models import Card, Deck, User
from app.models.card_state import CardState
from app.services.azure_b2c import build_oauth, load_b2c_config
from app.services.review_service import ReviewService
from app.services.session import sign_session

app = FastAPI(title="SRS Web")

# Needed by Authlib to store OIDC state/nonce during the redirect flow.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

templates = Jinja2Templates(directory="app/templates")

oauth = build_oauth()

# optional static folder
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/login")
async def login(request: Request):
    # Redirect to Azure AD B2C authorization endpoint
    # Ensure config exists early (gives a clear error if env vars missing)
    _ = load_b2c_config()
    return await oauth.azureb2c.authorize_redirect(request, settings.azure_b2c_redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    token = await oauth.azureb2c.authorize_access_token(request)

    # Parse & validate ID token using provider JWKS.
    # Contains claims including sub/email/name.
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
        # keep email updated if provided
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


@app.post("/logout")
def logout():
    # Local logout (clears our session cookie).
    # Optional: redirect to Azure B2C logout endpoint later.
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("session")
    return resp


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    decks = db.execute(select(Deck).where(Deck.user_id == user.id).order_by(Deck.created_at.desc())).scalars().all()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "decks": decks},
    )


@app.post("/decks")
def create_deck(
    name: str = Form(...),
    description: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = Deck(user_id=user.id, name=name, description=description or None)
    db.add(deck)
    db.commit()

    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/decks/{deck_id}", response_class=HTMLResponse)
def deck_cards(
    request: Request,
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or deck.user_id != user.id:
        raise HTTPException(status_code=404)

    cards = db.execute(select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())).scalars().all()
    return templates.TemplateResponse(
        "cards/list.html",
        {"request": request, "user": user, "deck": deck, "cards": cards},
    )


@app.post("/decks/{deck_id}/cards")
def create_card(
    deck_id: str,
    front: str = Form(...),
    back: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or deck.user_id != user.id:
        raise HTTPException(status_code=404)

    card = Card(deck_id=deck.id, front=front, back=back)
    db.add(card)
    db.flush()

    # create default state
    state = CardState(card_id=card.id)
    db.add(state)
    db.commit()

    return RedirectResponse(url=f"/decks/{deck.id}", status_code=303)


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse("review/page.html", {"request": request, "user": user})


@app.get("/review/next", response_class=HTMLResponse)
def review_next(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    svc = ReviewService()
    card = svc.next_due_card(db, user_id=user.id)
    if card is None:
        return templates.TemplateResponse("review/empty.html", {"request": request, "user": user})

    return templates.TemplateResponse(
        "review/card.html",
        {"request": request, "user": user, "card": card},
    )


@app.post("/review/rate", response_class=HTMLResponse)
def review_rate(
    request: Request,
    card_id: str = Form(...),
    rating: int = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    # ownership check: ensure card belongs to user
    card = db.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404)
    deck = db.get(Deck, card.deck_id)
    if not deck or deck.user_id != user.id:
        raise HTTPException(status_code=403)

    svc = ReviewService()
    svc.rate(db, card_id=card.id, rating=rating)
    db.commit()

    return review_next(request=request, user=user, db=db)
