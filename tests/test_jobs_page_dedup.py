"""Regression tests for the one-card-per-bulk UI on /settings/jobs.

Before this fix, the template iterated ``tab_jobs`` and rendered one
``<article class="jobs-bulk-card">`` per ``Job`` row. Because every
single-file retry created a new ``Job`` row, a single bulk could end
up with 10+ cards for the same upload. The fix introduced
``active_tab_bulks`` / ``history_tab_bulks`` (one entry per unique
bulk) and switched the template to iterate that list instead.

These tests pin that contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from app.api.routers import pages
from app.services.access import ROLE_SYSTEM_ADMIN
from tests.test_dashboard_routes import FakeDB, make_request, render_body


class FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class JobsPageDB(FakeDB):
    """Minimal DB stub matching ``JobsSettingsDB`` semantics — one list
    popped per ``execute()`` call."""

    def __init__(self, execute_results=None):
        super().__init__(objects={})
        self.execute_results = list(execute_results or [])

    def execute(self, stmt):
        result = self.execute_results.pop(0) if self.execute_results else []
        if isinstance(result, list) and result and isinstance(result[0], tuple):
            return SimpleNamespace(all=lambda: list(result))
        return FakeResult(result)


def _make_admin():
    return SimpleNamespace(
        id=uuid4(),
        role=ROLE_SYSTEM_ADMIN,
        organization_id=None,
        email="root@example.com",
        identity_sub="root-sub",
    )


def _make_job(*, reference_id, status, created_at=None, total_items=2):
    return SimpleNamespace(
        id=uuid4(),
        job_type="bulk_ai_upload",
        status=status,
        processed_items=0,
        total_items=total_items,
        failed_items=0,
        created_at=created_at,
        completed_at=None,
        reference_id=reference_id,
    )


def _make_bulk(*, bulk_id, status="processing", filename="batch.zip", deck_id=None):
    bulk = SimpleNamespace(
        id=bulk_id,
        filename=filename,
        total_files=2,
        status=status,
        deck_id=deck_id,
    )
    return bulk


def test_jobs_page_active_tab_renders_one_card_per_bulk_when_retry_dupes_job():
    """The core regression: 3 Job rows for the same bulk → 1 card on
    the page, plus a retries badge."""
    admin = _make_admin()
    bulk_id = uuid4()

    # Three Job rows, all referencing the same bulk (the bug shape):
    # 1st attempt created job #1, force-retry created job #2,
    # single-file retry created job #3.
    job1 = _make_job(reference_id=bulk_id, status="completed")
    job2 = _make_job(reference_id=bulk_id, status="failed")
    job3 = _make_job(reference_id=bulk_id, status="processing")

    bulk = _make_bulk(bulk_id=bulk_id)

    # Slots: [Job list, Bulk list, Deck list, child_rows, file_rows]
    db = JobsPageDB([[job1, job2, job3], [bulk], [], [], []])

    response = pages.jobs_page(
        make_request(path="/settings/jobs", query_string=b"tab=active"),
        user=admin,
        db=db,
    )

    body = render_body(response)
    assert response.status_code == 200

    # Count <article class="jobs-bulk-card"> occurrences in the rendered body.
    card_count = body.count('class="jobs-bulk-card"')
    assert card_count == 1, (
        f"Expected exactly one card per bulk, got {card_count}. "
        "The template is still iterating Job rows instead of bulks."
    )

    # Retries badge should be visible with the right count.
    assert 'class="jobs-bulk-card__retries-badge"' in body, (
        "Retries badge missing — bulks with multiple Job rows should "
        "show a '3 retries' indicator so users know the upload was retried."
    )
    assert ">3 retries<" in body, (
        "Retries badge should show '3 retries' for a bulk with 3 Job rows."
    )
    # Bulk id should appear on the article element AND on at least one
    # retry/cancel button (which is fine — they're deduped to the same bulk).
    # The point is the bulk is rendered once, not the count of bulk-id references.
    article_marker = f'<article class="jobs-bulk-card" data-bulk-id="{bulk_id}">'
    assert article_marker in body, (
        f"Article tag with data-bulk-id={bulk_id} not found. "
        "The card is not being rendered with the bulk_id attribute."
    )


def test_jobs_page_active_tab_no_retries_badge_when_single_job():
    """A bulk with exactly one Job row should NOT show the retries badge."""
    admin = _make_admin()
    bulk_id = uuid4()

    job = _make_job(reference_id=bulk_id, status="processing")
    bulk = _make_bulk(bulk_id=bulk_id)
    db = JobsPageDB([[job], [bulk], [], [], []])

    response = pages.jobs_page(
        make_request(path="/settings/jobs", query_string=b"tab=active"), user=admin, db=db
    )
    body = render_body(response)

    assert response.status_code == 200
    assert 'class="jobs-bulk-card__retries-badge"' not in body, (
        "Retries badge should only appear when job_count > 1."
    )
    # The card itself should still render once.
    assert body.count('class="jobs-bulk-card"') == 1


def test_jobs_page_active_tab_renders_distinct_bulks_as_distinct_cards():
    """Two different bulks → two cards. Confirms we collapsed within a
    bulk but did NOT collapse across bulks."""
    admin = _make_admin()
    bulk_a, bulk_b = uuid4(), uuid4()

    job_a1 = _make_job(reference_id=bulk_a, status="processing")
    job_a2 = _make_job(reference_id=bulk_a, status="failed")  # retry of A
    job_b1 = _make_job(reference_id=bulk_b, status="processing")

    bulk_a_obj = _make_bulk(bulk_id=bulk_a, filename="a.zip")
    bulk_b_obj = _make_bulk(bulk_id=bulk_b, filename="b.zip")

    db = JobsPageDB(
        [
            [job_a1, job_a2, job_b1],
            [bulk_a_obj, bulk_b_obj],
            [],
            [],
            [],
        ]
    )

    response = pages.jobs_page(
        make_request(path="/settings/jobs", query_string=b"tab=active"), user=admin, db=db
    )
    body = render_body(response)

    assert response.status_code == 200
    assert body.count('class="jobs-bulk-card"') == 2, (
        "Two distinct bulks should produce two cards, even when one bulk "
        "has multiple Job rows."
    )
    # Bulk A has 2 jobs → badge; Bulk B has 1 job → no badge.
    assert body.count('class="jobs-bulk-card__retries-badge"') == 1, (
        "Only Bulk A should show the retries badge (2 jobs > 1)."
    )
    assert ">2 retries<" in body
    assert f'data-bulk-id="{bulk_a}"' in body
    assert f'data-bulk-id="{bulk_b}"' in body


def test_jobs_page_tab_counts_reflect_bulks_not_jobs():
    """The Active tab count should be the number of unique bulks, not
    the number of Job rows. With 4 Job rows across 2 bulks, Active count
    should be 2."""
    admin = _make_admin()
    bulk_a, bulk_b = uuid4(), uuid4()

    jobs = [
        _make_job(reference_id=bulk_a, status="processing"),
        _make_job(reference_id=bulk_a, status="failed"),
        _make_job(reference_id=bulk_b, status="processing"),
        _make_job(reference_id=bulk_b, status="processing"),
    ]
    bulks = [
        _make_bulk(bulk_id=bulk_a, filename="a.zip"),
        _make_bulk(bulk_id=bulk_b, filename="b.zip"),
    ]
    db = JobsPageDB([jobs, bulks, [], [], []])

    response = pages.jobs_page(
        make_request(path="/settings/jobs", query_string=b"tab=active"), user=admin, db=db
    )
    body = render_body(response)

    assert response.status_code == 200

    # The Active tab button contains a span with the count. We assert
    # the count is 2 (bulks), not 4 (jobs).
    import re
    match = re.search(
        r'<a[^>]*href="/settings/jobs\?tab=active"[^>]*>.*?'
        r'<span class="jobs-page__tab-count">(\d+)</span>',
        body,
        re.DOTALL,
    )
    assert match is not None, "Could not find Active tab count span"
    count = int(match.group(1))
    assert count == 2, (
        f"Active tab count should be 2 (unique bulks), got {count}. "
        "The page is counting Job rows, not bulks."
    )
