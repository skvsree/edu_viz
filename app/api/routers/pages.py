from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import current_user, optional_current_user
from app.core.config import settings
from app.core.db import get_db
from app.models import Card, Deck, Organization, Review, Tag, Test, TestAttempt, TestAttemptAnswer, TestQuestion, User, UserDeckFavorite, deck_tags
from app.models.card_state import CardState
from app.services.access import (
    ROLE_ADMIN,
    ROLE_SYSTEM_ADMIN,
    ROLE_USER,
    can_access_deck,
    can_access_tests,
    can_manage_deck,
    can_manage_decks,
    can_open_test_center,
    can_use_ai_generation,
    deck_has_test_content,
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


def _can_manage_tags(user: User, deck: Deck) -> bool:
    if user.role == ROLE_SYSTEM_ADMIN:
        return True
    if user.role != ROLE_ADMIN:
        return False
    if deck.is_global:
        return False
    return bool(user.organization_id and deck.organization_id == user.organization_id)



def _default_organization(db: Session) -> Organization:
    organization = db.execute(select(Organization).where(Organization.name == "Default Organization")).scalars().first()
    if organization is None:
        organization = Organization(name="Default Organization", is_ai_enabled=False)
        db.add(organization)
        db.flush()
    return organization



def _resolve_deck_tag_organization(db: Session, *, user: User, deck: Deck) -> UUID:
    if deck.organization_id:
        return deck.organization_id

    if user.organization_id:
        deck.organization_id = user.organization_id
        db.flush()
        return user.organization_id

    organization = _default_organization(db)
    user.organization_id = organization.id
    deck.organization_id = organization.id
    db.flush()
    return organization.id



def _deck_has_published_tests(db: Session, deck_id: UUID) -> bool:
    return bool(db.execute(select(Test.id).where(Test.deck_id == deck_id, Test.is_published.is_(True)).limit(1)).scalars().all())


def _user_test_attempt_count(db: Session, *, deck_id: UUID, user_id: UUID) -> int:
    return db.execute(
        select(TestAttempt.id)
        .join(Test, TestAttempt.test_id == Test.id)
        .where(Test.deck_id == deck_id, TestAttempt.user_id == user_id)
    ).scalars().all().__len__()


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
    create_form: dict[str, str | bool] | None = None,
    edit_form: dict[str, str | bool] | None = None,
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
            "create_form": create_form or {"name": "", "is_ai_enabled": False},
            "edit_form": edit_form
            or {
                "name": modal_organization.name if modal_organization else "",
                "is_ai_enabled": modal_organization.is_ai_enabled if modal_organization else False,
            },
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



def _tag_form_context(deck: Deck) -> dict[str, str]:
    return {"tag_names": ", ".join(tag.name for tag in sorted(deck.tags, key=lambda item: item.name.lower()))}



def _deck_content_response(
    request: Request,
    *,
    user: User,
    deck: Deck,
    cards: list[Card],
    title: str,
    template_name: str,
    active_section: str,
    status_code: int = 200,
    import_error: str | None = None,
    import_success: str | None = None,
    update_error: str | None = None,
    update_success: str | None = None,
    has_published_tests: bool = False,
    tag_error: str | None = None,
    tag_success: str | None = None,
    tag_form: dict[str, str] | None = None,
):
    can_edit = can_manage_deck(user, deck)
    can_manage_tags = _can_manage_tags(user, deck)
    flashcards = [card for card in cards if card.card_type == "basic"]
    mcqs = [card for card in cards if card.card_type == "mcq"]
    has_test_content = deck_has_test_content(cards)
    tests_available = can_open_test_center(user, deck, has_test_content=has_test_content, has_published_tests=has_published_tests)
    return templates.TemplateResponse(
        template_name,
        {
            "request": request,
            "user": user,
            "deck": deck,
            "cards": cards,
            "flashcards": flashcards,
            "mcqs": mcqs,
            "flashcard_count": len(flashcards),
            "mcq_count": len(mcqs),
            "can_edit": can_edit,
            "can_manage_tags": can_manage_tags,
            "can_use_ai_generation": can_use_ai_generation(user) and can_edit,
            "tests_available": tests_available,
            "has_test_content": has_test_content,
            "has_published_tests": has_published_tests,
            "active_section": active_section,
            "title": title,
            "import_error": import_error,
            "import_success": import_success,
            "update_error": update_error,
            "update_success": update_success,
            "tag_error": tag_error,
            "tag_success": tag_success,
            "tag_form": tag_form or _tag_form_context(deck),
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
    edit_form: dict[str, str | bool] | None = None,
):
    deck_stats = list_accessible_deck_stats(db, user=user)

    # Compute user's favorite deck IDs
    fav_rows = db.execute(
        select(UserDeckFavorite.deck_id)
        .where(UserDeckFavorite.user_id == user.id)
    ).scalars().all()
    favorite_deck_ids = {str(f) for f in fav_rows}

    # Filter deck_stats to only favorites (for display on dashboard)
    favorite_deck_stats = [item for item in deck_stats if str(item.deck.id) in favorite_deck_ids]

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
            "deck_stats": favorite_deck_stats,
            "favorite_deck_ids": favorite_deck_ids,
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
                "is_global": modal_deck.is_global if modal_deck else False,
            },
            "tag_form": _tag_form_context(modal_deck) if modal_deck else {"tag_names": ""},
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


@router.get("/login/providers", response_class=HTMLResponse)
def login_providers(request: Request, user: User | None = Depends(optional_current_user)):
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=303)

    return templates.TemplateResponse(
        "auth/providers.html",
        {
            "request": request,
            "user": user,
            "title": "Sign in | edu selviz",
            "microsoft_login_url": "/login/microsoft",
            "google_login_url": "/login/google",
        },
    )


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return _dashboard_response(request, user=user, db=db)


BROWSE_PAGE_SIZE = 10


@router.get("/decks/browse", response_class=HTMLResponse)
def browse_decks(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.services.access import accessible_deck_clause

    q = request.query_params.get("q", "").strip()
    page = request.query_params.get("page", "1")
    try:
        page = max(1, int(page))
    except ValueError:
        page = 1

    # Base query — accessible decks
    base_q = (
        select(Deck)
        .options(selectinload(Deck.organization), selectinload(Deck.tags))
        .where(accessible_deck_clause(user), Deck.is_deleted.is_(False))
    )

    # Search filter
    if q:
        base_q = base_q.where(Deck.normalized_name.ilike(f"%{q}%"))

    # Count total
    count_q = select(func.count()).select_from(base_q.subquery())
    total = db.execute(count_q).scalar_one()

    # Paginated
    offset = (page - 1) * BROWSE_PAGE_SIZE
    decks = (
        db.execute(
            base_q.order_by(Deck.is_global.desc(), Deck.created_at.desc())
            .limit(BROWSE_PAGE_SIZE)
            .offset(offset)
        )
        .scalars()
        .all()
    )

    # Favorite IDs for current user
    fav_rows = db.execute(
        select(UserDeckFavorite.deck_id).where(UserDeckFavorite.user_id == user.id)
    ).scalars().all()
    favorite_deck_ids = {str(f) for f in fav_rows}

    total_pages = (total + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE if total > 0 else 1

    return templates.TemplateResponse(
        "decks/browse.html",
        {
            "request": request,
            "user": user,
            "decks": decks,
            "favorite_deck_ids": favorite_deck_ids,
            "q": q,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "page_size": BROWSE_PAGE_SIZE,
            "title": "Browse Decks | edu selviz",
        },
    )


@router.get("/analytics", response_class=HTMLResponse)
def analytics_home(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if user.role not in {ROLE_ADMIN, ROLE_SYSTEM_ADMIN}:
        raise HTTPException(status_code=403, detail="You do not have access to analytics")

    selected_user_id = request.query_params.get("user_id", "")
    selected_deck_id = request.query_params.get("deck_id", "")

    # Compute deck completion for accessible decks
    completion_stats: list[dict[str, object]] = []
    deck_stats = list_accessible_deck_stats(db, user=user)

    for stats in deck_stats:
        deck = stats.deck
        if selected_deck_id and str(deck.id) != selected_deck_id:
            continue

        total_cards = len(deck.cards)
        completed_reviews = stats.cards_reviewed
        percent_complete = 0
        if total_cards > 0:
            percent_complete = round(min(completed_reviews / total_cards * 100, 100))

        tests_taken_query = (
            select(func.count(TestAttempt.id))
            .join(Test, TestAttempt.test_id == Test.id)
            .where(Test.deck_id == deck.id)
        )
        if selected_user_id:
            tests_taken_query = tests_taken_query.where(TestAttempt.user_id == selected_user_id)
        tests_taken = db.execute(tests_taken_query).scalar_one()

        completion_stats.append(
            {
                "deck": deck,
                "percent_complete": percent_complete,
                "cards_due": stats.cards_due,
                "cards_reviewed": completed_reviews,
                "tests_taken": tests_taken,
            }
        )

    # Compute user analytics summaries
    user_summaries = []
    if user.role == ROLE_SYSTEM_ADMIN:
        visible_users = db.execute(select(User).order_by(User.created_at.desc())).scalars().all()
    else:
        visible_users = (
            db.execute(select(User).where(User.organization_id == user.organization_id).order_by(User.created_at.desc()))
            .scalars()
            .all()
        )

    if selected_user_id:
        visible_users = [target_user for target_user in visible_users if str(target_user.id) == selected_user_id]

    for target_user in visible_users:
        summary = {
            "user": target_user,
            "cards_reviewed": 0,
            "tests_taken": 0,
        }

        cards_reviewed_query = (
            select(func.count(Review.id))
            .join(Card, Card.id == Review.card_id)
            .where(Card.deck.has(Deck.organization_id == target_user.organization_id) | Deck.is_global.is_(True))
            .where(Review.card.has(Card.deck.has()))
        )
        if selected_deck_id:
            cards_reviewed_query = cards_reviewed_query.where(Card.deck_id == selected_deck_id)
        cards_reviewed = db.execute(cards_reviewed_query).scalar() or 0

        tests_taken_query = select(func.count(TestAttempt.id)).where(TestAttempt.user_id == target_user.id)
        if selected_deck_id:
            tests_taken_query = tests_taken_query.join(Test, TestAttempt.test_id == Test.id).where(Test.deck_id == selected_deck_id)
        tests_taken = db.execute(tests_taken_query).scalar() or 0

        summary["cards_reviewed"] = cards_reviewed
        summary["tests_taken"] = tests_taken
        user_summaries.append(summary)

    return templates.TemplateResponse(
        "analytics/index.html",
        {
            "request": request,
            "user": user,
            "deck_stats": completion_stats,
            "user_summaries": user_summaries,
            "visible_users": visible_users,
            "available_decks": deck_stats,
            "selected_user_id": selected_user_id,
            "selected_deck_id": selected_deck_id,
            "title": "Analytics | edu selviz",
        },
    )


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
    is_ai_enabled: bool = Form(default=False),
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
            create_form={"name": cleaned_name, "is_ai_enabled": is_ai_enabled},
        )

    organization = Organization(name=cleaned_name, is_ai_enabled=is_ai_enabled)
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
            create_form={"name": cleaned_name, "is_ai_enabled": is_ai_enabled},
        )

    return RedirectResponse(url="/settings/organizations?success=Organization+created", status_code=303)


@router.post("/settings/organizations/{organization_id}/update")
def update_organization(
    request: Request,
    organization_id: str,
    name: str = Form(...),
    is_ai_enabled: bool = Form(default=False),
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
            edit_form={"name": cleaned_name, "is_ai_enabled": is_ai_enabled},
        )

    organization.name = cleaned_name
    organization.is_ai_enabled = is_ai_enabled
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
            edit_form={"name": cleaned_name, "is_ai_enabled": is_ai_enabled},
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
    is_test_enabled: bool = Form(default=False),
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

    target.is_test_enabled = is_test_enabled

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
    is_global: bool = Form(default=False),
    next_url: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    cards = db.execute(select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())).scalars().all()
    cleaned_name = name.strip()
    cleaned_description = description.strip()

    redirect_target = next_url.strip() if isinstance(next_url, str) else ""
    if not redirect_target.startswith("/"):
        redirect_target = "/dashboard"

    def _append_message_param(url: str, key: str, message: str) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query[key] = message
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _deck_update_error_response(message: str):
        if redirect_target.startswith(f"/decks/{deck.id}"):
            flashcards = [card for card in cards if card.card_type == "basic"]
            mcqs = [card for card in cards if card.card_type == "mcq"]
            can_edit = can_manage_deck(user, deck)
            has_test_content = deck_has_test_content(cards)
            has_published_tests = _deck_has_published_tests(db, deck.id)
            tests_available = can_open_test_center(user, deck, has_test_content=has_test_content, has_published_tests=has_published_tests)
            test_count = _user_test_attempt_count(db, deck_id=deck.id, user_id=user.id) if tests_available else 0
            return templates.TemplateResponse(
                "decks/overview.html",
                {
                    "request": request,
                    "user": user,
                    "deck": deck,
                    "flashcard_count": len(flashcards),
                    "mcq_count": len(mcqs),
                    "can_edit": can_edit,
                    "can_use_ai_generation": can_use_ai_generation(user) and can_edit,
                    "tests_available": tests_available,
                    "test_count": test_count,
                    "update_error": message,
                    "title": f"{deck.name} | edu selviz",
                },
                status_code=400,
            )
        return _dashboard_response(
            request,
            user=user,
            db=db,
            status_code=400,
            dashboard_error=message,
            active_modal="edit",
            modal_deck=deck,
            edit_form={"name": cleaned_name, "description": cleaned_description, "is_global": is_global},
        )

    if not cleaned_name:
        return _deck_update_error_response("Deck name cannot be empty.")
    normalized_name = normalize_deck_name(cleaned_name)
    if not normalized_name:
        return _deck_update_error_response("Deck name must include letters or numbers.")

    if is_global and user.role != ROLE_SYSTEM_ADMIN:
        raise HTTPException(status_code=403, detail="Only system admins can make a deck global.")

    organization_id = None if is_global else user.organization_id
    if not is_global and organization_id is None:
        return _deck_update_error_response("Assign this admin to an organization before saving organization decks.")

    deck.name = cleaned_name
    deck.normalized_name = normalized_name
    deck.description = cleaned_description or None
    deck.is_global = is_global
    deck.organization_id = organization_id

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _deck_update_error_response("Unable to update: a deck with this normalized name already exists in the same scope.")

    if redirect_target.startswith(f"/decks/{deck.id}"):
        return RedirectResponse(url=_append_message_param(redirect_target, "update_success", "Deck details updated"), status_code=303)
    return RedirectResponse(url=_append_message_param(redirect_target, "success", "Deck details updated"), status_code=303)


@router.post("/decks/{deck_id}/tags")
def update_deck_tags(
    deck_id: str,
    tag_names: str = Form(default=""),
    next_url: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)
    if not _can_manage_tags(user, deck):
        raise HTTPException(status_code=403, detail="You do not have permission to manage tags for this deck.")

    redirect_target = next_url.strip() if isinstance(next_url, str) else ""
    if not redirect_target.startswith("/"):
        redirect_target = f"/decks/{deck.id}"

    raw_names = [item.strip() for item in tag_names.split(",")]
    cleaned_names: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_names:
        if not raw_name:
            continue
        normalized = normalize_deck_name(raw_name)
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned_names.append(raw_name)

    organization_id = _resolve_deck_tag_organization(db, user=user, deck=deck)

    existing_tags = {
        tag.normalized_name: tag
        for tag in db.execute(select(Tag).where(Tag.organization_id == organization_id)).scalars().all()
    }

    updated_tags: list[Tag] = []
    for cleaned_name in cleaned_names:
        normalized = normalize_deck_name(cleaned_name)
        tag = existing_tags.get(normalized)
        if tag is None:
            tag = Tag(
                organization_id=organization_id,
                name=cleaned_name,
                normalized_name=normalized,
            )
            db.add(tag)
            db.flush()
            existing_tags[normalized] = tag
        updated_tags.append(tag)

    deck.tags = updated_tags
    db.commit()

    message = "Tags updated" if updated_tags else "Tags cleared"
    return RedirectResponse(url=f"{redirect_target}?tag_success={quote_plus(message)}", status_code=303)


@router.post("/decks/{deck_id}/tags/{tag_id}/delete")
def remove_deck_tag(
    deck_id: str,
    tag_id: str,
    next_url: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)
    if not _can_manage_tags(user, deck):
        raise HTTPException(status_code=403, detail="You do not have permission to manage tags for this deck.")

    tag = db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404)

    if tag in deck.tags:
        deck.tags.remove(tag)
        db.commit()

    redirect_target = next_url.strip() if isinstance(next_url, str) else ""
    if not redirect_target.startswith("/"):
        redirect_target = f"/decks/{deck.id}"
    return RedirectResponse(url=f"{redirect_target}?tag_success={quote_plus('Tag removed')}", status_code=303)


@router.post("/decks/{deck_id}/delete")
def delete_deck(
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    # Hard-delete deck and dependent content (NEET import pipeline behavior)
    card_ids = db.execute(select(Card.id).where(Card.deck_id == deck.id)).scalars().all()
    card_id_list = list(card_ids)

    # Delete card/test dependencies first
    if card_id_list:
        affected_test_ids = db.execute(
            select(TestQuestion.test_id).where(TestQuestion.card_id.in_(card_id_list)).distinct()
        ).scalars().all()

        if affected_test_ids:
            db.execute(
                delete(TestAttemptAnswer).where(
                    TestAttemptAnswer.attempt_id.in_(
                        select(TestAttempt.id).where(TestAttempt.test_id.in_(affected_test_ids))
                    )
                )
            )
            db.execute(delete(TestAttempt).where(TestAttempt.test_id.in_(affected_test_ids)))
            db.execute(delete(TestQuestion).where(TestQuestion.test_id.in_(affected_test_ids)))
            db.execute(delete(Test).where(Test.id.in_(affected_test_ids)))

        db.execute(delete(CardState).where(CardState.card_id.in_(card_id_list)))
        db.execute(delete(Review).where(Review.card_id.in_(card_id_list)))
        db.execute(delete(Card).where(Card.id.in_(card_id_list)))

    # Delete deck_tags before deleting deck (foreign key constraint)
    db.execute(delete(deck_tags).where(deck_tags.c.deck_id == deck.id))
    db.execute(delete(Deck).where(Deck.id == deck.id))
    db.commit()

    success_message = quote_plus("Deck deleted")
    return RedirectResponse(url=f"/dashboard?success={success_message}", status_code=303)


@router.get("/decks/{deck_id}", response_class=HTMLResponse)
def deck_overview(
    request: Request,
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)

    cards = db.execute(select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())).scalars().all()
    has_published_tests = _deck_has_published_tests(db, deck.id)
    flashcards = [card for card in cards if card.card_type == "basic"]
    mcqs = [card for card in cards if card.card_type == "mcq"]
    can_edit = can_manage_deck(user, deck)
    can_manage_tags = _can_manage_tags(user, deck)
    has_test_content = deck_has_test_content(cards)
    tests_available = can_open_test_center(user, deck, has_test_content=has_test_content, has_published_tests=has_published_tests)
    test_count = _user_test_attempt_count(db, deck_id=deck.id, user_id=user.id) if tests_available else 0

    is_favorited = db.execute(
        select(UserDeckFavorite)
        .where(UserDeckFavorite.user_id == user.id, UserDeckFavorite.deck_id == deck.id)
    ).scalars().first() is not None

    return templates.TemplateResponse(
        "decks/overview.html",
        {
            "request": request,
            "user": user,
            "deck": deck,
            "is_favorited": is_favorited,
            "flashcard_count": len(flashcards),
            "mcq_count": len(mcqs),
            "can_edit": can_edit,
            "can_manage_tags": can_manage_tags,
            "can_use_ai_generation": can_use_ai_generation(user) and can_edit,
            "tests_available": tests_available,
            "test_count": test_count,
            "import_success": request.query_params.get("import_success"),
            "update_success": request.query_params.get("update_success"),
            "tag_error": request.query_params.get("tag_error"),
            "tag_success": request.query_params.get("tag_success"),
            "tag_form": _tag_form_context(deck),
            "title": f"{deck.name} | edu selviz",
        },
    )


@router.get("/decks/{deck_id}/flashcards", response_class=HTMLResponse)
def deck_flashcards(
    request: Request,
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)

    cards = db.execute(select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())).scalars().all()
    has_published_tests = _deck_has_published_tests(db, deck.id)
    return _deck_content_response(
        request,
        user=user,
        deck=deck,
        cards=cards,
        title=f"Flashcards | {deck.name}",
        template_name="cards/flashcards.html",
        active_section="flashcards",
        import_error=request.query_params.get("import_error"),
        import_success=request.query_params.get("import_success"),
        update_error=request.query_params.get("update_error"),
        update_success=request.query_params.get("update_success"),
        tag_error=request.query_params.get("tag_error"),
        tag_success=request.query_params.get("tag_success"),
        tag_form=_tag_form_context(deck),
        has_published_tests=has_published_tests,
    )


@router.get("/decks/{deck_id}/mcqs", response_class=HTMLResponse)
def deck_mcqs(
    request: Request,
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)

    cards = db.execute(select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())).scalars().all()
    has_published_tests = _deck_has_published_tests(db, deck.id)
    return _deck_content_response(
        request,
        user=user,
        deck=deck,
        cards=cards,
        title=f"MCQs | {deck.name}",
        template_name="cards/mcqs.html",
        active_section="mcqs",
        import_error=request.query_params.get("import_error"),
        import_success=request.query_params.get("import_success"),
        update_error=request.query_params.get("update_error"),
        update_success=request.query_params.get("update_success"),
        tag_error=request.query_params.get("tag_error"),
        tag_success=request.query_params.get("tag_success"),
        tag_form=_tag_form_context(deck),
        has_published_tests=has_published_tests,
    )


@router.get("/decks/{deck_id}/ai-upload", response_class=HTMLResponse)
def deck_ai_upload(
    request: Request,
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)
    if not can_use_ai_generation(user) or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    cards = db.execute(select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())).scalars().all()
    has_published_tests = _deck_has_published_tests(db, deck.id)
    return _deck_content_response(
        request,
        user=user,
        deck=deck,
        cards=cards,
        title=f"AI Upload | {deck.name}",
        template_name="cards/ai_upload.html",
        active_section="ai-upload",
        import_error=request.query_params.get("import_error"),
        import_success=request.query_params.get("import_success"),
        update_error=request.query_params.get("update_error"),
        update_success=request.query_params.get("update_success"),
        tag_error=request.query_params.get("tag_error"),
        tag_success=request.query_params.get("tag_success"),
        tag_form=_tag_form_context(deck),
        has_published_tests=has_published_tests,
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

    return RedirectResponse(url=f"/decks/{deck.id}/flashcards?import_success={quote_plus('Flashcard added')}", status_code=303)


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
        return _deck_content_response(
            request,
            user=user,
            deck=deck,
            cards=cards,
            title=f"{deck.name} | edu selviz",
            template_name="cards/flashcards.html",
            active_section="flashcards",
            status_code=400,
            import_error="Please upload a .csv file.",
        )

    try:
        imported_rows = parse_cards_csv(csv_file.file)
    except CsvImportError as exc:
        return _deck_content_response(
            request,
            user=user,
            deck=deck,
            cards=cards,
            title=f"{deck.name} | edu selviz",
            template_name="cards/flashcards.html",
            active_section="flashcards",
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
        url=f"/decks/{deck.id}/flashcards?import_success={success_message}",
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

    review_limit = None
    raw_limit = request.query_params.get("count")
    if raw_limit:
        try:
            review_limit = max(1, min(int(raw_limit), 100))
        except ValueError:
            review_limit = None

    return templates.TemplateResponse(
        "review/page.html",
        {
            "request": request,
            "user": user,
            "title": "Review | edu selviz",
            "review_deck": deck,
            "review_limit": review_limit,
            "review_options": [10, 25, 50, 100, 200],
        },
    )


def _review_next_inner(request, user, db, deck_id=None, remaining=None):
    svc = ReviewService()
    deck = None
    if deck_id:
        deck = db.get(Deck, deck_id)
        if not deck or not can_access_deck(user, deck):
            raise HTTPException(status_code=404)

    if remaining == 0:
        return templates.TemplateResponse(
            "review/empty.html",
            {"request": request, "user": user, "review_deck": deck, "review_complete": True},
        )

    card = svc.next_due_card(db, user=user, deck_id=deck.id if deck else None)
    if card is None:
        return templates.TemplateResponse("review/empty.html", {"request": request, "user": user, "review_deck": deck, "review_complete": False})

    return templates.TemplateResponse(
        "review/card.html",
        {"request": request, "user": user, "card": card, "review_deck": deck, "remaining": remaining},
    )


@router.get("/review/next", response_class=HTMLResponse)
def review_next(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck_id = request.query_params.get("deck_id")

    remaining = None
    raw_remaining = request.query_params.get("remaining")
    if raw_remaining:
        try:
            remaining = max(0, int(raw_remaining))
        except ValueError:
            remaining = None

    return _review_next_inner(request, user, db, deck_id=deck_id, remaining=remaining)


@router.post("/review/rate", response_class=HTMLResponse)
def review_rate(
    request: Request,
    card_id: str = Form(...),
    rating: int = Form(...),
    deck_id: str = Form(default=""),
    remaining: int | None = Form(default=None),
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

    next_remaining = None if remaining is None else max(remaining - 1, 0)
    effective_deck_id = deck_id or (str(deck.id) if deck else None)
    return _review_next_inner(request, user, db, deck_id=effective_deck_id, remaining=next_remaining)
