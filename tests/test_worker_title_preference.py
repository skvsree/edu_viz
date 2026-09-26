"""The worker must prefer a document's own navigation-block title.

The AI/derived title is a guess and it guessed badly on NCERT Mridang: it
returned "Chapter 02 - Life Around Us" (the unit number on the unit's title) for
*Ch 1 of Unit 2*, so four Class I English decks all read "Chapter 2". The
navigation block on the page is authoritative, so it wins whenever it exists.
"""
from __future__ import annotations

from app.services.job_worker import pick_file_title


def test_navigation_title_wins_over_the_derived_one():
    title, source = pick_file_title(
        nav_title="Unit 2 · Ch 1 · Picture Time",
        derived_title="Chapter 02 - Life Around Us",
    )
    assert title == "Unit 2 · Ch 1 · Picture Time"
    assert source == "nav"


def test_derived_title_still_used_when_the_document_has_no_nav_block():
    title, source = pick_file_title(
        nav_title=None,
        derived_title="Chapter 01 - Finding the Furry Cat!",
    )
    assert title == "Chapter 01 - Finding the Furry Cat!"
    assert source == "derived"


def test_no_title_at_all_is_reported_as_none():
    title, source = pick_file_title(nav_title=None, derived_title=None)
    assert title is None
    assert source == "none"


def test_title_is_capped_for_the_deck_name_column():
    title, _source = pick_file_title(nav_title="x" * 400, derived_title=None)
    assert title is not None and len(title) == 250
