from __future__ import annotations

import json
from dataclasses import dataclass

from openai import OpenAI

from app.core.config import settings


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


def _build_prompt(text: str, flashcard_count: int, mcq_count: int) -> str:
    sample = text[:12000]
    return (
        "You are preparing study material for Indian medical entrance exam revision. "
        "Return strict JSON with keys flashcards and mcqs. "
        "flashcards: array of objects with front and back. "
        "mcqs: array of objects with question, options (exactly 4 strings), answer_index (0-3), explanation. "
        f"Generate {flashcard_count} flashcards and {mcq_count} mcqs from this source text. "
        "Make the MCQs NEET-style, concept-focused, and not trivial.\n\n"
        f"{sample}"
    )


def generate_study_pack(text: str, *, flashcard_count: int = 12, mcq_count: int = 10) -> GeneratedStudyPack:
    if not settings.openai_generation_enabled:
        raise AIGenerationError("OpenAI generation is disabled by configuration.")
    if not settings.openai_api_key:
        raise AIGenerationError("OpenAI API key is not configured.")

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.responses.create(
        model=settings.openai_model,
        input=_build_prompt(text, flashcard_count, mcq_count),
    )
    raw = response.output_text
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIGenerationError("OpenAI returned invalid JSON for study generation.") from exc

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
        raise AIGenerationError("OpenAI did not return usable flashcards or MCQs.")
    return GeneratedStudyPack(flashcards=flashcards, mcqs=mcqs)
