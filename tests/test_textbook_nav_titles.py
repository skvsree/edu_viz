"""Deck titles must come from the textbook's own navigation block.

Context (prod, 2026-09-26)
--------------------------
Class I English decks were named from an AI title guess, which produced
"Chapter 02 - Life Around Us" for what is *Chapter 1 of Unit 2*, and four
different decks all reading "Chapter 2" — Mridang restarts chapter numbering in
every unit (Unit 1: 1-2, Unit 2: 1-3, Unit 3: 1-2, Unit 4: 1-2).

The text samples below are verbatim line sequences from the real NCERT PDFs
(aemr101..aemr109, aejm101), including the InDesign slug lines
("Chapter 1.indd 1 1/17/2025 12:09:01 PM") that appear on every NCERT page and
must never be mistaken for the printed chapter.
"""
from __future__ import annotations

from app.services.textbook_nav import (
    build_deck_title,
    derive_nav_title,
    extract_nav_block,
)

MRIDANG_UNIT_AND_CHAPTER = """Two little hands
go clap, clap, clap.
Two little legs
go tap, tap, tap.
Unit 1
My Family and Me
Chapter 1
Two Little Hands
Chapter 1.indd 1 11-01-2024 04:14:39
Chapter 1.indd 2Chapter 1.indd 2 18-05-2023 14:39:5118-05-2023 14:39:
"""

MRIDANG_UNIT_AND_CHAPTER_AFTER_RUNNING_TEXT = """Let us speak
Unit 3
Food
Chapter 1
Fun with Pictures
Reprint 2026-27
85
Mridang
(a) What do you see in this picture?
"""

MRIDANG_CHAPTER_ONLY = """Once there was a man who
sold caps. He carried many
caps in a basket on his head.
He was a cap-seller.
One day, he slept under a tree.
Let us read
Chapter 2
The Cap-seller and the Monkeys
Reprint 2026-27
Chapter 2.indd 54 2/26/2024 10:19:59 AM
"""

# aemr102: prints only "Chapter 2", no Unit line at all.
MRIDANG_CHAPTER_ONLY_NO_UNIT_LINE = """Let us read
When I meet someone in
the afternoon, I say ‘Good
afternoon’.
Chapter 2
Greetings
Chapter 1.indd 15 2/26/2024 10:18:24 AM
"""

# The Joyful Mathematics shape: no navigation block, only InDesign slugs whose
# number tracks the source document, not a printed chapter heading.
MATHS_SLUG_ONLY = """Finding the
Furry Cat!1
Let us Sing
Looking, looking, looking
Looking for my furry cat!
Chapter 1.indd 1 1/17/2025 12:09:01 PM
Chapter 1.indd 2 1/17/2025 12:07:49 PM
"""


def test_unit_and_chapter_are_read_off_the_page():
    title, unit = derive_nav_title(MRIDANG_UNIT_AND_CHAPTER)
    assert title == "Unit 1 · Ch 1 · Two Little Hands"
    assert unit == 1


def test_navigation_block_is_found_after_running_text_and_page_furniture():
    title, unit = derive_nav_title(MRIDANG_UNIT_AND_CHAPTER_AFTER_RUNNING_TEXT)
    assert title == "Unit 3 · Ch 1 · Fun with Pictures"
    assert unit == 3


def test_chapter_without_a_unit_line_uses_the_carried_unit():
    """aemr104 prints "Chapter 2" only; the walk of the book supplies Unit 2."""
    block = extract_nav_block(MRIDANG_CHAPTER_ONLY)
    assert block is not None and block.unit_no is None and block.chapter_no == 2

    assert build_deck_title(block) == "Ch 2 · The Cap-seller and the Monkeys"
    assert build_deck_title(block, carried_unit=2) == (
        "Unit 2 · Ch 2 · The Cap-seller and the Monkeys"
    )


def test_carried_unit_is_not_reported_as_a_declared_unit():
    """The caller keeps its own carry-forward value when the page omits the unit."""
    title, declared = derive_nav_title(MRIDANG_CHAPTER_ONLY_NO_UNIT_LINE, carried_unit=1)
    assert title == "Unit 1 · Ch 2 · Greetings"
    assert declared is None


def test_maths_slugs_are_not_a_navigation_block():
    """No printed chapter heading → no derived title, so the AI name stands."""
    assert extract_nav_block(MATHS_SLUG_ONLY) is None
    assert derive_nav_title(MATHS_SLUG_ONLY) == (None, None)


def test_inline_navigation_form_is_understood():
    title, unit = derive_nav_title("Chapter 4 - Dotty Bug and her Designs")
    assert title == "Ch 4 · Dotty Bug and her Designs"
    assert unit is None


def test_slug_number_never_becomes_the_chapter_number():
    """The aemr103 shape: every page says "Chapter 2", the page prints Chapter 1."""
    text = """Let us speak
Unit 2
Life Around Us
Chapter 1
Picture Time
Chapter 2.indd 47 12-01-2024 04:36:26
Chapter 2.indd 48Chapter 2.indd 48 18-05-2023 12:51:0418-05-2023 12:5
"""
    block = extract_nav_block(text)
    assert block is not None
    assert block.chapter_no == 1, "the InDesign slug number leaked into the chapter number"
    assert build_deck_title(block, carried_unit=2) == "Unit 2 · Ch 1 · Picture Time"


def test_empty_and_blank_documents_yield_nothing():
    assert extract_nav_block(None) is None
    assert extract_nav_block("") is None
    assert extract_nav_block("Just some prose without any navigation.\n") is None
