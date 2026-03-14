from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote_plus
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import current_user, optional_current_user
from app.core.config import settings
from app.core.db import get_db
from app.models import Card, Deck, Organization, User
from app.models.card_state import CardState
from app.services.access import (
    ROLE_ADMIN,
    ROLE_SYSTEM_ADMIN,
    ROLE_USER,
    can_access_deck,
    can_manage_deck,
    can_manage_decks,
    normalize_deck_name,
)
from app.services.csv_import import CsvImportError, parse_cards_csv
from app.services.dashboard import list_accessible_deck_stats
from app.services.review_service import ReviewService

router = APIRouter(tags=["pages"])

APP_DIR = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@lru_cache(maxsize=256)
def _asset_version(path: str, mtime_ns: int, size: int) -> str:
    asset_path = STATIC_DIR / path
    digest = sha256(asset_path.read_bytes()).hexdigest()[:12]
    return digest


def static_asset_url(path: str) -> str:
    normalized_path = path.lstrip("/")
    asset_path = STATIC_DIR / normalized_path
    if not asset_path.exists() or not asset_path.is_file():
        return f"/static/{normalized_path}"

    stat = asset_path.stat()
    version = _asset_version(normalized_path, stat.st_mtime_ns, stat.st_size)
    return f"/assets/{version}/{normalized_path}"


templates.env.globals["static_asset_url"] = static_asset_url
templates.env.globals["footer_copyright_text"] = settings.footer_copyright_text


def _require_settings_access(user: User) -> None:
    if user.role not in {ROLE_ADMIN, ROLE_SYSTEM_ADMIN}:
        raise HTTPException(status_code=403, detail="You do not have access to settings.")



def _require_system_admin(user: User) -> None:
    if user.role != ROLE_SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="Only system admins can access this page.")



def _visible_users_stmt(user: User):
    stmt = select(User).options(selectinload(User.organization)).order_by(User.created_at.desc())
    if user.role == ROLE_SYSTEM_ADMIN:
        return stmt
    return stmt.where(User.organization_id == user.organization_id)



def _settings_home_response(
    request: Request,
    *,
    user: User,
    db: Session,
    settings_error: str | None = None,
    settings_success: str | None = None,
):
    _require_settings_access(user)

    organization_count = db.execute(select(Organization)).scalars().all() if user.role == ROLE_SYSTEM_ADMIN else []
    visible_users = db.execute(_visible_users_stmt(user)).scalars().all()

    return templates.TemplateResponse(
        "settings/index.html",
        {
            "request": request,
            "user": user,
            "organization_count": len(organization_count) if user.role == ROLE_SYSTEM_ADMIN else (1 if user.organization_id else 0),
            "visible_user_count": len(visible_users),
            "settings_error": settings_error if settings_error is not None else request.query_params.get("error"),
            "settings_success": settings_success if settings_success is not None else request.query_params.get("success"),
            "title": "Settings | edu selviz",
        },
    )



def _organizations_response(
    request: Request,
    *,
    user: User,
    db: Session,
    status_code: int = 200,
    organization_error: str | None = None,
    organization_success: str | None = None,
    active_modal: str | None = None,
    modal_organization: Organization | None = None,
    create_form: dict[str, str] | None = None,
    edit_form: dict[str, str] | None = None,
):
    _require_system_admin(user)
    organizations = db.execute(select(Organization).order_by(Organization.name.asc())).scalars().all()
    requested_org_id = request.query_params.get("edit_org")
    if modal_organization is None and requested_org_id:
        modal_organization = db.get(Organization, requested_org_id)
        if modal_organization and active_modal is None:
            active_modal = "edit"

    org_user_counts = {
        str(org.id): len(org.users or [])
        for org in organizations
    }

    return templates.TemplateResponse(
        "settings/organizations.html",
        {
            "request": request,
            "user": user,
            "organizations": organizations,
            "org_user_counts": org_user_counts,
            "organization_error": organization_error if organization_error is not None else request.query_params.get("error"),
            "organization_success": organization_success if organization_success is not None else request.query_params.get("success"),
            "active_modal": active_modal or request.query_params.get("modal"),
            "modal_organization": modal_organization,
            "create_form": create_form or {"name": ""},
            "edit_form": edit_form or {"name": modal_organization.name if modal_organization else ""},
            "title": "Organizations | edu selviz",
        },
        status_code=status_code,
    )



def _allowed_role_options(editor: User, target: User) -> list[str]:
    if editor.role == ROLE_SYSTEM_ADMIN:
        return [ROLE_USER, ROLE_ADMIN, ROLE_SYSTEM_ADMIN]
    if editor.role == ROLE_ADMIN and target.organization_id == editor.organization_id:
        return [ROLE_USER, ROLE_ADMIN]
    return []



def _users_response(
    request: Request,
    *,
    user: User,
    db: Session,
    status_code: int = 200,
    user_error: str | None = None,
    user_success: str | None = None,
):
    _require_settings_access(user)
    users = db.execute(_visible_users_stmt(user)).scalars().all()
    organizations = []
    if user.role == ROLE_SYSTEM_ADMIN:
        organizations = db.execute(select(Organization).order_by(Organization.name.asc())).scalars().all()

    editable_roles = {str(item.id): _allowed_role_options(user, item) for item in users}

    return templates.TemplateResponse(
        "settings/users.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "organizations": organizations,
            "editable_roles": editable_roles,
            "user_error": user_error if user_error is not None else request.query_params.get("error"),
            "user_success": user_success if user_success is not None else request.query_params.get("success"),
            "title": "Users | edu selviz",
        },
        status_code=status_code,
    )



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
    can_edit = can_manage_deck(user, deck)
    return templates.TemplateResponse(
        "cards/list.html",
        {
            "request": request,
            "user": user,
            "deck": deck,
            "cards": cards,
            "can_edit": can_edit,
            "title": title,
            "import_error": import_error,
            "import_success": import_success,
            "update_error": update_error,
            "update_success": update_success,
        },
        status_code=status_code,
    )



def _dashboard_response(
    request: Request,
    *,
    user: User,
    db: Session,
    status_code: int = 200,
    dashboard_error: str | None = None,
    dashboard_success: str | None = None,
    active_modal: str | None = None,
    modal_deck: Deck | None = None,
    create_form: dict[str, str | bool] | None = None,
    edit_form: dict[str, str] | None = None,
):
    deck_stats = list_accessible_deck_stats(db, user=user)
    requested_edit_deck_id = request.query_params.get("edit_deck")
    if modal_deck is None and requested_edit_deck_id:
        requested_deck = db.get(Deck, requested_edit_deck_id)
        if requested_deck and can_manage_deck(user, requested_deck):
            modal_deck = requested_deck
            if active_modal is None:
                active_modal = "edit"

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "deck_stats": deck_stats,
            "can_manage_decks": can_manage_decks(user),
            "can_manage_deck": can_manage_deck,
            "dashboard_error": dashboard_error if dashboard_error is not None else request.query_params.get("error"),
            "dashboard_success": dashboard_success if dashboard_success is not None else request.query_params.get("success"),
            "active_modal": active_modal or request.query_params.get("modal"),
            "modal_deck": modal_deck,
            "create_form": create_form or {"name": "", "description": "", "is_global": False},
            "edit_form": edit_form
            or {
                "name": modal_deck.name if modal_deck else "",
                "description": (modal_deck.description or "") if modal_deck else "",
            },
            "title": "Workspace | edu selviz",
        },
        status_code=status_code,
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request, user: User | None = Depends(optional_current_user)):
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=303)

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "user": user,
            "title": "edu selviz | Professional study workflow",
        },
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return _dashboard_response(request, user=user, db=db)


@router.get("/settings", response_class=HTMLResponse)
def settings_home(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return _settings_home_response(request, user=user, db=db)


@router.get("/settings/organizations", response_class=HTMLResponse)
def organizations_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return _organizations_response(request, user=user, db=db)


@router.post("/settings/organizations")
def create_organization(
    request: Request,
    name: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_system_admin(user)
    cleaned_name = name.strip()
    if not cleaned_name:
        return _organizations_response(
            request,
            user=user,
            db=db,
            status_code=400,
            organization_error="Organization name is required.",
            active_modal="create",
            create_form={"name": cleaned_name},
        )

    organization = Organization(name=cleaned_name)
    db.add(organization)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _organizations_response(
            request,
            user=user,
            db=db,
            status_code=400,
            organization_error="An organization with that name already exists.",
            active_modal="create",
            create_form={"name": cleaned_name},
        )

    return RedirectResponse(url="/settings/organizations?success=Organization+created", status_code=303)


@router.post("/settings/organizations/{organization_id}/update")
def update_organization(
    request: Request,
    organization_id: str,
    name: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_system_admin(user)
    organization = db.get(Organization, organization_id)
    if not organization:
        raise HTTPException(status_code=404)

    cleaned_name = name.strip()
    if not cleaned_name:
        return _organizations_response(
            request,
            user=user,
            db=db,
            status_code=400,
            organization_error="Organization name is required.",
            active_modal="edit",
            modal_organization=organization,
            edit_form={"name": cleaned_name},
        )

    organization.name = cleaned_name
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _organizations_response(
            request,
            user=user,
            db=db,
            status_code=400,
            organization_error="Unable to update this organization right now.",
            active_modal="edit",
            modal_organization=organization,
            edit_form={"name": cleaned_name},
        )

    return RedirectResponse(url="/settings/organizations?success=Organization+updated", status_code=303)


@router.get("/settings/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return _users_response(request, user=user, db=db)


@router.post("/settings/users/{target_user_id}/update")
def update_user_settings(
    request: Request,
    target_user_id: str,
    role: str = Form(...),
    organization_id: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_settings_access(user)
    target = db.get(User, target_user_id)
    if not target:
        raise HTTPException(status_code=404)

    editable_roles = _allowed_role_options(user, target)
    if role not in editable_roles:
        return _users_response(
            request,
            user=user,
            db=db,
            status_code=400,
            user_error="You cannot assign that role for this user.",
        )

    if user.role == ROLE_ADMIN and target.organization_id != user.organization_id:
        raise HTTPException(status_code=404)

    target.role = role
    if user.role == ROLE_SYSTEM_ADMIN:
        target.organization_id = UUID(organization_id) if organization_id else None
    elif user.role == ROLE_ADMIN:
        target.organization_id = user.organization_id

    db.commit()
    return RedirectResponse(url="/settings/users?success=User+updated", status_code=303)


@router.post("/decks")
def create_deck(
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    is_global: bool = Form(default=False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if not can_manage_decks(user):
        raise HTTPException(status_code=403, detail="You do not have permission to create decks.")

    cleaned_name = name.strip()
    cleaned_description = description.strip()
    if not cleaned_name:
        return _dashboard_response(
            request,
            user=user,
            db=db,
            status_code=400,
            dashboard_error="Deck name is required.",
            active_modal="create",
            create_form={"name": cleaned_name, "description": cleaned_description, "is_global": is_global},
        )
    normalized_name = normalize_deck_name(cleaned_name)
    if not normalized_name:
        return _dashboard_response(
            request,
            user=user,
            db=db,
            status_code=400,
            dashboard_error="Deck name must include letters or numbers.",
            active_modal="create",
            create_form={"name": cleaned_name, "description": cleaned_description, "is_global": is_global},
        )

    if is_global and user.role != ROLE_SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="Only system admins can create global decks.")

    organization_id = None if is_global else user.organization_id
    if not is_global and organization_id is None:
        return _dashboard_response(
            request,
            user=user,
            db=db,
            status_code=400,
            dashboard_error="Assign this admin to an organization before creating organization decks.",
            active_modal="create",
            create_form={"name": cleaned_name, "description": cleaned_description, "is_global": is_global},
        )

    deck = Deck(
        user_id=user.id,
        organization_id=organization_id,
        name=cleaned_name,
        normalized_name=normalized_name,
        description=cleaned_description or None,
        is_global=is_global,
    )
    db.add(deck)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _dashboard_response(
            request,
            user=user,
            db=db,
            status_code=400,
            dashboard_error="An active deck with that normalized name already exists in this scope.",
            active_modal="create",
            create_form={"name": cleaned_name, "description": cleaned_description, "is_global": is_global},
        )

    success_message = quote_plus("Deck created")
    return RedirectResponse(url=f"/dashboard?success={success_message}", status_code=303)


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
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    cleaned_name = name.strip()
    cleaned_description = description.strip()
    if not cleaned_name:
        return _dashboard_response(
            request,
            user=user,
            db=db,
            status_code=400,
            dashboard_error="Deck name cannot be empty.",
            active_modal="edit",
            modal_deck=deck,
            edit_form={"name": cleaned_name, "description": cleaned_description},
        )
    normalized_name = normalize_deck_name(cleaned_name)
    if not normalized_name:
        return _dashboard_response(
            request,
            user=user,
            db=db,
            status_code=400,
            dashboard_error="Deck name must include letters or numbers.",
            active_modal="edit",
            modal_deck=deck,
            edit_form={"name": cleaned_name, "description": cleaned_description},
        )

    deck.name = cleaned_name
    deck.normalized_name = normalized_name
    deck.description = cleaned_description or None

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _dashboard_response(
            request,
            user=user,
            db=db,
            status_code=400,
            dashboard_error="Unable to update this deck right now. Please try again.",
            active_modal="edit",
            modal_deck=deck,
            edit_form={"name": cleaned_name, "description": cleaned_description},
        )

    success_message = quote_plus("Deck details updated")
    return RedirectResponse(url=f"/dashboard?success={success_message}", status_code=303)


@router.post("/decks/{deck_id}/delete")
def delete_deck(
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    deck.is_deleted = True
    deck.deleted_at = datetime.now(timezone.utc)
    db.commit()

    success_message = quote_plus("Deck deleted")
    return RedirectResponse(url=f"/dashboard?success={success_message}", status_code=303)


@router.get("/decks/{deck_id}", response_class=HTMLResponse)
def deck_cards(
    request: Request,
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
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
    if not deck or not can_manage_deck(user, deck):
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
    if not deck or not can_manage_deck(user, deck):
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
def review_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    deck_id = request.query_params.get("deck_id")
    deck = None
    if deck_id:
        deck = db.get(Deck, deck_id)
        if not deck or not can_access_deck(user, deck):
            raise HTTPException(status_code=404)

    return templates.TemplateResponse(
        "review/page.html",
        {"request": request, "user": user, "title": "Review | edu selviz", "review_deck": deck},
    )


@router.get("/review/next", response_class=HTMLResponse)
def review_next(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    svc = ReviewService()
    deck_id = request.query_params.get("deck_id")
    deck = None
    if deck_id:
        deck = db.get(Deck, deck_id)
        if not deck or not can_access_deck(user, deck):
            raise HTTPException(status_code=404)
    card = svc.next_due_card(db, user=user, deck_id=deck.id if deck else None)
    if card is None:
        return templates.TemplateResponse("review/empty.html", {"request": request, "user": user, "review_deck": deck})

    return templates.TemplateResponse(
        "review/card.html",
        {"request": request, "user": user, "card": card, "review_deck": deck},
    )


@router.post("/review/rate", response_class=HTMLResponse)
def review_rate(
    request: Request,
    card_id: str = Form(...),
    rating: int = Form(...),
    deck_id: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    card = db.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404)
    deck = db.get(Deck, card.deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=403)

    svc = ReviewService()
    svc.rate(db, card_id=card.id, rating=rating)
    db.commit()

    query = f"?deck_id={deck_id}" if deck_id else ""
    request.scope["query_string"] = query.lstrip("?").encode()
    return review_next(request=request, user=user, db=db)
