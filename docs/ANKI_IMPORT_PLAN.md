# EduViz Anki Import System — Implementation Plan

## 1. Overview

This plan extends EduViz to import Anki `.apkg` decks and render rich content (HTML cloze, images) in the review flow.

**Scope:**
- Parse Anki `.apkg` files (SQLite + embedded media)
- Map Anki notes → EduViz cards with rich content support
- Store media files in filesystem (`app/static/media/`)
- Extend Card model with cloze and media fields
- Render cloze and images in review UI

**Out of scope (future):**
- Two-way sync with Anki
- AnkiConnect integration
- Anki deck export

---

## 2. Current System

| Component | Current State |
|-----------|---------------|
| **Card model** | `front`, `back` (Text), `card_type`, `mcq_options` (JSON) |
| **Card types** | `"basic"`, `"mcq"` |
| **Review render** | `{{ card.front }}` / `{{ card.back }}` as plain text |
| **Import** | CSV (flashcard), JSON (MCQ), bulk import API |
| **Media** | None |

---

## 3. Design Decisions

### 3.1 Parser Strategy

Use **genanki** (pure Python, no Java dependency) to parse `.apkg` files.

- `genanki.Deck` can be imported directly from `.apkg`
- Extracts notes, fields, and media references
- Cloze detection: field content containing `{{c1::...}}` patterns

**Alternative considered:** Direct SQLite parsing via `sqlite3`. More complex (must handle Anki's internal schema), not recommended for MVP.

### 3.2 Media Storage

**Decision: Filesystem + DB reference**

| Aspect | Design |
|--------|--------|
| Location | `app/static/media/{deck_id}/{filename}` |
| DB field | `card.media_files` (JSON array of filenames) |
| URL generation | `/assets/media/{deck_id}/{filename}` |

Rationale:
- PostgreSQL BLOBs are heavier and harder to cache/serve
- Static file serving with asset versioning already exists
- Easy to cleanup on deck delete

### 3.3 Card Model Extension

Add fields to `Card`:

```python
class Card(Base):
    # Existing fields remain...
    front: Mapped[str]
    back: Mapped[str]
    card_type: Mapped[str]  # "basic", "mcq", "cloze"

    # New fields
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True)  # Full HTML with cloze/image markup
    media_files: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)  # ["img1.jpg", "img2.png"]
    cloze_number: Mapped[int | None] = mapped_column(Integer, nullable=True)  # For cloze cards: which {{cN::}}
```

**Backward compatibility:** Existing `"basic"` cards with plain text in `front`/`back` continue to work.

### 3.4 Cloze Rendering

Cloze syntax: `{{c1::hidden text}}` or `{{c2::hidden text with [multiple] gaps}}`

**Renderer approach:** Jinja2 custom filter + inline JavaScript

1. Store cloze markup in `content_html` (e.g., `The {{c1::heart}} pumps blood`)
2. In review template, render as:
   - Hidden gap on front (click to reveal)
   - Revealed text on back
3. Implementation: Simple regex replacement + CSS/JS for interaction

**No external cloze library needed** — keep it lightweight.

### 3.5 Image Handling

1. Extract media from `.apkg` (genanki provides media list)
2. Save to `app/static/media/{deck_id}/{filename}`
3. Update `content_html` image src to use static asset URL
4. `media_files` JSON tracks what was imported for cleanup

---

## 4. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     API / Upload Layer                      │
│  POST /decks/{id}/anki-import (multipart .apkg file)       │
└─────────────────────┬───────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────┐
│                   AnkiImportService                          │
│  - parse_apkg(file) → List[AnkiNote]                        │
│  - extract_media(apkg, deck_id) → List[str]                │
│  - convert_to_cards(notes) → List[Card]                    │
│  - deduplicate against existing                             │
└─────────────────────┬───────────────────────────────────────┘
                      │
        ┌─────────────┴─────────────┐
        │                           │
┌───────▼───────┐         ┌─────────▼────────┐
│  File System   │         │    PostgreSQL    │
│ app/static/   │         │  cards table     │
│ media/{deck}  │         │  (extended)      │
└───────────────┘         └──────────────────┘
```

---

## 5. Implementation Plan

### Phase 1: Core Infrastructure (Day 1)

#### 5.1 Database Migration

**File:** `alembic/versions/xxxx_add_anki_card_fields.py`

```python
def upgrade():
    op.add_column('cards', sa.Column('content_html', sa.Text(), nullable=True))
    op.add_column('cards', sa.Column('media_files', JSON, nullable=True))
    op.add_column('cards', sa.Column('cloze_number', sa.Integer, nullable=True))

def downgrade():
    op.drop_column('cards', 'cloze_number')
    op.drop_column('cards', 'media_files')
    op.drop_column('cards', 'content_html')
```

#### 5.2 Card Model Update

**File:** `app/models/card.py`

Add new fields:
```python
content_html: Mapped[str | None] = mapped_column(Text, nullable=True)
media_files: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
cloze_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

#### 5.3 Anki Import Service

**File:** `app/services/anki_import.py` (new)

```python
class AnkiImportError(Exception):
    """Raised when Anki import fails."""

@dataclass
class AnkiCard:
    front: str
    back: str
    card_type: str  # "basic" or "cloze"
    content_html: str | None
    media_files: list[str]
    cloze_number: int | None
    source_label: str = "anki-import"

class AnkiImportService:
    def __init__(self, db: Session, deck: Deck, user: User):
        self.db = db
        self.deck = deck
        self.user = user

    def parse_apkg(self, file_obj: BinaryIO) -> list[AnkiCard]:
        """Parse .apkg file and return card data."""
        # Use genanki to read deck
        # Extract media files
        # Convert notes to AnkiCard objects
        pass

    def import_cards(self, cards: list[AnkiCard]) -> int:
        """Import cards with deduplication."""
        # Check existing
        # Insert new cards + CardState
        # Return count
        pass

    def extract_media(self, file_obj: BinaryIO) -> list[str]:
        """Extract media files from .apkg to filesystem."""
        # Save to app/static/media/{self.deck.id}/
        # Return list of filenames
        pass
```

#### 5.4 API Endpoint

**File:** `app/api/routers/content.py` (add endpoint)

```python
@router.post("/decks/{deck_id}/anki-import")
async def anki_import(
    deck_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Import Anki deck (.apkg) into a deck."""
    # Auth check (can_manage_deck)
    # Parse and import
    # Return result
```

**Request:** `multipart/form-data` with `.apkg` file  
**Response:**
```json
{
  "cards_imported": 123,
  "media_files": 5,
  "errors": []
}
```

---

### Phase 2: Review Renderer (Day 2)

#### 5.5 Cloze Renderer Service

**File:** `app/services/cloze_renderer.py` (new)

```python
import re

CLOZE_PATTERN = re.compile(r'\{\{c(\d+)::([^}]+)\}\}')
CLOZE_HTML = '<span class="cloze cloze-c{num}" data-answer="{text}">{hidden}</span>'

def render_cloze_front(html: str) -> str:
    """Replace cloze markers with hidden spans for front side."""
    def replace(match):
        num = match.group(1)
        text = match.group(2)
        hidden = '·' * min(len(text), 20)  # Placeholder dots
        return CLOZE_HTML.format(num=num, text=text, hidden=hidden)
    return CLOZE_PATTERN.sub(replace, html)

def render_cloze_back(html: str) -> str:
    """Replace cloze markers with revealed spans for back side."""
    def replace(match):
        num = match.group(1)
        text = match.group(2)
        return f'<span class="cloze cloze-c{num} cloze-revealed">{text}</span>'
    return CLOZE_PATTERN.sub(replace, html)
```

#### 5.6 Media URL Filter

**File:** `app/services/media_urls.py` (new)

```python
def resolve_media_urls(html: str, deck_id: str, static_url_fn) -> str:
    """Rewrite img src to use static asset URLs."""
    # Find <img src="..."> with relative paths (from Anki)
    # Replace with /assets/media/{deck_id}/filename
    pass
```

#### 5.7 Review Template Update

**File:** `app/templates/review/card.html`

```html
{% if card.card_type == 'cloze' and card.content_html %}
  <div class="review-card__text review-card__text--prompt cloze-content">
    {{ card.content_html | render_cloze_front | resolve_media_urls(card.deck_id) | safe }}
  </div>
  <details class="review-answer">
    <summary>Reveal answer</summary>
    <div class="review-card__text review-card__text--answer">
      {{ card.content_html | render_cloze_back | resolve_media_urls(card.deck_id) | safe }}
    </div>
  </details>
{% else %}
  <!-- Existing basic/MCQ rendering -->
  <div class="review-card__text review-card__text--prompt">{{ card.front }}</div>
  <details class="review-answer">
    <summary>Reveal answer</summary>
    <div class="review-card__text review-card__text--answer">{{ card.back }}</div>
  </details>
{% endif %}
```

#### 5.8 CSS Updates

**File:** `app/static/styles.css` (append)

```css
/* Cloze card styling */
.cloze {
  background: rgba(120, 113, 108, 0.1);
  border-radius: 4px;
  padding: 2px 6px;
  cursor: pointer;
}
.cloze-revealed {
  background: rgba(76, 175, 80, 0.15);
}
.cloze-content img {
  max-width: 100%;
  height: auto;
}
```

---

### Phase 3: Deck Import Page (Day 2-3)

#### 5.9 Import UI Page

**File:** `app/templates/cards/anki_import.html` (new)

- Upload form with `.apkg` file input
- Progress indicator for large decks
- Summary of cards/media found
- Confirm import button

**Route:** `GET /decks/{id}/anki-import` (page)  
**Route:** `POST /decks/{id}/anki-import` (submit)

#### 5.10 Deck Overview Link

**File:** `app/templates/decks/overview.html`

Add "Import from Anki" button linking to `/decks/{id}/anki-import`.

---

### Phase 4: Cleanup & Edge Cases (Day 3)

#### 5.11 Media Cleanup on Deck Delete

- When deck is deleted, also remove `app/static/media/{deck_id}/`
- Add to existing deck delete logic in content router

#### 5.12 Error Handling

- Invalid .apkg format → show user-friendly error
- Duplicate cards → skip with count in response
- Missing media → import anyway, note missing in response
- Large decks (8917 cards) → async/background processing?

**Note:** For 8917 cards, consider:
1. Synchronous for <1000 cards
2. Background task (Celery) for larger

For MVP, keep synchronous but add timeout config.

#### 5.13 Deduplication

Check existing cards by:
- For basic: `(front, back, card_type)`
- For cloze: `(content_html, card_type)`

---

## 6. Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `alembic/versions/xxxx_add_anki_card_fields.py` | Create | Migration for new Card fields |
| `app/models/card.py` | Modify | Add `content_html`, `media_files`, `cloze_number` |
| `app/services/anki_import.py` | Create | AnkiParseService, AnkiCard dataclass |
| `app/services/cloze_renderer.py` | Create | Cloze regex, render functions |
| `app/services/media_urls.py` | Create | Media URL resolution |
| `app/api/routers/content.py` | Modify | Add `/anki-import` endpoint |
| `app/templates/review/card.html` | Modify | Cloze + image rendering |
| `app/static/styles.css` | Modify | Cloze styling |
| `app/templates/cards/anki_import.html` | Create | Import UI page |
| `app/templates/decks/overview.html` | Modify | Add import button |
| `requirements.txt` | Modify | Add `genanki` |

---

## 7. API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/decks/{id}/anki-import` | GET | manager+ | Show import page |
| `/decks/{id}/anki-import` | POST | manager+ | Process .apkg upload |

**Request (POST):**
```
Content-Type: multipart/form-data
file: <.apkg binary>
```

**Response:**
```json
{
  "success": true,
  "cards_imported": 8917,
  "media_files": 302,
  "duplicates_skipped": 0,
  "errors": []
}
```

**Error Response (400):**
```json
{
  "success": false,
  "error": "Invalid .apkg format"
}
```

---

## 8. Database Changes

### Migration: `xxxx_add_anki_card_fields.py`

```sql
ALTER TABLE cards ADD COLUMN content_html TEXT;
ALTER TABLE cards ADD COLUMN media_files JSONB;
ALTER TABLE cards ADD COLUMN cloze_number INTEGER;
```

### Model Fields

| Field | Type | Nullable | Default | Notes |
|-------|------|----------|---------|-------|
| `content_html` | Text | Yes | NULL | Full HTML with cloze/image markup |
| `media_files` | JSON | Yes | NULL | `["file1.jpg", "file2.png"]` |
| `cloze_number` | Integer | Yes | NULL | Cloze index (1, 2, 3...) or NULL |

---

## 9. Dependencies

Add to `requirements.txt`:
```
genanki>=0.14.0
```

---

## 10. Testing Plan

### Unit Tests
- `test_cloze_renderer.py` — cloze pattern matching, front/back rendering
- `test_media_url_resolver.py` — img src rewriting
- `test_anki_import_deduplication.py` — skip existing cards

### Integration Tests
- Import small .apkg (10 cards, 2 images)
- Import NEET-sized .apkg (8917 cards, 302 images) — verify performance
- Review cloze card end-to-end
- Deck delete cleans up media

---

## 11. Timeline

| Phase | Tasks | Estimated |
|-------|-------|-----------|
| **Phase 1** | Migration, model, AnkiImportService, API endpoint | 4 hours |
| **Phase 2** | Cloze renderer, media URLs, template update, CSS | 3 hours |
| **Phase 3** | Import UI page, deck overview link | 2 hours |
| **Phase 4** | Cleanup, error handling, deduplication | 2 hours |
| **Testing** | Unit + integration tests | 2 hours |
| **Buffer** | Edge cases, review | 1 hour |
| **Total** | | ~14 hours |

---

## 12. Open Questions / Future Considerations

1. **Large deck async processing:** For 8917 cards, consider background tasks. MVP can be sync with timeout.
2. **Two-way sync:** Export back to Anki? Not in scope.
3. **Cloze multiple gaps:** Current design handles single cloze per card. Multi-cloze (`{{c1::...}} {{c2::...}}`) needs UI refinement.
4. **Anki tags → EduViz tags:** Map Anki deck/tag structure to EduViz organization tags.
5. **Scheduling:** Cloze cards use same FSRS scheduler as basic cards.

---

## 13. Coordination Notes

- **Broad** has context on the existing review flow and deck management.
- Template changes to `card.html` must preserve existing basic/MCQ rendering for backward compatibility.
- Asset versioning (`static_asset_url()`) applies to media files.
- Theme: Review pages use warm cream theme — cloze styling should harmonize.

---

*Plan created: 2026-04-03*
