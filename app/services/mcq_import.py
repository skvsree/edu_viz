from __future__ import annotations

import json
from dataclasses import dataclass


class McqImportError(ValueError):
    pass


@dataclass(slots=True)
class ImportedMcq:
    question: str
    options: list[str]
    answer_index: int
    explanation: str


def parse_mcq_json(raw: bytes) -> list[ImportedMcq]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise McqImportError("Invalid JSON file.") from exc

    items = payload.get("mcqs") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        raise McqImportError("JSON must be an array of MCQs or an object with a non-empty 'mcqs' array.")

    parsed: list[ImportedMcq] = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise McqImportError(f"MCQ #{idx} must be an object.")
        question = str(item.get("question", "")).strip()
        options = item.get("options")
        explanation = str(item.get("explanation", "")).strip()
        answer_index = item.get("answer_index")
        if not question:
            raise McqImportError(f"MCQ #{idx} is missing question.")
        if not isinstance(options, list) or len(options) != 4 or any(not str(option).strip() for option in options):
            raise McqImportError(f"MCQ #{idx} must have exactly 4 non-empty options.")
        if not isinstance(answer_index, int) or answer_index not in {0, 1, 2, 3}:
            raise McqImportError(f"MCQ #{idx} must have answer_index between 0 and 3.")
        parsed.append(
            ImportedMcq(
                question=question,
                options=[str(option).strip() for option in options],
                answer_index=answer_index,
                explanation=explanation,
            )
        )
    return parsed
