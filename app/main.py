from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routers import auth, pages
from app.core.config import settings

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="edu selviz")

# Needed by Authlib to store OIDC state/nonce during the redirect flow.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

# Serve static assets reliably regardless of the process working directory.
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# Routers (controllers)
app.include_router(auth.router)
app.include_router(pages.router)
