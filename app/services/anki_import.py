"""
Anki .apkg import service for EduViz.

Parses Anki deck files, extracts media, and imports cards.
"""

import io
import json
import logging
import os
import re
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Card, CardState, Deck
from app.services.cloze_renderer import extract_cloze_numbers, is_cloze_content, render_cloze_front, render_cloze_back
from app.services.media_urls import extract_media_filenames, resolve_media_urls

logger = logging.getLogger(__name__)


class AnkiImportError(Exception):
    """Raised when Anki import fails."""
    pass


@dataclass
class AnkiCard:
    """Represents a card parsed from Anki deck."""
    front: str
    back: str
    card_type: str = "basic"  # "basic" or "cloze"
    content_html: Optional[str] = None  # Front side (or full for basic cards)
    content_html_back: Optional[str] = None  # Back side for cloze cards
    media_files: list[str] = field(default_factory=list)
    cloze_number: Optional[int] = None
    source_label: str = "anki-import"
    tags: list[str] = field(default_factory=list)


@dataclass
class AnkiImportResult:
    """Result of an Anki import operation."""
    cards_imported: int = 0
    media_files: int = 0
    duplicates_skipped: int = 0
    errors: list[str] = field(default_factory=list)


class AnkiImportService:
    """
    Service for importing Anki .apkg files into EduViz.

    Usage:
        service = AnkiImportService(db, deck, user)
        result = service.import_apkg(file_obj)
    """

    def __init__(self, db: Session, deck: Deck):
        self.db = db
        self.deck = deck
        self.media_dir = Path(__file__).resolve().parent.parent / "static" / "media" / str(deck.id)
        self._existing_front_backs: set[tuple[str, str]] = set()
        self._existing_html: set[str] = set()

    def import_apkg(self, file_obj: BinaryIO) -> AnkiImportResult:
        """
        Import an .apkg file into the deck.

        Args:
            file_obj: File-like object containing .apkg data

        Returns:
            AnkiImportResult with import statistics
        """
        result = AnkiImportResult()

        # Extract and parse the apkg
        try:
            deck_data = self._parse_apkg(file_obj)
        except Exception as e:
            raise AnkiImportError(f"Failed to parse .apkg file: {e}")

        # Extract media files
        file_obj.seek(0)
        try:
            media_count = self._extract_media(file_obj, deck_data.get("media", {}))
            result.media_files = media_count
        except Exception as e:
            logger.warning(f"Failed to extract media: {e}")
            result.errors.append(f"Media extraction failed: {e}")

        # Convert to cards
        cards = self._convert_to_cards(deck_data.get("notes", []), deck_data.get("media", {}))

        # Deduplicate against existing
        self._load_existing()

        # Import cards
        cards_to_add = []
        study_source_lines: list[str] = []
        for card_data in cards:
            # Check for duplicates
            if self._is_duplicate(card_data):
                result.duplicates_skipped += 1
                continue

            try:
                card = self._create_card(card_data)
                cards_to_add.append(card)
                result.cards_imported += 1
                study_source_lines.extend([card_data.front, card_data.back])
            except Exception as e:
                result.errors.append(f"Failed to create card: {e}")

        # Bulk insert
        if cards_to_add:
            self.db.add_all(cards_to_add)
            self.db.flush()

            # Create card states
            states = [CardState(card_id=card.id) for card in cards_to_add]
            self.db.add_all(states)

            self.db.commit()

        return result

        return result

    def _parse_apkg(self, file_obj: BinaryIO) -> dict:
        """
        Parse .apkg file and extract notes and media info.

        .apkg is a zip containing:
        - collection.anki21 (SQLite DB with notes/cards)
        - media/ (folder with media files, named by integer ID)

        Args:
            file_obj: File-like object

        Returns:
            Dict with 'notes' and 'media' keys
        """
        file_obj.seek(0)
        notes = []
        media_map = {}

        with zipfile.ZipFile(file_obj, 'r') as zf:
            # Find the main collection file
            collection_name = None
            for name in zf.namelist():
                if name.startswith('collection') and name.endswith(('.anki21', '.anki2')):
                    if collection_name is None or name > collection_name:
                        collection_name = name

            if collection_name is None:
                raise AnkiImportError("No collection file found in .apkg")

            # Read collection as SQLite
            with zf.open(collection_name) as col_file:
                db_data = col_file.read()

            # Use a temporary file for SQLite - it works better with the format
            import tempfile
            import os
            with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
                tmp.write(db_data)
                tmp_path = tmp.name
            
            # Read media map from zip (new format uses JSON file)
            try:
                media_content = zf.read('media')
                media_map = json.loads(media_content)
                logger.info(f"Loaded {len(media_map)} media mappings from zip")
            except KeyError:
                logger.warning("No media file in zip - media map will be empty")
            
            conn = sqlite3.connect(tmp_path)
            try:
                # Parse col table - new format has separate columns for models/decks
                cursor = conn.execute("SELECT * FROM col LIMIT 1")
                row = cursor.fetchone()
                
                # Handle both old format (single JSON in first column) and new format (columns)
                if isinstance(row[0], (str, bytes, bytearray)):
                    # Old format: first column is JSON string
                    col_json = json.loads(row[0])
                    models_json = col_json.get("models", {})
                    decks_json = col_json.get("decks", {})
                else:
                    # New format: models is column 9, decks is column 10
                    models_json = json.loads(row[9]) if row[9] else {}
                    decks_json = json.loads(row[10]) if row[10] else {}

                # Get notes
                try:
                    for note_row in conn.execute("SELECT id, mid, flds, tags FROM notes"):
                        note_id, model_id, flds, tags = note_row
                        fields = flds.split('\x1f') if flds else []

                        # Get model info
                        model = models_json.get(str(model_id), {})
                        field_names = [f.get('name', '') for f in model.get('flds', [])]

                        # Detect cloze
                        front = fields[0] if len(fields) > 0 else ""
                        back = fields[1] if len(fields) > 1 else ""

                        is_cloze = is_cloze_content(front) or is_cloze_content(back)
                        card_type = "cloze" if is_cloze else "basic"

                        cloze_num = None
                        if is_cloze:
                            cloze_nums = extract_cloze_numbers(front + back)
                            cloze_num = cloze_nums[0] if cloze_nums else None

                        # Extract media references
                        content_full = '\x1f'.join(fields)
                        media_files = extract_media_filenames(content_full)

                        notes.append(AnkiCard(
                            front=front,
                            back=back,
                            card_type=card_type,
                            content_html=content_full,
                            media_files=media_files,
                            cloze_number=cloze_num,
                            tags=tags.split() if tags else []
                        ))
                except Exception as e:
                    logger.warning(f"Error reading notes: {e}")

                # Parse media map from collection (old format fallback)
                if not media_map:
                    try:
                        for media_row in conn.execute("SELECT * FROM media"):
                            if media_row:
                                key, value = media_row[0], media_row[1]
                                media_map[str(key)] = value
                    except:
                        pass
            finally:
                conn.close()
                os.unlink(tmp_path)

        return {"notes": notes, "media": media_map}

    def _extract_media(self, file_obj: BinaryIO, media_map: dict) -> int:
        """
        Extract media files from .apkg to filesystem.

        Args:
            file_obj: File-like object
            media_map: Dict mapping file ID to filename

        Returns:
            Number of media files extracted
        """
        file_obj.seek(0)
        extracted = 0

        # Create media directory
        self.media_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(file_obj, 'r') as zf:
            for name in zf.namelist():
                # Media files are stored as integers (e.g., "1", "2", "123")
                if name.isdigit():
                    file_id = name
                    filename = media_map.get(file_id, f"{file_id}.bin")

                    # Sanitize filename
                    safe_filename = re.sub(r'[^\w\s.-]', '_', filename)
                    out_path = self.media_dir / safe_filename

                    try:
                        with zf.open(name) as src, open(out_path, 'wb') as dst:
                            shutil.copyfileobj(src, dst)
                        extracted += 1
                    except Exception as e:
                        logger.warning(f"Failed to extract media {name}: {e}")

        return extracted

    def _convert_to_cards(self, notes: list, media_map: dict) -> list[AnkiCard]:
        """
        Convert Anki notes to AnkiCard objects.

        Args:
            notes: List of AnkiCard objects
            media_map: Media file mapping

        Returns:
            Processed list of AnkiCard objects
        """
        for note in notes:
            # Resolve media URLs in HTML
            if note.content_html:
                note.content_html = resolve_media_urls(
                    note.content_html,
                    str(self.deck.id)
                )
                # Pre-render cloze markers for cloze cards (front = hidden, back = revealed)
                if note.card_type == "cloze":
                    note.content_html_back = render_cloze_back(note.content_html)
                    note.content_html = render_cloze_front(note.content_html)

        return notes

    def _load_existing(self) -> None:
        """Load existing card front/back for deduplication."""
        existing = self.db.execute(
            select(Card).where(Card.deck_id == self.deck.id)
        ).scalars().all()

        for card in existing:
            self._existing_front_backs.add((card.front, card.back))
            if card.content_html:
                self._existing_html.add(card.content_html)

    def _is_duplicate(self, card_data: AnkiCard) -> bool:
        """Check if card is a duplicate of existing."""
        # Check basic front/back
        if (card_data.front, card_data.back) in self._existing_front_backs:
            return True

        # Check HTML content for cloze cards
        if card_data.card_type == "cloze" and card_data.content_html:
            if card_data.content_html in self._existing_html:
                return True

        return False

    def _create_card(self, card_data: AnkiCard) -> Card:
        """Create a Card model instance from AnkiCard data."""
        return Card(
            deck_id=self.deck.id,
            front=card_data.front,
            back=card_data.back,
            card_type=card_data.card_type,
            content_html=card_data.content_html,
            content_html_back=card_data.content_html_back,
            media_files=card_data.media_files if card_data.media_files else None,
            cloze_number=card_data.cloze_number,
            source_label=card_data.source_label,
        )
