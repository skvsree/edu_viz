from __future__ import annotations

import re
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from datetime import datetime
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from uuid import UUID
from types import SimpleNamespace

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import current_user, optional_current_user
from app.core.config import settings
from app.core.db import get_db
from app.models import (
    BulkAIUpload,
    BulkAIUploadFile,
    BulkAIUploadFileStatus,
    Card,
    Deck,
    Job,
    Organization,
    Review,
    Tag,
    Test,
    TestAttempt,
    User,
    UserDeckFavorite,
)
from app.models.analytics import AnalyticsEvent
from app.models.card_state import CardState
from app.services.access import (
    ROLE_ADMIN,
    ROLE_SYSTEM_ADMIN,
    ROLE_USER,
    TAB_GLOBAL,
    browse_filter_clause,
    can_access_deck,
    can_manage_deck,
    can_manage_decks,
    can_open_test_center,
    can_use_ai_generation,
    can_write_tab,
    deck_has_test_content,
    get_browse_tabs,
    is_org_admin,
    is_system_admin,
    normalize_deck_name,
)
from app.services.ai_auth import (
    get_scope_provider,
    has_scope_credential,
    is_env_ai_available,
    save_ai_credential,
)
from app.services.csv_import import CsvImportError, parse_cards_csv
from app.services.dashboard import list_accessible_deck_stats
from app.services.review_service import ReviewService
from app.services.storage import deck_media_prefix, get_storage


def _normalize_review_html(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("\\\\", "\\")
    text = text.replace("<hl>", '<span class="hl">').replace("</hl>", "</span>")
    text = text.replace("<br>", "<br/>")
    return text


def _render_mixed_card_content(text: str | None) -> tuple[str, str]:
    if not text:
        return "", ""
    parts = [p for p in text.split("\x1f") if p]
    if not parts:
        return "", ""
    front = _normalize_review_html(parts[0])
    back = _normalize_review_html(parts[1]) if len(parts) > 1 else ""
    if back and "<br" not in back:
        back = back.replace("\n", "<br/>")
    return front, back


router = APIRouter(tags=["pages"])

APP_DIR = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
COMPONENTS_DIR = APP_DIR / "components"
COMPONENT_TEMPLATES_DIR = COMPONENTS_DIR / "multiselect" / "templates"
templates = Jinja2Templates(
    directory=[str(TEMPLATES_DIR), str(COMPONENT_TEMPLATES_DIR)]
)


def _sanitize_html(text: str | None) -> str:
    """Allow only safe HTML tags for card content (no scripting)."""
    if not text:
        return ""
    # For MVP: strip all HTML, keep only text
    # This prevents XSS while supporting basic content
    import re

    # Remove script, style, and event handlers
    text = re.sub(
        r"<script[^>]*>.*?</script>", "", text, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"on\w+\s*=", "", text, flags=re.IGNORECASE)
    text = re.sub(r"javascript:", "", text, flags=re.IGNORECASE)
    return text


templates.env.filters["sanitize"] = _sanitize_html


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


def _html_no_store(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _can_manage_tags(user: User, deck: Deck) -> bool:
    if user.role == ROLE_SYSTEM_ADMIN:
        return True
    if user.role != ROLE_ADMIN:
        return False
    if deck.is_global:
        return False
    return bool(user.organization_id and deck.organization_id == user.organization_id)


def _default_organization(db: Session) -> Organization:
    organization = (
        db.execute(
            select(Organization).where(Organization.name == "Default Organization")
        )
        .scalars()
        .first()
    )
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
    return bool(
        db.execute(
            select(Test.id)
            .where(Test.deck_id == deck_id, Test.is_published.is_(True))
            .limit(1)
        )
        .scalars()
        .all()
    )


def _user_test_attempt_count(db: Session, *, deck_id: UUID, user_id: UUID) -> int:
    return (
        db.execute(
            select(TestAttempt.id)
            .join(Test, TestAttempt.test_id == Test.id)
            .where(Test.deck_id == deck_id, TestAttempt.user_id == user_id)
        )
        .scalars()
        .all()
        .__len__()
    )


def _require_settings_access(user: User) -> None:
    if user.role not in {ROLE_ADMIN, ROLE_SYSTEM_ADMIN}:
        raise HTTPException(
            status_code=403, detail="You do not have access to settings."
        )


def _require_system_admin(user: User) -> None:
    if user.role != ROLE_SYSTEM_ADMIN:
        raise HTTPException(
            status_code=403, detail="Only system admins can access this page."
        )


def _visible_users_stmt(user: User, org_id: str | None = None):
    stmt = (
        select(User)
        .options(selectinload(User.organization))
        .order_by(User.created_at.desc())
    )
    if user.role == ROLE_SYSTEM_ADMIN:
        if org_id:
            stmt = stmt.where(User.organization_id == UUID(org_id))
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

    organization_count = (
        db.execute(select(Organization)).scalars().all()
        if user.role == ROLE_SYSTEM_ADMIN
        else []
    )
    visible_users = db.execute(_visible_users_stmt(user)).scalars().all()
    job_count = (
        db.execute(select(func.count(Job.id))).scalar_one()
        if user.role == ROLE_SYSTEM_ADMIN
        else 0
    )

    return templates.TemplateResponse(
        "settings/index.html",
        {
            "request": request,
            "user": user,
            "organization_count": (
                len(organization_count)
                if user.role == ROLE_SYSTEM_ADMIN
                else (1 if user.organization_id else 0)
            ),
            "visible_user_count": len(visible_users),
            "job_count": job_count,
            "settings_error": (
                settings_error
                if settings_error is not None
                else request.query_params.get("error")
            ),
            "settings_success": (
                settings_success
                if settings_success is not None
                else request.query_params.get("success")
            ),
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
    organizations = (
        db.execute(select(Organization).order_by(Organization.name.asc()))
        .scalars()
        .all()
    )
    requested_org_id = request.query_params.get("edit_org")
    if modal_organization is None and requested_org_id:
        modal_organization = db.get(Organization, requested_org_id)
        if modal_organization and active_modal is None:
            active_modal = "edit"

    org_user_counts = {str(org.id): len(org.users or []) for org in organizations}

    # AI credential state per org
    org_ai_has_cred = {
        str(org.id): has_scope_credential(db, "organization", org.id)
        for org in organizations
    }
    org_ai_provider = {
        str(org.id): get_scope_provider(db, "organization", org.id)
        for org in organizations
    }

    # For modal org edit form, carry override state
    modal_has_cred = (
        has_scope_credential(db, "organization", modal_organization.id)
        if modal_organization
        else False
    )
    modal_provider = (
        get_scope_provider(db, "organization", modal_organization.id)
        if modal_organization
        else "openai"
    )

    env_ai_available = is_env_ai_available()

    return templates.TemplateResponse(
        "settings/organizations.html",
        {
            "request": request,
            "user": user,
            "organizations": organizations,
            "org_user_counts": org_user_counts,
            "org_ai_has_cred": org_ai_has_cred,
            "org_ai_provider": org_ai_provider,
            "organization_error": (
                organization_error
                if organization_error is not None
                else request.query_params.get("error")
            ),
            "organization_success": (
                organization_success
                if organization_success is not None
                else request.query_params.get("success")
            ),
            "active_modal": active_modal or request.query_params.get("modal"),
            "modal_organization": modal_organization,
            "modal_has_cred": modal_has_cred,
            "modal_provider": modal_provider,
            "env_ai_available": env_ai_available,
            "create_form": create_form or {"name": "", "is_ai_enabled": False},
            "edit_form": edit_form
            or {
                "name": modal_organization.name if modal_organization else "",
                "is_ai_enabled": (
                    modal_organization.is_ai_enabled if modal_organization else False
                ),
            },
            "test_global_limit": settings.test_daily_limit,
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
    org_id: str | None = None,
):
    _require_settings_access(user)
    users = db.execute(_visible_users_stmt(user, org_id)).scalars().all()
    organizations = []
    if user.role == ROLE_SYSTEM_ADMIN:
        organizations = (
            db.execute(select(Organization).order_by(Organization.name.asc()))
            .scalars()
            .all()
        )

    filtered_org_name = None
    if org_id:
        org = db.get(Organization, org_id)
        if org:
            filtered_org_name = org.name

    editable_roles = {str(item.id): _allowed_role_options(user, item) for item in users}

    # AI credential state per user
    user_org_ai_enabled = {}
    for u in users:
        if not u.organization_id:
            user_org_ai_enabled[str(u.id)] = False
            continue
        org = getattr(u, "organization", None) or db.get(
            Organization, u.organization_id
        )
        user_org_ai_enabled[str(u.id)] = bool(
            org and getattr(org, "is_ai_enabled", False)
        )
    user_ai_has_cred = {
        str(u.id): has_scope_credential(db, "user", u.id) for u in users
    }
    user_ai_provider = {str(u.id): get_scope_provider(db, "user", u.id) for u in users}

    return templates.TemplateResponse(
        "settings/users.html",
        {
            "request": request,
            "user": user,
            "users": users,
            "organizations": organizations,
            "editable_roles": editable_roles,
            "user_org_ai_enabled": user_org_ai_enabled,
            "user_ai_has_cred": user_ai_has_cred,
            "user_ai_provider": user_ai_provider,
            "filtered_org_name": filtered_org_name,
            "user_error": (
                user_error
                if user_error is not None
                else request.query_params.get("error")
            ),
            "user_success": (
                user_success
                if user_success is not None
                else request.query_params.get("success")
            ),
            "title": "Users | edu selviz",
        },
        status_code=status_code,
    )


def _jobs_response(
    request: Request,
    *,
    user: User,
    db: Session,
    status_code: int = 200,
):
    _require_system_admin(user)

    # Get recent jobs with bulk uploads
    jobs = (
        db.execute(select(Job).order_by(Job.created_at.desc()).limit(50))
        .scalars()
        .all()
    )

    # Get associated bulk uploads
    bulk_ids = {j.reference_id for j in jobs if j.reference_id}
    bulks = {}
    if bulk_ids:
        bulk_results = (
            db.execute(select(BulkAIUpload).where(BulkAIUpload.id.in_(bulk_ids)))
            .scalars()
            .all()
        )
        bulks = {b.id: b for b in bulk_results}

    deck_ids = {b.deck_id for b in bulks.values() if getattr(b, "deck_id", None)}
    bulk_output_decks: dict = {}
    bulk_failed_files: dict = {}
    bulk_file_rows: dict = {}
    bulk_file_counts: dict = {}
    bulk_has_active_retry: set = set()
    bulk_jobs_by_file: dict = {}
    if bulk_ids:
        file_rows_result = db.execute(
            select(BulkAIUploadFile)
            .where(BulkAIUploadFile.bulk_upload_id.in_(bulk_ids))
            .order_by(BulkAIUploadFile.created_at.asc())
        )
        if hasattr(file_rows_result, "scalars"):
            file_rows = file_rows_result.scalars().all()
        else:
            file_rows = file_rows_result.all()
        if file_rows and isinstance(file_rows[0], tuple):
            file_rows = []
        for file_row in file_rows:
            bulk_upload_id = file_row.bulk_upload_id
            bulk_file_rows.setdefault(bulk_upload_id, []).append(file_row)
            if file_row.created_deck_id:
                deck_ids.add(file_row.created_deck_id)
            if file_row.status == BulkAIUploadFileStatus.FAILED.value:
                bulk_failed_files.setdefault(bulk_upload_id, []).append(file_row)
            if file_row.status in {
                BulkAIUploadFileStatus.PENDING.value,
                BulkAIUploadFileStatus.PROCESSING.value,
            }:
                bulk_has_active_retry.add(bulk_upload_id)

        for bulk_id, file_list in bulk_file_rows.items():
            counts = {"completed": 0, "processing": 0, "failed": 0, "pending": 0}
            for file_row in file_list:
                status = (file_row.status or "").lower()
                if status in counts:
                    counts[status] += 1
                else:
                    counts["pending"] += 1
            bulk_file_counts[bulk_id] = counts

            if bulk_failed_files.get(bulk_id):
                continue
            processing_rows = [
                file_row for file_row in file_list
                if file_row.status == BulkAIUploadFileStatus.PROCESSING.value
            ]
            if processing_rows:
                bulk_failed_files[bulk_id] = processing_rows

    decks = {}
    if deck_ids:
        deck_results = (
            db.execute(select(Deck).where(Deck.id.in_(deck_ids))).scalars().all()
        )
        decks = {d.id: d for d in deck_results}

    job_rank = {"running": 4, "pending": 3, "failed": 2, "completed": 1}
    for bulk_id, file_list in bulk_file_rows.items():
        file_groups: dict = {}
        for file_row in file_list:
            file_groups.setdefault(file_row.original_filename, []).append(file_row)

        grouped_items = []
        for filename, grouped_rows in file_groups.items():
            latest_row = max(
                grouped_rows,
                key=lambda item: (
                    item.created_at or datetime.min,
                    item.started_at or datetime.min,
                    item.completed_at or datetime.min,
                ),
            )
            grouped_rows_sorted = sorted(
                grouped_rows,
                key=lambda item: (
                    job_rank.get((item.status or "").lower(), 0),
                    item.created_at or datetime.min,
                    item.started_at or datetime.min,
                    item.completed_at or datetime.min,
                ),
                reverse=True,
            )
            grouped_item = {
                "filename": filename,
                "latest_file": latest_row,
                "jobs": grouped_rows_sorted,
            }
            grouped_items.append(grouped_item)

        bulk_jobs_by_file[bulk_id] = sorted(
            grouped_items,
            key=lambda item: (
                job_rank.get((item["latest_file"].status or "").lower(), 0),
                item["latest_file"].created_at or datetime.min,
                item["latest_file"].started_at or datetime.min,
                item["latest_file"].completed_at or datetime.min,
            ),
            reverse=True,
        )
        bulk_output_decks[bulk_id] = bulk_jobs_by_file[bulk_id]

    return templates.TemplateResponse(
        "settings/jobs.html",
        {
            "request": request,
            "user": user,
            "jobs": jobs,
            "bulks": bulks,
            "decks": decks,
            "bulk_output_decks": bulk_output_decks,
            "bulk_failed_files": bulk_failed_files,
            "bulk_file_counts": bulk_file_counts,
            "bulk_has_active_retry": bulk_has_active_retry,
            "bulk_jobs_by_file": bulk_jobs_by_file,
            "title": "Jobs | edu selviz",
        },
        status_code=status_code,
    )


@router.get("/settings/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return _jobs_response(request, user=user, db=db)


def _tag_form_context(deck: Deck) -> dict[str, str]:
    tags = getattr(deck, "tags", []) or []
    return {
        "tag_names": ", ".join(
            tag.name for tag in sorted(tags, key=lambda item: item.name.lower())
        )
    }


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
    retry_file=None,
    retry_bulk=None,
    retry_filename: str | None = None,
):
    can_edit = can_manage_deck(user, deck)
    can_manage_tags = _can_manage_tags(user, deck)
    flashcards = [card for card in cards if card.card_type == "basic"]
    mcqs = [card for card in cards if card.card_type == "mcq"]
    has_test_content = deck_has_test_content(cards)
    tests_available = can_open_test_center(
        user,
        deck,
        has_test_content=has_test_content,
        has_published_tests=has_published_tests,
    )
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
            "can_use_ai_generation": (can_use_ai_generation(user) and can_edit),
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
            "retry_file": retry_file,
            "retry_bulk": retry_bulk,
            "retry_filename": retry_filename,
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
    folder_id: UUID | None = None,
    folder_tree: list[dict] | None = None,
    folder_error: str | None = None,
    folder_success: str | None = None,
):
    from app.models import Folder

    deck_stats = list_accessible_deck_stats(db, user=user)

    # Compute user's favorite deck IDs
    fav_rows = (
        db.execute(
            select(UserDeckFavorite.deck_id).where(UserDeckFavorite.user_id == user.id)
        )
        .scalars()
        .all()
    )
    favorite_deck_ids = {str(f) for f in fav_rows}

    def _folder_path_label(folder_id: UUID | None) -> str:
        if not folder_id:
            return "Root"
        parts: list[str] = []
        current_id: UUID | None = folder_id
        while current_id:
            node = db.get(Folder, current_id)
            if not node:
                break
            parts.insert(0, node.name)
            current_id = node.parent_id
        return " / ".join(parts) if parts else "Root"

    # If favorites exist, show those on dashboard. Otherwise fall back to all accessible decks
    displayed_stats = deck_stats
    if favorite_deck_ids:
        displayed_stats = [
            item for item in deck_stats if str(item.deck.id) in favorite_deck_ids
        ]

    favorite_deck_stats = [
        {
            "deck": item.deck,
            "folder_path": _folder_path_label(getattr(item.deck, "folder_id", None)),
        }
        for item in displayed_stats
    ]

    # Folder context
    folders = []
    breadcrumb = []
    current_folder = None
    folder_decks = []

    if folder_id:
        # Load current folder and its subfolders
        current_folder = db.get(Folder, folder_id)
        if current_folder and current_folder.user_id == user.id:
            # Breadcrumb path
            path_ids = []
            node_id: UUID | None = folder_id
            while node_id:
                node = db.get(Folder, node_id)
                if not node:
                    break
                path_ids.insert(0, node)
                node_id = node.parent_id

            breadcrumb = [(str(f.id), f.name) for f in path_ids]

            # Subfolders of current folder
            folder_rows = (
                db.execute(
                    select(Folder)
                    .where(Folder.parent_id == folder_id)
                    .order_by(Folder.name.asc())
                )
                .scalars()
                .all()
            )

            from app.api.routers.folders import (
                _count_decks_in_folder,
                _count_subfolders,
            )

            for f in folder_rows:
                folders.append(
                    {
                        "id": str(f.id),
                        "name": f.name,
                        "deck_count": _count_decks_in_folder(db, f.id),
                        "subfolder_count": _count_subfolders(db, f.id),
                    }
                )

            # Decks in this folder
            folder_decks = (
                db.execute(
                    select(Deck)
                    .where(Deck.folder_id == folder_id, Deck.is_deleted.is_(False))
                    .order_by(Deck.name.asc())
                )
                .scalars()
                .all()
            )
    else:
        # Root level — load root folders
        folder_rows = (
            db.execute(
                select(Folder)
                .where(Folder.parent_id.is_(None), Folder.user_id == user.id)
                .order_by(Folder.name.asc())
            )
            .scalars()
            .all()
        )

        from app.api.routers.folders import _count_decks_in_folder, _count_subfolders

        for f in folder_rows:
            folders.append(
                {
                    "id": str(f.id),
                    "name": f.name,
                    "deck_count": _count_decks_in_folder(db, f.id),
                    "subfolder_count": _count_subfolders(db, f.id),
                }
            )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "deck_stats": favorite_deck_stats,
            "favorite_deck_ids": favorite_deck_ids,
            "can_manage_decks": can_manage_decks(user),
            "can_manage_deck": can_manage_deck,
            "dashboard_error": (
                dashboard_error
                if dashboard_error is not None
                else request.query_params.get("error")
            ),
            "dashboard_success": (
                dashboard_success
                if dashboard_success is not None
                else request.query_params.get("success")
            ),
            "active_modal": active_modal,  # Don't use query params - prevents unwanted modal on reload
            "modal_deck": modal_deck,
            "create_form": create_form
            or {"name": "", "description": "", "access_level": "user"},
            "edit_form": edit_form
            or {
                "name": modal_deck.name if modal_deck else "",
                "description": (modal_deck.description or "") if modal_deck else "",
                "access_level": (
                    (
                        getattr(modal_deck, "access_level", None)
                        or (
                            "global"
                            if modal_deck and getattr(modal_deck, "is_global", False)
                            else (
                                "org"
                                if modal_deck
                                and getattr(modal_deck, "organization_id", None)
                                else "user"
                            )
                        )
                    )
                    if modal_deck
                    else "user"
                ),
            },
            "tag_form": (
                _tag_form_context(modal_deck) if modal_deck else {"tag_names": ""}
            ),
            "title": "Workspace | edu selviz",
            # Folder context
            "folders": folders,
            "breadcrumb": breadcrumb,
            "current_folder": current_folder,
            "folder_decks": folder_decks,
            "folder_tree": folder_tree or [],
            "folder_error": folder_error,
            "folder_success": folder_success,
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
def login_providers(
    request: Request, user: User | None = Depends(optional_current_user)
):
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
    from app.api.routers.folders import get_folder_tree

    folder_id = request.query_params.get("folder")
    parsed_folder_id: UUID | None = None
    if folder_id:
        try:
            parsed_folder_id = UUID(folder_id)
        except ValueError:
            pass

    folder_tree = get_folder_tree(user=user, db=db)

    return _dashboard_response(
        request,
        user=user,
        db=db,
        folder_id=parsed_folder_id,
        folder_tree=folder_tree,
    )


# ── Folder CRUD handlers ──────────────────────────────────────────────────────

_FOLDER_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")


@router.post("/folders")
def create_folder(
    request: Request,
    name: str = Form(...),
    parent_id: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.models import Folder
    from app.api.routers.folders import get_folder_tree

    name = name.strip()
    parent_uuid: UUID | None = None
    if parent_id:
        try:
            parent_uuid = UUID(parent_id)
        except ValueError:
            pass

    if not name or len(name) > 255:
        return RedirectResponse(
            "/dashboard" + (f"?folder={parent_id}" if parent_id else ""),
            status_code=303,
        )
    if not _FOLDER_NAME_RE.match(name):
        folder_tree = get_folder_tree(user=user, db=db)
        return _dashboard_response(
            request,
            user=user,
            db=db,
            folder_id=parent_uuid,
            folder_tree=folder_tree,
            folder_error="Folder name can only contain letters, numbers, and underscores.",
        )

    # Verify parent ownership
    if parent_uuid:
        parent = db.get(Folder, parent_uuid)
        if not parent or parent.user_id != user.id:
            return RedirectResponse("/dashboard", status_code=303)

    # Check duplicate sibling name
    existing = (
        db.execute(
            select(Folder).where(
                Folder.parent_id == parent_uuid,
                Folder.user_id == user.id,
                Folder.name == name,
            )
        )
        .scalars()
        .first()
    )
    if existing:
        folder_tree = get_folder_tree(user=user, db=db)
        return _dashboard_response(
            request,
            user=user,
            db=db,
            folder_id=parent_uuid,
            folder_tree=folder_tree,
            folder_error="A folder with this name already exists here.",
        )

    folder = Folder(name=name, parent_id=parent_uuid, user_id=user.id)
    db.add(folder)
    db.commit()
    db.refresh(folder)

    return RedirectResponse(
        f"/decks/browse?folder={folder.id}&success=Folder+created",
        status_code=303,
    )


@router.post("/folders/{folder_id}/rename")
def rename_folder(
    request: Request,
    folder_id: UUID,
    name: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.models import Folder
    from app.api.routers.folders import get_folder_tree

    folder = db.get(Folder, folder_id)
    if not folder or folder.user_id != user.id:
        return RedirectResponse("/dashboard", status_code=303)

    name = name.strip()
    if not name or len(name) > 255 or not _FOLDER_NAME_RE.match(name):
        folder_tree = get_folder_tree(user=user, db=db)
        return _dashboard_response(
            request,
            user=user,
            db=db,
            folder_id=folder.parent_id,
            folder_tree=folder_tree,
            folder_error="Folder name can only contain letters, numbers, and underscores.",
        )

    # Check duplicate sibling name
    existing = (
        db.execute(
            select(Folder).where(
                Folder.parent_id == folder.parent_id,
                Folder.user_id == user.id,
                Folder.name == name,
                Folder.id != folder_id,
            )
        )
        .scalars()
        .first()
    )
    if existing:
        folder_tree = get_folder_tree(user=user, db=db)
        return _dashboard_response(
            request,
            user=user,
            db=db,
            folder_id=folder.parent_id,
            folder_tree=folder_tree,
            folder_error="A folder with this name already exists here.",
        )

    folder.name = name
    db.commit()

    return RedirectResponse(
        f"/decks/browse?folder={folder_id}&success=Folder+renamed",
        status_code=303,
    )


@router.post("/folders/{folder_id}/delete")
def delete_folder(
    request: Request,
    folder_id: UUID,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.models import Folder

    folder = db.get(Folder, folder_id)
    if not folder or folder.user_id != user.id:
        return RedirectResponse("/dashboard", status_code=303)

    parent_id = folder.parent_id

    # Move child decks to parent
    for deck in (
        db.execute(select(Deck).where(Deck.folder_id == folder_id)).scalars().all()
    ):
        deck.folder_id = parent_id

    db.delete(folder)
    db.commit()

    return RedirectResponse(
        "/decks/browse"
        + (
            f"?folder={parent_id}&success=Folder+deleted"
            if parent_id
            else "?success=Folder+deleted"
        ),
        status_code=303,
    )


BROWSE_PAGE_SIZE = 10


@router.get("/decks/browse", response_class=HTMLResponse)
def browse_decks(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.models import Folder
    from app.api.routers.folders import (
        get_folder_tree,
        _count_decks_in_folder,
        _count_subfolders,
    )

    q = request.query_params.get("q", "").strip()
    folder_param = request.query_params.get("folder", "")
    try:
        folder_id = UUID(folder_param) if folder_param else None
    except ValueError:
        folder_id = None

    # Tab handling (only for root-level, not folder or search)
    tabs = get_browse_tabs(user)
    tab_param = request.query_params.get("tab", "")
    if tab_param not in {t["key"] for t in tabs}:
        tab_param = tabs[0]["key"] if tabs else TAB_GLOBAL
    show_tabs = bool(tabs)
    show_action_bar = can_write_tab(user, tab_param)

    from app.services.access import (
        can_delete_deck as _cdd,
        can_set_deck_access as _csda,
        can_write_deck as _cwd,
        is_org_admin as _ioa,
        is_system_admin as _isa,
        load_browse_deck_query,
    )

    folder_tree = get_folder_tree(user=user, db=db)

    def _can_view_folder_local(folder: Folder | None) -> bool:
        if not folder:
            return False
        if _isa(user):
            return True
        if str(folder.user_id) == str(user.id):
            return True
        if (
            user.organization_id
            and folder.organization_id
            and str(folder.organization_id) == str(user.organization_id)
        ):
            return True
        visible_child_filters = [
            Deck.user_id == user.id,
            Deck.access_level == "global",
        ]
        if user.organization_id:
            visible_child_filters.append(
                and_(
                    Deck.access_level == "org",
                    Deck.organization_id == user.organization_id,
                )
            )
        folder_ids_to_check = [folder.id]
        seen_folder_ids = set()
        while folder_ids_to_check:
            current_folder_id = folder_ids_to_check.pop()
            if current_folder_id in seen_folder_ids:
                continue
            seen_folder_ids.add(current_folder_id)

            accessible_child_exists = db.execute(
                select(Deck.id)
                .where(
                    Deck.folder_id == current_folder_id,
                    Deck.is_deleted.is_(False),
                    or_(*visible_child_filters),
                )
                .limit(1)
            ).first()
            if accessible_child_exists is not None:
                return True

            child_folder_ids = db.execute(
                select(Folder.id).where(Folder.parent_id == current_folder_id)
            ).scalars().all()
            folder_ids_to_check.extend(child_folder_ids)

        return False

    def _can_manage_folder_local(folder: Folder | None) -> bool:
        if not folder:
            return False
        if _isa(user):
            return True
        if str(folder.user_id) == str(user.id):
            return True
        return bool(
            _ioa(user)
            and user.organization_id
            and folder.organization_id
            and str(folder.organization_id) == str(user.organization_id)
        )

    def _folder_payload(folder: Folder) -> dict:
        return {
            "id": str(folder.id),
            "name": folder.name,
            "deck_count": _count_decks_in_folder(db, folder.id),
            "subfolder_count": _count_subfolders(db, folder.id),
            "can_write": _can_manage_folder_local(folder),
        }

    # Breadcrumb
    breadcrumb: list[tuple[str, str]] = []
    current_folder = None
    if folder_id:
        maybe_folder = db.get(Folder, folder_id)
        if _can_view_folder_local(maybe_folder):
            current_folder = maybe_folder
            node_id: UUID | None = folder_id
            path_ids = []
            while node_id:
                node = db.get(Folder, node_id)
                if not node or not _can_view_folder_local(node):
                    break
                path_ids.insert(0, node)
                node_id = node.parent_id
            breadcrumb = [(str(f.id), f.name) for f in path_ids]

    # Subfolders in current folder
    subfolders = []
    for f in (
        db.execute(
            select(Folder)
            .where(
                Folder.parent_id == folder_id,
            )
            .order_by(Folder.name.asc())
        )
        .scalars()
        .all()
    ):
        if _can_view_folder_local(f):
            subfolders.append(_folder_payload(f))

    # Root folders (for sidebar or root-level display)
    root_folders = []
    for f in (
        db.execute(
            select(Folder)
            .where(
                Folder.parent_id.is_(None),
            )
            .order_by(Folder.name.asc())
        )
        .scalars()
        .all()
    ):
        if _can_view_folder_local(f):
            root_folders.append(_folder_payload(f))

    # Base query — apply tab filter only at root level (no folder, no search)
    apply_tab_filter = not folder_id and not q
    if apply_tab_filter:
        deck_filter = browse_filter_clause(user, tab_param)
    else:
        visible_filters = [
            Deck.user_id == user.id,
            Deck.access_level == "global",
        ]
        if user.organization_id:
            visible_filters.append(
                and_(
                    Deck.access_level == "org",
                    Deck.organization_id == user.organization_id,
                )
            )
        deck_filter = Deck.is_deleted.is_(False) & or_(*visible_filters)

    base_q = load_browse_deck_query(user).where(deck_filter).group_by(Deck.id)

    # Folder filter (only when inside a folder)
    if folder_id and not q:
        base_q = base_q.where(Deck.folder_id == folder_id)

    # Search (search always shows accessible decks across all tabs)
    if q:
        from app.services.access import normalize_deck_name

        normalized_q = normalize_deck_name(q)
        if normalized_q:
            base_q = (
                base_q.outerjoin(Deck.tags)
                .where(
                    or_(
                        Deck.normalized_name.ilike(f"%{normalized_q}%"),
                        Tag.normalized_name.ilike(f"%{normalized_q}%"),
                    )
                )
                .group_by(Deck.id)
            )

    # Pagination for browse/search views
    page = request.query_params.get("page", "1")
    try:
        page = max(1, int(page))
    except ValueError:
        page = 1

    count_q = select(func.count()).select_from(base_q.subquery())
    total = db.execute(count_q).scalar_one()
    total_pages = (total + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE if total > 0 else 1
    page = min(page, total_pages)
    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)

    offset = (page - 1) * BROWSE_PAGE_SIZE
    decks = (
        db.execute(
            base_q.order_by(Deck.access_level.desc(), Deck.created_at.desc())
            .limit(BROWSE_PAGE_SIZE)
            .offset(offset)
        )
        .scalars()
        .all()
    )

    # Root-level decks only (no folder, for root view)
    root_decks = []
    if not q and not folder_id:
        root_q = (
            load_browse_deck_query(user)
            .where(deck_filter)
            .where(Deck.folder_id.is_(None))
            .group_by(Deck.id)
            .order_by(Deck.access_level.desc(), Deck.created_at.desc())
        )
        root_decks = db.execute(root_q).scalars().all()

    # Favorite IDs
    fav_rows = (
        db.execute(
            select(UserDeckFavorite.deck_id).where(UserDeckFavorite.user_id == user.id)
        )
        .scalars()
        .all()
    )
    favorite_deck_ids = {str(f) for f in fav_rows}

    # Browse permissions for per-deck actions
    visible_decks = {str(d.id): d for d in [*decks, *root_decks]}
    writable_deck_ids = {
        deck_id for deck_id, d in visible_decks.items() if _cwd(user, d)
    }
    deletable_deck_ids = {
        deck_id for deck_id, d in visible_decks.items() if _cdd(user, d)
    }
    access_manageable_deck_ids = {
        deck_id for deck_id, d in visible_decks.items() if _csda(user, d)
    }

    return templates.TemplateResponse(
        "decks/browse.html",
        {
            "request": request,
            "user": user,
            "decks": decks,
            "favorite_deck_ids": favorite_deck_ids,
            "writable_deck_ids": writable_deck_ids,
            "deletable_deck_ids": deletable_deck_ids,
            "access_manageable_deck_ids": access_manageable_deck_ids,
            "access_level_options": [
                ("global", "Global"),
                ("org", "Org"),
                ("user", "Mine"),
            ],
            "q": q,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "start_page": start_page,
            "end_page": end_page,
            "title": "Browse | edu selviz",
            # Folder context
            "subfolders": subfolders,
            "root_folders": root_folders,
            "root_decks": root_decks,
            "breadcrumb": breadcrumb,
            "current_folder": current_folder,
            "folder_tree": folder_tree,
            "can_manage_decks": can_manage_decks(user),
            # Tab context
            "tabs": tabs,
            "active_tab": tab_param,
            "show_tabs": show_tabs,
            "show_action_bar": show_action_bar,
            # Bulk AI upload: decks user can write to
            "writeable_decks_list": [
                d for d in visible_decks.values() if _cwd(user, d)
            ],
        },
    )


@router.get("/analytics", response_class=HTMLResponse)
def analytics_home(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if user.role not in {ROLE_ADMIN, ROLE_SYSTEM_ADMIN}:
        raise HTTPException(
            status_code=403, detail="You do not have access to analytics"
        )

    selected_user_id = request.query_params.get("user_id", "")
    # Get deck_id(s) from query param (handles both single and multiselect)
    selected_deck_id = request.query_params.get(
        "deck_id", ""
    ) or request.query_params.get("deck_ids", "")
    selected_deck_ids = (
        [d.strip() for d in selected_deck_id.split(",") if d.strip()]
        if selected_deck_id
        else []
    )

    # Only compute data when both filters are applied
    has_filters = bool(selected_user_id and selected_deck_ids)

    # Always load filter options for dropdowns
    if user.role == ROLE_SYSTEM_ADMIN:
        visible_users = (
            db.execute(select(User).order_by(User.created_at.desc())).scalars().all()
        )
    else:
        visible_users = (
            db.execute(
                select(User)
                .where(User.organization_id == user.organization_id)
                .order_by(User.created_at.desc())
            )
            .scalars()
            .all()
        )
    available_decks = list_accessible_deck_stats(db, user=user)

    # Register deck options for multiselect component
    request.app.state.multiselect_options["deck_ids"] = [
        {"key": str(stats.deck.id), "title": stats.deck.name}
        for stats in available_decks
    ]

    # Only compute analytics when both filters are applied
    completion_stats: list[dict[str, object]] = []
    user_summaries = []

    if has_filters:
        # Compute deck completion for the selected deck
        for stats in available_decks:
            deck = stats.deck
            if str(deck.id) not in selected_deck_ids:
                continue

            total_cards = len(deck.cards)
            completed_reviews = stats.cards_reviewed
            percent_complete = 0
            if total_cards > 0:
                percent_complete = round(
                    min(completed_reviews / total_cards * 100, 100)
                )

            tests_taken_query = (
                select(func.count(TestAttempt.id))
                .join(Test, TestAttempt.test_id == Test.id)
                .where(Test.deck_id == deck.id)
            )
            if selected_user_id:
                tests_taken_query = tests_taken_query.where(
                    TestAttempt.user_id == selected_user_id
                )
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

        # Compute user analytics for selected user
        target_user_list = [u for u in visible_users if str(u.id) == selected_user_id]
        for target_user in target_user_list:
            summary = {
                "user": target_user,
                "cards_reviewed": 0,
                "tests_taken": 0,
            }

            cards_reviewed_query = (
                select(func.count(Review.id))
                .join(Card, Card.id == Review.card_id)
                .where(
                    Card.deck.has(Deck.organization_id == target_user.organization_id)
                    | Deck.is_global.is_(True)
                )
                .where(Review.card.has(Card.deck.has()))
            )
            if selected_deck_ids:
                cards_reviewed_query = cards_reviewed_query.where(
                    Card.deck_id.in_(selected_deck_ids)
                )
            cards_reviewed = db.execute(cards_reviewed_query).scalar() or 0

            tests_taken_query = select(func.count(TestAttempt.id)).where(
                TestAttempt.user_id == target_user.id
            )
            if selected_deck_ids:
                tests_taken_query = tests_taken_query.join(
                    Test,
                    TestAttempt.test_id == Test.id,
                ).where(Test.deck_id.in_(selected_deck_ids))
            tests_taken = db.execute(tests_taken_query).scalar() or 0

            summary["cards_reviewed"] = cards_reviewed
            summary["tests_taken"] = tests_taken
            user_summaries.append(summary)

    # Get recent analytics events (limited to last 50)
    recent_events = (
        db.execute(
            select(AnalyticsEvent)
            .order_by(AnalyticsEvent.event_timestamp.desc())
            .limit(50)
        )
        .scalars()
        .all()
    )

    return templates.TemplateResponse(
        "analytics/index.html",
        {
            "request": request,
            "user": user,
            "deck_stats": completion_stats,
            "user_summaries": user_summaries,
            "visible_users": visible_users,
            "available_decks": available_decks,
            "selected_user_id": selected_user_id,
            "selected_deck_id": selected_deck_id,
            "selected_deck_ids": selected_deck_ids,
            "has_filters": has_filters,
            "recent_events": recent_events,
            "title": "Analytics | edu selviz",
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_home(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return _settings_home_response(
        request,
        user=user,
        db=db,
    )


@router.get("/settings/organizations", response_class=HTMLResponse)
def organizations_page(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return _organizations_response(
        request,
        user=user,
        db=db,
    )


@router.post("/settings/organizations")
def create_organization(
    request: Request,
    name: str = Form(...),
    is_ai_enabled: bool = Form(default=False),
    is_test_enabled: bool = Form(default=False),
    test_daily_limit: int = Form(default=0),
    ai_override_global: bool = Form(default=False),
    provider: str = Form(default="openai"),
    ai_secret: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_system_admin(user)
    cleaned_name = name.strip()
    test_daily_limit = min(max(test_daily_limit, 0), settings.test_daily_limit or 9999)
    if not cleaned_name:
        return _organizations_response(
            request,
            user=user,
            db=db,
            status_code=400,
            organization_error="Organization name is required.",
            active_modal="create",
            create_form={
                "name": cleaned_name,
                "is_ai_enabled": is_ai_enabled,
                "is_test_enabled": is_test_enabled,
                "test_daily_limit": test_daily_limit,
            },
        )

    organization = Organization(
        name=cleaned_name,
        is_ai_enabled=is_ai_enabled,
        is_test_enabled=is_test_enabled,
        test_daily_limit=test_daily_limit,
    )
    db.add(organization)
    try:
        db.flush()  # get org.id before commit
        if is_ai_enabled and ai_override_global and ai_secret.strip():
            save_ai_credential(
                db,
                scope_type="organization",
                scope_id=organization.id,
                provider=provider,
                secret=ai_secret.strip(),
            )
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
            create_form={
                "name": cleaned_name,
                "is_ai_enabled": is_ai_enabled,
                "is_test_enabled": is_test_enabled,
                "test_daily_limit": test_daily_limit,
            },
        )

    return RedirectResponse(
        url="/settings/organizations?success=Organization+created", status_code=303
    )


@router.post("/settings/organizations/{organization_id}/update")
def update_organization(
    request: Request,
    organization_id: str,
    name: str = Form(...),
    is_ai_enabled: bool = Form(default=False),
    is_test_enabled: bool = Form(default=False),
    test_daily_limit: int = Form(default=0),
    ai_override_global: bool = Form(default=False),
    provider: str = Form(default="openai"),
    ai_secret: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require_system_admin(user)
    organization = db.get(Organization, organization_id)
    if not organization:
        raise HTTPException(status_code=404)

    cleaned_name = name.strip()
    test_daily_limit = min(max(test_daily_limit, 0), settings.test_daily_limit or 9999)
    if not cleaned_name:
        return _organizations_response(
            request,
            user=user,
            db=db,
            status_code=400,
            organization_error="Organization name is required.",
            active_modal="edit",
            modal_organization=organization,
            edit_form={
                "name": cleaned_name,
                "is_ai_enabled": is_ai_enabled,
                "is_test_enabled": is_test_enabled,
            },
        )

    organization.name = cleaned_name
    organization.is_ai_enabled = is_ai_enabled
    organization.is_test_enabled = is_test_enabled
    organization.test_daily_limit = test_daily_limit

    # Handle AI credential override
    if is_ai_enabled and ai_override_global and ai_secret.strip():
        save_ai_credential(
            db,
            scope_type="organization",
            scope_id=organization.id,
            provider=provider,
            secret=ai_secret.strip(),
        )
    elif is_ai_enabled and not ai_override_global:
        # Clear any existing org-level credential so it falls back to env
        from app.models import AICredentialScope

        db.query(AICredentialScope).filter_by(
            scope_type="organization", scope_id=organization.id
        ).delete()

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
            edit_form={
                "name": cleaned_name,
                "is_ai_enabled": is_ai_enabled,
                "is_test_enabled": is_test_enabled,
            },
        )

    return RedirectResponse(
        url="/settings/organizations?success=Organization+updated", status_code=303
    )


@router.get("/settings/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    org: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    return _users_response(request, user=user, db=db, org_id=org)


@router.post("/settings/users/{target_user_id}/update")
def update_user_settings(
    request: Request,
    target_user_id: str,
    role: str = Form(...),
    organization_id: str = Form(default=""),
    is_test_enabled: bool = Form(default=False),
    ai_enabled: bool = Form(default=False),
    ai_override_org: bool = Form(default=False),
    provider: str = Form(default="openai"),
    ai_secret: str = Form(default=""),
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

    # Handle AI credential: only save if AI is on, override is on, and secret is provided
    if ai_enabled and ai_override_org and ai_secret.strip():
        save_ai_credential(
            db,
            scope_type="user",
            scope_id=target.id,
            provider=provider,
            secret=ai_secret.strip(),
        )
    else:
        # Clear any existing user AI credential when AI is disabled or override is removed
        from app.models import AICredentialScope

        db.query(AICredentialScope).filter_by(
            scope_type="user", scope_id=target.id
        ).delete()

    db.commit()
    return RedirectResponse(url="/settings/users?success=User+updated", status_code=303)


@router.post("/decks")
def create_deck(
    request: Request,
    name: str = Form(...),
    description: str = Form(default=""),
    access_level: str = Form(default="user"),
    is_global: bool = Form(default=False),
    folder_id: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    from app.models import Folder  # noqa: F401

    if not can_manage_decks(user):
        raise HTTPException(
            status_code=403, detail="You do not have permission to create decks."
        )

    original_name = name
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
            create_form={
                "name": original_name,
                "description": cleaned_description,
                "access_level": access_level,
            },
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
            create_form={
                "name": original_name,
                "description": cleaned_description,
                "access_level": access_level,
            },
        )

    valid_levels = {"global", "org", "user"}
    if is_global:
        access_level = "global"
    elif access_level not in valid_levels:
        access_level = "user"

    if access_level == "global" and not is_system_admin(user):
        raise HTTPException(
            status_code=403, detail="Only system admins can create global decks."
        )
    if access_level == "org" and not is_org_admin(user):
        return _dashboard_response(
            request,
            user=user,
            db=db,
            status_code=400,
            dashboard_error="Assign this admin to an organization before creating organization decks.",
            active_modal="create",
            create_form={
                "name": original_name,
                "description": cleaned_description,
                "access_level": access_level,
            },
        )

    organization_id = user.organization_id if access_level == "org" else None

    # Validate folder_id
    deck_folder_id = None
    if folder_id:
        try:
            folder_uuid = UUID(folder_id)
            # Verify folder exists and user has access
            folder = db.get(Folder, folder_uuid)
            if folder:
                # Check user can write to this folder
                if folder.user_id == user.id or (
                    folder.organization_id == user.organization_id
                    and user.organization_id
                ):
                    deck_folder_id = folder_uuid
        except ValueError:
            pass  # Invalid UUID, create in root

    deck = Deck(
        user_id=user.id,
        organization_id=organization_id,
        name=cleaned_name,
        normalized_name=normalized_name,
        description=cleaned_description or None,
        access_level=access_level,
        is_global=(access_level == "global"),
        folder_id=deck_folder_id,
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
            dashboard_error=(
                "An active deck with that normalized name already exists in this scope."
            ),
            active_modal="create",
            create_form={
                "name": original_name,
                "description": cleaned_description,
                "access_level": access_level,
            },
        )

    success_message = quote_plus("Deck created")
    return RedirectResponse(
        url=f"/dashboard?success={success_message}", status_code=303
    )


@router.post("/decks/{deck_id}/update")
def update_deck(
    request: Request,
    deck_id: str,
    name: str = Form(...),
    description: str = Form(default=""),
    access_level: str = Form(default="user"),
    is_global: bool = Form(default=False),
    next_url: str = Form(default=""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_manage_deck(user, deck):
        raise HTTPException(status_code=404)

    cards = (
        db.execute(
            select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())
        )
        .scalars()
        .all()
    )
    original_name = name
    cleaned_name = name.strip()
    cleaned_description = description.strip()

    redirect_target = next_url.strip() if isinstance(next_url, str) else ""

    if not redirect_target.startswith("/"):
        redirect_target = "/dashboard"

    should_render_inline_errors = not (isinstance(next_url, str) and next_url.strip())

    def _append_message_param(url: str, key: str, message: str) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query[key] = message
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    def _deck_update_error_response(message: str):
        if should_render_inline_errors:
            flashcards = [card for card in cards if card.card_type == "basic"]
            mcqs = [card for card in cards if card.card_type == "mcq"]
            can_edit = can_manage_deck(user, deck)
            has_test_content = deck_has_test_content(cards)
            has_published_tests = _deck_has_published_tests(db, deck.id)
            tests_available = can_open_test_center(
                user,
                deck,
                has_test_content=has_test_content,
                has_published_tests=has_published_tests,
            )
            test_count = (
                _user_test_attempt_count(db, deck_id=deck.id, user_id=user.id)
                if tests_available
                else 0
            )
            deck_view = SimpleNamespace(**getattr(deck, "__dict__", {}))
            deck_view.name = original_name
            deck_view.description = cleaned_description
            deck_view.access_level = "global" if is_global else access_level
            deck_view.is_global = is_global or access_level == "global"
            return templates.TemplateResponse(
                "decks/overview.html",
                {
                    "request": request,
                    "user": user,
                    "deck": deck_view,
                    "flashcard_count": len(flashcards),
                    "mcq_count": len(mcqs),
                    "can_edit": can_edit,
                    "can_use_ai_generation": (can_use_ai_generation(user) and can_edit),
                    "tests_available": tests_available,
                    "test_count": test_count,
                    "default_test_count": 10,
                    "update_error": message,
                    "is_favorited": False,
                    "tag_form": _tag_form_context(deck),
                    "edit_form": {
                        "name": original_name,
                        "description": cleaned_description,
                        "access_level": ("global" if is_global else access_level),
                    },
                    "title": f"{deck.name} | edu selviz",
                },
                status_code=400,
            )
        return RedirectResponse(
            url=_append_message_param(redirect_target, "error", message),
            status_code=303,
        )

    if not cleaned_name:
        return _deck_update_error_response("Deck name cannot be empty.")
    normalized_name = normalize_deck_name(cleaned_name)
    if not normalized_name:
        return _deck_update_error_response("Deck name must include letters or numbers.")

    valid_levels = {"global", "org", "user"}
    current_access_level = getattr(deck, "access_level", None) or (
        "global"
        if getattr(deck, "is_global", False)
        else "org" if getattr(deck, "organization_id", None) else "user"
    )
    if is_global:
        access_level = "global"
    elif access_level not in valid_levels:
        access_level = current_access_level  # keep current if invalid

    if access_level == "global" and not is_system_admin(user):
        raise HTTPException(
            status_code=403, detail="Only system admins can make a deck global."
        )
    if access_level == "org" and not is_org_admin(user):
        return _deck_update_error_response(
            "Assign this admin to an organization before saving organization decks."
        )

    organization_id = user.organization_id if access_level == "org" else None

    deck.name = cleaned_name
    deck.normalized_name = normalized_name
    deck.description = cleaned_description or None
    deck.access_level = access_level
    deck.is_global = access_level == "global"
    deck.organization_id = organization_id

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _deck_update_error_response(
            "Unable to update: a deck with this normalized name already exists in the same scope."
        )

    if redirect_target.startswith(f"/decks/{deck.id}"):
        return RedirectResponse(
            url=_append_message_param(
                redirect_target,
                "update_success",
                "Deck details updated",
            ),
            status_code=303,
        )
    return RedirectResponse(
        url=_append_message_param(
            redirect_target,
            "success",
            "Deck details updated",
        ),
        status_code=303,
    )


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
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to manage tags for this deck.",
        )

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
        for tag in db.execute(select(Tag).where(Tag.organization_id == organization_id))
        .scalars()
        .all()
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
    return RedirectResponse(
        url=f"{redirect_target}?tag_success={quote_plus(message)}", status_code=303
    )


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
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to manage tags for this deck.",
        )

    tag = db.get(Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404)

    if tag in deck.tags:
        deck.tags.remove(tag)
        db.commit()

    redirect_target = next_url.strip() if isinstance(next_url, str) else ""
    if not redirect_target.startswith("/"):
        redirect_target = f"/decks/{deck.id}"
    return RedirectResponse(
        url=f"{redirect_target}?tag_success={quote_plus('Tag removed')}",
        status_code=303,
    )


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
    deck.deleted_at = datetime.utcnow()
    db.commit()

    success_message = quote_plus("Deck deleted")
    return RedirectResponse(
        url=f"/dashboard?success={success_message}", status_code=303
    )


def _cleanup_deck_media(deck_id: str) -> None:
    """Remove all media objects for a deck after deletion."""
    try:
        get_storage().delete_prefix(prefix=deck_media_prefix(deck_id))
    except Exception:
        pass  # Best effort cleanup


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

    cards = (
        db.execute(
            select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())
        )
        .scalars()
        .all()
    )
    has_published_tests = _deck_has_published_tests(db, deck.id)
    flashcards = [card for card in cards if card.card_type == "basic"]
    mcqs = [card for card in cards if card.card_type == "mcq"]
    can_edit = can_manage_deck(user, deck)
    can_manage_tags = _can_manage_tags(user, deck)
    has_test_content = deck_has_test_content(cards)
    tests_available = can_open_test_center(
        user,
        deck,
        has_test_content=has_test_content,
        has_published_tests=has_published_tests,
    )
    test_count = (
        _user_test_attempt_count(db, deck_id=deck.id, user_id=user.id)
        if tests_available
        else 0
    )

    favorite_scalars = db.execute(
        select(UserDeckFavorite).where(
            UserDeckFavorite.user_id == user.id, UserDeckFavorite.deck_id == deck.id
        )
    ).scalars()
    favorite_first = (
        favorite_scalars.first()
        if hasattr(favorite_scalars, "first")
        else next(iter(favorite_scalars.all()), None)
    )
    is_favorited = favorite_first is not None

    return _html_no_store(
        templates.TemplateResponse(
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
                "can_use_ai_generation": (can_use_ai_generation(user) and can_edit),
                "tests_available": tests_available,
                "test_count": test_count,
                "default_test_count": settings.default_test_count,
                "import_success": request.query_params.get("import_success"),
                "update_success": request.query_params.get("update_success"),
                "tag_error": request.query_params.get("tag_error"),
                "tag_success": request.query_params.get("tag_success"),
                "tag_form": _tag_form_context(deck),
                "title": f"{deck.name} | edu selviz",
            },
        )
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

    cards = (
        db.execute(
            select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())
        )
        .scalars()
        .all()
    )
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

    cards = (
        db.execute(
            select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())
        )
        .scalars()
        .all()
    )
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

    cards = (
        db.execute(
            select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())
        )
        .scalars()
        .all()
    )
    retry_file_result = db.execute(
        select(BulkAIUploadFile)
        .where(BulkAIUploadFile.created_deck_id == deck.id)
        .where(BulkAIUploadFile.status == BulkAIUploadFileStatus.FAILED.value)
        .order_by(BulkAIUploadFile.created_at.desc())
        .limit(1)
    )
    if hasattr(retry_file_result, "scalar_one_or_none"):
        retry_file = retry_file_result.scalar_one_or_none()
    else:
        retry_items = retry_file_result.scalars().all()
        retry_file = retry_items[0] if retry_items else None
    retry_bulk = None
    retry_filename = None
    if retry_file:
        retry_bulk = db.get(BulkAIUpload, retry_file.bulk_upload_id)
        retry_filename = retry_file.original_filename
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
        retry_file=retry_file,
        retry_bulk=retry_bulk,
        retry_filename=retry_filename,
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

    return RedirectResponse(
        url=(
            f"/decks/{deck.id}/flashcards"
            f"?import_success={quote_plus('Flashcard added')}"
        ),
        status_code=303,
    )


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

    cards = (
        db.execute(
            select(Card).where(Card.deck_id == deck.id).order_by(Card.created_at.desc())
        )
        .scalars()
        .all()
    )

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

    new_cards = [
        Card(deck_id=deck.id, front=row.front, back=row.back) for row in imported_rows
    ]
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


# =============================================================================
# Anki Import
# =============================================================================


@router.get("/decks/{deck_id}/anki-import", response_class=HTMLResponse)
def anki_import_page(
    request: Request,
    deck_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)
    if not can_manage_deck(user, deck):
        raise HTTPException(
            status_code=403, detail="You do not have permission to import to this deck"
        )

    return templates.TemplateResponse(
        "cards/anki_import.html",
        {
            "request": request,
            "user": user,
            "deck": deck,
            "title": f"Import Anki Deck | {deck.name}",
        },
    )


@router.post("/decks/{deck_id}/anki-import")
def anki_import_upload(
    request: Request,
    deck_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    from fastapi.responses import JSONResponse

    deck = db.get(Deck, deck_id)
    if not deck or not can_access_deck(user, deck):
        raise HTTPException(status_code=404)
    if not can_manage_deck(user, deck):
        raise HTTPException(
            status_code=403, detail="You do not have permission to import to this deck"
        )

    # Validate file
    if not file.filename or not file.filename.endswith(".apkg"):
        return JSONResponse(
            {"success": False, "error": "File must be a .apkg file"},
            status_code=400,
        )

    # Import using AnkiImportService
    from app.services.anki_import import AnkiImportError, AnkiImportService

    try:
        service = AnkiImportService(db, deck)
        result = service.import_apkg(file.file)
    except AnkiImportError as e:
        return JSONResponse(
            {"success": False, "error": str(e)},
            status_code=400,
        )
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": f"Import failed: {e}"},
            status_code=500,
        )

    return JSONResponse(
        {
            "success": True,
            "cards_imported": result.cards_imported,
            "media_files": result.media_files,
            "duplicates_skipped": result.duplicates_skipped,
            "errors": result.errors,
            "redirect_url": f"/decks/{deck.id}?import_success={quote_plus(f'Imported {result.cards_imported} cards')}",
        }
    )


@router.get("/review", response_class=HTMLResponse)
def review_page(
    request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)
):
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
            {
                "request": request,
                "user": user,
                "review_deck": deck,
                "review_complete": True,
            },
        )

    card = svc.next_due_card(db, user=user, deck_id=deck.id if deck else None)
    if card is None:
        return templates.TemplateResponse(
            "review/empty.html",
            {
                "request": request,
                "user": user,
                "review_deck": deck,
                "review_complete": False,
            },
        )

    cleaned_card = type("ReviewCardView", (), {})()
    cleaned_card.id = card.id
    cleaned_card.is_cloze = card.is_cloze
    front_html, back_html = _render_mixed_card_content(card.content_html)
    fallback_front = _normalize_review_html(card.front)
    fallback_back = _normalize_review_html(card.back)
    cleaned_card.front = front_html or fallback_front
    cleaned_card.back = back_html or fallback_back
    cleaned_card.content_html = front_html
    cleaned_card.content_html_back = back_html

    return templates.TemplateResponse(
        "review/card.html",
        {
            "request": request,
            "user": user,
            "card": cleaned_card,
            "review_deck": deck,
            "remaining": remaining,
        },
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
    return _review_next_inner(
        request, user, db, deck_id=effective_deck_id, remaining=next_remaining
    )
