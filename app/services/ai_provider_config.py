"""The app-wide AI provider/model, as an admin-editable configuration.

Every AI call in the app runs on ONE provider and ONE model. Those used to come
only from ``AI_PROVIDER``/``AI_MODEL``, so switching vendors meant editing the
deployment and recreating the container — and a half-applied switch (new
provider, key for the old one) failed every call with the generic "AI provider
did not return usable flashcards or MCQs".

This module owns the stored override. Precedence is: stored row > environment >
built-in default. The endpoint always follows the chosen provider, so provider,
model, endpoint and key are decided together instead of by independent env vars.

The provider classes call :func:`effective_model` on every pass, including from
worker threads with no session in hand, so the stored row is cached for
``CACHE_TTL_SECONDS`` and the cache is dropped whenever the setting is written.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ai_provider_setting import AIProviderSetting

CACHE_TTL_SECONDS = 60


@dataclass(frozen=True)
class ProviderOption:
    """One selectable provider, with the facts needed to warn about mismatches."""

    name: str
    label: str
    default_model: str
    key_hint: str


PROVIDER_OPTIONS: tuple[ProviderOption, ...] = (
    ProviderOption(
        "opencode",
        "OpenCode (Zen)",
        "deepseek-v4.1-flash",
        "OpenCode Zen key — 67 chars, sk-…",
    ),
    ProviderOption(
        "deepseek",
        "DeepSeek",
        "deepseek-flash",
        "DeepSeek platform key — 35 chars, sk- + 32 hex",
    ),
    ProviderOption(
        "openai",
        "OpenAI",
        "gpt-4.1-mini",
        "OpenAI API key — sk-…",
    ),
    ProviderOption(
        "minimax",
        "Minimax",
        "",
        "Minimax API key",
    ),
    ProviderOption(
        "claude",
        "Claude",
        "",
        "Anthropic API key",
    ),
)

_OPTIONS_BY_NAME = {option.name: option for option in PROVIDER_OPTIONS}

_lock = threading.Lock()
_cache: tuple[float, str | None, str | None, str | None] = (0.0, None, None, None)


def provider_options() -> tuple[ProviderOption, ...]:
    return PROVIDER_OPTIONS


def option_for(provider: str | None) -> ProviderOption | None:
    return _OPTIONS_BY_NAME.get((provider or "").strip().lower())


def invalidate_cache() -> None:
    """Drop the cached row so the next call re-reads it (used after a write)."""
    global _cache
    with _lock:
        _cache = (0.0, None, None, None)


def _read_row(db: Session | None) -> tuple[str | None, str | None, str | None]:
    """The stored (provider, model, encrypted key); ``(None, None, None)`` if unset.

    This never raises. A worker that starts before the migration has run, or a
    database that is briefly unreachable, must fall back to the environment
    instead of failing every AI call over its configuration lookup.
    """
    if db is not None:
        try:
            row = db.execute(select(AIProviderSetting).limit(1)).scalars().first()
        except Exception:
            return None, None, None
        if row is None:
            return None, None, None
        return row.provider, row.model, row.api_key_encrypted

    global _cache
    with _lock:
        fetched_at, provider, model, key = _cache
    if fetched_at and time.time() - fetched_at < CACHE_TTL_SECONDS:
        return provider, model, key

    from app.core.db import SessionLocal

    session = None
    try:
        session = SessionLocal()
        provider, model, key = _read_row(session)
    except Exception:
        return None, None, None
    finally:
        if session is not None:
            session.close()
    with _lock:
        _cache = (time.time(), provider, model, key)
    return provider, model, key


def effective_provider(db: Session | None = None) -> str:
    """The provider every AI call should use."""
    stored_provider, _model, _key = _read_row(db)
    return (stored_provider or settings.ai_provider or "").strip().lower()


def effective_model(db: Session | None = None) -> str:
    """The model every AI call should use."""
    _provider, stored_model, _key = _read_row(db)
    return (stored_model or settings.ai_model or "").strip()


def stored_api_key(db: Session | None = None) -> str | None:
    """The API key stored with the setting, decrypted; ``None`` when unset."""
    _provider, _model, encrypted = _read_row(db)
    if not encrypted:
        return None
    from app.services.ai_auth import decrypt_secret

    return decrypt_secret(encrypted)


def endpoint_for(provider: str | None) -> str:
    """The endpoint the chosen provider will actually post to.

    ``AI_ENDPOINT``, when set, overrides the endpoint of *every* provider class —
    which is how a switch can be defeated by a stale value, so the settings page
    shows it rather than hiding it.
    """
    override = (settings.ai_endpoint or "").strip()
    if override:
        return override
    name = (provider or "").strip().lower()
    if name == "opencode":
        return settings.opencode_api_endpoint
    if name == "deepseek":
        return settings.revision_notes_api_endpoint
    option = option_for(name)
    return f"{option.label}'s built-in endpoint" if option else ""


def endpoint_override_active() -> bool:
    return bool((settings.ai_endpoint or "").strip())


def key_source(db: Session | None = None) -> str:
    """Where the key for the effective provider comes from, for display."""
    if stored_api_key(db):
        return "stored"
    env_provider = (settings.ai_provider or "").strip().lower()
    if settings.ai_api_key and env_provider == effective_provider(db):
        return "environment"
    return "unset"


def describe(db: Session | None = None) -> dict[str, object]:
    """Everything the settings page shows about the current configuration."""
    provider, model, _encrypted = _read_row(db)
    effective = effective_provider(db)
    return {
        "stored_provider": provider,
        "stored_model": model,
        "provider": effective,
        "model": effective_model(db),
        "label": (option_for(effective).label if option_for(effective) else effective),
        "endpoint": endpoint_for(effective),
        "endpoint_override": (settings.ai_endpoint or "").strip(),
        "key_source": key_source(db),
        "env_provider": (settings.ai_provider or "").strip().lower(),
        "env_model": (settings.ai_model or "").strip(),
        "source": "stored" if provider else "environment",
    }


def save_setting(
    db: Session,
    *,
    provider: str,
    model: str,
    api_key: str | None = None,
    user_email: str | None = None,
) -> AIProviderSetting:
    """Store the app-wide provider/model (and optionally a key).

    ``api_key`` is only touched when not ``None``: an empty string clears the
    stored key so the environment key applies again.
    """
    from app.services.ai_auth import encrypt_secret

    name = (provider or "").strip().lower()
    if name not in _OPTIONS_BY_NAME:
        raise ValueError(f"Unsupported AI provider: {provider!r}")
    cleaned_model = (model or "").strip()
    if not cleaned_model:
        raise ValueError("A model name is required.")

    row = db.execute(select(AIProviderSetting).limit(1)).scalars().first()
    if row is None:
        row = AIProviderSetting(singleton=True)
        db.add(row)
    row.provider = name
    row.model = cleaned_model
    if api_key is not None:
        row.api_key_encrypted = encrypt_secret(api_key.strip()) if api_key.strip() else None
    row.updated_by_email = (user_email or "").strip() or None
    db.commit()
    db.refresh(row)
    invalidate_cache()
    return row


def clear_setting(db: Session) -> bool:
    """Delete the stored override so the environment decides again."""
    row = db.execute(select(AIProviderSetting).limit(1)).scalars().first()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    invalidate_cache()
    return True
