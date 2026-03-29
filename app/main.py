from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routers import auth, pages, analytics
from app.api.routers.bulk_import import router as bulk_import_router
from app.api.routers.content import router as content_router
from app.core.config import settings
from app.core.db import SessionLocal
from app.services.admin_bootstrap import bootstrap_system_admin_by_email

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
    with SessionLocal() as db:
        bootstrap_system_admin_by_email(db)


app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(content_router)
app.include_router(analytics.router)
app.include_router(bulk_import_router)
