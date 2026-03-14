from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy.exc import IntegrityError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.db import get_db
from app.models import Card, Deck, User
from app.models.card_state import CardState
from app.services.csv_import import CsvImportError, parse_cards_csv
from app.services.review_service import ReviewService

router = APIRouter(tags=["pages"])

APP_DIR = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def static_asset_url(path: str) -> str:
    asset_path = STATIC_DIR / path.lstrip("/")
    if not asset_path.exists():
        return f"/static/{path.lstrip('/')}"

    version = asset_path.stat().st_mtime_ns
    return f"/static/{path.lstrip('/')}?v={version}"


templates.env.globals["static_asset_url"] = static_asset_url


def _deck_cards_response(
    request: Request,
    *,
    user: User,
    deck: Deck,
    cards: list[Card],
    title: str,
    status_code: int = 200,
    import_error: str | None = None,
    import_success: str | None = None,
    update_error: str | None = None,
    update_success: str | None = None,
):
    return templates.TemplateResponse(
        "cards/list.html",
        {
            "request": request,
            "user": user,
            "deck": deck,
            "cards": cards,
            "title": title,
            "import_error": import_error,
            "import_success": import_success,
            "update_error": update_error,
            "update_success": update_success,
        },
        status_code=status_code,
    )


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
    cleaned_name = name.strip()
    cleaned_description = description.strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="Deck name is required.")

    deck = Deck(user_id=user.id, name=cleaned_name, description=cleaned_description or None)
    db.add(deck)
    db.commit()

    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/decks/{deck_id}/update")
def update_deck(
    request: Request,
    deck_id: str,
    name: str = Form(...),
    description: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or deck.user_id != user.id:
        raise HTTPException(status_code=404)

    cleaned_name = name.strip()
    cleaned_description = description.strip()
    if not cleaned_name:
        cards = db.execute(select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())).scalars().all()
        return _deck_cards_response(
            request,
            user=user,
            deck=deck,
            cards=cards,
            title=f"{deck.name} | edu selviz",
            status_code=400,
            update_error="Deck name cannot be empty.",
        )

    deck.name = cleaned_name
    deck.description = cleaned_description or None

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        cards = db.execute(select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())).scalars().all()
        return _deck_cards_response(
            request,
            user=user,
            deck=deck,
            cards=cards,
            title=f"{deck.name} | edu selviz",
            status_code=400,
            update_error="Unable to update this deck right now. Please try again.",
        )

    success_message = quote_plus("Deck details updated")
    return RedirectResponse(url=f"/decks/{deck.id}?update_success={success_message}", status_code=303)


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
    return _deck_cards_response(
        request,
        user=user,
        deck=deck,
        cards=cards,
        title=f"{deck.name} | edu selviz",
        import_success=request.query_params.get("import_success"),
        update_success=request.query_params.get("update_success"),
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


@router.post("/decks/{deck_id}/cards/import")
def import_cards_csv(
    request: Request,
    deck_id: str,
    csv_file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or deck.user_id != user.id:
        raise HTTPException(status_code=404)

    cards = db.execute(select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())).scalars().all()

    filename = (csv_file.filename or "").strip()
    if not filename.lower().endswith(".csv"):
        return _deck_cards_response(
            request,
            user=user,
            deck=deck,
            cards=cards,
            title=f"{deck.name} | edu selviz",
            status_code=400,
            import_error="Please upload a .csv file.",
        )

    try:
        imported_rows = parse_cards_csv(csv_file.file)
    except CsvImportError as exc:
        return _deck_cards_response(
            request,
            user=user,
            deck=deck,
            cards=cards,
            title=f"{deck.name} | edu selviz",
            status_code=400,
            import_error=str(exc),
        )
    finally:
        csv_file.file.close()

    new_cards = [Card(deck_id=deck.id, front=row.front, back=row.back) for row in imported_rows]
    db.add_all(new_cards)
    db.flush()
    db.add_all([CardState(card_id=card.id) for card in new_cards])
    db.commit()

    card_word = "card" if len(new_cards) == 1 else "cards"
    success_message = quote_plus(f"Imported {len(new_cards)} {card_word} from CSV")
    return RedirectResponse(
        url=f"/decks/{deck.id}?import_success={success_message}",
        status_code=303,
    )


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
