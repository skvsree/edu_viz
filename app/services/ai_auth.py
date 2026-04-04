from __future__ import annotations

from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AICredentialScope, Organization, User


class AIAuthError(RuntimeError):
    pass


@dataclass(slots=True)
class ResolvedAICredential:
    provider: str
    auth_type: str
    secret: str
    refresh_token: str | None = None
    source: str = "env"


@dataclass(slots=True)
class AIResolution:
    credential: ResolvedAICredential | None
    source: str | None = None
    scope: str | None = None
    allowed: bool = False
    reason: str | None = None


def _fernet() -> Fernet:
    key = getattr(settings, "ai_secrets_fernet_key", None)
    if not key:
        raise AIAuthError("AI secrets encryption key is not configured.")
    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise AIAuthError("Unable to decrypt AI credential.") from exc


def _env_credential(provider: str) -> ResolvedAICredential | None:
    provider = provider.strip().lower()
    if provider == "openai" and settings.openai_api_key:
        return ResolvedAICredential(provider="openai", auth_type="api_key", secret=settings.openai_api_key, source="env")
    return None


def get_env_ai_provider_name() -> str | None:
    return "openai" if settings.openai_api_key else None


def is_env_ai_available(provider: str = "openai") -> bool:
    return _env_credential(provider) is not None


def has_scope_credential(db: Session, scope_type: str, scope_id) -> bool:
    """Check if a scope (user/org) has any AI credential stored."""
    return db.query(AICredentialScope).filter_by(scope_type=scope_type, scope_id=str(scope_id)).first() is not None


def get_scope_provider(db: Session, scope_type: str, scope_id, default: str = "openai") -> str | None:
    """Return the provider name for a scope's stored credential, or default if env-based."""
    cred = db.query(AICredentialScope).filter_by(scope_type=scope_type, scope_id=str(scope_id)).first()
    if cred:
        return cred.provider
    return default if is_env_ai_available() else None



def resolve_ai_credential(db: Session, user: User, provider: str, *, allow_env: bool = True) -> AIResolution:
    provider = provider.strip().lower()
    if not provider:
        return AIResolution(None, reason="No provider specified.")

    env_cred = _env_credential(provider)

    if getattr(user, "id", None):
        user_cred = db.query(AICredentialScope).filter_by(scope_type="user", scope_id=user.id, provider=provider).first()
        if user_cred:
            return AIResolution(
                credential=ResolvedAICredential(
                    provider=user_cred.provider,
                    auth_type=user_cred.auth_type,
                    secret=decrypt_secret(user_cred.secret_encrypted),
                    refresh_token=decrypt_secret(user_cred.refresh_token_encrypted) if user_cred.refresh_token_encrypted else None,
                    source="user",
                ),
                source="user",
                scope="user",
                allowed=True,
            )

    org = db.get(Organization, user.organization_id) if getattr(user, "organization_id", None) else None
    if org and org.is_ai_enabled:
        org_cred = db.query(AICredentialScope).filter_by(scope_type="organization", scope_id=org.id, provider=provider).first()
        if org_cred:
            return AIResolution(
                credential=ResolvedAICredential(
                    provider=org_cred.provider,
                    auth_type=org_cred.auth_type,
                    secret=decrypt_secret(org_cred.secret_encrypted),
                    refresh_token=decrypt_secret(org_cred.refresh_token_encrypted) if org_cred.refresh_token_encrypted else None,
                    source="organization",
                ),
                source="organization",
                scope="organization",
                allowed=True,
            )
        if env_cred and allow_env:
            return AIResolution(credential=env_cred, source="env", scope="organization", allowed=True)
        return AIResolution(None, source="organization", scope="organization", allowed=False, reason="Your organization is AI-enabled, but no usable provider is configured.")

    if env_cred and allow_env:
        return AIResolution(credential=env_cred, source="env", scope="env", allowed=True)

    return AIResolution(None, reason="No AI credential configured for your user, organization, or environment.")


def save_ai_credential(db: Session, *, scope_type: str, scope_id, provider: str, secret: str, auth_type: str = "api_key", refresh_token: str | None = None, metadata_json: str | None = None) -> AICredentialScope:
    provider = provider.strip().lower()
    existing = db.query(AICredentialScope).filter_by(scope_type=scope_type, scope_id=scope_id, provider=provider).first()
    if existing is None:
        existing = AICredentialScope(scope_type=scope_type, scope_id=scope_id, provider=provider, auth_type=auth_type, secret_encrypted=encrypt_secret(secret), refresh_token_encrypted=encrypt_secret(refresh_token) if refresh_token else None, metadata_json=metadata_json)
        db.add(existing)
    else:
        existing.auth_type = auth_type
        existing.secret_encrypted = encrypt_secret(secret)
        existing.refresh_token_encrypted = encrypt_secret(refresh_token) if refresh_token else None
        existing.metadata_json = metadata_json
    return existing
