from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.db import Base, engine, get_db
from app.models import Card, Deck, User
from app.models.card_state import CardState
from app.services.review_service import ReviewService
from app.services.security import hash_password, verify_password
from app.services.session import sign_session

app = FastAPI(title="SRS Web")

templates = Jinja2Templates(directory="app/templates")

# optional static folder
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
def _startup():
    # For MVP: create tables automatically.
    # Replace with Alembic migrations in production.
    Base.metadata.create_all(bind=engine)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("home.html", {"request": request})


@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    return templates.TemplateResponse("auth/register.html", {"request": request})


@app.post("/register")
def register(
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    existing = db.execute(select(User).where(User.email == email)).scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail="email already registered")

    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()

    resp = RedirectResponse(url="/dashboard", status_code=303)
    resp.set_cookie("session", sign_session(user.id), httponly=True, samesite="lax")
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse("auth/login.html", {"request": request})


@app.post("/login")
def login(
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.execute(select(User).where(User.email == email)).scalars().first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=400, detail="invalid credentials")

    resp = RedirectResponse(url="/dashboard", status_code=303)
    resp.set_cookie("session", sign_session(user.id), httponly=True, samesite="lax")
    return resp


@app.post("/logout")
def logout():
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
def review_page(
    request: Request,
    user: User = Depends(current_user),
):
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

    # Return the next card fragment for HTMX
    return review_next(request=request, user=user, db=db)
