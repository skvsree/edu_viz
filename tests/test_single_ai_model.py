"""One provider, one model: every AI call site must resolve the same model.

The app used to carry a model per call site (``OPENCODE_MODEL`` for bulk study
packs, ``REVISION_NOTES_MODEL`` for revision notes) plus a hardcoded
``provider_name = "deepseek"`` for the revision-notes job. That let the two
drift apart, so a model change fixed bulk generation while revision notes kept
running on something else. These tests pin the collapse into a single AI_MODEL.
"""
from pathlib import Path

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    """Build Settings isolated from the developer's .env file."""
    return Settings(_env_file=None, _env_parse_none_str="", **overrides)


def test_ai_model_governs_both_call_sites():
    s = _settings(
        ai_model="deepseek-v4.1-flash",
        opencode_model="glm-5.3-flash",
        revision_notes_model="deepseek-v4-pro",
    )
    assert s.ai_model == "deepseek-v4.1-flash"
    assert s.opencode_model == "deepseek-v4.1-flash"
    assert s.revision_notes_model == "deepseek-v4.1-flash"


def test_legacy_opencode_model_seeds_ai_model_when_unset():
    """An older .env that only set OPENCODE_MODEL keeps resolving."""
    s = _settings(
        ai_model="",
        opencode_model="glm-5.3-flash",
        revision_notes_model="deepseek-v4-pro",
    )
    assert s.ai_model == "glm-5.3-flash"
    assert s.opencode_model == "glm-5.3-flash"
    assert s.revision_notes_model == "glm-5.3-flash"


def test_legacy_revision_notes_model_seeds_when_opencode_model_absent():
    s = _settings(ai_model="", opencode_model="", revision_notes_model="deepseek-v4-pro")
    assert s.ai_model == "deepseek-v4-pro"
    assert s.opencode_model == "deepseek-v4-pro"
    assert s.revision_notes_model == "deepseek-v4-pro"


def test_ai_model_defaults_when_nothing_is_configured():
    s = _settings(ai_model="", opencode_model="", revision_notes_model="")
    assert s.ai_model == "deepseek-v4.1-flash"
    assert s.opencode_model == s.revision_notes_model == "deepseek-v4.1-flash"


def test_default_model_follows_the_provider():
    """A model id is only valid on the endpoint that serves it.

    DeepSeek's platform names it ``deepseek-flash``; OpenCode Zen names it
    ``deepseek-v4.1-flash``. Defaulting to the Zen id while pointing at
    DeepSeek fails every call with "Model is unavailable".
    """
    on_deepseek = _settings(ai_provider="deepseek", ai_model="", opencode_model="",
                            revision_notes_model="")
    assert on_deepseek.ai_model == "deepseek-flash"

    on_opencode = _settings(ai_provider="opencode", ai_model="", opencode_model="",
                            revision_notes_model="")
    assert on_opencode.ai_model == "deepseek-v4.1-flash"


def test_explicit_ai_model_beats_the_provider_default():
    s = _settings(ai_provider="deepseek", ai_model="deepseek-v4-pro")
    assert s.ai_model == "deepseek-v4-pro"


def test_whitespace_only_ai_model_falls_through_to_legacy():
    s = _settings(ai_model="   ", opencode_model="glm-5.3-flash")
    assert s.ai_model == "glm-5.3-flash"


def test_both_providers_expose_the_same_model():
    """The two provider classes must not disagree about the model."""
    from app.core.config import settings
    from app.services.ai_generation import get_study_pack_provider

    for name in ("opencode", "deepseek"):
        provider = get_study_pack_provider(name, session_id=f"test-{name}")
        assert provider.model == settings.ai_model, name


def test_study_card_system_prompt_is_the_default():
    from app.services.ai_generation import (
        STUDY_CARD_SYSTEM_PROMPT,
        OpencodeStudyPackProvider,
        _UNSET,
    )

    msgs = OpencodeStudyPackProvider._messages_for("the prompt", _UNSET)
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == STUDY_CARD_SYSTEM_PROMPT
    assert msgs[-1] == {"role": "user", "content": "the prompt"}


def test_system_prompt_none_reproduces_the_revision_notes_shape():
    """Revision notes is not a study-card ask: it must keep a bare user turn.

    Routing it through the shared provider without this produced a request that
    asked a topics prompt to "return flashcards and mcqs".
    """
    from app.services.ai_generation import OpencodeStudyPackProvider

    msgs = OpencodeStudyPackProvider._messages_for("the prompt", None)
    assert [m["role"] for m in msgs] == ["user"]


def test_revision_notes_gets_a_longer_ceiling_than_a_bulk_pass():
    """Regression guard for a measured capability loss.

    A revision-notes chapter response measured ~14.5K chars over ~74s. Sharing
    the bulk 60s ceiling made every call time out, and generate_ai_topics
    silently fell back to the heuristic (used_ai=False, 0 topics).
    """
    from app.core.config import settings

    assert settings.revision_notes_request_timeout > settings.opencode_request_timeout
    assert settings.revision_notes_request_timeout >= 300


def test_generation_paths_use_the_configured_provider(monkeypatch):
    """A per-scope provider setting must not redirect a generation path.

    resolve_ai_credential() looks up a credential *for a given provider*, so
    choosing the provider from a user/org scope meant MCQ and card generation
    could run on a different vendor (and a different model) than bulk uploads -
    while AI_MODEL still named a model that vendor does not serve.
    """
    from app.core.config import settings
    from app.services import ai_auth

    seen = {}

    def fake_resolve(db, user, provider, *, allow_env=True):
        seen["provider"] = provider
        return ai_auth.AIResolution(credential=None, reason="stub")

    monkeypatch.setattr(ai_auth, "resolve_ai_credential", fake_resolve)
    monkeypatch.setattr(settings, "ai_provider", "opencode", raising=False)

    ai_auth.resolve_configured_ai_credential(db=object(), user=object())
    assert seen["provider"] == "opencode"


def test_no_scope_provider_lookup_in_generation_paths():
    """Both generation modules must resolve through the configured provider.

    get_scope_provider() returns whatever provider a user/org stored; leaving a
    call to it in a generation path is how the split creeps back.
    """
    root = Path(__file__).resolve().parents[1]
    for rel in ("app/api/routers/content.py", "app/services/job_worker.py"):
        text = (root / rel).read_text()
        assert "get_scope_provider(" not in text, f"{rel} still picks a scope provider"


def test_mcq_generation_has_no_inline_provider_dispatch():
    """MCQ generation used to switch on the provider inline, hardcoding
    gpt-4.1-mini / claude-sonnet-4 / MiniMax-M3, so it could use a different
    model than the rest of the app."""
    root = Path(__file__).resolve().parents[1]
    text = (root / "app/api/routers/content.py").read_text()
    for hardcoded in ('"gpt-4.1-mini"', '"claude-sonnet-4-20250514"', '"MiniMax-M3"'):
        assert hardcoded not in text, f"{hardcoded} is still hardcoded"


# --- the endpoint must match the provider (2026-09-30 prod outage) ---------
#
# The deepseek provider took its endpoint from a setting whose default was the
# OpenCode Zen URL. So AI_PROVIDER=deepseek posted a DeepSeek-platform key AT
# ZEN, every call returned 401, and a chapter produced 0 cards in ~39s while the
# identical job on opencode produced 695. A provider name is only half of a
# provider: the endpoint has to belong to the same vendor as the key.


def test_deepseek_provider_posts_to_deepseek_not_zen():
    from app.services.ai_generation import DeepSeekRevisionProvider

    endpoint = DeepSeekRevisionProvider(session_id="test-deepseek").api_endpoint
    assert "api.deepseek.com" in endpoint
    assert "opencode.ai" not in endpoint


def test_opencode_provider_still_posts_to_zen():
    from app.services.ai_generation import OpencodeStudyPackProvider

    endpoint = OpencodeStudyPackProvider(session_id="test-opencode").api_endpoint
    assert "opencode.ai" in endpoint


def test_ai_endpoint_overrides_every_provider(monkeypatch):
    """One AI_ENDPOINT governs whichever provider is selected."""
    from app.core.config import settings
    from app.services.ai_generation import (
        DeepSeekRevisionProvider,
        OpencodeStudyPackProvider,
    )

    override = "https://gateway.example/v1/chat/completions"
    monkeypatch.setattr(settings, "ai_endpoint", override, raising=False)
    for cls in (DeepSeekRevisionProvider, OpencodeStudyPackProvider):
        assert cls(session_id="t").api_endpoint == override, cls.__name__


def test_blank_ai_endpoint_falls_back_to_the_provider_default(monkeypatch):
    """Whitespace must not be treated as a configured endpoint."""
    from app.core.config import settings
    from app.services.ai_generation import DeepSeekRevisionProvider

    monkeypatch.setattr(settings, "ai_endpoint", "   ", raising=False)
    assert (
        DeepSeekRevisionProvider(session_id="t").api_endpoint
        == settings.revision_notes_api_endpoint
    )


# --- the provider/model is a stored setting, not an env-only fact (2026-10-03)
#
# Switching vendor used to mean editing AI_PROVIDER/AI_MODEL and recreating the
# container - and a half-applied switch (new provider, key for the old one) made
# every call fail with the generic "AI provider did not return usable flashcards
# or MCQs". The stored setting decides provider+model together, and the key rule
# below keeps a switch from borrowing the previous provider's key.


def _fernet_key(monkeypatch):
    """A throwaway encryption key so secret storage can be exercised."""
    from cryptography.fernet import Fernet

    from app.core.config import settings

    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "ai_secrets_fernet_key", key, raising=False)
    return key


def _stored(monkeypatch, provider, model, key=None):
    """Pretend the settings row holds this, without touching a database."""
    from app.services import ai_provider_config

    _fernet_key(monkeypatch)
    encrypted = None
    if key is not None:
        from app.services.ai_auth import encrypt_secret

        encrypted = encrypt_secret(key)
    monkeypatch.setattr(
        ai_provider_config,
        "_read_row",
        lambda db=None: (provider, model, encrypted),
    )
    return ai_provider_config


def test_stored_setting_overrides_the_environment(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ai_provider", "deepseek", raising=False)
    monkeypatch.setattr(settings, "ai_model", "deepseek-flash", raising=False)
    config = _stored(monkeypatch, "opencode", "deepseek-v4.1-flash")

    assert config.effective_provider() == "opencode"
    assert config.effective_model() == "deepseek-v4.1-flash"
    described = config.describe()
    assert described["source"] == "stored"
    assert "opencode.ai" in described["endpoint"], described["endpoint"]


def test_no_stored_setting_leaves_the_environment_in_charge(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "ai_provider", "deepseek", raising=False)
    monkeypatch.setattr(settings, "ai_model", "deepseek-flash", raising=False)
    config = _stored(monkeypatch, None, None)

    assert config.effective_provider() == "deepseek"
    assert config.effective_model() == "deepseek-flash"
    assert config.describe()["source"] == "environment"


def test_environment_key_is_not_borrowed_by_a_switched_provider(monkeypatch):
    """The whole point of the 2026-09-30 outage, as a regression test."""
    from app.core.config import settings
    from app.services import ai_auth

    monkeypatch.setattr(settings, "ai_provider", "deepseek", raising=False)
    monkeypatch.setattr(settings, "ai_api_key", "sk-deepseek-platform-key", raising=False)
    _stored(monkeypatch, "opencode", "deepseek-v4.1-flash")

    assert ai_auth._env_credential("deepseek") is not None
    assert ai_auth._env_credential("opencode") is None


def test_stored_key_is_offered_only_for_its_own_provider(monkeypatch):
    from app.services import ai_auth

    _stored(monkeypatch, "opencode", "deepseek-v4.1-flash", key="sk-zen-key")

    credential = ai_auth._app_credential("opencode")
    assert credential is not None and credential.source == "app"
    assert credential.secret == "sk-zen-key"
    assert ai_auth._app_credential("openai") is None


def test_stored_provider_is_the_one_generation_resolves(monkeypatch):
    from app.services import ai_auth

    _stored(monkeypatch, "opencode", "deepseek-v4.1-flash")
    seen = {}

    def fake_resolve(db, user, provider, *, allow_env=True):
        seen["provider"] = provider
        return ai_auth.AIResolution(credential=None, reason="stub")

    monkeypatch.setattr(ai_auth, "resolve_ai_credential", fake_resolve)
    ai_auth.resolve_configured_ai_credential(db=object(), user=object())
    assert seen["provider"] == "opencode"


class _Rows:
    """The subset of a SQLAlchemy result the config service uses."""

    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _RowSession:
    """Session double for the singleton row: enough for save/clear."""

    def __init__(self, row=None):
        self.row = row
        self.committed = 0
        self.deleted = []

    def execute(self, stmt):
        return _Rows([self.row] if self.row is not None else [])

    def add(self, value):
        self.row = value

    def delete(self, value):
        self.deleted.append(value)
        self.row = None

    def commit(self):
        self.committed += 1

    def refresh(self, value):
        pass


def test_saving_rejects_an_unknown_provider_or_a_blank_model():
    from app.services import ai_provider_config

    db = _RowSession()
    for kwargs in (
        {"provider": "gemini", "model": "whatever"},
        {"provider": "opencode", "model": "   "},
    ):
        try:
            ai_provider_config.save_setting(db, **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"accepted {kwargs}")


def test_saving_keeps_the_stored_key_unless_asked_to_change_it(monkeypatch):
    from app.services import ai_provider_config

    _fernet_key(monkeypatch)
    db = _RowSession()
    ai_provider_config.save_setting(
        db, provider="opencode", model="deepseek-v4.1-flash", api_key="sk-zen-key"
    )
    encrypted = db.row.api_key_encrypted

    ai_provider_config.save_setting(
        db, provider="opencode", model="deepseek-v4.1-flash", api_key=None
    )
    assert db.row.api_key_encrypted == encrypted

    ai_provider_config.save_setting(
        db, provider="opencode", model="deepseek-v4.1-flash", api_key=""
    )
    assert db.row.api_key_encrypted is None


def test_clearing_the_setting_returns_control_to_the_environment():
    from app.services import ai_provider_config

    row = ai_provider_config.AIProviderSetting(
        singleton=True, provider="opencode", model="deepseek-v4.1-flash"
    )
    db = _RowSession(row)
    assert ai_provider_config.clear_setting(db) is True
    assert ai_provider_config.clear_setting(db) is False


def test_only_system_admins_reach_the_ai_provider_page():
    from types import SimpleNamespace
    from uuid import uuid4

    from fastapi import HTTPException

    from app.api.routers import pages
    from app.services.access import ROLE_ADMIN, ROLE_SYSTEM_ADMIN
    from app.services import ai_provider_config
    from tests.test_dashboard_routes import make_request, render_body

    request = make_request("/settings/ai")

    for role in (ROLE_ADMIN, "teacher"):
        try:
            pages.settings_ai_page(
                request, user=SimpleNamespace(role=role, id=uuid4()), db=_RowSession()
            )
        except HTTPException as exc:
            assert exc.status_code == 403, role
        else:
            raise AssertionError(f"{role} reached /settings/ai")

    response = pages.settings_ai_page(
        request,
        user=SimpleNamespace(role=ROLE_SYSTEM_ADMIN, id=uuid4(), email="a@b.c"),
        db=_RowSession(),
    )
    body = render_body(response)
    assert "Choose the provider" in body
    assert "Test this configuration" in body
    assert ai_provider_config.PROVIDER_OPTIONS


# --- listing the models a provider serves (2026-10-03) ----------------------
#
# A model id must match the endpoint exactly, and the mismatch is invisible until
# every call fails. Both the vendor's own listing and the public models.dev
# catalog are reachable, so the settings page offers them instead of asking an
# admin to recall an id.


def test_model_ids_reads_both_listing_shapes():
    from app.services.ai_provider_config import _model_ids

    assert _model_ids({"data": [{"id": "a"}, {"id": "a"}, {"name": "b"}]}) == ["a", "b"]
    assert _model_ids({"models": [{"id": "c"}, "d"]}) == ["c", "d"]
    assert _model_ids({"data": []}) == []
    assert _model_ids("nonsense") == []


def test_models_endpoint_ignores_the_ai_endpoint_override(monkeypatch):
    """A stale AI_ENDPOINT must not make this ask the wrong vendor's list."""
    from app.core.config import settings
    from app.services import ai_provider_config

    monkeypatch.setattr(
        settings, "ai_endpoint", "https://gateway.example/v1/chat/completions", raising=False
    )
    assert ai_provider_config.models_endpoint("opencode").endswith("opencode.ai/zen/go/v1/models")
    assert "api.deepseek.com" in ai_provider_config.models_endpoint("deepseek")
    assert "api.openai.com" in ai_provider_config.models_endpoint("openai")
    assert "api.anthropic.com" in ai_provider_config.models_endpoint("claude")
    assert ai_provider_config.models_endpoint("minimax") == ""


def test_list_models_prefers_the_provider_own_listing(monkeypatch):
    from app.services import ai_provider_config

    monkeypatch.setattr(ai_provider_config, "_catalog_cache", (0.0, {}))
    monkeypatch.setattr(
        ai_provider_config,
        "_get_json",
        lambda url, headers, timeout: {"data": [{"id": "deepseek-v4.1-flash"}, {"id": "glm-5.3"}]},
    )
    info = ai_provider_config.list_models("opencode", api_key="sk-test")
    assert info["source"] == "provider"
    assert info["models"] == ["deepseek-v4.1-flash", "glm-5.3"]
    assert info["endpoint"].endswith("/models")


def test_list_models_falls_back_to_the_public_catalog(monkeypatch):
    from app.services import ai_provider_config

    monkeypatch.setattr(ai_provider_config, "_catalog_cache", (0.0, {}))
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(url)
        if "models.dev" in url:
            return {"openai": {"models": {"gpt-5.4": {"id": "gpt-5.4"}, "gpt-5.4-mini": {}}}}
        raise OSError("HTTP Error 401: Unauthorized")

    monkeypatch.setattr(ai_provider_config, "_get_json", fake_get)
    info = ai_provider_config.list_models("openai")
    assert info["source"] == "catalog"
    assert sorted(info["models"]) == ["gpt-5.4", "gpt-5.4-mini"]
    assert "401" in info["note"]

    before = len(calls)
    ai_provider_config.list_models("openai")
    catalog_calls = [url for url in calls if "models.dev" in url]
    assert len(catalog_calls) == 1, "the ~5 MB catalog must be cached, not re-downloaded"
    assert len(calls) == before + 1, "only the provider listing may be retried"


def test_list_models_reports_nothing_rather_than_raising(monkeypatch):
    from app.services import ai_provider_config

    monkeypatch.setattr(ai_provider_config, "_catalog_cache", (0.0, {}))
    monkeypatch.setattr(
        ai_provider_config, "_get_json", lambda url, headers, timeout: (_ for _ in ()).throw(OSError("down"))
    )
    info = ai_provider_config.list_models("claude")
    assert info["source"] == "none" and info["models"] == [] and info["note"]

    try:
        ai_provider_config.list_models("gemini")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown provider must be rejected")


def test_model_listing_route_is_admin_only_and_reports_the_source(monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4

    from fastapi import HTTPException

    from app.api.routers import pages
    from app.services import ai_provider_config
    from app.services.access import ROLE_SYSTEM_ADMIN
    from tests.test_dashboard_routes import make_request

    try:
        pages.settings_ai_models(
            provider="opencode", api_key="", user=SimpleNamespace(role="teacher"), db=_RowSession()
        )
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("a non-admin reached the model listing")

    monkeypatch.setattr(
        ai_provider_config,
        "list_models",
        lambda provider, api_key=None, timeout=20: {
            "provider": provider,
            "models": ["deepseek-v4.1-flash"],
            "count": 1,
            "source": "provider",
            "endpoint": "https://example/models",
            "note": "",
        },
    )
    response = pages.settings_ai_models(
        provider="opencode",
        api_key="",
        user=SimpleNamespace(role=ROLE_SYSTEM_ADMIN, id=uuid4(), email="a@b.c"),
        db=_RowSession(),
    )
    body = response.body.decode()
    assert '"ok":true' in body and "deepseek-v4.1-flash" in body and '"source":"provider"' in body

    admin = SimpleNamespace(role=ROLE_SYSTEM_ADMIN, id=uuid4(), email="a@b.c")
    page = pages.settings_ai_page(make_request("/settings/ai"), user=admin, db=_RowSession())
    html = page.body.decode()
    assert "Fetch models" in html and 'id="ai-model-options"' in html


def test_saving_a_provider_with_no_reachable_key_says_so(monkeypatch):
    """A switch must not silently leave every generation path without a key."""
    from types import SimpleNamespace
    from uuid import uuid4

    from app.api.routers import pages
    from app.services import ai_provider_config
    from app.services.access import ROLE_SYSTEM_ADMIN
    from tests.test_dashboard_routes import make_request

    _fernet_key(monkeypatch)
    monkeypatch.setattr(ai_provider_config, "_read_row", lambda db=None: (None, None, None))

    saved = {}

    def fake_save(db, *, provider, model, api_key=None, user_email=None):
        saved.update(provider=provider, model=model, api_key=api_key)
        monkeypatch.setattr(
            ai_provider_config, "_read_row", lambda db=None: (provider, model, None)
        )
        return SimpleNamespace(provider=provider, model=model)

    monkeypatch.setattr(ai_provider_config, "save_setting", fake_save)

    from app.core.config import settings

    monkeypatch.setattr(settings, "ai_provider", "deepseek", raising=False)
    monkeypatch.setattr(settings, "ai_api_key", "sk-deepseek-platform-key", raising=False)

    request = make_request("/settings/ai")
    admin = SimpleNamespace(role=ROLE_SYSTEM_ADMIN, id=uuid4(), email="a@b.c")
    response = pages.settings_ai_save(
        request,
        provider="opencode",
        model="deepseek-v4.1-flash",
        api_key="",
        clear_key="",
        action="save",
        user=admin,
        db=_RowSession(),
    )
    body = response.body.decode()
    assert saved["provider"] == "opencode", "the save must still happen"
    assert "Saved, but no API key" in body, "the page must warn about the missing key"
    assert "No AI credential configured" in body


def test_keys_are_stored_per_provider_and_never_lent(monkeypatch):
    """A key entered for one provider must not become another provider's key."""
    from app.services import ai_auth, ai_provider_config

    _fernet_key(monkeypatch)
    db = _RowSession()

    def fake_read_row(session=None):
        """Serve the session's row whether or not a session was passed in."""
        source = session if getattr(session, "row", None) is not None else db
        if getattr(source, "row", None) is None:
            return None, None, None
        return source.row.provider, source.row.model, source.row.api_key_encrypted

    monkeypatch.setattr(ai_provider_config, "_read_row", fake_read_row)
    ai_provider_config.save_setting(
        db, provider="opencode", model="deepseek-v4.1-flash", api_key="sk-zen-key"
    )
    ai_provider_config.save_setting(
        db, provider="deepseek", model="deepseek-flash", api_key="sk-deepseek-key"
    )
    assert ai_provider_config.stored_key_providers(db) == ["deepseek", "opencode"]
    assert ai_provider_config.stored_api_key(db, provider="opencode") == "sk-zen-key"
    assert ai_provider_config.stored_api_key(db, provider="deepseek") == "sk-deepseek-key"
    assert ai_provider_config.stored_api_key(db, provider="openai") is None

    assert ai_auth._app_credential("deepseek").secret == "sk-deepseek-key"
    assert ai_auth._app_credential("openai") is None

    # Switching back to a provider with a blank field keeps its own key.
    ai_provider_config.save_setting(
        db, provider="opencode", model="deepseek-v4.1-flash", api_key=None
    )
    assert ai_provider_config.stored_api_key(db, provider="opencode") == "sk-zen-key"
    assert ai_provider_config.stored_api_key(db, provider="deepseek") == "sk-deepseek-key"


def test_clearing_one_providers_key_leaves_the_others_alone(monkeypatch):
    from app.services import ai_provider_config

    _fernet_key(monkeypatch)
    db = _RowSession()
    ai_provider_config.save_setting(db, provider="opencode", model="m", api_key="sk-zen")
    ai_provider_config.save_setting(db, provider="openai", model="m", api_key="sk-openai")

    ai_provider_config.save_setting(db, provider="opencode", model="m", api_key="")
    assert ai_provider_config.stored_api_key(db, provider="opencode") is None
    assert ai_provider_config.stored_api_key(db, provider="openai") == "sk-openai"


def test_a_stored_key_is_encrypted_at_rest(monkeypatch):
    from app.services import ai_provider_config

    _fernet_key(monkeypatch)
    db = _RowSession()
    ai_provider_config.save_setting(
        db, provider="opencode", model="deepseek-v4.1-flash", api_key="sk-plain-text-key"
    )
    stored = db.row.api_key_encrypted
    assert "sk-plain-text-key" not in stored, "the key must not be readable in the column"
    assert stored.startswith("{"), "keys are stored per provider"
    assert ai_provider_config.stored_api_key(db, provider="opencode") == "sk-plain-text-key"


def test_a_legacy_single_key_row_belongs_to_its_own_provider(monkeypatch):
    """Rows written before keys were per-provider hold a bare ciphertext."""
    from app.services import ai_provider_config
    from app.services.ai_auth import encrypt_secret

    _fernet_key(monkeypatch)
    encrypted = encrypt_secret("sk-legacy")
    monkeypatch.setattr(
        ai_provider_config, "_read_row", lambda db=None: ("deepseek", "deepseek-flash", encrypted)
    )
    assert ai_provider_config.stored_key_providers() == ["deepseek"]
    assert ai_provider_config.stored_api_key(provider="deepseek") == "sk-legacy"
    assert ai_provider_config.stored_api_key(provider="opencode") is None


def test_storing_a_key_without_the_encryption_key_fails_clearly(monkeypatch):
    """No Fernet key means the key cannot be stored - say so, do not traceback."""
    from types import SimpleNamespace
    from uuid import uuid4

    from app.core.config import settings
    from app.services import ai_provider_config
    from tests.test_dashboard_routes import make_request

    monkeypatch.setattr(settings, "ai_secrets_fernet_key", "", raising=False)
    assert ai_provider_config.secrets_encryption_available() is False

    db = _RowSession()
    try:
        ai_provider_config.save_setting(db, provider="opencode", model="m", api_key="sk-x")
    except ValueError as exc:
        assert "AI_SECRETS_FERNET_KEY" in str(exc)
    else:
        raise AssertionError("a key must not be silently dropped")

    from app.api.routers import pages
    from app.services.access import ROLE_SYSTEM_ADMIN

    admin = SimpleNamespace(role=ROLE_SYSTEM_ADMIN, id=uuid4(), email="a@b.c")
    body = pages.settings_ai_page(
        make_request("/settings/ai"), user=admin, db=_RowSession()
    ).body.decode()
    assert "Storing a key is unavailable" in body
    assert "AI_SECRETS_FERNET_KEY" in body


def test_storing_a_key_works_when_encryption_is_configured(monkeypatch):
    from app.core.config import settings
    from app.services import ai_provider_config

    _fernet_key(monkeypatch)
    assert ai_provider_config.secrets_encryption_available() is True
    monkeypatch.setattr(settings, "ai_secrets_fernet_key", "not-a-valid-key", raising=False)
    assert ai_provider_config.secrets_encryption_available() is False
