"""Backfill concept maps for existing decks that have uploaded content."""
import sys
sys.path.insert(0, "/app")

from app.core.db import get_db  # noqa: E402
from app.models import BulkAIUploadFile, BulkAIUploadFileStatus, Deck  # noqa: E402
from app.services.concept_map import generate_concept_map  # noqa: E402

db = next(get_db())

# Find all decks that have completed uploads with text content
decks = (
    db.query(Deck)
    .join(
        BulkAIUploadFile,
        BulkAIUploadFile.created_deck_id == Deck.id,
    )
    .filter(
        BulkAIUploadFile.status == BulkAIUploadFileStatus.COMPLETED.value,
        BulkAIUploadFile.content_text.isnot(None),
        BulkAIUploadFile.content_text != "",
    )
    .distinct()
    .order_by(Deck.name)
    .all()
)

print(f"Found {len(decks)} decks with uploaded content.")

success = 0
failed = 0
for deck in decks:
    # Get the latest completed file for this deck
    source_file = (
        db.query(BulkAIUploadFile)
        .filter(
            BulkAIUploadFile.created_deck_id == deck.id,
            BulkAIUploadFile.status == BulkAIUploadFileStatus.COMPLETED.value,
            BulkAIUploadFile.content_text.isnot(None),
            BulkAIUploadFile.content_text != "",
        )
        .order_by(BulkAIUploadFile.completed_at.desc())
        .first()
    )
    if not source_file:
        continue

    try:
        cm = generate_concept_map(
            db,
            deck_id=deck.id,
            source_file_id=source_file.id,
            title=source_file.extracted_title or deck.name,
        )
        if cm.status == "ready":
            print(f"  OK  {deck.name[:50]:50s} ({cm.node_count} nodes)")
            success += 1
        else:
            print(f"  FAIL {deck.name[:50]:50s} - {cm.error_message}")
            failed += 1
    except Exception as exc:
        print(f"  ERR  {deck.name[:50]:50s} - {exc}")
        failed += 1

print(f"\nDone: {success} success, {failed} failed")
db.close()
