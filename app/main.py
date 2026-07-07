from __future__ import annotations

from pathlib import Path
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api.routers import auth, pages, analytics
from app.api.routers.favorites import router as favorites_router
from app.api.routers.bulk_import import router as bulk_import_router
from app.api.routers.content import router as content_router
from app.api.routers.folders import router as folders_router
from app.api.routers.deck_accesses import router as deck_access_router
from app.api.routers.users import router as users_router
from app.api.routers.bulk_ai_upload import router as bulk_ai_upload_router
from app.api.routers.deck_live import router as deck_live_router
from app.components.multiselect.routes import router as multiselect_router
from app.core.config import settings
from app.core.db import SessionLocal
from app.services.admin_bootstrap import bootstrap_system_admin_by_email
from app.services.job_worker import run_worker_loop
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


def _build_legacy_media_index() -> dict[str, Path]:
    """Build a single-file lookup for the legacy ``/{filename}.{ext}`` endpoint.

    Walks the local media directory once so the per-request hot path is an
    O(1) dict lookup instead of an O(N) ``iterdir()`` over every deck folder.
    Returns an empty dict for non-local storage backends.
    """
    media_root = (STATIC_DIR / "media").resolve()
    index: dict[str, Path] = {}
    if not media_root.exists():
        return index
    for candidate in media_root.rglob("*"):
        if candidate.is_file():
            index[candidate.name] = candidate
    return index


_LEGACY_MEDIA_INDEX: dict[str, Path] = {}
_LEGACY_MEDIA_INDEX_LOCK = threading.Lock()


def _get_legacy_media_index() -> dict[str, Path]:
    global _LEGACY_MEDIA_INDEX
    if _LEGACY_MEDIA_INDEX:
        return _LEGACY_MEDIA_INDEX
    with _LEGACY_MEDIA_INDEX_LOCK:
        if not _LEGACY_MEDIA_INDEX:
            _LEGACY_MEDIA_INDEX = _build_legacy_media_index()
    return _LEGACY_MEDIA_INDEX


@app.get("/{filename}.{ext}")
def serve_temp_media_file(filename: str, ext: str):
    """Serve media files by original filename (e.g., /temp_file_hash.jpg)

    Uses an in-memory lookup index built lazily on first request so repeated
    thumbnail hits stay O(1) regardless of how many decks exist on disk.
    """
    from fastapi import HTTPException

    # Reject path traversal attempts
    if ".." in filename or "/" in filename or chr(92) in filename:
        raise HTTPException(status_code=400)

    search_name = f"{filename}.{ext}"
    candidate = _get_legacy_media_index().get(search_name)
    if candidate is None:
        raise HTTPException(status_code=404)

    # Defensive: double-check the path still sits inside the media root in case
    # the file got replaced by something outside.
    media_root = (STATIC_DIR / "media").resolve()
    try:
        candidate.relative_to(media_root)
    except ValueError:
        raise HTTPException(status_code=400)
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/assets/media/{filename:path}")
def serve_legacy_media_file(filename: str):
    return serve_media_file(filename)


@app.get("/media/{object_key:path}")
def serve_media_file(object_key: str):
    """Serve media files from the configured storage backend.

    Streams chunks from storage instead of buffering the full payload in
    memory, so multi-megabyte media responses do not blow up the worker.
    """
    if ".." in object_key or chr(92) in object_key:
        raise HTTPException(status_code=400)

    try:
        chunk_iter, content_type, total_size = get_storage().open_stream(key=object_key)
    except FileNotFoundError:
        raise HTTPException(status_code=404)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    if total_size is not None:
        headers["Content-Length"] = str(total_size)
    return StreamingResponse(
        chunk_iter,
        media_type=content_type or "application/octet-stream",
        headers=headers,
    )


@app.get("/debug/storage", include_in_schema=False)
def debug_storage():
    test_key = f"debug/storage/{threading.get_ident()}.txt"
    storage = get_storage()
    payload = b"edu_viz storage debug"
    storage.save_bytes(key=test_key, data=payload, content_type="text/plain")
    read_back, content_type = storage.open_bytes(key=test_key)
    return JSONResponse(
        {
            "ok": True,
            "backend": settings.storage_backend,
            "bucket": settings.storage_s3_bucket,
            "endpoint": settings.storage_s3_endpoint_url,
            "key": test_key,
            "size": len(read_back),
            "content_type": content_type,
            "matches": read_back == payload,
        }
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
app.include_router(bulk_ai_upload_router)
app.include_router(deck_live_router)
app.include_router(multiselect_router)


@app.on_event("startup")
def init_multiselect_options() -> None:
    """Initialize multiselect component options storage."""
    app.state.multiselect_options = {}


@app.on_event("startup")
def start_background_job_worker() -> None:
    if getattr(app.state, "job_worker_started", False):
        return
    worker = threading.Thread(target=run_worker_loop, name="edu-viz-job-worker", daemon=True)
    worker.start()
    app.state.job_worker_started = True
    app.state.job_worker_thread = worker
