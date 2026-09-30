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
