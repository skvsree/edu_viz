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
    """The environment key, for the provider that key actually belongs to.

    ``AI_API_KEY`` is only valid on the endpoint ``AI_PROVIDER`` names, so the
    comparison is against the *environment* provider, not the effective one: a
    stored override that switches provider must not borrow the old provider's
    key (that is what posted a DeepSeek key to Zen and 401'd every call).
    """
    provider = provider.strip().lower()
    env_provider = (settings.ai_provider or "").strip().lower()
    if settings.ai_api_key and provider == env_provider:
        return ResolvedAICredential(
            provider=env_provider,
            auth_type="api_key",
            secret=settings.ai_api_key,
            source="env",
        )
    return None


def _app_credential(provider: str) -> ResolvedAICredential | None:
    """The key stored on the settings page for this exact provider, if any.

    Keys are stored per provider, so this cannot hand a provider a key that was
    entered for a different one.
    """
    from app.services.ai_provider_config import stored_api_key

    name = provider.strip().lower()
    secret = stored_api_key(provider=name)
    if not secret:
        return None
    return ResolvedAICredential(
        provider=name,
        auth_type="api_key",
        secret=secret,
        source="app",
    )


def get_env_ai_provider_name() -> str | None:
    return settings.ai_provider.strip().lower() if settings.ai_api_key else None


def is_env_ai_available(provider: str = "openai") -> bool:
    return _env_credential(provider) is not None


def has_scope_credential(db: Session, scope_type: str, scope_id) -> bool:
    """Check if a scope (user/org) has any AI credential stored."""
    if not hasattr(db, "query"):
        return False
    return (
        db.query(AICredentialScope)
        .filter_by(scope_type=scope_type, scope_id=str(scope_id))
        .first()
        is not None
    )


def get_scope_provider(db: Session, scope_type: str, scope_id, default: str = "openai") -> str | None:
    """Return the provider name for a scope's stored credential, or default if env-based."""
    if not hasattr(db, "query"):
        return default if is_env_ai_available() else None
    cred = db.query(AICredentialScope).filter_by(scope_type=scope_type, scope_id=str(scope_id)).first()
    if cred:
        return cred.provider
    return default if is_env_ai_available() else None


def resolve_ai_credential(db: Session, user: User, provider: str, *, allow_env: bool = True) -> AIResolution:
    provider = provider.strip().lower()
    if not provider:
        return AIResolution(None, reason="No provider specified.")

    env_cred = _env_credential(provider)
    app_cred = _app_credential(provider)

    if getattr(user, "id", None):
        user_cred = db.query(AICredentialScope).filter_by(
            scope_type="user",
            scope_id=user.id,
            provider=provider,
        ).first()
        if user_cred:
            return AIResolution(
                credential=ResolvedAICredential(
                    provider=user_cred.provider,
                    auth_type=user_cred.auth_type,
                    secret=decrypt_secret(user_cred.secret_encrypted),
                    refresh_token=(
                        decrypt_secret(user_cred.refresh_token_encrypted)
                        if user_cred.refresh_token_encrypted
                        else None
                    ),
                    source="user",
                ),
                source="user",
                scope="user",
                allowed=True,
            )

    org = db.get(Organization, user.organization_id) if getattr(user, "organization_id", None) else None
    if org and org.is_ai_enabled:
        org_cred = db.query(AICredentialScope).filter_by(
            scope_type="organization",
            scope_id=org.id,
            provider=provider,
        ).first()
        if org_cred:
            return AIResolution(
                credential=ResolvedAICredential(
                    provider=org_cred.provider,
                    auth_type=org_cred.auth_type,
                    secret=decrypt_secret(org_cred.secret_encrypted),
                    refresh_token=(
                        decrypt_secret(org_cred.refresh_token_encrypted)
                        if org_cred.refresh_token_encrypted
                        else None
                    ),
                    source="organization",
                ),
                source="organization",
                scope="organization",
                allowed=True,
            )
        if app_cred and allow_env:
            return AIResolution(
                credential=app_cred, source="app", scope="app", allowed=True
            )
        if env_cred and allow_env:
            return AIResolution(credential=env_cred, source="env", scope="organization", allowed=True)
        return AIResolution(
            None,
            source="organization",
            scope="organization",
            allowed=False,
            reason=(
                "Your organization is AI-enabled, but no usable provider is configured."
            ),
        )

    if app_cred and allow_env:
        return AIResolution(credential=app_cred, source="app", scope="app", allowed=True)

    if env_cred and allow_env:
        return AIResolution(credential=env_cred, source="env", scope="env", allowed=True)

    return AIResolution(None, reason="No AI credential configured for your user, organization, or environment.")


def resolve_configured_ai_credential(
    db: Session, user: User, *, allow_env: bool = True
) -> AIResolution:
    """Resolve a credential for the provider the app is configured to use.

    One provider, one model: AI_MODEL names a model on the configured
    provider's endpoint, so a call site must never pick its own provider from
    a per-user or per-org setting - a scope that selected another vendor would
    be sent a model id that vendor does not serve. Scope credentials are still
    honoured *for that provider*, so bring-your-own-key keeps working.
    """
    from app.services.ai_provider_config import effective_provider

    provider = effective_provider(db)
    if not provider:
        return AIResolution(
            None,
            reason=(
                "No AI provider configured "
                "(set AI_PROVIDER or choose one in Settings → AI provider)."
            ),
        )
    return resolve_ai_credential(db, user, provider, allow_env=allow_env)


def save_ai_credential(
    db: Session,
    *,
    scope_type: str,
    scope_id,
    provider: str,
    secret: str,
    auth_type: str = "api_key",
    refresh_token: str | None = None,
    metadata_json: str | None = None,
) -> AICredentialScope:
    provider = provider.strip().lower()
    existing = db.query(AICredentialScope).filter_by(
        scope_type=scope_type,
        scope_id=scope_id,
        provider=provider,
    ).first()
    if existing is None:
        existing = AICredentialScope(
            scope_type=scope_type,
            scope_id=scope_id,
            provider=provider,
            auth_type=auth_type,
            secret_encrypted=encrypt_secret(secret),
            refresh_token_encrypted=(
                encrypt_secret(refresh_token) if refresh_token else None
            ),
            metadata_json=metadata_json,
        )
        db.add(existing)
    else:
        existing.auth_type = auth_type
        existing.secret_encrypted = encrypt_secret(secret)
        existing.refresh_token_encrypted = encrypt_secret(refresh_token) if refresh_token else None
        existing.metadata_json = metadata_json
    return existing
