from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from starlette.responses import RedirectResponse

from app.api.routers import auth, pages
from tests.test_dashboard_routes import make_request


def test_home_for_anonymous_user_shows_marketing_nav_only():
    response = pages.home(make_request(path="/"), user=None)

    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "Sign in to continue" in body
    assert ">Home<" in body
    assert ">Sign in<" in body
    assert ">Workspace<" not in body
    assert ">Review<" not in body
    assert "Logout" not in body
    assert pages.static_asset_url("vendor/htmx.min.js") in body


def test_home_redirects_authenticated_user_to_dashboard():
    user = SimpleNamespace(id=uuid4(), role="user", organization_id=None)

    response = pages.home(make_request(path="/"), user=user)

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


def test_login_redirects_authenticated_user_to_dashboard():
    user = SimpleNamespace(id=uuid4(), role="user", organization_id=None)

    response = asyncio.run(auth.login(make_request(path="/login"), user=user))

    assert isinstance(response, RedirectResponse)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


def test_login_starts_oauth_for_anonymous_user():
    oauth = SimpleNamespace(
        microsoft=SimpleNamespace(authorize_redirect=None),
    )
    redirect_response = RedirectResponse(url="https://login.example.test")

    async def fake_authorize_redirect(request, redirect_uri):
        assert redirect_uri == "https://callback.example.test"
        return redirect_response

    oauth.microsoft.authorize_redirect = fake_authorize_redirect

    with patch.object(auth, "load_identity_config", return_value=SimpleNamespace(redirect_uri="https://callback.example.test")):
        with patch.object(auth, "build_oauth", return_value=oauth):
            response = asyncio.run(auth.login(make_request(path="/login"), user=None))

    assert response is redirect_response
