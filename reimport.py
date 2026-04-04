#!/usr/bin/env python3
import sys
sys.path.insert(0, '/app')

from app.services.anki_import import AnkiImportService
from app.core.db import SessionLocal
from app.models import Deck
from sqlalchemy import select

db = SessionLocal()
deck = db.execute(select(Deck).where(Deck.id == 'f623c94f-1e12-4e57-940f-251cb2ec370b')).scalar_one()

service = AnkiImportService(db, deck)
result = service.import_apkg(open('/tmp/neet.apkg', 'rb'))

print(f"Imported: {result.cards_imported} cards")
print(f"Media: {result.media_files} files")
db.close()
