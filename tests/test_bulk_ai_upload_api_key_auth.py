"""The bulk-import API key must also authenticate the bulk AI upload surface.

The web UI authenticates with the session cookie; scripts and the NCERT importer
used to have to forge that cookie because the API key only covered
``/api/v1/import/*``. These tests pin the additive rule:

* a valid session still wins,
* without a session, ``X-Api-Key`` authenticates as the system admin,
* bad/anonymous requests keep their old 401/403/503 shape,
* every bulk-AI-upload route (and the folder routes an importer needs) accepts
  the key, while destructive folder routes stay session-only.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import deps
from app.api.deps import bulk_import_or_session_user
from app.main import app
from app.services.access import ROLE_SYSTEM_ADMIN


class FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def first(self):
        return self._items[0] if self._items else None


class FakeDB:
    def __init__(self, admin=None):
        self.admin = admin

    def execute(self, stmt):
        if "FROM users" in str(stmt):
            return FakeResult([self.admin] if self.admin else [])
        return FakeResult([])


def _admin():
    return SimpleNamespace(
        id=uuid4(),
        role=ROLE_SYSTEM_ADMIN,
        organization_id=None,
        created_at=None,
    )


def _no_session(monkeypatch):
    monkeypatch.setattr(deps, "_resolve_user", lambda session, db: None)


def test_api_key_authenticates_as_the_system_admin(monkeypatch):
    monkeypatch.setattr(deps.settings, "bulk_import_api_key", "secret")
    _no_session(monkeypatch)
    admin = _admin()

    user = bulk_import_or_session_user(
        session=None, x_api_key="secret", db=FakeDB(admin=admin)
    )

    assert user is admin


def test_session_cookie_still_wins_and_skips_the_key(monkeypatch):
    monkeypatch.setattr(deps.settings, "bulk_import_api_key", "secret")
    session_user = SimpleNamespace(id=uuid4(), role="admin", organization_id=None)
    monkeypatch.setattr(deps, "_resolve_user", lambda session, db: session_user)

    user = bulk_import_or_session_user(
        session="signed-cookie", x_api_key="wrong", db=FakeDB(admin=_admin())
    )

    assert user is session_user


def test_wrong_api_key_is_forbidden(monkeypatch):
    monkeypatch.setattr(deps.settings, "bulk_import_api_key", "secret")
    _no_session(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        bulk_import_or_session_user(
            session=None, x_api_key="wrong", db=FakeDB(admin=_admin())
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid API key"


def test_anonymous_request_is_unauthorized(monkeypatch):
    monkeypatch.setattr(deps.settings, "bulk_import_api_key", "secret")
    _no_session(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        bulk_import_or_session_user(session=None, x_api_key=None, db=FakeDB(admin=_admin()))

    assert exc.value.status_code == 401
    assert exc.value.detail == "not authenticated"


def test_api_key_without_a_configured_key_is_unavailable(monkeypatch):
    monkeypatch.setattr(deps.settings, "bulk_import_api_key", None)
    _no_session(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        bulk_import_or_session_user(session=None, x_api_key="secret", db=FakeDB())

    assert exc.value.status_code == 503
    assert exc.value.detail == "Bulk import API is not configured"


def test_api_key_without_a_system_admin_is_unavailable(monkeypatch):
    monkeypatch.setattr(deps.settings, "bulk_import_api_key", "secret")
    _no_session(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        bulk_import_or_session_user(session=None, x_api_key="secret", db=FakeDB(admin=None))

    assert exc.value.status_code == 503
    assert exc.value.detail == "No system admin is available for bulk import"


def _dependency_calls(route) -> set:
    found = set()
    dependant = getattr(route, "dependant", None)
    stack = list(getattr(dependant, "dependencies", []) or [])
    while stack:
        dep = stack.pop()
        call = getattr(dep, "call", None)
        if call is not None:
            found.add(call)
        stack.extend(getattr(dep, "dependencies", []) or [])
    return found


def _routes(methods: set[str], path_prefix: str = "", path_exact: str = ""):
    matched = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if path_exact and path != path_exact:
            continue
        if path_prefix and not path.startswith(path_prefix):
            continue
        if methods.isdisjoint(getattr(route, "methods", set()) or set()):
            continue
        matched.append(route)
    return matched


def _is_bulk_route(path: str) -> bool:
    return path.startswith("/api/v1/bulk-ai-upload") or path.endswith("/ai-import/start")


def test_every_bulk_ai_upload_route_accepts_the_api_key():
    routes = [
        route
        for route in app.routes
        if _is_bulk_route(getattr(route, "path", ""))
        and not {"GET", "POST"}.isdisjoint(getattr(route, "methods", set()) or set())
    ]

    assert len(routes) >= 8, f"expected the whole bulk-ai-upload surface, got {len(routes)}"
    for route in routes:
        assert bulk_import_or_session_user in _dependency_calls(route), (
            f"{sorted(route.methods)} {route.path} no longer accepts the bulk import API key"
        )


def test_jobs_route_accepts_the_api_key():
    routes = _routes({"GET"}, path_exact="/api/v1/jobs")

    assert routes, "/api/v1/jobs is missing"
    assert bulk_import_or_session_user in _dependency_calls(routes[0])


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/v1/folders"),
        ("GET", "/api/v1/folders/{folder_id}"),
        ("GET", "/api/v1/folders/{folder_id}/subfolders"),
        ("GET", "/api/v1/folders/{folder_id}/breadcrumb"),
        ("GET", "/api/v1/folders/{folder_id}/decks"),
        ("GET", "/api/v1/folders/tree"),
        ("POST", "/api/v1/folders"),
    ],
)
def test_folder_routes_an_importer_needs_accept_the_api_key(method, path):
    routes = _routes({method}, path_exact=path)

    assert routes, f"{method} {path} is missing"
    assert bulk_import_or_session_user in _dependency_calls(routes[0])


@pytest.mark.parametrize(
    "method,path",
    [
        ("PUT", "/api/v1/folders/{folder_id}"),
        ("DELETE", "/api/v1/folders/{folder_id}"),
        ("PUT", "/api/v1/folders/{folder_id}/move"),
        ("POST", "/api/v1/decks/move"),
    ],
)
def test_destructive_folder_routes_stay_session_only(method, path):
    routes = _routes({method}, path_exact=path)

    assert routes, f"{method} {path} is missing"
    assert bulk_import_or_session_user not in _dependency_calls(routes[0])
