from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routers import auth, pages, analytics
from app.api.routers.favorites import router as favorites_router
from app.api.routers.bulk_import import router as bulk_import_router
from app.api.routers.content import router as content_router
from app.api.routers.folders import router as folders_router
from app.api.routers.deck_accesses import router as deck_access_router
from app.api.routers.users import router as users_router
from app.components.multiselect.routes import router as multiselect_router
from app.core.config import settings
from app.core.db import SessionLocal
from app.services.admin_bootstrap import bootstrap_system_admin_by_email
from app.services.storage import StorageError, get_storage

BASE_DIR = Path(__file__).resolve().parent

# Paths that are served as HTML pages (not API endpoints).
# An unauthenticated request to any of these should redirect to home.
_PAGE_PREFIXES = ("/dashboard", "/decks", "/review", "/tests", "/attempts", "/settings", "/analytics")

app = FastAPI(
    title="edu selviz",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    swagger_ui_oauth2_redirect_url="/api/docs/oauth2-redirect",
)


@app.exception_handler(HTTPException)
async def redirect_unauthenticated_to_home(request: Request, exc: HTTPException):
    if exc.status_code == 401 and request.url.path.startswith(_PAGE_PREFIXES):
        return RedirectResponse(url="/", status_code=302)
    from fastapi.exception_handlers import http_exception_handler
    return await http_exception_handler(request, exc)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=settings.oidc_state_session_max_age_seconds,
)

STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/{filename}.{ext}")
def serve_temp_media_file(filename: str, ext: str):
    """Serve media files by original filename (e.g., /temp_file_hash.jpg)"""
    from fastapi import HTTPException

    # Reject path traversal attempts
    if ".." in filename or "/" in filename or chr(92) in filename:
        raise HTTPException(status_code=400)

    media_root = STATIC_DIR / "media"
    if not media_root.exists():
        raise HTTPException(status_code=404)

    search_name = f"{filename}.{ext}"
    for deck_dir in media_root.iterdir():
        if deck_dir.is_dir():
            candidate = (deck_dir / search_name).resolve()
            # Ensure path stays within media_root
            try:
                candidate.relative_to(media_root.resolve())
            except ValueError:
                raise HTTPException(status_code=400)
            if candidate.exists() and candidate.is_file():
                return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"})
    raise HTTPException(status_code=404)


@app.get("/assets/media/{filename:path}")
def serve_legacy_media_file(filename: str):
    return serve_media_file(filename)


@app.get("/media/{object_key:path}")
def serve_media_file(object_key: str):
    """Serve media files from the configured storage backend."""
    if ".." in object_key or chr(92) in object_key:
        raise HTTPException(status_code=400)

    try:
        payload, content_type = get_storage().open_bytes(key=object_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return Response(
        content=payload,
        media_type=content_type or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/assets/{version}/{asset_path:path}")
def versioned_static_asset(version: str, asset_path: str):
    static_root = STATIC_DIR.resolve()
    candidate = (STATIC_DIR / asset_path).resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError:
        raise HTTPException(status_code=404)
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404)

    return FileResponse(
        candidate,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


@app.on_event("startup")
def promote_bootstrap_system_admin() -> None:
    try:
        get_storage().ensure_ready()
    except Exception as exc:
        print(f"Storage not ready at startup: {exc}")
    with SessionLocal() as db:
        bootstrap_system_admin_by_email(db)


app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(content_router)
app.include_router(analytics.router)
app.include_router(bulk_import_router)
app.include_router(favorites_router)
app.include_router(folders_router)
app.include_router(deck_access_router)
app.include_router(users_router)
app.include_router(multiselect_router)


@app.on_event("startup")
def init_multiselect_options() -> None:
    """Initialize multiselect component options storage."""
    app.state.multiselect_options = {}
