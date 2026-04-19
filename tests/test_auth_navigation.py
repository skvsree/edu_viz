from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from starlette.responses import RedirectResponse

from app.models import User

from app.api.routers import auth, pages
from app.services.microsoft_identity import (
    build_claims_options,
    build_oauth,
    load_identity_config,
    validate_userinfo_issuer,
)
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

    with patch.object(
        auth,
        "load_identity_config",
        return_value=SimpleNamespace(redirect_uri="https://callback.example.test"),
    ):
        with patch.object(auth, "build_oauth", return_value=oauth):
            response = asyncio.run(auth.login(make_request(path="/login"), user=None))

    assert response is redirect_response


def test_new_oidc_user_gets_tests_enabled_by_default():
    db = SimpleNamespace(
        execute=lambda stmt: SimpleNamespace(
            scalars=lambda: SimpleNamespace(first=lambda: None)
        )
    )
    added: list[User] = []

    def add(obj):
        added.append(obj)

    db.add = add
    db.commit = lambda: None

    cfg = SimpleNamespace(subject_claim="sub")
    user = auth._resolve_user_from_oidc_userinfo(
        db,
        cfg,
        {"sub": "new-user-sub", "email": "new@example.com"},
    )

    assert isinstance(user, User)
    assert user.is_test_enabled is True
    assert added and added[0].is_test_enabled is True


def test_build_oauth_uses_split_authorize_url():
    cfg = SimpleNamespace(
        client_id="client-id",
        client_secret="client-secret",
        metadata_url="https://login.microsoftonline.com/tenant/v2.0/.well-known/openid-configuration",
        authorize_url="https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize",
        scope="openid profile email",
        redirect_uri="https://callback.example.test",
    )

    with patch(
        "app.services.microsoft_identity.load_identity_config", return_value=cfg
    ):
        oauth = build_oauth()

    client = oauth.create_client("microsoft")
    assert client._server_metadata_url == cfg.metadata_url
    assert client.authorize_url == cfg.authorize_url


def test_load_identity_config_derives_separate_authorize_url():
    with patch(
        "app.services.microsoft_identity.settings",
        SimpleNamespace(
            microsoft_entra_external_id_client_id="client-id",
            microsoft_entra_external_id_client_secret="client-secret",
            microsoft_entra_external_id_redirect_uri="https://callback.example.test",
            microsoft_entra_external_id_scopes="openid profile email",
            microsoft_entra_external_id_authority=(
                "https://contoso.ciamlogin.com/contoso.onmicrosoft.com"
            ),
            microsoft_entra_external_id_authorize_authority=(
                "https://login.contoso.com/custom-authority"
            ),
            microsoft_entra_external_id_metadata_url=(
                "https://contoso.ciamlogin.com/contoso.onmicrosoft.com/"
                "v2.0/.well-known/openid-configuration"
            ),
            azure_b2c_client_id=None,
            azure_b2c_client_secret=None,
            azure_b2c_redirect_uri=None,
            azure_b2c_tenant_domain=None,
            azure_b2c_tenant_name=None,
            azure_b2c_policy=None,
        ),
    ):
        cfg = load_identity_config()

    assert (
        cfg.metadata_url
        == "https://contoso.ciamlogin.com/contoso.onmicrosoft.com/v2.0/.well-known/openid-configuration"
    )
    assert (
        cfg.authorize_url
        == "https://login.contoso.com/custom-authority/oauth2/v2.0/authorize"
    )


def test_load_identity_config_ignores_broad_authorize_authority_when_metadata_is_tenant_scoped():
    with patch(
        "app.services.microsoft_identity.settings",
        SimpleNamespace(
            microsoft_entra_external_id_client_id="client-id",
            microsoft_entra_external_id_client_secret="client-secret",
            microsoft_entra_external_id_redirect_uri="https://callback.example.test",
            microsoft_entra_external_id_scopes="openid profile email",
            microsoft_entra_external_id_authority=None,
            microsoft_entra_external_id_authorize_authority=(
                "https://login.microsoftonline.com/organizations"
            ),
            microsoft_entra_external_id_metadata_url=(
                "https://login.microsoftonline.com/tenant-id/"
                "v2.0/.well-known/openid-configuration"
            ),
            azure_b2c_client_id=None,
            azure_b2c_client_secret=None,
            azure_b2c_redirect_uri=None,
            azure_b2c_tenant_domain=None,
            azure_b2c_tenant_name=None,
            azure_b2c_policy=None,
        ),
    ):
        cfg = load_identity_config()

    assert (
        cfg.metadata_url
        == "https://login.microsoftonline.com/tenant-id/v2.0/.well-known/openid-configuration"
    )
    assert cfg.authorize_url is None


def test_load_identity_config_keeps_common_authority_aligned_for_authorize_and_metadata():
    with patch(
        "app.services.microsoft_identity.settings",
        SimpleNamespace(
            microsoft_entra_external_id_client_id="client-id",
            microsoft_entra_external_id_client_secret="client-secret",
            microsoft_entra_external_id_redirect_uri="https://callback.example.test",
            microsoft_entra_external_id_scopes="openid profile email",
            microsoft_entra_external_id_authority="https://login.microsoftonline.com/common/v2.0",
            microsoft_entra_external_id_authorize_authority=None,
            microsoft_entra_external_id_metadata_url=None,
            azure_b2c_client_id=None,
            azure_b2c_client_secret=None,
            azure_b2c_redirect_uri=None,
            azure_b2c_tenant_domain=None,
            azure_b2c_tenant_name=None,
            azure_b2c_policy=None,
        ),
    ):
        cfg = load_identity_config()

    assert (
        cfg.metadata_url
        == "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration"
    )
    assert (
        cfg.authorize_url
        == "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    )


def test_build_claims_options_skips_literal_issuer_check_for_common_metadata():
    assert (
        build_claims_options("https://login.microsoftonline.com/{tenantid}/v2.0") == {}
    )


def test_validate_userinfo_issuer_accepts_common_metadata_template_with_tid():
    assert validate_userinfo_issuer(
        "https://login.microsoftonline.com/{tenantid}/v2.0",
        {
            "iss": "https://login.microsoftonline.com/96b2d897-3d2e-4432-b244-03ce5c7bedfa/v2.0",
            "tid": "96b2d897-3d2e-4432-b244-03ce5c7bedfa",
        },
    )


def test_validate_userinfo_issuer_rejects_mismatched_common_metadata_issuer():
    assert not validate_userinfo_issuer(
        "https://login.microsoftonline.com/{tenantid}/v2.0",
        {
            "iss": "https://login.microsoftonline.com/other-tenant/v2.0",
            "tid": "96b2d897-3d2e-4432-b244-03ce5c7bedfa",
        },
    )
