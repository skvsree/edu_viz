"""Test that the 'already covered' avoid list in study-pack prompts is
trimmed to the most recent 15 items at 80 chars each, not the full
40-item × 180-char bloated list it used to be.

Why: by chunk 5-6 of a 6-chunk file, the dedup list was ~7KB per
prompt. That's ~1,800 tokens of 'do not repeat' noise on every API
call. Trimming to 15×80 (~1.2KB / ~300 tokens) gives the model
enough recent-context signal to avoid the most-likely duplicates,
while the post-hoc dedup loop in job_worker.py catches exact
duplicates regardless of prompt size.

The dedup list is a *hint* to the model, not a hard constraint.
"""

from __future__ import annotations

from app.services.ai_generation import build_iterative_study_pack_prompt


def test_avoid_list_keeps_at_most_15_flashcards():
    """Even when 50 flashcards are passed in, only the first 15
    should appear in the rendered prompt. The model only needs
    recent context to avoid obvious rephrasings.
    """
    fronts = [f"flashcard front number {i:02d} with some padding text" for i in range(50)]
    prompt = build_iterative_study_pack_prompt(
        "source text " * 100,
        mode="core",
        existing_flashcards=fronts,
        existing_mcqs=[],
        max_flashcards=18,
        max_mcqs=18,
    )

    # The first 15 should appear; later ones should not.
    assert "flashcard front number 00 " in prompt
    assert "flashcard front number 14 " in prompt
    # The 16th onwards must be trimmed.
    assert "flashcard front number 15 " not in prompt
    assert "flashcard front number 49 " not in prompt


def test_avoid_list_keeps_at_most_15_mcqs():
    """Same for MCQ questions — trim to 15 most recent."""
    questions = [f"MCQ question text number {i:02d} extra padding" for i in range(50)]
    prompt = build_iterative_study_pack_prompt(
        "source text " * 100,
        mode="traps",
        existing_flashcards=[],
        existing_mcqs=questions,
        max_flashcards=18,
        max_mcqs=18,
    )

    assert "MCQ question text number 00 " in prompt
    assert "MCQ question text number 14 " in prompt
    assert "MCQ question text number 15 " not in prompt
    assert "MCQ question text number 49 " not in prompt


def test_avoid_list_truncates_each_item_to_80_chars():
    """Each entry is sliced to 80 chars max. A 500-char question
    must not occupy 500 chars of prompt real estate.
    """
    long_question = "X" * 500  # 500 chars
    prompt = build_iterative_study_pack_prompt(
        "source text " * 100,
        mode="core",
        existing_flashcards=[],
        existing_mcqs=[long_question],
        max_flashcards=18,
        max_mcqs=18,
    )

    # Find the line containing the avoid list entry. It must end at
    # 80 X's followed by the closing newline of the entry — not 500.
    rendered_line = next(
        (line for line in prompt.splitlines() if line.startswith("- ") and "X" in line),
        None,
    )
    assert rendered_line is not None, "expected to find the avoid-list line in the prompt"
    # Format is "- " (2 chars) + 80 X's = 82 chars total.
    assert len(rendered_line) == 82, (
        f"avoid-list line is {len(rendered_line)} chars, expected 82 "
        f"(2 for '- ' + 80 for the truncated question)"
    )


def test_avoid_list_shows_none_when_lists_empty():
    """When the caller passes empty lists (chunk 1 of a file), the
    rendered prompt must show '- none' as a placeholder so the model
    knows there are no prior items to avoid.
    """
    prompt = build_iterative_study_pack_prompt(
        "source text " * 100,
        mode="core",
        existing_flashcards=[],
        existing_mcqs=[],
        max_flashcards=18,
        max_mcqs=18,
    )

    assert "- none" in prompt


def test_avoid_list_prompt_size_reduced_significantly():
    """The whole point: prompt bloat. With 30 flashcards and 30 MCQs
    passed in, the rendered prompt's 'avoid' section should be small.
    Old code: 30×180 + 30×180 = 10,800 chars minimum in the avoid
    section. New code: 15×82 + 15×82 = 2,460 chars (the '+2' is for
    the '- ' prefix on each line). Verify the new prompt is much
    smaller than the old one.
    """
    fronts = [f"flashcard {i:02d} " + "padding " * 10 for i in range(30)]
    questions = [f"mcq {i:02d} " + "padding " * 10 for i in range(30)]

    prompt = build_iterative_study_pack_prompt(
        "source text " * 100,
        mode="core",
        existing_flashcards=fronts,
        existing_mcqs=questions,
        max_flashcards=18,
        max_mcqs=18,
    )

    # Locate the 'avoid' section. The marker is
    # 'Already covered flashcard fronts to avoid repeating:'.
    avoid_marker = "Already covered flashcard fronts to avoid repeating:"
    assert avoid_marker in prompt
    start = prompt.index(avoid_marker)
    # Take everything from the marker to the next blank line or the
    # next section header.
    avoid_section = prompt[start:start + 5000]
    # The old code would put all 30 flashcards (each 180 chars
    # × 30 = ~5,400 chars in this section alone). The new code
    # caps at 15 × 82 = ~1,230 chars. Verify we're well under 4KB
    # for the flashcard avoid section.
    fc_section_end = avoid_section.find("Already covered MCQ questions")
    fc_section = avoid_section[:fc_section_end] if fc_section_end > 0 else avoid_section
    assert len(fc_section) < 2000, (
        f"flashcard avoid section is {len(fc_section)} chars, expected <2000. "
        f"Old code would have ~5,400+ chars here."
    )
