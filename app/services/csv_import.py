from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import BinaryIO


class CsvImportError(ValueError):
    """Raised when a CSV import cannot be processed safely."""


@dataclass(slots=True)
class ImportedCardRow:
    front: str
    back: str
    row_number: int


_HEADER_ALIASES = {
    "front": {"front", "question", "prompt", "cue", "term"},
    "back": {"back", "answer", "response", "definition", "explanation"},
}


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _resolve_columns(fieldnames: list[str] | None) -> tuple[str, str]:
    if not fieldnames:
        raise CsvImportError(
            "The CSV file is empty or missing a header row. Include headers such as question/answer or front/back."
        )

    normalized_to_original = {_normalize_header(name): name for name in fieldnames if name and name.strip()}

    front_column = next(
        (normalized_to_original[key] for key in _HEADER_ALIASES["front"] if key in normalized_to_original),
        None,
    )
    back_column = next(
        (normalized_to_original[key] for key in _HEADER_ALIASES["back"] if key in normalized_to_original),
        None,
    )

    if not front_column or not back_column:
        headers = ", ".join(fieldnames)
        raise CsvImportError(
            "Missing required columns. Add question/answer, front/back, or prompt/response headers. "
            f"Found headers: {headers or '(none)'}"
        )

    return front_column, back_column


def parse_cards_csv(file_obj: BinaryIO) -> list[ImportedCardRow]:
    text_stream = io.TextIOWrapper(file_obj, encoding="utf-8-sig", newline="")

    try:
        reader = csv.DictReader(text_stream)
        front_column, back_column = _resolve_columns(reader.fieldnames)

        rows: list[ImportedCardRow] = []
        for row_number, row in enumerate(reader, start=2):
            if row is None:
                continue

            if None in row:
                raise CsvImportError(
                    f"CSV formatting error near row {row_number}. Please check quotes, commas, and column alignment."
                )

            front = (row.get(front_column) or "").strip()
            back = (row.get(back_column) or "").strip()

            if not front and not back and not any((value or "").strip() for value in row.values()):
                continue

            if not front or not back:
                missing = "question/front" if not front else "answer/back"
                raise CsvImportError(f"Row {row_number} is missing {missing} text.")

            rows.append(ImportedCardRow(front=front, back=back, row_number=row_number))
    except UnicodeDecodeError as exc:
        raise CsvImportError("The CSV file must be UTF-8 encoded.") from exc
    except csv.Error as exc:
        raise CsvImportError(f"Malformed CSV: {exc}") from exc
    finally:
        text_stream.detach()

    if not rows:
        raise CsvImportError("No usable cards were found in the CSV file.")

    return rows
