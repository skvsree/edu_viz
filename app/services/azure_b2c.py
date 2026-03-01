from __future__ import annotations

from dataclasses import dataclass

from authlib.integrations.starlette_client import OAuth

from app.core.config import settings


@dataclass(frozen=True)
class AzureB2CConfig:
    tenant: str
    policy: str
    client_id: str
    client_secret: str | None
    redirect_uri: str

    @property
    def authority(self) -> str:
        # For Azure AD B2C, issuer/metadata is under the tenant domain + policy.
        # Using "tenant" as full domain (xxx.onmicrosoft.com).
        return f"https://{self.tenant}/{self.policy}/v2.0"

    @property
    def metadata_url(self) -> str:
        return f"{self.authority}/.well-known/openid-configuration"


def load_b2c_config() -> AzureB2CConfig:
    tenant = settings.azure_b2c_tenant_domain
    if not tenant and settings.azure_b2c_tenant_name:
        tenant = f"{settings.azure_b2c_tenant_name}.onmicrosoft.com"

    if not tenant or not settings.azure_b2c_policy or not settings.azure_b2c_client_id:
        raise RuntimeError(
            "Azure B2C is not configured. Set AZURE_B2C_TENANT_DOMAIN (or AZURE_B2C_TENANT_NAME), "
            "AZURE_B2C_POLICY, and AZURE_B2C_CLIENT_ID."
        )

    return AzureB2CConfig(
        tenant=tenant,
        policy=settings.azure_b2c_policy,
        client_id=settings.azure_b2c_client_id,
        client_secret=settings.azure_b2c_client_secret,
        redirect_uri=settings.azure_b2c_redirect_uri,
    )


def build_oauth() -> OAuth:
    cfg = load_b2c_config()

    oauth = OAuth()
    oauth.register(
        name="azureb2c",
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        server_metadata_url=cfg.metadata_url,
        client_kwargs={"scope": "openid profile email"},
        redirect_uri=cfg.redirect_uri,
    )
    return oauth
