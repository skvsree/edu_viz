from __future__ import annotations

from io import BytesIO

from docx import Document
from pypdf import PdfReader


class ContentExtractionError(ValueError):
    pass


def extract_text(filename: str, payload: bytes) -> str:
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        reader = PdfReader(BytesIO(payload))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    elif lower.endswith(".docx"):
        document = Document(BytesIO(payload))
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    else:
        raise ContentExtractionError("Only PDF and DOCX files are supported.")

    cleaned = text.strip()
    if not cleaned:
        raise ContentExtractionError("Could not extract readable text from this file.")
    return cleaned
