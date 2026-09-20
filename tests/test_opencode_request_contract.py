"""OpenCode Go request-contract tests.

The opencode.ai Go endpoint rejects requests without ``x-opencode-session``
(400 MissingSessionID) and without a client User-Agent (403, Cloudflare error
code 1010), so every opencode call site must build its headers through
``_opencode_headers``. These tests are offline — they assert on header
construction and provider wiring only.
"""
from __future__ import annotations

from app.services import ai_generation as ag

CREDENTIAL = ag.AICredential(provider="opencode", auth_type="api_key", secret="secret-key")


def _session_id(provider) -> str:
    """Read the session id off a provider without narrowing the Protocol type."""
    return str(getattr(provider, "session_id"))


def test_opencode_session_id_is_prefixed():
    assert ag.opencode_session_id("job-123") == "eduviz-job-123"
    assert ag.opencode_session_id().startswith("eduviz-")


def test_generated_session_ids_are_unique():
    assert ag.opencode_session_id() != ag.opencode_session_id()


def test_opencode_headers_send_session_and_user_agent():
    headers = ag._opencode_headers("secret-key", "eduviz-job-123")
    assert headers["Authorization"] == "Bearer secret-key"
    assert headers["Content-Type"] == "application/json"
    assert headers["x-opencode-session"] == "eduviz-job-123"
    assert headers["User-Agent"]


def test_providers_keep_one_session_per_instance():
    provider = ag.get_study_pack_provider("opencode", session_id="eduviz-job-abc")
    assert _session_id(provider) == "eduviz-job-abc"

    auto = ag.get_study_pack_provider("opencode")
    assert _session_id(auto).startswith("eduviz-")
    assert _session_id(auto) != _session_id(ag.get_study_pack_provider("opencode"))

    revision = ag.get_study_pack_provider("deepseek", session_id="eduviz-rev-abc")
    assert _session_id(revision) == "eduviz-rev-abc"


def test_opencode_request_carries_session_header(monkeypatch):
    """The study-pack POST must include the session + UA headers."""
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        return FakeResponse()

    import requests

    monkeypatch.setattr(requests, "post", fake_post)

    provider = ag.get_study_pack_provider("opencode", session_id="eduviz-job-xyz")
    assert provider.generate_text("hello", CREDENTIAL) == "ok"
    assert captured["headers"]["x-opencode-session"] == "eduviz-job-xyz"
    assert captured["headers"]["User-Agent"]
