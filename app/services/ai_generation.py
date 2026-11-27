from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Protocol
import re
import threading
import unicodedata

logger = logging.getLogger(__name__)


def opencode_session_id(suffix: str | None = None) -> str:
    """Build a session id for the ``x-opencode-session`` header.

    OpenCode Go wants one stable id per conversation so it can route and
    cache prompts; pass the same ``suffix`` (job id, child file id, ...) to
    share a session across the calls of a single conversation.
    """
    if suffix:
        return f"eduviz-{suffix}"
    return f"eduviz-{uuid.uuid4().hex[:12]}"


def _opencode_headers(secret: str, session_id: str) -> dict[str, str]:
    """Headers for an OpenCode Go chat-completions request.

    Without ``x-opencode-session`` the endpoint answers 400
    ``MissingSessionID``, and without a client user agent the request is
    rejected at the edge (403, Cloudflare error code 1010).
    """
    from app.core.config import settings

    return {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "User-Agent": settings.opencode_client_ua,
        "x-opencode-session": session_id,
    }


def _extract_minimax_text(data: dict) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AIGenerationError("Minimax returned no choices.")

    choice = choices[0] or {}
    if not isinstance(choice, dict):
        raise AIGenerationError("Minimax returned an invalid choice payload.")

    message = choice.get("message") or {}
    if not isinstance(message, dict):
        message = {}

    content = message.get("content")
    if isinstance(content, str):
        content = content.strip()
        if content:
            return content
    elif isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item.strip():
                text_parts.append(item.strip())
                continue
            if isinstance(item, dict):
                for key in ("text", "content"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        text_parts.append(value.strip())
                        break
        joined = "\n".join(part for part in text_parts if part)
        if joined:
            return joined

    for key in ("reply", "output_text", "text"):
        value = choice.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    raise AIGenerationError("Minimax returned empty response.")


class AIGenerationError(RuntimeError):
    pass


@dataclass(slots=True)
class TokenUsage:
    """Token counts captured from a provider response.

    ``cache_hit_tokens`` / ``cache_miss_tokens`` mirror DeepSeek's
    ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens`` fields so runs
    can see how much input was reused via the provider's prompt cache.
    """
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cache_hit_tokens += other.cache_hit_tokens
        self.cache_miss_tokens += other.cache_miss_tokens
        self.reasoning_tokens += other.reasoning_tokens


def _extract_usage(data: dict) -> TokenUsage | None:
    """Read the OpenAI-compatible ``usage`` block from a response body."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("completion_tokens_details")
    if not isinstance(details, dict):
        details = {}
    return TokenUsage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        cache_hit_tokens=int(usage.get("prompt_cache_hit_tokens") or 0),
        cache_miss_tokens=int(usage.get("prompt_cache_miss_tokens") or 0),
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
    )


def _raise_if_truncated(provider: str, choice: dict) -> None:
    """Fail fast on a truncated completion instead of retrying it.

    ``finish_reason == "length"`` means the completion hit ``max_tokens``; the
    JSON is almost certainly cut mid-object, and re-sending the identical prompt
    would burn the same tokens for zero cards (the old 5-retry format loop).
    """
    finish_reason = (choice or {}).get("finish_reason")
    if finish_reason == "length":
        raise AIGenerationError(
            f"{provider} response truncated (finish_reason=length): the "
            "completion hit max_tokens. Raise the completion budget instead of "
            "retrying the same prompt."
        )


def _log_usage(provider: str, model: str, usage: TokenUsage | None) -> None:
    """Emit one line per AI pass so token spend is measurable.

    Uses print to stdout to match the worker's existing log stream; this is the
    T1 baseline that every later optimisation is measured against.
    """
    if usage is None:
        print(f"[ai-usage] provider={provider} model={model} usage=missing", flush=True)
        return
    print(
        f"[ai-usage] provider={provider} model={model} "
        f"prompt={usage.prompt_tokens} cache_hit={usage.cache_hit_tokens} "
        f"cache_miss={usage.cache_miss_tokens} completion={usage.completion_tokens} "
        f"reasoning={usage.reasoning_tokens} total={usage.total_tokens}",
        flush=True,
    )


class _UsageAccumulatorMixin:
    """Thread-safe running token total for a provider instance.

    A provider is shared across the parallel extraction modes of a chunk, so a
    plain ``last_usage`` attribute would race. The running total is guarded by a
    lock and is reset once per file by the bulk worker.
    """

    def _init_usage(self) -> None:
        self._usage_lock = threading.Lock()
        self.accumulated_usage = TokenUsage()

    def _record_usage(self, provider_name: str, model: str, data: dict) -> None:
        usage = _extract_usage(data)
        _log_usage(provider_name, model, usage)
        if usage is not None:
            with self._usage_lock:
                self.accumulated_usage.add(usage)

    def reset_usage(self) -> None:
        with self._usage_lock:
            self.accumulated_usage = TokenUsage()

    def snapshot_usage(self) -> TokenUsage:
        with self._usage_lock:
            return self.accumulated_usage


@dataclass(slots=True)
class GeneratedFlashcard:
    front: str
    back: str


@dataclass(slots=True)
class GeneratedMcq:
    question: str
    options: list[str]
    answer_index: int
    explanation: str


@dataclass(slots=True)
class GeneratedStudyPack:
    flashcards: list[GeneratedFlashcard]
    mcqs: list[GeneratedMcq]


@dataclass(slots=True)
class AICredential:
    provider: str
    auth_type: str
    secret: str
    refresh_token: str | None = None
    source: str = "env"


class StudyPackProvider(Protocol):
    name: str

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack: ...
    def generate_from_prompt(self, prompt: str, credential: AICredential | None = None) -> GeneratedStudyPack: ...
    def generate_text(self, prompt: str, credential: AICredential | None = None) -> str: ...


class OpenAIStudyPackProvider:
    name = "openai"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        return self.generate_from_prompt(_build_prompt(text), credential)

    def generate_text(self, prompt: str, credential: AICredential | None = None) -> str:
        if not credential or credential.provider != "openai":
            raise AIGenerationError("OpenAI credential is required.")
        if credential.auth_type not in {"api_key", "oauth"}:
            raise AIGenerationError(f"Unsupported OpenAI auth type: {credential.auth_type}")

        from openai import OpenAI

        client = OpenAI(api_key=credential.secret)
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )
        return response.output_text or ""

    def generate_from_prompt(self, prompt: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        return _parse_study_pack_json(self.generate_text(prompt, credential))


class MinimaxStudyPackProvider:
    name = "minimax"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        return self.generate_from_prompt(_build_prompt(text), credential)

    def generate_text(self, prompt: str, credential: AICredential | None = None) -> str:
        if not credential or credential.provider != "minimax":
            raise AIGenerationError("Minimax credential is required.")
        if credential.auth_type not in {"api_key"}:
            raise AIGenerationError(f"Unsupported Minimax auth type: {credential.auth_type}")

        import requests
        response = requests.post(
            "https://api.minimax.io/v1/text/chatcompletion_v2",
            headers={
                "Authorization": f"Bearer {credential.secret}",
                "Content-Type": "application/json",
            },
            json={
                "model": "MiniMax-M3",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Return compact strict JSON only. No markdown, "
                            "no code fences, no commentary, no prose, "
                            "no trailing text."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "max_completion_tokens": 8192,
                "temperature": 0.2,
            },
            timeout=180,
        )
        if response.status_code != 200:
            raise AIGenerationError(f"Minimax API error: {response.status_code} - {response.text[:200]}")
        data = response.json()
        return _extract_minimax_text(data)

    def generate_from_prompt(self, prompt: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        return _parse_study_pack_json(self.generate_text(prompt, credential))


class ClaudeStudyPackProvider:
    name = "claude"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        return self.generate_from_prompt(_build_prompt(text), credential)

    def generate_text(self, prompt: str, credential: AICredential | None = None) -> str:
        if not credential or credential.provider != "claude":
            raise AIGenerationError("Claude credential is required.")
        if credential.auth_type not in {"api_key"}:
            raise AIGenerationError(f"Unsupported Claude auth type: {credential.auth_type}")

        import requests
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": credential.secret,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        if response.status_code != 200:
            raise AIGenerationError(f"Claude API error: {response.status_code} - {response.text[:200]}")
        data = response.json()
        content = data.get("content", [{}])[0].get("text", "")
        if not content:
            raise AIGenerationError("Claude returned empty response.")
        return content

    def generate_from_prompt(self, prompt: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        return _parse_study_pack_json(self.generate_text(prompt, credential))


def build_study_pack_prompt(text: str, num_flashcards: int = 3, num_mcqs: int = 5) -> str:
    sample = text[:10000]
    return (
        "You are preparing study material for Indian medical entrance exam revision. "
        "Return strict JSON with keys flashcards and mcqs. "
        f"Generate exactly {num_flashcards} flashcards and exactly {num_mcqs} MCQs. "
        "flashcards: array of objects with front and back. "
        "mcqs: array of objects with question, options (exactly 4 strings), answer_index (0-3), explanation. "
        "Cover key definitions, mechanisms, cause-effect links, comparisons, "
        "classifications, formulas, and exam-relevant traps. "
        "Make the MCQs NEET-style, concept-focused, and not trivial.\n\n"
        f"{sample}"
    )


def _build_prompt(text: str, num_flashcards: int = 3, num_mcqs: int = 5) -> str:
    return build_study_pack_prompt(text, num_flashcards=num_flashcards, num_mcqs=num_mcqs)


def normalize_generated_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", (value or "").strip().lower())
    value = re.sub(r"\s+", " ", value)
    return value


def merge_study_packs(*packs: GeneratedStudyPack) -> GeneratedStudyPack:
    flashcards: list[GeneratedFlashcard] = []
    mcqs: list[GeneratedMcq] = []
    seen_flashcards: set[tuple[str, str]] = set()
    seen_mcqs: set[str] = set()

    for pack in packs:
        for item in pack.flashcards:
            key = (normalize_generated_text(item.front), normalize_generated_text(item.back))
            if not key[0] or not key[1] or key in seen_flashcards:
                continue
            seen_flashcards.add(key)
            flashcards.append(item)

        for item in pack.mcqs:
            key = normalize_generated_text(item.question)
            if not key or key in seen_mcqs:
                continue
            seen_mcqs.add(key)
            mcqs.append(item)

    return GeneratedStudyPack(flashcards=flashcards, mcqs=mcqs)


def build_iterative_study_pack_prompt(
    text: str,
    *,
    mode: str,
    existing_flashcards: list[str] | None = None,
    existing_mcqs: list[str] | None = None,
    max_flashcards: int = 18,
    max_mcqs: int = 18,
) -> str:
    try:
        from app.core.config import settings

        prompt_cap = max(1000, int(getattr(settings, "ai_prompt_text_chars", 12000) or 12000))
    except Exception:  # noqa: BLE001 - never fail prompt building over config
        prompt_cap = 12000
    sample = text[:prompt_cap]
    existing_flashcards = existing_flashcards or []
    existing_mcqs = existing_mcqs or []

    mode_instructions = {
        "core": (
            "Extract core facts, definitions, names, classifications, direct "
            "recall points, and high-yield statements."
        ),
        "mechanisms": (
            "Extract mechanisms, pathways, processes, sequences, cause-effect "
            "links, relationships, and functional reasoning points."
        ),
        "traps": (
            "Extract comparisons, exceptions, confusing look-alikes, edge cases, "
            "exam traps, and application-focused concepts that make strong MCQs."
        ),
        "merged": (
            "Cover ALL THREE lenses in one balanced pass: (1) core facts, "
            "definitions, names, classifications, and high-yield statements; "
            "(2) mechanisms, pathways, processes, sequences, cause-effect "
            "links, and functional reasoning; (3) comparisons, exceptions, "
            "exam traps, and application-focused concepts."
        ),
    }
    instruction = mode_instructions.get(mode, mode_instructions["core"])

    # The 'avoid' list is a hint to the model about recent items so
    # it doesn't re-derive the same fact in different words. We trim
    # to 15 most-recent items at 80 chars each (~1.2KB total) so
    # the prompt stays small on later chunks of a long file. The
    # post-hoc dedup loop in job_worker.py catches exact duplicates
    # regardless, so the avoid list does not need to be exhaustive.
    flashcard_avoid = "\n".join(f"- {item[:80]}" for item in existing_flashcards[:15]) or "- none"
    mcq_avoid = "\n".join(f"- {item[:80]}" for item in existing_mcqs[:15]) or "- none"

    # Cache-first order (T7): the static instruction plus the source text form
    # a byte-identical prefix across every pass of a chunk, so the provider's
    # prompt cache can reuse them. The mode lens and avoid lists vary per pass
    # and sit after the text so they never break the shared prefix (measured
    # 86% cache-hit vs 0% before this reorder).
    return (
        "You are preparing dense study material for Indian medical entrance exam revision. "
        "Return compact strict JSON only with keys flashcards and mcqs. "
        f"Generate up to {max_flashcards} flashcards and up to {max_mcqs} "
        "MCQs, focusing on NEW items not already covered. "
        "Prefer maximum useful coverage, not minimal output. If the text "
        "supports many good items, fill the response close to capacity. "
        "Do not repeat meaning-equivalent items. "
        "Make MCQs concept-focused, exam-style, and not trivial.\n\n"
        "Source text:\n"
        f"{sample}\n\n"
        f"Current extraction mode: {instruction}\n\n"
        "Already covered flashcard fronts to avoid repeating:\n"
        f"{flashcard_avoid}\n\n"
        "Already covered MCQ questions to avoid repeating:\n"
        f"{mcq_avoid}\n\n"
        "Output contract:\n"
        "flashcards: array of objects with front and back. "
        "mcqs: array of objects with question, options (exactly 4 strings), answer_index (0-3), explanation."
    )


def build_title_generation_prompt(text: str, filename: str, archive_filename: str | None = None) -> str:
    sample = (text or "")[:12000]
    safe_filename = (filename or "upload.pdf").strip()
    safe_archive_filename = (archive_filename or "").strip()
    archive_context = ""
    if safe_archive_filename:
        archive_context = f"Archive filename: {safe_archive_filename}\n"
    return (
        "Return strict JSON only with keys title and description. "
        "Choose the best deck title from the source text itself, not just the filename. "
        "Prefer the real document or chapter/lesson title as a clean human title. "
        "If the source text clearly identifies the chapter or lesson number, format the "
        "title as 'Chapter {N} - {full chapter or lesson title}' or 'Lesson {N} - {full title}'. "
        "Pad the numeric portion with a leading zero to two digits (e.g. 'Lesson 02 - ...', "
        "'Lesson 10 - ...') so decks sort naturally in listings. "
        "If the source does not identify a chapter/lesson number, use the full title alone. "
        "Do not prefix the title with generic labels like Unit, Part, Module, or section "
        "numbers unless they are genuinely part of the natural title text. "
        "Use filenames only as weak fallback context. "
        "When the archive filename and inner PDF filename are the same or nearly the same, "
        "do not copy that repeated filename as the title unless the source text clearly "
        "confirms it is the document title. "
        "Do not include file extensions. Keep title under 255 characters. "
        "Description should be a concise 1-2 sentence summary under 500 characters. "
        f"{archive_context}"
        f"PDF filename: {safe_filename}\n\n"
        "Source text:\n"
        f"{sample}"
    )


def parse_title_generation_json(raw: str) -> tuple[str | None, str | None]:
    raw = (raw or "").strip()
    candidates = [raw]
    if "```" in raw:
        fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.S)
        candidates.extend(item.strip() for item in fenced if item.strip())
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        extracted = raw[start:end + 1].strip()
        if extracted and extracted not in candidates:
            candidates.append(extracted)

    data = None
    last_exc = None
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            break
        except json.JSONDecodeError as exc:
            last_exc = exc

    if data is None:
        raise AIGenerationError("AI provider returned invalid JSON for title generation.") from last_exc

    title = str(data.get("title", "") or "").strip() or None
    description = str(data.get("description", "") or "").strip() or None
    return title, description


def _parse_study_pack_json(raw: str) -> GeneratedStudyPack:
    raw = (raw or "").strip()
    candidates = [raw]

    if "```" in raw:
        import re
        fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", raw, flags=re.S)
        candidates.extend(item.strip() for item in fenced if item.strip())

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        extracted = raw[start:end + 1].strip()
        if extracted and extracted not in candidates:
            candidates.append(extracted)

    data = None
    last_exc = None
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            break
        except json.JSONDecodeError as exc:
            last_exc = exc

    if data is None:
        raise AIGenerationError("AI provider returned invalid JSON for study generation.") from last_exc

    flashcards = [
        GeneratedFlashcard(front=item["front"].strip(), back=item["back"].strip())
        for item in data.get("flashcards", [])
        if item.get("front") and item.get("back")
    ]
    mcqs: list[GeneratedMcq] = []
    for item in data.get("mcqs", []):
        options = [str(opt).strip() for opt in item.get("options", []) if str(opt).strip()]
        explanation = str(item.get("explanation", "")).strip()
        # Try to parse answer_index, handle cases where AI confuses fields
        answer_index = -1
        raw_answer = item.get("answer_index")
        if raw_answer is not None:
            # Direct number
            if isinstance(raw_answer, int):
                answer_index = raw_answer
            else:
                # Try parsing as number
                try:
                    answer_index = int(raw_answer)
                except (ValueError, TypeError):
                    # AI might have put answer text in answer_index - try to match against options
                    raw_str = str(raw_answer).strip().lower()
                    for idx, opt in enumerate(options):
                        if raw_str == opt.strip().lower() or raw_str in opt.strip().lower():
                            answer_index = idx
                            break
                    # Also check if explanation field contains a valid index
                    if answer_index == -1 and explanation:
                        try:
                            answer_index = int(explanation)
                        except (ValueError, TypeError):
                            pass
        if item.get("question") and len(options) == 4 and answer_index in {0, 1, 2, 3}:
            mcqs.append(
                GeneratedMcq(
                    question=item["question"].strip(),
                    options=options,
                    answer_index=answer_index,
                    explanation=str(item.get("explanation", "")).strip(),
                )
            )

    if not flashcards and not mcqs:
        raise AIGenerationError("AI provider did not return usable flashcards or MCQs.")
    return GeneratedStudyPack(flashcards=flashcards, mcqs=mcqs)


def get_study_pack_provider(name: str, session_id: str | None = None) -> StudyPackProvider:
    name = name.strip().lower()
    if name == "openai":
        return OpenAIStudyPackProvider()
    if name == "minimax":
        return MinimaxStudyPackProvider()
    if name == "claude":
        return ClaudeStudyPackProvider()
    if name == "opencode":
        return OpencodeStudyPackProvider(session_id=session_id)
    if name == "deepseek":
        return DeepSeekRevisionProvider(session_id=session_id)
    raise AIGenerationError(f"Unsupported AI study pack provider: {name}")


class DeepSeekRevisionProvider(_UsageAccumulatorMixin):
    """DeepSeek provider for revision-notes generation.

    Uses DeepSeek v4 Pro (reasoning model) via an OpenAI-compatible
    endpoint. Handles reasoning_content vs content split — extracts
    only the final content, ignoring chain-of-thought.
    """
    name = "deepseek"

    def __init__(self, session_id: str | None = None) -> None:
        from app.core.config import settings
        self.api_endpoint = (
            (settings.ai_endpoint or "").strip()
            or settings.revision_notes_api_endpoint
        )
        from app.services.ai_provider_config import effective_model

        self.model = effective_model()
        self.max_tokens = settings.revision_notes_max_tokens
        self.reasoning_effort = getattr(settings, "ai_reasoning_effort", "") or ""
        self._init_usage()
        # Stick to one session per provider instance so every call of the
        # same job/conversation reuses the same OpenCode routing bucket.
        self.session_id = session_id or opencode_session_id()

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        return self.generate_from_prompt(_build_prompt(text), credential)

    def generate_from_prompt(self, prompt: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        raw = self.generate_text(prompt, credential)
        return _parse_study_pack_json(raw)

    def _call_api(self, model: str, prompt: str, api_key: str) -> str:
        """Call the OpenAI-compatible endpoint with the given model."""
        import requests
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": 0.3,
        }
        # Reasoning tokens dominate cost (measured 2026-10-20: high 152, low
        # 110, none 80 output tokens/card). Empty keeps the provider default.
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        response = requests.post(
            self.api_endpoint,
            headers=_opencode_headers(api_key, self.session_id),
            json=payload,
            timeout=300,
        )
        if response.status_code != 200:
            body = response.text[:500]
            raise AIGenerationError(
                f"DeepSeek API error ({response.status_code}): {body}"
            )

        data = response.json()
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AIGenerationError("DeepSeek API returned no choices.")

        choice = choices[0] or {}
        _raise_if_truncated("DeepSeek", choice)
        self._record_usage("deepseek", model, data)

        message = choice.get("message", {})
        # DeepSeek v4 Pro is a reasoning model — it returns
        # reasoning_content (chain-of-thought) separately from
        # content (the actual answer). We only want content.
        content = message.get("content", "")
        if not content:
            raise AIGenerationError("DeepSeek API returned empty response.")
        return content

    def generate_text(self, prompt: str, credential: AICredential | None = None) -> str:
        if not credential or not credential.secret:
            raise AIGenerationError(
                "DeepSeek credential (API key) is required. "
                "Set AI_API_KEY in .env or configure a user/org credential."
            )

        api_key = credential.secret

        # Try primary model. If it's unavailable (503), the
        # higher-level code in generate_ai_topics() will catch
        # the exception and fall back to the heuristic path.
        return self._call_api(self.model, prompt, api_key)


def _opencode_timeout() -> int:
    """Read timeout for one OpenCode Go pass (seconds).

    Passes run sequentially, so this is the block size for a hung request; a
    healthy pass finishes in 10-40s.
    """
    try:
        from app.core.config import settings

        return max(10, int(getattr(settings, "opencode_request_timeout", 60) or 60))
    except Exception:  # noqa: BLE001 - never fail a request over config lookup
        return 60


def _opencode_max_tokens() -> int:
    """Completion budget for OpenCode Go.

    Requests asking for a long completion come back as empty-bodied 503s, so
    this is configurable and deliberately smaller than the old 16384.
    """
    try:
        from app.core.config import settings

        return max(256, int(getattr(settings, "opencode_max_tokens", 4096) or 4096))
    except Exception:  # noqa: BLE001 - never fail a request over config lookup
        return 4096


_UNSET = object()

# The study-card system message. Call sites that are not generating study
# cards (revision notes) pass system_prompt=None to keep their original
# prompt shape instead of inheriting "return flashcards and mcqs".
STUDY_CARD_SYSTEM_PROMPT = (
    "You are a precise study-card generator. "
    "Always produce valid JSON with keys flashcards and mcqs in your response. "
    "No markdown code fences, no extra commentary."
)


class OpencodeStudyPackProvider(_UsageAccumulatorMixin):
    """OpenCode provider via OpenAI-compatible HTTP API."""
    name = "opencode"

    def __init__(self, session_id: str | None = None) -> None:
        from app.core.config import settings
        self.api_endpoint = (
            (settings.ai_endpoint or "").strip()
            or settings.opencode_api_endpoint
        )
        from app.services.ai_provider_config import effective_model

        self.model = effective_model()
        # One session per provider instance: OpenCode Go wants a stable
        # x-opencode-session per conversation for routing + prompt caching.
        self.session_id = session_id or opencode_session_id()
        self._init_usage()

    @staticmethod
    def _messages_for(prompt: str, system_prompt: object) -> list[dict[str, str]]:
        """Build the message list; system_prompt=None means no system turn."""
        if system_prompt is _UNSET:
            system_prompt = STUDY_CARD_SYSTEM_PROMPT
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": str(system_prompt)})
        messages.append({"role": "user", "content": prompt})
        return messages

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        return self.generate_from_prompt(_build_prompt(text), credential)

    def generate_from_prompt(self, prompt: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        raw = self.generate_text(prompt, credential)
        return _parse_study_pack_json(raw)

    def generate_text(
        self,
        prompt: str,
        credential: AICredential | None = None,
        *,
        system_prompt: str | None | object = _UNSET,
        max_tokens: int | None = None,
        timeout: int | None = None,
    ) -> str:
        """Call the endpoint once.

        One provider and one model serve every AI call in the app. The
        completion budget and read timeout stay per-call because they are
        properties of the ask rather than of the model: a bulk pass wants a
        short ceiling so one hung request cannot block the serial worker,
        while revision notes legitimately streams ~15K chars over ~75s.
        """
        if not credential or not credential.secret:
            raise AIGenerationError(
                "OpenCode credential (API key) is required. "
                "Set AI_API_KEY in .env or configure a user/org credential."
            )

        import requests
        response = requests.post(
            self.api_endpoint,
            headers=_opencode_headers(credential.secret, self.session_id),
            json={
                "model": self.model,
                "messages": self._messages_for(prompt, system_prompt),
                "max_tokens": max_tokens or _opencode_max_tokens(),
                "temperature": 0.3,
            },
            timeout=timeout or _opencode_timeout(),
        )
        if response.status_code != 200:
            body = response.text[:300]
            hint = ""
            if "MissingSessionID" in body:
                hint = (
                    " (the x-opencode-session header was rejected; check "
                    "OPENCODE_CLIENT_UA / provider session id)"
                )
            raise AIGenerationError(
                f"OpenCode API error ({response.status_code}): {body}{hint}"
            )

        data = response.json()
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AIGenerationError("OpenCode API returned no choices.")

        choice = choices[0] or {}
        _raise_if_truncated("OpenCode", choice)
        self._record_usage("opencode", self.model, data)

        content = choice.get("message", {}).get("content", "")
        if not content:
            raise AIGenerationError("OpenCode API returned empty response.")
        return content


class CodexStudyPackProvider:
    """Codex CLI provider for study pack generation."""
    name = "codex"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        import subprocess
        import tempfile
        import os

        prompt = _build_prompt(text)

        # Create temp file with prompt
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            result = subprocess.run(
                ["codex", "exec", f"Return JSON only (no explanation): {prompt}"],
                cwd="/opt/edu_viz",
                capture_output=True,
                text=True,
                timeout=180,
            )
            output = result.stdout
            if not output:
                output = result.stderr
        except subprocess.TimeoutExpired:
            raise AIGenerationError("Codex CLI timed out.")
        except FileNotFoundError:
            raise AIGenerationError("Codex CLI not found. Install with: npm install -g @openai/codex")
        finally:
            os.unlink(prompt_file)

        if not output or "error" in output.lower():
            raise AIGenerationError(f"Codex CLI error: {output[:200]}")

        return _parse_study_pack_json(output)


def generate_study_pack(text: str, *, provider_name: str, credential: AICredential) -> GeneratedStudyPack:
    return get_study_pack_provider(provider_name).generate(text, credential)
