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

import json
import threading
import time
import urllib.request
import uuid
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


# --- which models a provider serves (asked, not guessed) --------------------
#
# A model id has to match the endpoint exactly, and a mismatch is invisible until
# every call fails. Both halves are discoverable: every OpenAI-compatible vendor
# lists its models, and models.dev is a public catalog that covers the providers
# whose own listing needs a key this app may not have.

CATALOG_URL = "https://models.dev/api.json"
CATALOG_TTL_SECONDS = 3600
_HTTP_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class ModelListing:
    """How to ask one provider which model ids it serves."""

    provider: str
    catalog_id: str
    auth: str
    url: str = ""


MODEL_LISTINGS: dict[str, ModelListing] = {
    "opencode": ModelListing("opencode", "opencode", "opencode"),
    "deepseek": ModelListing("deepseek", "deepseek", "bearer"),
    "openai": ModelListing("openai", "openai", "bearer", "https://api.openai.com/v1/models"),
    "claude": ModelListing(
        "claude", "anthropic", "anthropic", "https://api.anthropic.com/v1/models"
    ),
    "minimax": ModelListing("minimax", "minimax", "bearer"),
}

_catalog_cache: tuple[float, dict] = (0.0, {})


def models_endpoint(provider: str | None) -> str:
    """Where to ask a provider for its own model ids (``""`` when unknown).

    Derived from that provider's chat endpoint rather than ``AI_ENDPOINT``, so a
    stale override cannot make this ask one vendor for another's models.
    """
    name = (provider or "").strip().lower()
    listing = MODEL_LISTINGS.get(name)
    if listing is None:
        return ""
    if listing.url:
        return listing.url
    if name == "opencode":
        chat = (settings.opencode_api_endpoint or "").strip()
    elif name == "deepseek":
        chat = (settings.revision_notes_api_endpoint or "").strip()
    else:
        return ""
    if not chat:
        return ""
    if chat.endswith("/chat/completions"):
        return chat[: -len("/chat/completions")] + "/models"
    return f"{chat.rstrip('/')}/models"


def _listing_headers(listing: ModelListing, api_key: str | None) -> dict[str, str]:
    key = (api_key or "").strip()
    user_agent = (settings.opencode_client_ua or "eduviz/1.0")
    if listing.auth == "opencode":
        session_id = f"models-{uuid.uuid4().hex[:8]}"
        if not key:
            # The listing is public, and an empty "Bearer " header is not.
            return {
                "Accept": "application/json",
                "User-Agent": user_agent,
                "x-opencode-session": session_id,
            }
        from app.services.ai_generation import _opencode_headers

        return _opencode_headers(key, session_id)
    headers = {"Accept": "application/json", "User-Agent": user_agent}
    if listing.auth == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _get_json(url: str, headers: dict[str, str], timeout: int) -> object:
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _model_ids(payload: object) -> list[str]:
    """Model ids out of either listing shape, in the order the vendor sent them.

    ``{"data": [{"id": …}]}`` (OpenAI, OpenCode and Anthropic) and
    ``{"models": [...]}`` both appear in the wild.
    """
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("models") or []
    else:
        items = payload if isinstance(payload, list) else []
    ids: list[str] = []
    for item in items:
        value = item.get("id") or item.get("name") if isinstance(item, dict) else item
        if isinstance(value, str) and value.strip() and value.strip() not in ids:
            ids.append(value.strip())
    return ids


def _catalog_models(catalog_id: str) -> list[str]:
    """Model ids from the public models.dev catalog (cached: it is ~5 MB)."""
    global _catalog_cache
    with _lock:
        fetched_at, payload = _catalog_cache
    if not payload or time.time() - fetched_at > CATALOG_TTL_SECONDS:
        fetched = _get_json(
            CATALOG_URL,
            {"Accept": "application/json", "User-Agent": settings.opencode_client_ua or "eduviz/1.0"},
            max(_HTTP_TIMEOUT_SECONDS, 30),
        )
        payload = fetched if isinstance(fetched, dict) else {}
        with _lock:
            _catalog_cache = (time.time(), payload)
    entry = payload.get(catalog_id) if isinstance(payload, dict) else None
    models = entry.get("models") if isinstance(entry, dict) else None
    if isinstance(models, dict):
        return [str(name) for name in models]
    return _model_ids(models)


def list_models(
    provider: str | None,
    *,
    api_key: str | None = None,
    timeout: int = _HTTP_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Ask the provider, then the public catalog, which model ids it serves.

    Never raises for a provider-side failure: the caller shows which source the
    list came from, because "the vendor would not tell us" and "here is the
    vendor's own list" are different answers.
    """
    name = (provider or "").strip().lower()
    listing = MODEL_LISTINGS.get(name)
    if listing is None:
        raise ValueError(f"Unsupported AI provider: {provider!r}")

    endpoint = models_endpoint(name)
    provider_error = ""
    if endpoint:
        try:
            payload = _get_json(endpoint, _listing_headers(listing, api_key), timeout)
            ids = _model_ids(payload)
            if ids:
                return {
                    "provider": name,
                    "models": ids,
                    "count": len(ids),
                    "source": "provider",
                    "endpoint": endpoint,
                    "note": "",
                }
            provider_error = "the provider listed no models"
        except Exception as exc:
            provider_error = f"{type(exc).__name__}: {exc}"[:200]

    catalog_error = ""
    try:
        ids = _catalog_models(listing.catalog_id)
    except Exception as exc:
        ids = []
        catalog_error = f"{type(exc).__name__}: {exc}"[:200]
    if ids:
        note = "public catalog (models.dev)"
        if provider_error:
            note = f"{note} — the provider's own listing said: {provider_error}"
        return {
            "provider": name,
            "models": ids,
            "count": len(ids),
            "source": "catalog",
            "endpoint": CATALOG_URL,
            "note": note,
        }
    return {
        "provider": name,
        "models": [],
        "count": 0,
        "source": "none",
        "endpoint": endpoint or CATALOG_URL,
        "note": provider_error or catalog_error or "no models found",
    }
