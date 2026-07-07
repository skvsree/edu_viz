"""Dedupe BulkAIUploadFile rows created by force-retry against legacy bulks.

Symptom
-------
Force-retry on a bulk upload whose file rows pre-date the
BulkAIUploadChildFile relationship (i.e. ``child_file_id IS NULL``) creates
a *new* file row that lacks a child_file_id. The retry path also does not
dedupe on ``(original_filename, storage_key)`` while the source row's
child_file_id is NULL, so the new row is treated as a brand-new file by
``_latest_bulk_attempt_rows()``. The user ends up seeing N+1 files in the
bulk where there should be N.

This script inspects every bulk and, for each
``(original_filename, storage_key)`` group that has more than one row, marks
all but the *latest* row as ``status='stopped'`` with
``error_message='Superseded by retry (cleanup migration)'``. It deliberately
does NOT touch rows in ``processing`` or ``completed`` states — those
represent real work that must finish.

Usage
-----
    # Dry run (default): prints what would change, no DB writes.
    python scripts/dedupe_bulk_ai_upload_files.py

    # Apply changes for real.
    python scripts/dedupe_bulk_ai_upload_files.py --apply

    # Only act on a specific bulk (useful for the in-flight 94cba926 bulk).
    python scripts/dedupe_bulk_ai_upload_files.py --apply --bulk-id 94cba926-...

Safety
------
- Only rows in ``pending`` / ``stopped`` / ``failed`` states are touched.
- Rows in ``processing`` are left alone (the worker is actively using them).
- Rows in ``completed`` are left alone (their cards are persisted; we would
  lose user-visible work by marking them superseded).
- The "winner" of each dedup group is the row with the most recent
  ``created_at`` — which is the one the most-recent retry created.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from uuid import UUID

# Allow running as ``python scripts/dedupe_bulk_ai_upload_files.py`` from
# the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.models.bulk_ai_upload import (  # noqa: E402
    BulkAIUploadFile,
    BulkAIUploadFileStatus,
)


SUPERSEDED_MESSAGE = "Superseded by retry (cleanup migration)"

# States we will *touch*. Rows in `processing` or `completed` are sacred.
TOUCHABLE_STATUSES = {
    BulkAIUploadFileStatus.PENDING.value,
    BulkAIUploadFileStatus.STOPPED.value,
    BulkAIUploadFileStatus.FAILED.value,
    BulkAIUploadFileStatus.SKIPPED.value,
}


def find_duplicate_groups(
    db,
    bulk_id: UUID | None = None,
) -> dict[UUID, dict[tuple[str, str | None], list[BulkAIUploadFile]]]:
    """Return {bulk_id: {(filename, storage_key): [rows, ...]}} for every
    bulk that has more than one row sharing the same (filename, storage_key).
    """
    stmt = select(BulkAIUploadFile).order_by(
        BulkAIUploadFile.bulk_upload_id,
        BulkAIUploadFile.original_filename,
        BulkAIUploadFile.storage_key,
        BulkAIUploadFile.created_at,
    )
    if bulk_id is not None:
        stmt = stmt.where(BulkAIUploadFile.bulk_upload_id == bulk_id)
    rows = db.execute(stmt).scalars().all()

    by_bulk: dict[UUID, dict[tuple[str, str | None], list[BulkAIUploadFile]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        key = (row.original_filename, row.storage_key)
        by_bulk[row.bulk_upload_id][key].append(row)

    # Drop groups that don't actually have duplicates.
    return {
        bulk_id_value: {key: rows for key, rows in groups.items() if len(rows) > 1}
        for bulk_id_value, groups in by_bulk.items()
        if any(len(rows) > 1 for rows in groups.values())
    }


def pick_winner(rows: list[BulkAIUploadFile]) -> BulkAIUploadFile:
    """Pick the row to keep for a duplicate group.

    Rules (in order):
    1. Prefer a row in a "live" state (pending, processing) — those represent
       work the worker is supposed to finish. If multiple live rows exist,
       the most recent one wins.
    2. Otherwise prefer the most recent ``created_at`` — that's the retry
       attempt the user most recently created.
    """
    live_states = {BulkAIUploadFileStatus.PENDING.value, BulkAIUploadFileStatus.PROCESSING.value}
    live = [r for r in rows if (r.status or "").lower() in live_states]
    candidates = live if live else rows
    return max(candidates, key=lambda r: r.created_at or r.started_at or r.completed_at)


def plan_dedup(
    groups: dict[UUID, dict[tuple[str, str | None], list[BulkAIUploadFile]]],
) -> list[tuple[BulkAIUploadFile, BulkAIUploadFile]]:
    """Return [(loser, winner), ...] pairs that would be marked superseded.

    The winner is left untouched. Losers that are in a sacred state
    (``processing`` or ``completed``) are skipped — they shouldn't be
    superseded.
    """
    actions: list[tuple[BulkAIUploadFile, BulkAIUploadFile]] = []
    for _bulk_id, group_map in groups.items():
        for _key, rows in group_map.items():
            winner = pick_winner(rows)
            for row in rows:
                if row.id == winner.id:
                    continue
                if (row.status or "").lower() not in TOUCHABLE_STATUSES:
                    continue
                actions.append((row, winner))
    return actions


def apply_actions(actions: list[tuple[BulkAIUploadFile, BulkAIUploadFile]]) -> None:
    """Mark each loser row as superseded. Caller must own the DB session."""
    for loser, _winner in actions:
        loser.status = BulkAIUploadFileStatus.STOPPED.value
        loser.error_message = SUPERSEDED_MESSAGE
        if not loser.completed_at:
            from datetime import datetime
            loser.completed_at = datetime.utcnow()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the changes to the DB. Without this flag the script is a dry run.",
    )
    parser.add_argument(
        "--bulk-id",
        type=str,
        default=None,
        help="Only act on this BulkAIUpload UUID. Useful for the in-flight 94cba926 bulk.",
    )
    args = parser.parse_args()

    bulk_id = UUID(args.bulk_id) if args.bulk_id else None

    db = SessionLocal()
    try:
        groups = find_duplicate_groups(db, bulk_id=bulk_id)
        if not groups:
            print("No duplicate file rows found.")
            return 0

        actions = plan_dedup(groups)
        if not actions:
            print(
                "Found duplicate groups, but every loser row is in a sacred "
                "state (processing/completed). Nothing to do."
            )
            for bulk_id_value, group_map in groups.items():
                print(f"  bulk {bulk_id_value}:")
                for key, rows in group_map.items():
                    statuses = [(r.status or "?").lower() for r in rows]
                    print(f"    {key}: {len(rows)} rows, statuses={statuses} (skipped)")
            return 0

        total_bulks = len(groups)
        total_actions = len(actions)
        print(f"Found {total_actions} duplicate rows across {total_bulks} bulk upload(s):")
        for bulk_id_value, group_map in groups.items():
            bulk_actions = [a for a in actions if a[0].bulk_upload_id == bulk_id_value]
            print(f"  bulk {bulk_id_value}: {len(bulk_actions)} row(s) to mark superseded")
            for loser, winner in bulk_actions:
                print(
                    f"    - {loser.original_filename} row {loser.id} "
                    f"status={loser.status} -> superseded "
                    f"(winner: {winner.id} status={winner.status})"
                )

        if not args.apply:
            print("\nDRY RUN. Re-run with --apply to commit.")
            return 0

        apply_actions(actions)
        db.commit()
        print(f"\nApplied {total_actions} change(s).")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
