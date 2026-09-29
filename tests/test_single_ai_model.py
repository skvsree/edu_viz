"""One provider, one model: every AI call site must resolve the same model.

The app used to carry a model per call site (``OPENCODE_MODEL`` for bulk study
packs, ``REVISION_NOTES_MODEL`` for revision notes) plus a hardcoded
``provider_name = "deepseek"`` for the revision-notes job. That let the two
drift apart, so a model change fixed bulk generation while revision notes kept
running on something else. These tests pin the collapse into a single AI_MODEL.
"""
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
