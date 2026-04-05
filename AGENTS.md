# EduViz agent notes

## Project overview
- EduViz (`edu selviz` in the UI) is a server-rendered spaced-repetition study app.
- Product shape: FastAPI backend, Jinja templates, HTMX interactions, PostgreSQL persistence, FSRS-style scheduling adapter.
- UX direction is intentionally calm, minimal, and professional rather than "gamified".

## Branch strategy
- `main`: active development branch — all new features land here.
- `phase-2`: completed — organization-aware access, settings, review/dashboard polish.
- Unless told otherwise, new work happens on `main` and gets deployed from there.

## Stack / architecture
- Python 3.12
- FastAPI app entry: `app.main:app`
- Templates: `app/templates`
- Static assets: `app/static`
- Styles: `app/static/styles.css`
- DB: PostgreSQL via SQLAlchemy
- Connection pooling: PgBouncer (transaction mode) in Docker Compose
- Migrations: Alembic
- Review interactions: HTMX (`/review` page loads `/review/next`, rating posts to `/review/rate`)
- Docker runtime: `docker-compose.yml`, `Dockerfile`, `entrypoint.sh`

## Auth setup
- Auth is now treated as generic OIDC with **Microsoft Entra External ID** as the preferred provider.
- Preferred env vars are `MICROSOFT_ENTRA_EXTERNAL_ID_*`.
- Legacy `AZURE_B2C_*` vars are still accepted as compatibility fallback while migrating old setups.
- Redirect URI in production should be `https://edu.selviz.in/auth/callback`.
- Relevant code:
  - config/env loading: `app/core/config.py`
  - OIDC config + discovery: `app/services/microsoft_identity.py`
  - auth routes: `app/api/routers/auth.py`
  - legacy compatibility shim: `app/services/azure_b2c.py`
- Do not hardcode tenant/app values in code or docs. Keep secrets only in env files / deploy config.

## Roles / organizations model
- Roles: `user`, `admin`, `system_admin`.
- Users belong to at most one organization (`users.organization_id`).
- Deck visibility is organization-aware plus legacy personal/global access rules.
- `admin` and `system_admin` can manage decks; regular `user` can review but not create/edit decks.
- `system_admin` can manage all orgs and user assignments.
- Phase-2 status:
  - organizations exist and can be created/renamed
  - users can be assigned orgs / roles from settings
  - dashboard shows accessible decks with progress metrics
  - review flow is working inside the phase-2 UI refresh
  - bootstrap email can be promoted to `system_admin` on startup/login
  - older personal data is backfilled to keep pre-phase-2 usage working

## UI / UX preferences gathered so far
- Keep pages minimal, clean, and focused.
- Prefer calm, professional copy; avoid clutter and loud visual treatment.
- Brand text should use the standard treatment:
  - `edu selviz`
  - subtitle: `Professional study workflow`
- Review page should stay distraction-light even when branded.
- Review, Test, and Test Report pages use a fixed warm cream theme (no light mode toggle).
- Dashboard/settings modals should feel native and not break navigation state.
- Use the existing logo asset instead of introducing alternate brand artwork.

## Theme system
- Browser-level dark/light toggle using `data-theme="light"` on `<html>` and CSS variables.
- Theme stored in `localStorage` (no DB), persists across sessions.
- Toggle button (🌙/☀️) in `base.html` topbar + keyboard shortcut `T` toggles on main app pages.
- All `[data-theme="light"]` element overrides are scoped under `.page-content` class — review/test pages do not use this class so their warm cream theme is unaffected.
- Review, Test, and Test Report pages have their own standalone HTML templates (not extending base.html) with a fixed warm cream gradient theme.

## Deployment notes
- Live deployment path: `/opt/edu_viz`.
- Runtime uses Docker Compose.
- Common deploy flow:
  1. Pull/merge to `main`
  2. Deploy with `docker compose up --build -d`
  3. Test on `https://qa.edu.selviz.in`
- `entrypoint.sh` waits for Postgres, runs `alembic upgrade head`, then starts Uvicorn.
- Static assets are cache-busted through `static_asset_url()` in `app/api/routers/pages.py`, which hashes file contents and serves `/assets/<version>/...` URLs.
- Because of hashed asset URLs, CSS/image changes normally do not need manual cache purges after deploy.

### PgBouncer
- Running as `edu_viz-pgbouncer-1` container in Docker Compose
- App connects via `pgbouncer:5432` (configured in `.env` DATABASE_URL)
- Uses SCRAM-SHA-256 auth (matches PostgreSQL 16 default)
- Transaction pooling mode (optimal for request-response web apps)
- Key settings: `pool_mode=transaction`, `default_pool_size=20`, `max_client_conn=500`

## Known pitfalls / regressions already seen
- Auth naming drift: codebase moved from Azure AD B2C wording to Microsoft Entra External ID. Compatibility shim remains; do not remove old env support casually or you may break current deployments.
- **Starlette query_params caching**: `request.query_params` is cached. Modifying `request.scope["query_string"]` after the fact has no effect. If you need to pass modified params between internal function calls, pass them as explicit arguments instead.
- Review page styling is intentionally separate from the dark shell used elsewhere. Reusing generic topbar styles blindly can make review mode feel heavy.
- Modal flows in dashboard/settings depend on query params + client-side dialog wiring. It is easy to break edit/create reopen behavior if IDs/data attributes drift.
- Static asset changes may appear stale only if you bypass `static_asset_url()`; always use the helper in templates for CSS/images/icons.
- Organization/admin logic is sensitive: visibility and management permissions are not the same thing. Check `app/services/access.py` before changing deck/user behavior.
- **Jinja2 UUID serialization**: `tojson` filter cannot serialize UUID objects. Use `| map('string') | list | tojson` when passing UUIDs to JavaScript.
- **Auth callback user lookup**: Users are matched first by `identity_sub`, then by `email` as fallback. If a user exists with a different `identity_sub` (e.g., from a previous auth provider), the email fallback updates their `identity_sub`.

## Be careful when editing
### Auth
- Touch `app/services/microsoft_identity.py`, `app/api/routers/auth.py`, or `app/core/config.py` carefully.
- Preserve fallback behavior for legacy env names unless the migration is explicitly completed.
- Avoid changing callback/session behavior without checking both login and logout flows.

### Modals
- Dashboard/settings dialogs rely on matching IDs, `data-modal-open`, `data-modal-close`, and query-param-driven server rendering.
- Test both direct page loads with query params and click-open interactions.

### Review UI
- Main files: `app/templates/review/page.html`, `app/templates/review/card.html`, `app/templates/review/empty.html`, `app/static/styles.css`.
- Keep the review page centered, lightweight, and readable.
- Do not accidentally pull in the full app topbar/navigation unless explicitly requested.
- Review page has its own branded logo — do not replace with the main app logo without checking.

### Bulk delete
- Flashcard bulk delete: `POST /decks/{id}/flashcards/bulk-delete`
- MCQ bulk delete: `POST /decks/{id}/mcqs/bulk-delete`
- Both clean up card state dependencies before deleting; keep tests aligned if changing logic.

### Tests
- Test center lives at `/decks/{id}/tests` and is accessible to managers (admin/system_admin).
- Test creation, taking, submission, and attempt reports are in `app/api/routers/content.py`.
- Templates: `app/templates/tests/list.html`, `take.html`, `report.html`.
- `take.html` and `report.html` are standalone pages (not extending base.html), matching review page style.
- Question payload parsing was recently fixed — be careful with card type discrimination when changing test submission logic.
- When submitting answers via form POST, `question_ids` must be individual hidden fields per question, not comma-separated.
- Favorite star buttons (`deck-star-btn`) have a glow hover effect via `filter: drop-shadow(...)`. No transform scale on hover to avoid cursor jump.
- Do NOT use `request.scope["query_string"]` hack to pass params between functions — Starlette caches `query_params`.

### Static assets
- Current logo asset: `app/static/brand/logo.jpg`.
- Reference assets through `static_asset_url(...)` so cache-busting keeps working.

## Feature summary (current state)

### Bulk import
- Bulk import endpoint is `POST /api/v1/import/deck` and `POST /api/v1/import/decks` with `X-Api-Key` auth via `require_bulk_import_api_key`.
- Added optional `subject` to bulk import payloads so subject-level full decks can be named like `grade_11_biology_full`, `grade_11_chemistry_full`, etc.
- QA verified the new subject naming path by importing `grade_11_biology_full`, `grade_11_chemistry_full`, `grade_11_physics_full`, `grade_12_biology_full`, `grade_12_chemistry_full`, and `grade_12_physics_full`.
- The old `grade_{grade}_science_full` naming still exists for legacy full-science imports.
- Runtime env key for QA/prod bulk import was confirmed to be `BULK_IMPORT_API_KEY` in `/opt/edu_viz/.env`.

## Feature summary (current state)

### Dashboard
- Home page shows simplified deck cards with deck name + Review (green outline) and Test (purple outline) buttons
- Clicking the deck card opens the deck overview page
- Real-time deck search with case-insensitive filtering by deck name and tags

### Browse Decks Search
- Available at `/decks/browse`
- Search normalizes query using `normalize_deck_name()` before comparing
- Matches both deck `normalized_name` and associated tag `normalized_name`
- Uses `OR` condition with grouping to avoid duplicate results

### Deck Overview
- New hub page at `/decks/{id}` — entry point when user clicks a deck from dashboard
- Nav card with icons + text: Home, MCQs (count), Flash Cards (count), Tests (count)
- Action card with color-coded buttons: Review (green), Test (purple)
- Edit deck form at bottom (for editors/admins)
- Sub-pages (MCQs, Flash Cards, Tests) have only a "Back to Overview" button — no other nav clutter

### Content management
- Decks: create, edit, delete (soft delete), global/org scope toggle in edit flow
- Flashcards: CRUD per deck, bulk delete
- MCQs: CRUD per deck, bulk delete, JSON import
- AI upload: dedicated per-deck page (`/decks/{id}/ai-upload`) for PDF/DOCX ingestion via OpenAI
- Anki CSV export per deck (`/decks/{id}/anki-export.csv`)

### Review
- FSRS-style review flow at `/review`
- HTMX-powered: loads `/review/next`, posts ratings to `/review/rate`
- Review page is intentionally branded (`edu selviz` logo) and styled separately from the main app shell — keep it distraction-light
- Review starts immediately — no count selection required (count can optionally be passed as URL param `remaining=N`)
- Review deck isolation: `review_rate` calls `_review_next_inner()` directly (passing deck_id and remaining as explicit params) instead of hacking `request.scope["query_string"]` which is cached by Starlette

### Tests
- Tests are for user self-evaluation — no manual test creation or metadata (title/description) required.
- Test titles are auto-generated as `Test taken @ {datetime}` (e.g., "Test taken @ 2026-03-17 18:30").
- Each test attempt is a standalone test — there's no separate "test attempt" concept.
- Flow: User clicks "Take Test" → question count modal (10/25/50/All) → test auto-created → questions appear immediately.
- Deck-level test center (`/decks/{id}/tests`) lists all past tests with scores and links to reports.
- Test take page blocks navigation until question is answered (Next/Submit disabled until answer selected).
- Test report has "All" / "Incorrect only" filter toggle.
- Templates: `app/templates/tests/list.html`, `take.html`, `report.html`.
- `take.html` and `report.html` are standalone pages (not extending base.html), matching review page style.
- One question at a time with Previous/Next navigation, answers stored in JS until submit.
- Question IDs are sent as individual hidden fields (name=`question_ids`), NOT comma-separated.
- UUID values in templates must be converted to string before `tojson` filter.

### Test Throttling
- Configurable via env vars in `app/core/config.py`:
  - `TEST_DAILY_LIMIT`: Max tests per user per day (default: 0 = unlimited)
  - `TEST_COOLDOWN_SECONDS`: Minimum time between test attempts (default: 0 = no cooldown)
- Org-level override: Organizations can set `test_daily_limit` (0-10, capped by env max)
  - Set via `/settings/organizations` edit modal
  - Displayed in org card: "Tests Enabled (5/day)" or "Tests Enabled" (using global)
- Logic in `check_test_throttle()` (`app/services/access.py`):
  1. System admins bypass all limits
  2. Check cooldown (per deck)
  3. Check daily limit (org-specific, capped by env)
- Migration: `0012_org_test_daily_limit.py` adds `test_daily_limit` column to organizations

### Analytics
- Analytics page at `/analytics` for admin/system_admin roles
- Shows personal, organization, and system-wide analytics
- Deck filter uses multiselect component with `deck_ids` control
- User filter uses native select dropdown
- Backend expects `selected_deck_ids` as a list for multiselect
- Multiselect component: `app/components/multiselect/templates/multiselect.html`
- Multiselect stores selected keys in hidden input as comma-separated string
- For Deck objects, use `opt.id|string` for key extraction (not `opt.key|default(opt.id)` which returns 'undefined')

### AI Generation
- Three providers: OpenAI, Minimax, Claude
- Config via two env vars: `AI_PROVIDER` (openai|minimax|claude) and `AI_API_KEY`
- Resolution hierarchy: user > org > global env
- AI buttons only visible when org has `is_ai_enabled=true` and user is admin
- Checked via `can_use_ai_generation(user)` in `app/services/access.py`
- AI upload page (`/decks/{id}/ai-upload`) validates `can_use_ai_generation` + `can_manage_deck`
- Both generate-mcqs and ai-import use dynamic provider resolution

### Settings (admin)
- Organizations management (`/settings/organizations`) — create, edit via modals, delete (future)
- Users management (`/settings/users`) — update role, assign organization, AI and test settings
- Global deck toggle in deck edit flow (marks deck as globally accessible)

#### Settings page UI patterns
- **Organizations page**: deck-grid of org cards with stats (users, AI, tests). Create/edit via modals.
  - Modal flow: `Enable AI generation` checkbox → conditional `Override global AI setting` → Provider + API key inputs
  - Same pattern for `Enable tests` → `Daily test limit` input
  - Modal pre-fills from data attributes on the Edit button
- **Users page**: stacked form-cards per user matching the `form-card` pattern (section-heading outside form, form class="stack-md", each field in a `<div>` wrapper)
  - AI settings section: `Enable AI generation` → `Override org AI` → Provider + API key (same flow as orgs)
  - AI section only shown when org AI is enabled for that user
  - AI status shown in grid: "Org AI off" or "Org AI on (user key)"
  - "View users" from org page filters to that org via `?org=<org_id>` query param
  - Filtered view shows "Showing users in [Org] — Show all" link

### Routes reference

**Pages router** (`app/api/routers/pages.py`):
- `GET /` — home
- `GET /dashboard` — dashboard with accessible decks and progress metrics
- `GET /settings`, `/settings/organizations`, `/settings/users` — admin settings
- `POST /decks`, `POST /decks/{id}/update`, `POST /decks/{id}/delete` — deck CRUD
- `GET /decks/{id}` — deck overview (nav + action cards)
- `GET /decks/{id}/flashcards`, `GET /decks/{id}/mcqs`, `GET /decks/{id}/ai-upload` — content pages (Back to Overview link)
- `POST /decks/{id}/cards`, `POST /decks/{id}/cards/import` — card creation/import
- `GET /review`, `GET /review/next`, `POST /review/rate` — review flow

**Content router** (`app/api/routers/content.py`):
- `POST /decks/{id}/ai-import` — AI PDF/DOCX ingestion
- `POST /decks/{id}/mcqs/import-json` — MCQ JSON import
- `GET /decks/{id}/flashcards/{card_id}/edit`, `POST ...` — flashcard edit
- `GET /decks/{id}/mcqs/{card_id}/edit`, `POST ...` — MCQ edit
- `POST /decks/{id}/flashcards/bulk-delete`, `POST /decks/{id}/mcqs/bulk-delete` — bulk delete
- `GET /decks/{id}/tests`, `POST /decks/{id}/tests` — test center
- `GET /tests/{id}`, `POST /tests/{id}/submit` — take test
- `GET /attempts/{id}` — attempt report
- `GET /decks/{id}/anki-export.csv` — Anki export

### Templates structure
```
app/templates/
├── base.html
├── home.html
├── dashboard.html
├── decks/
│   └── overview.html       # deck hub page with nav + action cards
├── cards/
│   ├── list.html          # flashcard + MCQ listing per deck
│   ├── ai_upload.html     # AI PDF/DOCX upload
│   ├── edit_flashcard.html
│   ├── edit_mcq.html
│   ├── flashcards.html
│   └── mcqs.html
├── review/
│   ├── page.html
│   ├── card.html
│   └── empty.html
├── settings/
│   ├── index.html
│   ├── organizations.html
│   └── users.html
└── tests/
    ├── list.html
    ├── take.html
    └── report.html
```

## Useful files to inspect first
- `README.md`
- `app/api/routers/pages.py`
- `app/api/routers/content.py`
- `app/services/access.py`
- `app/services/microsoft_identity.py`
- `app/templates/base.html`
- `app/templates/dashboard.html`
- `app/templates/review/page.html`
- `app/templates/decks/overview.html`
- `app/templates/tests/list.html`
- `app/static/styles.css`

## Working rule for future agents
- Keep changes practical and small.
- Don't leak secrets into docs, code, commits, or screenshots.
- If you deploy UI changes, verify the live page after the container restarts.
- Review page styling is separate from the main shell — do not pull in the full topbar/nav into review mode.
- Modal flows in dashboard/settings depend on query params + client-side dialog wiring; test both direct page loads and click-open interactions.
- Static assets use `static_asset_url()` for cache-busting; always reference CSS/images/icons through this helper.

## Anki Import System (feature/anki-import)

### Overview
Extended EduViz to import Anki .apkg decks with rich content support.

### Files Created
- `alembic/versions/0013_add_anki_card_fields.py` - Migration for new card fields
- `app/services/anki_import.py` - AnkiImportService for parsing .apkg files
- `app/services/cloze_renderer.py` - Cloze text rendering ({{c1::text}} syntax)
- `app/services/media_urls.py` - Media URL resolution for images
- `docs/ANKI_IMPORT_PLAN.md` - Full implementation plan

### Files Modified
- `app/models/card.py` - Added content_html, media_files, cloze_number fields
- `app/api/routers/bulk_import.py` - Added POST /api/v1/import/decks/{id}/anki-import
- `requirements.txt` - Added genanki>=0.14.0

### New Card Fields
- `content_html`: Full HTML with cloze/image markup
- `media_files`: JSON array of media filenames
- `cloze_number`: Cloze index (1, 2, 3...) or NULL

### API Endpoint
```
POST /api/v1/import/decks/{deck_id}/anki-import
Content-Type: multipart/form-data
file: <.apkg binary>

Response: {
  "success": true,
  "cards_imported": 8917,
  "media_files": 302,
  "duplicates_skipped": 0,
  "errors": []
}
```

### Phase 2 (Pending)
- Review template update (cloze rendering)
- Import UI page
- Media cleanup on deck delete

### Current Branch: feature/anki-import
- Recent commits: c7b7613 (Minimax/Claude providers), ce168a9 (dynamic provider + button visibility), 2ea17e4 (simplified AI_PROVIDER/AI_API_KEY env vars)

### OpenCode Provider
- `OpencodeStudyPackProvider` in `app/services/ai_generation.py` uses MiniMax API directly
- API: `https://api.minimax.chat/v1/text/chatcompletion_v2?GroupId=RqdqdwGe0gBWoGFh`
- Model: `MiniMax-M2` with system prompt "Answer briefly. Do not explain reasoning. Give only final answer in JSON."
- Requires `minimax` credential with `api_key` auth type

### Security Fixes (2026-04-05)
- Path traversal in `/assets/media/{filename}` fixed with resolve() + relative_to() checks
- Stored XSS in review cards fixed - replaced `| safe` with `| sanitize` filter
- Sanitize filter strips: <script>, <style>, event handlers, javascript: URLs

### Ruff Fixes Applied
- Removed unused imports (F401)
- Fixed F811 redefinition of generate_study_pack
- Fixed F841 unused variables
- Fixed E402 imports order in ai_generation.py
- Added MCQGeneration/MCQGenerationStatus to models __all__
