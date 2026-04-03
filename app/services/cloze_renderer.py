"""
Cloze text renderer for Anki-style cloze deletions.

Handles {{c1::text}} syntax to render:
- Front side: hidden gaps (placeholder dots)
- Back side: revealed text
"""

import re
from typing import Match

# Pattern: {{c1::text}} or {{c2::text with [brackets]}}
CLOZE_PATTERN = re.compile(r'\{\{c(\d+)::([^}]+)\}\}')

# Hidden placeholder (dots)
HIDDEN_PLACEHOLDER = "··" * 8  # "········"


def render_cloze_front(html: str) -> str:
    """
    Render cloze markup for the front side (question).

    Replaces {{cN::text}} with hidden spans showing placeholder dots.
    Clicking reveals the answer.

    Args:
        html: HTML string containing cloze markers

    Returns:
        HTML with cloze markers replaced by hidden spans
    """
    def replace_cloze(match: Match[str]) -> str:
        num = match.group(1)
        text = match.group(2)
        return f'<span class="cloze cloze-c{num}" data-answer="{escape_html(text)}">{HIDDEN_PLACEHOLDER}</span>'

    return CLOZE_PATTERN.sub(replace_cloze, html)


def render_cloze_back(html: str) -> str:
    """
    Render cloze markup for the back side (answer).

    Replaces {{cN::text}} with revealed spans showing the actual text.

    Args:
        html: HTML string containing cloze markers

    Returns:
        HTML with cloze markers replaced by revealed spans
    """
    def replace_cloze(match: Match[str]) -> str:
        num = match.group(1)
        text = match.group(2)
        return f'<span class="cloze cloze-c{num} cloze-revealed">{escape_html(text)}</span>'

    return CLOZE_PATTERN.sub(replace_cloze, html)


def extract_cloze_numbers(html: str) -> list[int]:
    """
    Extract all cloze numbers from an HTML string.

    Args:
        html: HTML string containing cloze markers

    Returns:
        List of unique cloze numbers (e.g., [1, 2, 3])
    """
    matches = CLOZE_PATTERN.findall(html)
    return sorted(set(int(num) for num, _ in matches))


def is_cloze_content(html: str) -> bool:
    """
    Check if HTML contains any cloze markers.

    Args:
        html: HTML string to check

    Returns:
        True if cloze markers are present
    """
    return bool(CLOZE_PATTERN.search(html))


def escape_html(text: str) -> str:
    """
    Escape HTML special characters for safe embedding in data attributes.

    Args:
        text: Raw text

    Returns:
        HTML-escaped text
    """
    return (
        text.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def strip_cloze(html: str) -> str:
    """
    Remove all cloze markers, keeping only the content.

    Useful for generating plain text front/back from cloze cards.

    Args:
        html: HTML string with cloze markers

    Returns:
        HTML with cloze markers removed, keeping inner text
    """
    return CLOZE_PATTERN.sub(lambda m: m.group(2), html)
