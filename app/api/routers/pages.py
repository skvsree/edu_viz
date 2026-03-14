from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pathlib import Path

from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.db import get_db
from app.models import Card, Deck, User
from app.models.card_state import CardState
from app.services.review_service import ReviewService

router = APIRouter(tags=["pages"])

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "title": "edu selviz | Professional study workflow"},
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    decks = db.execute(select(Deck).where(Deck.user_id == user.id).order_by(Deck.created_at.desc())).scalars().all()
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user, "decks": decks, "title": "Workspace | edu selviz"},
    )


@router.post("/decks")
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


@router.get("/decks/{deck_id}", response_class=HTMLResponse)
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
        {"request": request, "user": user, "deck": deck, "cards": cards, "title": f"{deck.name} | edu selviz"},
    )


@router.post("/decks/{deck_id}/cards")
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

    state = CardState(card_id=card.id)
    db.add(state)
    db.commit()

    return RedirectResponse(url=f"/decks/{deck.id}", status_code=303)


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(
        "review/page.html",
        {"request": request, "user": user, "title": "Review | edu selviz"},
    )


@router.get("/review/next", response_class=HTMLResponse)
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


@router.post("/review/rate", response_class=HTMLResponse)
def review_rate(
    request: Request,
    card_id: str = Form(...),
    rating: int = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
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
