from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.parse import urlparse

from authlib.integrations.starlette_client import OAuth

from app.core.config import settings


@dataclass(frozen=True)
class MicrosoftIdentityConfig:
    provider_name: str
    client_id: str
    client_secret: str | None
    redirect_uri: str
    scope: str
    authority: str | None
    authorize_authority: str | None
    authorize_url: str | None
    metadata_url: str
    subject_claim: str = "sub"


@dataclass(frozen=True)
class MicrosoftIdentityConfigStatus:
    configured: bool
    provider_name: str
    missing: tuple[str, ...]
    metadata_source: str | None

    @property
    def message(self) -> str:
        if self.configured:
            return f"{self.provider_name} OIDC is configured."
        return (
            f"{self.provider_name} OIDC is not configured. Set "
            + ", ".join(self.missing)
            + "."
        )


def _normalize_url(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().rstrip("/")


def _legacy_b2c_authority() -> str | None:
    tenant = settings.azure_b2c_tenant_domain
    if not tenant and settings.azure_b2c_tenant_name:
        tenant = f"{settings.azure_b2c_tenant_name}.onmicrosoft.com"

    if tenant and settings.azure_b2c_policy:
        return f"https://{tenant}/{settings.azure_b2c_policy}/v2.0"
    return None


def _resolve_authority() -> str | None:
    authority = _normalize_url(settings.microsoft_entra_external_id_authority)
    if authority:
        return authority
    return _normalize_url(_legacy_b2c_authority())


def _metadata_is_tenant_scoped_microsoft(metadata_url: str | None) -> bool:
    if not metadata_url:
        return False

    parsed = urlparse(metadata_url)
    if parsed.netloc.lower() != "login.microsoftonline.com":
        return False

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return False

    return segments[0].lower() not in {"common", "organizations", "consumers"}


def _is_broad_microsoft_authority(authority: str | None) -> bool:
    if not authority:
        return False

    parsed = urlparse(authority)
    if parsed.netloc.lower() != "login.microsoftonline.com":
        return False

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments:
        return False

    return segments[0].lower() in {"common", "organizations", "consumers"}


def _resolve_authorize_authority(authority: str | None, metadata_url: str | None) -> str | None:
    explicit = _normalize_url(settings.microsoft_entra_external_id_authorize_authority)
    if explicit:
        if _is_broad_microsoft_authority(explicit) and _metadata_is_tenant_scoped_microsoft(metadata_url):
            return None
        return explicit
    return authority


def _resolve_authorize_url(authorize_authority: str | None) -> str | None:
    if not authorize_authority:
        return None

    parsed = urlparse(authorize_authority)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if segments and segments[-1].lower() == "v2.0":
        segments = segments[:-1]

    authorize_path = "/" + "/".join(segments + ["oauth2", "v2.0", "authorize"])
    return parsed._replace(path=authorize_path).geturl().rstrip("/")


def _resolve_metadata_url(authority: str | None) -> tuple[str | None, str | None]:
    explicit = _normalize_url(settings.microsoft_entra_external_id_metadata_url)
    if explicit:
        return explicit, "MICROSOFT_ENTRA_EXTERNAL_ID_METADATA_URL"
    if authority:
        return f"{authority}/.well-known/openid-configuration", "authority"
    return None, None


def _decode_jwt_claims_without_verification(id_token: str | None) -> dict[str, object]:
    if not id_token:
        return {}

    parts = id_token.split(".")
    if len(parts) < 2:
        return {}

    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}

    return claims if isinstance(claims, dict) else {}


def build_claims_options(metadata_issuer: str | None) -> dict[str, dict[str, list[str]]] | None:
    metadata_issuer = _normalize_url(metadata_issuer)
    if not metadata_issuer:
        return None

    if "{tenantid}" in metadata_issuer:
        return {}

    return {"iss": {"values": [metadata_issuer]}}


def validate_userinfo_issuer(metadata_issuer: str | None, userinfo: dict[str, object] | None) -> bool:
    metadata_issuer = _normalize_url(metadata_issuer)
    if not metadata_issuer or not isinstance(userinfo, dict):
        return False

    issuer = userinfo.get("iss")
    if not isinstance(issuer, str):
        return False

    if "{tenantid}" not in metadata_issuer:
        return issuer == metadata_issuer

    tenant_id = userinfo.get("tid")
    if not isinstance(tenant_id, str):
        return False

    expected_issuer = metadata_issuer.replace("{tenantid}", tenant_id)
    return issuer == expected_issuer


def get_identity_config_status() -> MicrosoftIdentityConfigStatus:
    client_id = (
        settings.microsoft_entra_external_id_client_id
        or settings.azure_b2c_client_id
    )
    authority = _resolve_authority()
    metadata_url, metadata_source = _resolve_metadata_url(authority)

    missing: list[str] = []
    if not client_id:
        missing.append("MICROSOFT_ENTRA_EXTERNAL_ID_CLIENT_ID")
    if not metadata_url:
        missing.append(
            "MICROSOFT_ENTRA_EXTERNAL_ID_METADATA_URL or MICROSOFT_ENTRA_EXTERNAL_ID_AUTHORITY"
        )

    return MicrosoftIdentityConfigStatus(
        configured=not missing,
        provider_name="Microsoft Entra External ID",
        missing=tuple(missing),
        metadata_source=metadata_source,
    )


def load_identity_config() -> MicrosoftIdentityConfig:
    status = get_identity_config_status()
    if not status.configured:
        raise RuntimeError(status.message)

    authority = _resolve_authority()
    metadata_url, _ = _resolve_metadata_url(authority)
    assert metadata_url is not None
    authorize_authority = _resolve_authorize_authority(authority, metadata_url)

    return MicrosoftIdentityConfig(
        provider_name=status.provider_name,
        client_id=(
            settings.microsoft_entra_external_id_client_id
            or settings.azure_b2c_client_id
            or ""
        ),
        client_secret=(
            settings.microsoft_entra_external_id_client_secret
            or settings.azure_b2c_client_secret
        ),
        redirect_uri=(
            settings.microsoft_entra_external_id_redirect_uri
            or settings.azure_b2c_redirect_uri
            or "http://localhost:8000/auth/callback"
        ),
        scope=settings.microsoft_entra_external_id_scopes,
        authority=authority,
        authorize_authority=authorize_authority,
        authorize_url=_resolve_authorize_url(authorize_authority),
        metadata_url=metadata_url,
    )


def build_oauth() -> OAuth:
    cfg = load_identity_config()

    oauth = OAuth()
    oauth.register(
        name="microsoft",
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        server_metadata_url=cfg.metadata_url,
        authorize_url=cfg.authorize_url,
        client_kwargs={"scope": cfg.scope},
        redirect_uri=cfg.redirect_uri,
    )
    return oauth
