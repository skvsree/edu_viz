from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol


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


class OpenAIStudyPackProvider:
    name = "openai"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        if not credential or credential.provider != "openai":
            raise AIGenerationError("OpenAI credential is required.")
        if credential.auth_type not in {"api_key", "oauth"}:
            raise AIGenerationError(f"Unsupported OpenAI auth type: {credential.auth_type}")

        from openai import OpenAI

        client = OpenAI(api_key=credential.secret)
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=_build_prompt(text),
        )
        return _parse_study_pack_json(response.output_text)


class MinimaxStudyPackProvider:
    name = "minimax"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
        if not credential or credential.provider != "minimax":
            raise AIGenerationError("Minimax credential is required.")
        if credential.auth_type not in {"api_key"}:
            raise AIGenerationError(f"Unsupported Minimax auth type: {credential.auth_type}")

        import requests
        response = requests.post(
            "https://api.minimax.chat/v1/text/chatcompletion_pro",
            headers={
                "Authorization": f"Bearer {credential.secret}",
                "Content-Type": "application/json",
            },
            json={
                "model": "MiniMax-Text-01",
                "messages": [{"role": "user", "content": _build_prompt(text)}],
                "max_tokens": 8192,
                "temperature": 0.7,
            },
            timeout=120,
        )
        if response.status_code != 200:
            raise AIGenerationError(f"Minimax API error: {response.status_code} - {response.text[:200]}")
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            raise AIGenerationError("Minimax returned empty response.")
        return _parse_study_pack_json(content)


class ClaudeStudyPackProvider:
    name = "claude"

    def generate(self, text: str, credential: AICredential | None = None) -> GeneratedStudyPack:
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
                "messages": [{"role": "user", "content": _build_prompt(text)}],
            },
            timeout=120,
        )
        if response.status_code != 200:
            raise AIGenerationError(f"Claude API error: {response.status_code} - {response.text[:200]}")
        data = response.json()
        content = data.get("content", [{}])[0].get("text", "")
        if not content:
            raise AIGenerationError("Claude returned empty response.")
        return _parse_study_pack_json(content)


def _build_prompt(text: str) -> str:
    sample = text[:12000]
    return (
        "You are preparing study material for Indian medical entrance exam revision. "
        "Return strict JSON with keys flashcards and mcqs. "
        "flashcards: array of objects with front and back. "
        "mcqs: array of objects with question, options (exactly 4 strings), answer_index (0-3), explanation. "
        "Generate a comprehensive, high-value study pack from this source text. "
        "Include as many useful flashcards and MCQs as the material naturally supports without padding or repetition. "
        "Cover key definitions, mechanisms, cause-effect links, comparisons, classifications, formulas, and exam-relevant traps. "
        "Make the MCQs NEET-style, concept-focused, and not trivial.\n\n"
        f"{sample}"
    )


def _parse_study_pack_json(raw: str) -> GeneratedStudyPack:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIGenerationError("AI provider returned invalid JSON for study generation.") from exc

    flashcards = [
        GeneratedFlashcard(front=item["front"].strip(), back=item["back"].strip())
        for item in data.get("flashcards", [])
        if item.get("front") and item.get("back")
    ]
    mcqs: list[GeneratedMcq] = []
    for item in data.get("mcqs", []):
        options = [str(opt).strip() for opt in item.get("options", []) if str(opt).strip()]
        answer_index = int(item.get("answer_index", -1))
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
    raise AIGenerationError(f"Unsupported AI study pack provider: {name}")


def generate_study_pack(text: str, *, provider_name: str, credential: AICredential) -> GeneratedStudyPack:
    return get_study_pack_provider(provider_name).generate(text, credential)
