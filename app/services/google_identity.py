from __future__ import annotations

from dataclasses import dataclass

from authlib.integrations.starlette_client import OAuth

from app.core.config import settings


@dataclass(frozen=True)
class GoogleIdentityConfig:
    provider_name: str
    client_id: str
    client_secret: str | None
    redirect_uri: str
    scope: str
    metadata_url: str
    subject_claim: str = "sub"


@dataclass(frozen=True)
class GoogleIdentityConfigStatus:
    configured: bool
    provider_name: str
    missing: tuple[str, ...]

    @property
    def message(self) -> str:
        if self.configured:
            return f"{self.provider_name} OIDC is configured."
        return f"{self.provider_name} OIDC is not configured. Set " + ", ".join(self.missing) + "."


def get_google_identity_config_status() -> GoogleIdentityConfigStatus:
    missing: list[str] = []
    if not (settings.google_client_id or "").strip():
        missing.append("GOOGLE_CLIENT_ID")
    if not (settings.google_client_secret or "").strip():
        missing.append("GOOGLE_CLIENT_SECRET")
    if not (settings.google_redirect_uri or "").strip():
        missing.append("GOOGLE_REDIRECT_URI")

    return GoogleIdentityConfigStatus(
        configured=not missing,
        provider_name="Google",
        missing=tuple(missing),
    )


def load_google_identity_config() -> GoogleIdentityConfig:
    status = get_google_identity_config_status()
    if not status.configured:
        raise RuntimeError(status.message)

    return GoogleIdentityConfig(
        provider_name=status.provider_name,
        client_id=settings.google_client_id or "",
        client_secret=settings.google_client_secret,
        redirect_uri=settings.google_redirect_uri or "",
        scope=settings.google_scopes,
        metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    )


def build_google_oauth() -> OAuth:
    cfg = load_google_identity_config()

    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        server_metadata_url=cfg.metadata_url,
        client_kwargs={"scope": cfg.scope},
        redirect_uri=cfg.redirect_uri,
    )
    return oauth
