from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routers import auth, pages
from app.core.config import settings

app = FastAPI(title="SRS Web")

# Needed by Authlib to store OIDC state/nonce during the redirect flow.
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

# optional static folder
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Routers (controllers)
app.include_router(auth.router)
app.include_router(pages.router)
