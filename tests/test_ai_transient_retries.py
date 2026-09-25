"""Retry + reporting for transient AI provider failures.

The 2026-09-20 bulk run of fecu105.pdf lost every extraction mode on chunks 3-6
to HTTP 503s and 180s read timeouts. Neither was retried, and the file still
finished "completed" with 72 of the ~650 cards the same chapter produced on
earlier runs, so nothing surfaced the loss.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services import job_worker as jw
from app.services.ai_generation import GeneratedStudyPack, merge_study_packs

TIMEOUT_ERR = (
    "HTTPSConnectionPool(host='opencode.ai', port=443): Read timed out. "
    "(read timeout=180)"
)


def _pack() -> GeneratedStudyPack:
    return GeneratedStudyPack(flashcards=[], mcqs=[])


class _Provider:
    """Fails the first N calls with a transient error, then answers."""

    def __init__(self, failures: int, error: Exception | None = None):
        self.failures = failures
        self.error = error or jw.AIGenerationError("OpenCode API error (503): ")
        self.calls = 0

    def generate_from_prompt(self, prompt, credential=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return _pack()


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def test_transient_classifier_matches_provider_hiccups():
    assert jw._is_retryable_transient_error(jw.AIGenerationError("OpenCode API error (503): "))
    assert jw._is_retryable_transient_error(Exception(TIMEOUT_ERR))
    assert jw._is_retryable_transient_error(Exception("HTTP 429 too many requests"))
    assert jw._is_retryable_transient_error(Exception("Connection reset by peer"))
    assert jw._is_retryable_transient_error(Exception("OpenCode API error (504): gateway timeout"))


def test_transient_classifier_ignores_permanent_errors():
    assert not jw._is_retryable_transient_error(
        jw.AIGenerationError("OpenCode API error (401): invalid api key")
    )
    assert not jw._is_retryable_transient_error(
        jw.AIGenerationError("Unsupported AI study pack provider: bogus")
    )


def test_pass_failure_buckets_name_the_cause():
    assert jw._classify_pass_failure(jw.AIGenerationError("OpenCode API error (503): ")) == "HTTP 503"
    assert jw._classify_pass_failure(Exception(TIMEOUT_ERR)) == "timeout"
    assert (
        jw._classify_pass_failure(jw.AIGenerationError("OpenCode API returned no choices."))
        == "unparseable response"
    )


def test_failure_summary_lists_the_commonest_reason_first():
    summary = jw._describe_pass_failures({"timeout": 2, "HTTP 503": 5})
    assert summary.startswith("HTTP 503 x5")
    assert "timeout x2" in summary


# --------------------------------------------------------------------------
# retry
# --------------------------------------------------------------------------


def test_pack_retry_recovers_from_a_503(monkeypatch):
    monkeypatch.setattr(jw.time, "sleep", lambda seconds: None)
    provider = _Provider(failures=2)

    result = jw._generate_pack_with_retry(
        provider, "prompt", credential=None, log_prefix="test"
    )

    assert provider.calls == 3
    assert isinstance(result, GeneratedStudyPack)


def test_pack_retry_recovers_from_a_read_timeout(monkeypatch):
    monkeypatch.setattr(jw.time, "sleep", lambda seconds: None)
    provider = _Provider(failures=1, error=Exception(TIMEOUT_ERR))

    jw._generate_pack_with_retry(provider, "prompt", credential=None, log_prefix="test")

    assert provider.calls == 2


def test_pack_retry_gives_up_with_a_clear_error(monkeypatch):
    monkeypatch.setattr(jw.time, "sleep", lambda seconds: None)
    provider = _Provider(failures=99)

    with pytest.raises(jw.AIGenerationError) as excinfo:
        jw._generate_pack_with_retry(provider, "prompt", credential=None, log_prefix="test")

    assert "unavailable after" in str(excinfo.value)
    assert provider.calls == jw.MAX_TRANSIENT_RETRIES


def test_text_retry_also_covers_transient_failures(monkeypatch):
    monkeypatch.setattr(jw.time, "sleep", lambda seconds: None)

    class TextProvider:
        def __init__(self):
            self.calls = 0

        def generate_text(self, prompt, credential=None):
            self.calls += 1
            if self.calls == 1:
                raise jw.AIGenerationError("OpenCode API error (503): ")
            return "ok"

    provider = TextProvider()
    assert jw._generate_text_with_retry(provider, "prompt", credential=None, log_prefix="t") == "ok"
    assert provider.calls == 2


# --------------------------------------------------------------------------
# failure reporting from the chunk runner
# --------------------------------------------------------------------------


def test_chunk_runner_reports_why_passes_failed(monkeypatch):
    monkeypatch.setattr(jw.time, "sleep", lambda seconds: None)
    provider = _Provider(failures=99)
    failure_log: dict = {}

    with ThreadPoolExecutor(max_workers=3) as executor:
        pack, failed_modes = jw._run_chunk_modes_parallel(
            provider=provider,
            credential=None,
            chunk_text="Photosynthesis converts light into chemical energy. " * 20,
            aggregate=merge_study_packs(),
            modes=("core", "mechanisms", "traps"),
            max_flashcards=18,
            max_mcqs=18,
            log_prefix="test",
            executor=executor,
            failure_log=failure_log,
        )

    assert failed_modes == {"core", "mechanisms", "traps"}
    assert set(failure_log) == {"core", "mechanisms", "traps"}
    assert set(failure_log.values()) == {"HTTP 503"}
    assert pack.flashcards == [] and pack.mcqs == []


def test_chunk_runner_without_a_failure_log_still_works(monkeypatch):
    """The out-parameter is optional: existing callers keep working."""
    monkeypatch.setattr(jw.time, "sleep", lambda seconds: None)

    with ThreadPoolExecutor(max_workers=1) as executor:
        pack, failed_modes = jw._run_chunk_modes_parallel(
            provider=_Provider(failures=0),
            credential=None,
            chunk_text="Cells are the basic unit of life. " * 20,
            aggregate=merge_study_packs(),
            modes=("core",),
            max_flashcards=5,
            max_mcqs=5,
            log_prefix="test",
            executor=executor,
        )

    assert failed_modes == set()
    assert pack is not None


def test_timeouts_get_a_shorter_retry_budget(monkeypatch):
    """A hung request costs its whole read timeout, so it gets fewer attempts.

    With 4 attempts at a 300s timeout a single wedged pass blocked the run for
    20 minutes; timeouts now get MAX_TIMEOUT_RETRIES instead.
    """
    monkeypatch.setattr(jw.time, "sleep", lambda _seconds: None)

    class _HangingProvider:
        def __init__(self):
            self.calls = 0

        def generate_from_prompt(self, prompt, credential=None):
            self.calls += 1
            raise jw.AIGenerationError(
                "HTTPSConnectionPool(host='opencode.ai', port=443): Read timed "
                "out. (read timeout=180)"
            )

    provider = _HangingProvider()
    with pytest.raises(jw.AIGenerationError):
        jw._generate_pack_with_retry(
            provider, "prompt", credential=None, log_prefix="test"
        )

    assert provider.calls == jw.MAX_TIMEOUT_RETRIES
    assert jw.MAX_TIMEOUT_RETRIES < jw.MAX_TRANSIENT_RETRIES


def test_transient_retries_wait_a_flat_fifteen_seconds():
    """The wait between transient retries is flat, not a ramp-up.

    Passes run sequentially, so time spent ramping backoff is time the whole
    run spends blocked; the provider is either back by then or it is not.
    """
    assert jw.TRANSIENT_RETRY_DELAY_SECONDS == 15
    delays = [jw._transient_retry_delay(attempt) for attempt in range(1, 6)]
    assert delays == [15, 15, 15, 15, 15]


def test_opencode_pass_timeout_is_short_enough_to_keep_a_sequential_run_moving():
    """A hung pass must not block the run for minutes.

    Measured: a healthy pass returns in 10-40s, while 180s hangs were the
    single largest cost in a run (8 of them = ~24 minutes).
    """
    from app.core.config import settings
    from app.services.ai_generation import _opencode_timeout

    assert settings.opencode_request_timeout == 60
    assert _opencode_timeout() == 60
    assert _opencode_timeout() < 180
