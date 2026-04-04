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
    if name.strip().lower() == "openai":
        return OpenAIStudyPackProvider()
    raise AIGenerationError(f"Unsupported AI study pack provider: {name}")


def generate_study_pack(text: str, *, provider_name: str, credential: AICredential) -> GeneratedStudyPack:
    return get_study_pack_provider(provider_name).generate(text, credential)
