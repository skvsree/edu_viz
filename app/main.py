from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routers import auth, pages
from app.api.routers.content import router as content_router
from app.core.config import settings
from app.core.db import SessionLocal
from app.services.admin_bootstrap import bootstrap_system_admin_by_email

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="edu selviz")

# Needed by Authlib to store OIDC state/nonce during the redirect flow.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=settings.oidc_state_session_max_age_seconds,
)

STATIC_DIR = BASE_DIR / "static"

# Serve static assets reliably regardless of the process working directory.
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


# Routers (controllers)
app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(content_router)
