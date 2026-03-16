# EduViz agent notes

## Project overview
- EduViz (`edu selviz` in the UI) is a server-rendered spaced-repetition study app.
- Product shape: FastAPI backend, Jinja templates, HTMX interactions, PostgreSQL persistence, FSRS-style scheduling adapter.
- UX direction is intentionally calm, minimal, and professional rather than "gamified".

## Branch strategy
- `main`: safer/stable line.
- `phase-2`: active branch for organization-aware access, settings, review/dashboard polish, and the current deployment work.
- Unless told otherwise, new work for the ongoing multi-tenant/admin rollout should happen on `phase-2`.
- Keep `phase-2`, `origin/phase-2`, and `/opt/edu_viz` aligned when deploying.

## Stack / architecture
- Python 3.12
- FastAPI app entry: `app.main:app`
- Templates: `app/templates`
- Static assets: `app/static`
- Styles: `app/static/styles.css`
- DB: PostgreSQL via SQLAlchemy
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
- Dashboard/settings modals should feel native and not break navigation state.
- Use the existing logo asset instead of introducing alternate brand artwork.

## Deployment notes
- Live deployment path: `/opt/edu_viz`.
- Runtime uses Docker Compose.
- Common deploy flow is effectively:
  1. update repo on `phase-2`
  2. sync to `/opt/edu_viz`
  3. rebuild/restart with `docker compose up --build -d`
- `entrypoint.sh` waits for Postgres, runs `alembic upgrade head`, then starts Uvicorn.
- Static assets are cache-busted through `static_asset_url()` in `app/api/routers/pages.py`, which hashes file contents and serves `/assets/<version>/...` URLs.
- Because of hashed asset URLs, CSS/image changes normally do not need manual cache purges after deploy.

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
- Do NOT use `request.scope["query_string"]` hack to pass params between functions — Starlette caches `query_params`.

### Static assets
- Current logo asset: `app/static/brand/logo.jpg`.
- Reference assets through `static_asset_url(...)` so cache-busting keeps working.

## Feature summary (current state)

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
- Study counts moved to review launch and test launch pages
- Review deck isolation: `review_rate` calls `_review_next_inner()` directly (passing deck_id and remaining as explicit params) instead of hacking `request.scope["query_string"]` which is cached by Starlette
- Launch options: 10, 25, 50, 100, 200 cards

### Tests
- Deck-level test center (`/decks/{id}/tests`) — accessible to managers (admin/system_admin)
- Test creation, test-taking (`/tests/{id}`), submission, attempt reports (`/attempts/{id}`)
- Multiple user attempts per test with analysis reports
- Test take page (`take.html`) is standalone (not extending base.html), matches review page style
- One question at a time with Previous/Next navigation, answers stored in JS until submit
- Question IDs are sent as individual hidden fields (name=`question_ids`), NOT comma-separated
- Questions are randomized before selection (shuffle full pool, then slice chosen count)
- Launch options: 10, 25, 50, 100, 200 questions
- Test report page (`report.html`) is standalone, slideshow-style with green/red color coding
- UUID values in templates must be converted to string before `tojson` filter

### Settings (admin)
- Organizations management (`/settings/organizations`) — create, rename, assign users
- Users management (`/settings/users`) — update role, assign organization
- Global deck toggle in deck edit flow (marks deck as globally accessible)

### Routes reference

**Pages router** (`app/api/routers/pages.py`):
- `GET /` — home
- `GET /dashboard` — dashboard with accessible decks and progress metrics
- `GET /settings`, `/settings/organizations`, `/settings/users` — admin settings
- `POST /decks`, `POST /decks/{id}/update`, `POST /decks/{id}/delete` — deck CRUD
- `GET /decks/{id}` — deck detail
- `GET /decks/{id}/flashcards`, `GET /decks/{id}/mcqs`, `GET /decks/{id}/ai-upload` — content pages
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
- `app/templates/tests/list.html`
- `app/static/styles.css`

## Working rule for future agents
- Keep changes practical and small.
- Don't leak secrets into docs, code, commits, or screenshots.
- If you deploy UI changes, verify the live page after the container restarts.
- Review page styling is separate from the main shell — do not pull in the full topbar/nav into review mode.
- Modal flows in dashboard/settings depend on query params + client-side dialog wiring; test both direct page loads and click-open interactions.
- Static assets use `static_asset_url()` for cache-busting; always reference CSS/images/icons through this helper.
