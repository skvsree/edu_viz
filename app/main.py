from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routers import auth, pages
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

# Serve static assets reliably regardless of the process working directory.
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

@app.on_event("startup")
def promote_bootstrap_system_admin() -> None:
    with SessionLocal() as db:
        bootstrap_system_admin_by_email(db)


# Routers (controllers)
app.include_router(auth.router)
app.include_router(pages.router)
