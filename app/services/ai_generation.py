from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol
import re
import unicodedata

logger = logging.getLogger(__name__)


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
                "model": "MiniMax-M2",
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
    sample = text[:12000]
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
    }
    instruction = mode_instructions.get(mode, mode_instructions["core"])

    flashcard_avoid = "\n".join(f"- {item[:180]}" for item in existing_flashcards[:40]) or "- none"
    mcq_avoid = "\n".join(f"- {item[:180]}" for item in existing_mcqs[:40]) or "- none"

    return (
        "You are preparing dense study material for Indian medical entrance exam revision. "
        "Return compact strict JSON only with keys flashcards and mcqs. "
        f"Generate up to {max_flashcards} flashcards and up to {max_mcqs} "
        "MCQs, focusing on NEW items not already covered. "
        "Prefer maximum useful coverage, not minimal output. If the text "
        "supports many good items, fill the response close to capacity. "
        "Do not repeat meaning-equivalent items. "
        "flashcards: array of objects with front and back. "
        "mcqs: array of objects with question, options (exactly 4 strings), answer_index (0-3), explanation. "
        "Make MCQs concept-focused, exam-style, and not trivial. "
        f"Current extraction mode: {instruction}\n\n"
        "Already covered flashcard fronts to avoid repeating:\n"
        f"{flashcard_avoid}\n\n"
        "Already covered MCQ questions to avoid repeating:\n"
        f"{mcq_avoid}\n\n"
        "Source text:\n"
        f"{sample}"
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
        "Prefer the real document or chapter title as a clean human title. "
        "Do not prefix the title with labels like Chapter, Lesson, Unit, Part, "
        "Module, or section numbers unless they are genuinely part of the natural title text. "
        "If the PDF is one chapter from a larger book, prefer the chapter's actual "
        "title text without adding synthetic prefixes like 'Chapter 3 -'. "
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


def get_study_pack_provider(name: str) -> StudyPackProvider:
    name = name.strip().lower()
    if name == "openai":
        return OpenAIStudyPackProvider()
    if name == "minimax":
        return MinimaxStudyPackProvider()
    if name == "claude":
        return ClaudeStudyPackProvider()
    if name == "opencode":
        return OpencodeStudyPackProvider()
    raise AIGenerationError(f"Unsupported AI study pack provider: {name}")


class OpencodeStudyPackProvider:
    """OpenCode provider using MiniMax API."""
    name = "opencode"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        return self.generate_from_prompt(_build_prompt(text), credential)

    def generate_from_prompt(self, prompt: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        if not credential or credential.provider != "minimax":
            raise AIGenerationError("Minimax credential is required for opencode provider.")
        if credential.auth_type not in {"api_key"}:
            raise AIGenerationError(f"Unsupported auth type: {credential.auth_type}")

        import requests
        response = requests.post(
            "https://api.minimax.io/v1/text/chatcompletion_v2",
            headers={
                "Authorization": f"Bearer {credential.secret}",
                "Content-Type": "application/json",
            },
            json={
                "model": "MiniMax-M2",
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
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise AIGenerationError("Minimax returned empty response.")
        return _parse_study_pack_json(content)


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
