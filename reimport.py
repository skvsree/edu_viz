#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.models import Deck  # noqa: E402
from app.services.anki_import import AnkiImportService  # noqa: E402


db = SessionLocal()
deck = db.execute(select(Deck).where(Deck.id == "f623c94f-1e12-4e57-940f-251cb2ec370b")).scalar_one()

service = AnkiImportService(db, deck)
with open("/tmp/neet.apkg", "rb") as file_obj:
    result = service.import_apkg(file_obj)

print(f"Imported: {result.cards_imported} cards")
print(f"Media: {result.media_files} files")
db.close()
