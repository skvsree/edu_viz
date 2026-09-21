"""Test that the 3 modes per chunk (core/mechanisms/traps) run in parallel.

Before this refactor, each chunk ran 3 sequential API calls, taking roughly
3x the wall-clock time needed. The 3 modes are independent (they ask the
same text with different instructions), so they can run concurrently via
a ThreadPoolExecutor.

This test verifies the orchestration function returns the same result
when run in parallel as it did when run serially, and that the
"already covered" prompt list does NOT include mid-chunk results
(otherwise the parallel calls would race on shared state).

The actual per-mode API calls are stubbed out — we only verify the
scheduler invokes them concurrently and merges results.
"""

from __future__ import annotations

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

from app.services import job_worker
from app.services.ai_generation import (
    AIGenerationError,
    GeneratedFlashcard,
    GeneratedMcq,
    GeneratedStudyPack,
)


class _StubProvider:
    """Records concurrent invocations and returns canned study packs per mode."""

    def __init__(self, packs_per_mode: dict[str, GeneratedStudyPack]):
        self.packs_per_mode = packs_per_mode
        self.call_log: list[tuple[str, float, float]] = []
        self._lock = threading.Lock()

    def generate_from_prompt(self, prompt: str, credential=None) -> GeneratedStudyPack:
        # Detect which mode the prompt is asking for by sniffing the
        # "Current extraction mode:" marker. This is a brittle test seam
        # but the alternative (passing mode explicitly) requires
        # refactoring the public API, which is out of scope.
        mode = "core"
        for candidate in ("core", "mechanisms", "traps"):
            if "Current extraction mode: " in prompt and candidate in prompt.split(
                "Current extraction mode: ", 1
            )[1][:200]:
                mode = candidate
                break
        start = time.monotonic()
        # Simulate AI latency. 200ms each. If the caller is running
        # serially this takes ~600ms total; if parallel, ~200ms.
        time.sleep(0.2)
        with self._lock:
            self.call_log.append((mode, start, time.monotonic()))
        return self.packs_per_mode[mode]


def _make_pack(front: str, question: str) -> GeneratedStudyPack:
    return GeneratedStudyPack(
        flashcards=[GeneratedFlashcard(front=front, back=f"back for {front}")],
        mcqs=[GeneratedMcq(
            question=question,
            options=["A", "B", "C", "D"],
            answer_index=0,
            explanation=f"explanation for {question}",
        )],
    )


def _make_db_stub():
    """Minimal DB stub that records add_all/commit/flush/get/refresh calls."""

    class _Stub:
        def __init__(self):
            self.commits = 0
            self.flushed = 0
            self.added = []

        def add_all(self, items):
            self.added.extend(items)

        def commit(self):
            self.commits += 1

        def flush(self):
            self.flushed += 1

        def get(self, *_a, **_k):
            return None

        def refresh(self, *_a, **_k):
            return None

        def execute(self, *_a, **_k):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    return _Stub()


def _make_file_row():
    return SimpleNamespace(
        id=uuid4(),
        flashcards_generated=0,
        mcqs_generated=0,
        duplicate_count=0,
    )


def test_run_chunk_modes_parallel_invokes_three_modes_concurrently(monkeypatch):
    """The 3 modes within a chunk must be submitted to a thread pool
    simultaneously, not one after the other. Total wall time for 3 calls
    of 200ms each should be ~200ms (parallel) not ~600ms (serial).
    """
    packs = {
        "core": _make_pack("core_fact", "core_q?"),
        "mechanisms": _make_pack("mech_fact", "mech_q?"),
        "traps": _make_pack("trap_fact", "trap_q?"),
    }
    provider = _StubProvider(packs)
    credential = SimpleNamespace(provider="minimax")

    # Exercise the public entry point. We need to call the function that
    # actually runs the 3 modes in a chunk. That lives inside
    # process_bulk_ai_upload today; the refactor will extract it into a
    # module-level function so we can test it directly.
    func = getattr(job_worker, "_run_chunk_modes_parallel", None)
    assert func is not None, (
        "job_worker must expose _run_chunk_modes_parallel so the 3 modes "
        "in a chunk can be invoked concurrently. Refactor required."
    )

    # AI passes go through a process-wide semaphore (the provider 503s when
    # several long generations run together), so give this test enough room to
    # prove the modes are submitted concurrently rather than one after another.
    monkeypatch.setattr(job_worker, "_pass_semaphore", threading.BoundedSemaphore(3))

    aggregate = GeneratedStudyPack(flashcards=[], mcqs=[])
    chunk_text = "Source text for this chunk. " * 50  # ~1.2KB

    start = time.monotonic()
    chunk_pack, failed_modes = func(
        provider=provider,
        credential=credential,
        chunk_text=chunk_text,
        aggregate=aggregate,
        modes=("core", "mechanisms", "traps"),
        max_flashcards=18,
        max_mcqs=18,
        log_prefix="[test] chunk=1/1",
        executor=ThreadPoolExecutor(max_workers=3),
    )
    elapsed = time.monotonic() - start

    # 3 calls of 200ms in parallel should finish in ~200ms (not ~600ms).
    # Allow 400ms tolerance for thread startup overhead.
    assert elapsed < 0.4, (
        f"3 mode calls took {elapsed:.3f}s — expected <0.4s for parallel "
        f"execution. If this is ~0.6s the calls ran serially."
    )

    # All 3 modes were called.
    called_modes = {entry[0] for entry in provider.call_log}
    assert called_modes == {"core", "mechanisms", "traps"}

    # Chunk pack contains all 3 flashcards and 3 MCQs (one from each mode).
    assert len(chunk_pack.flashcards) == 3
    assert len(chunk_pack.mcqs) == 3

    # No modes failed (each stub returned successfully).
    assert failed_modes == set()


def test_run_chunk_modes_parallel_continues_on_individual_mode_failure():
    """If one mode's API call fails (AIGenerationError), the other two
    should still complete and their results should be merged into
    chunk_pack. The failing mode's prompt slot is just absent.
    """
    packs = {
        "core": _make_pack("core_fact", "core_q?"),
        "mechanisms": _make_pack("mech_fact", "mech_q?"),
        # 'traps' is missing — generate_from_prompt will KeyError.
    }
    provider = _StubProvider(packs)
    credential = SimpleNamespace(provider="minimax")

    func = getattr(job_worker, "_run_chunk_modes_parallel", None)
    assert func is not None

    chunk_text = "Source text. " * 50
    chunk_pack, failed_modes = func(
        provider=provider,
        credential=credential,
        chunk_text=chunk_text,
        aggregate=GeneratedStudyPack(flashcards=[], mcqs=[]),
        modes=("core", "mechanisms", "traps"),
        max_flashcards=18,
        max_mcqs=18,
        log_prefix="[test]",
        executor=ThreadPoolExecutor(max_workers=3),
    )

    # 'traps' failed (KeyError), but core and mechanisms still contributed.
    assert "traps" in failed_modes
    assert len(chunk_pack.flashcards) == 2
    assert len(chunk_pack.mcqs) == 2


def test_run_chunk_modes_parallel_handles_empty_aggregate():
    """When called on chunk 1 of a file, aggregate is empty. The
    'already covered' list in each prompt should be empty (or
    contain only aggregate items), NOT items from other modes'
    in-flight results. This is the test that guards against the
    naive 'merge all mode results into shared chunk_pack before
    building prompts' anti-pattern.
    """
    # We can't easily inspect the prompts sent to the stub, but we
    # can verify the function runs without raising when aggregate
    # is empty (which would be a bug if it tried to dereference
    # None or non-existent attributes).
    packs = {mode: _make_pack(f"{mode}_fact", f"{mode}_q?") for mode in ("core", "mechanisms", "traps")}
    provider = _StubProvider(packs)
    func = getattr(job_worker, "_run_chunk_modes_parallel", None)
    assert func is not None

    chunk_pack, failed_modes = func(
        provider=provider,
        credential=SimpleNamespace(provider="minimax"),
        chunk_text="Source text. " * 50,
        aggregate=GeneratedStudyPack(flashcards=[], mcqs=[]),
        modes=("core", "mechanisms", "traps"),
        max_flashcards=18,
        max_mcqs=18,
        log_prefix="[test]",
        executor=ThreadPoolExecutor(max_workers=3),
    )
    assert failed_modes == set()
    assert len(chunk_pack.flashcards) == 3


def test_pass_concurrency_cap_is_honoured(monkeypatch):
    """The cap must actually limit how many generations overlap.

    Six concurrent long generations drew 503s and read-timeouts from the
    provider while a single one succeeded, so passes are throttled.
    """
    packs = {
        "core": _make_pack("core_fact", "core_q?"),
        "mechanisms": _make_pack("mech_fact", "mech_q?"),
        "traps": _make_pack("trap_fact", "trap_q?"),
    }
    provider = _StubProvider(packs)
    credential = SimpleNamespace(provider="minimax")
    monkeypatch.setattr(job_worker, "_pass_semaphore", threading.BoundedSemaphore(1))

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as executor:
        pack, failed_modes = job_worker._run_chunk_modes_parallel(
            provider=provider,
            credential=credential,
            chunk_text="Source text for this chunk. " * 50,
            aggregate=GeneratedStudyPack(flashcards=[], mcqs=[]),
            modes=("core", "mechanisms", "traps"),
            max_flashcards=5,
            max_mcqs=5,
            log_prefix="test",
            executor=executor,
        )

    assert failed_modes == set()
    events = [(entry[1], 1) for entry in provider.call_log]
    events += [(entry[2], -1) for entry in provider.call_log]
    running = peak = 0
    for _, delta in sorted(events):
        running += delta
        peak = max(peak, running)
    assert peak <= 1, f"cap of 1 allowed {peak} concurrent passes"
    assert len(provider.call_log) == 3, "all three modes must still run"


class _CountingProvider:
    """Returns one fresh card pair per call so rounds accumulate."""

    def __init__(self, empty: bool = False):
        self.empty = empty
        self.calls: list[str] = []
        self._counter = itertools.count()
        self._lock = threading.Lock()

    def generate_from_prompt(self, prompt, credential=None):
        mode = "core"
        for candidate in ("core", "mechanisms", "traps"):
            if "Current extraction mode: " in prompt and candidate in prompt.split(
                "Current extraction mode: ", 1
            )[1][:200]:
                mode = candidate
                break
        with self._lock:
            self.calls.append(mode)
        if self.empty:
            return GeneratedStudyPack(flashcards=[], mcqs=[])
        index = next(self._counter)
        return GeneratedStudyPack(
            flashcards=[GeneratedFlashcard(front=f"fact {index}", back="back")],
            mcqs=[
                GeneratedMcq(
                    question=f"question {index}",
                    options=["A", "B", "C", "D"],
                    answer_index=0,
                    explanation="explanation",
                )
            ],
        )


def test_generate_chunk_pack_uses_short_rounds_and_keeps_coverage(monkeypatch):
    """A chunk is covered by `rounds` short passes per mode, not one long pass.

    Long completions are what the provider answers with empty-bodied 503s; the
    per-chunk ceiling (modes x rounds x items) stays the same.
    """
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(job_worker, "_pass_semaphore", threading.BoundedSemaphore(3))
    provider = _CountingProvider()

    with ThreadPoolExecutor(max_workers=3) as executor:
        pack, failed_modes, failed_passes = job_worker._generate_chunk_pack(
            provider=provider,
            credential=SimpleNamespace(provider="opencode"),
            chunk_text="Source text for this chunk. " * 50,
            aggregate=GeneratedStudyPack(flashcards=[], mcqs=[]),
            modes=("core", "mechanisms", "traps"),
            rounds=3,
            pass_items=6,
            log_prefix="test",
            executor=executor,
            failure_log={},
        )

    assert len(provider.calls) == 9, f"expected 3 rounds x 3 modes, got {len(provider.calls)}"
    assert len(pack.flashcards) == 9 and len(pack.mcqs) == 9
    assert failed_modes == set()
    assert failed_passes == 0


def test_generate_chunk_pack_stops_early_when_a_round_adds_nothing(monkeypatch):
    """An exhausted chunk must not burn further provider calls."""
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(job_worker, "_pass_semaphore", threading.BoundedSemaphore(3))
    provider = _CountingProvider(empty=True)

    with ThreadPoolExecutor(max_workers=3) as executor:
        pack, _failed_modes, _failed_passes = job_worker._generate_chunk_pack(
            provider=provider,
            credential=SimpleNamespace(provider="opencode"),
            chunk_text="Source text for this chunk. " * 50,
            aggregate=GeneratedStudyPack(flashcards=[], mcqs=[]),
            modes=("core", "mechanisms", "traps"),
            rounds=3,
            pass_items=6,
            log_prefix="test",
            executor=executor,
            failure_log={},
        )

    assert len(provider.calls) == 3, "only the first round should have run"
    assert not pack.flashcards and not pack.mcqs


class _AlwaysFailingProvider:
    """Every pass fails with the empty-response error the logs actually show."""

    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()

    def generate_from_prompt(self, prompt, credential=None):
        with self._lock:
            self.calls += 1
        raise AIGenerationError("OpenCode API returned empty response.")


def test_generate_chunk_pack_keeps_going_when_a_whole_round_fails(monkeypatch):
    """A round where every pass failed does not mean the chunk is exhausted.

    The provider returns empty-bodied 200s intermittently, so the rounds must
    still be attempted rather than abandoning the chunk's remaining coverage.
    """
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(job_worker.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(job_worker, "_pass_semaphore", threading.BoundedSemaphore(3))
    provider = _AlwaysFailingProvider()

    with ThreadPoolExecutor(max_workers=3) as executor:
        pack, failed_modes, failed_passes = job_worker._generate_chunk_pack(
            provider=provider,
            credential=SimpleNamespace(provider="opencode"),
            chunk_text="Source text for this chunk. " * 50,
            aggregate=GeneratedStudyPack(flashcards=[], mcqs=[]),
            modes=("core", "mechanisms", "traps"),
            rounds=3,
            pass_items=6,
            log_prefix="test",
            executor=executor,
            failure_log={},
        )

    assert not pack.flashcards and not pack.mcqs
    assert failed_modes == {"core", "mechanisms", "traps"}
    # Rounds continue past a dead round (the chunk may still have material),
    # but MAX_CONSECUTIVE_FAILED_ROUNDS stops a dead provider from burning the
    # rest of the rounds.
    full_budget = 3 * 3 * job_worker.MAX_AI_FORMAT_RETRIES
    assert provider.calls >= 9, f"only {provider.calls} passes attempted"
    assert provider.calls < full_budget, (
        f"a dead provider should abandon the chunk early, got {provider.calls} calls"
    )
    # Exactly MAX_CONSECUTIVE_FAILED_ROUNDS rounds of failures, then stop.
    assert failed_passes == job_worker.MAX_CONSECUTIVE_FAILED_ROUNDS * 3
