# EduViz agent notes

## Project overview
- EduViz (`edu selviz` in the UI) is a server-rendered spaced-repetition study app.
- Product shape: FastAPI backend, Jinja templates, HTMX interactions, PostgreSQL persistence, FSRS-style scheduling adapter.
- UX direction is intentionally calm, minimal, and professional rather than "gamified".

## Branch strategy
- `main`: stable base branch.
- `feature/bulk-ai-upload`: current active deploy branch in `/opt/edu_viz` for bulk AI upload flow, jobs page updates, and storage-backed upload handling.
- `feature/deck_segregation`: older branch used for role-based browse tabs, folder UX, and deck access work.
- `phase-2`: completed — organization-aware access, settings, review/dashboard polish.
- Unless told otherwise, check the branch in `/opt/edu_viz` before starting and deploy from the branch currently used by production.

## Stack / architecture
- Python 3.12
- FastAPI app entry: `app.main:app`
- Templates: `app/templates`
- Static assets: `app/static`
- Styles: mostly inline per-page styles in templates plus shared CSS in `app/static/styles.css`
- DB: PostgreSQL via SQLAlchemy
- Connection pooling: PgBouncer (transaction mode) in Docker Compose
- Migrations: Alembic
- Docker runtime: `docker-compose.yml`, `Dockerfile`, `entrypoint.sh`

## Working rules
- Read this file before making changes.
- Update this file during commits with notes useful to future agents.
- Prefer small targeted template/CSS fixes over broad layout rewrites.
- For UI changes in deployed app code, rebuild and restart the `app` service with Docker Compose.
- Code is baked into the image, not bind-mounted. Template/CSS changes require rebuild.
- Run PEP 8 checks with `./scripts/lint_pep8.sh` after Python changes; flake8 config lives in `.flake8`.
- Install tracked repo hooks with `./scripts/install_git_hooks.sh` so commits and pushes are blocked on PEP 8 failures.

## Deployment notes
- Active deploy path: `/opt/edu_viz`
- Rebuild command:
  - `docker compose build app && docker compose up -d app`
- Quick verification:
  - `docker compose logs app --tail=20`
- For deck overview/favorites live metadata changes, verify both rendered HTML and CSS/JS behavior in the running container, not just source files.

## Processing badge notes
- Deck overview processing badge is server-rendered from `show_processing_badge` in `app/api/routers/pages.py`.
- Deck overview live SSE in `app/templates/decks/overview.html` updates only flashcard/MCQ counts; it must not infer processing from count deltas.
- Hidden processing pills should be force-hidden with CSS (`[hidden] { display: none !important; }` scoped to the pill) because theme/reset styles may otherwise leak them visible.
- Dashboard favorites still have separate client-side live-meta logic in `app/templates/dashboard.html`; if badge logic changes there, use the same status source instead of count-only inference.

## Mobile browse layout notes
- Main browse page styles live in `app/templates/decks/browse.html`.
- Deck card markup lives in `app/templates/components/browse_item.html`.
- Mobile browse deck cards are sensitive to DOM order and flex behavior.
- Keep the main mobile deck row simple: checkbox, deck link/content, favorite star.
- Do not switch mobile deck cards to CSS grid unless the DOM structure is redesigned too.
- The favorite star should remain the last control in the row.
- Global/Org badge belongs under title/description inside `.deck-item__text`.
- Access controls should live inside `.deck-item__text` on mobile so they render below description instead of taking a separate side column.
- Badge and access controls may sit side by side under the description if space allows.
- Narrower-feeling mobile cards are achieved by adjusting scroll-area padding and card padding, not by breaking the row structure.

## Current UI intent for browse page
- Mobile deck card order: checkbox -> content -> star
- Badge/access row under description
- Global/Org badge and access icon may sit side by side
- Star on the right, not overlapping checkbox
- Avoid extra right-edge clipping by keeping some right padding in `.browse-content-scroll`
- Mobile top nav now uses icons for Home, Analytics, Settings, and Logout to save width.
- Mobile header user/email area should stay compact: no green status dot, very small padding, truncated text.
- Browse page should not repeat the current folder name above the search box.
- Mobile browse header/action/search spacing is intentionally tight; prefer reducing padding/margins before changing structure.

## Deck access model
- Deck visibility scope lives on `Deck.access_level` with values:
  - `global`: anyone can read
  - `org`: users in the same organization can read
  - `user`: only the owner can read unless explicitly shared
- Per-user deck grants live in `deck_accesses` / `DeckAccess`.
- Per-user grant levels are:
  - `none`
  - `read`
  - `write`
  - `delete`
- Access grants are per deck, per user, unique by `(deck_id, user_id)`.
- Owner always has read/write/delete on their own deck.
- System admin always has full access.
- Org admin can write/delete org-scope decks in their own organization.
- Explicit per-user grants can expand access beyond the deck scope.
- A `read` grant allows viewing a user-scope deck even if the user is not the owner.
- A `write` grant allows modifying the deck.
- A `delete` grant allows deleting the deck.
- In browse, deck-row access icon is display-only; do not reintroduce row-level scope toggling there without explicit request.
- Browse folder visibility must stay aligned with visible deck scope: if global/org-visible decks live inside folders, those folders must also be visible to the same user.
- Be careful with folder/search browse code paths that reuse explicit-share helpers; the live DB enum uses lowercase grant levels (`none/read/write/delete`) and mismatched enum assumptions can break browse.

## Changing access
- Changing the deck scope (`global` / `org` / `user`) is separate from granting per-user deck access.
- Only the owner can change a deck's scope by default.
- System admin can change any deck scope.
- Regular users cannot promote a deck to `global`.
- `org` scope requires org-admin capability.
- `/login` should send unauthenticated users to `/login/providers`; provider-specific OAuth should start only from `/login/microsoft` or `/login/google`.
- Granting/revoking per-user deck access is allowed for:
  - deck owner
  - system admin
  - org admin for org-scope decks in their own organization
- In the UI, the scope selector controls deck scope, while the security/manage-access action is for per-user grants.

## File Storage (SeaweedFS)

- Media files are stored via SeaweedFS (container: `edu_viz-seaweedfs-1` in Docker Compose)
- Storage service in `app/services/storage.py`
- Key env vars: `SEAWEEDFS_URL`, `SEAWEEDFS_COLLECTION`, `SEAWEEDFS_ENABLED`
- Backfill script: `scripts/backfill_media_to_s3.py`
- Media URLs served via `/media/{file_id}` endpoint
- Collection `eduviz-media` stores uploaded images, PDFs, etc.
- Filer port 8888, S3 gateway port 8333, master port 9333, volume port 8080
- Fix: `-volumeSizeLimitMB` flag was wrong → correct is `-master.volumeSizeLimitMB`

## Bulk AI upload title generation
 
- Bulk AI upload deck titles are now AI-first in `app/services/job_worker.py` using the same configured provider/credential already selected for study-pack generation.
- Shared provider code in `app/services/ai_generation.py` now exposes raw text generation so non-study-pack JSON tasks can reuse OpenAI / Minimax / Claude safely.
- Title prompt requires strict JSON `{title, description}`.
- Naming rule: if source text clearly identifies a chapter, format the title as `Chapter {number} - {full chapter or book title}`; otherwise use the full book title only.
- If AI title generation fails or returns invalid JSON, worker falls back to existing heuristic `extract_title_from_text(...)` behavior instead of failing the upload.
- App was rebuilt with `docker compose up --build -d app` after this change because code is baked into the image.

## Bulk AI upload + jobs flow

- Current active work in `/opt/edu_viz` is on `feature/bulk-ai-upload`; verify branch before assuming older folder/browse-only context.
- Bulk AI upload queuing lives in `app/api/routers/bulk_ai_upload.py` via `enqueue_ai_upload_job(...)`.
- Uploads store source PDFs in SeaweedFS through `app/services/storage.py` when storage is available; file rows fall back to inline text only if storage save fails.
- Single-PDF AI import can target an existing deck; ZIP uploads must create/reuse per-file decks and cannot target one existing deck.
- UX intent: bulk upload popup should stay in the same modal during file submission and show only a simple upload progress bar/status before redirecting to `/settings/jobs`.
- While upload submission is active, modal cancel/close actions should abort the in-flight upload request instead of silently dismissing the dialog.
- `/settings/jobs` is the place to monitor background bulk upload progress after submission and should stay mobile-friendly.
- Bulk `Retry all` must preserve each file row's own `created_deck_id`; resetting all files to one shared deck causes cross-file card mixing and duplicated-looking question sets across the batch.
- Jobs retry modal UX should switch to a simple result state after any retry starts: show only retry details plus an `OK` button, and refresh the current job-details view when `OK` is clicked.
- Deck overview live metadata in `app/templates/decks/overview.html` should continue using server count endpoints/SSE only for count refresh, not for inferring separate processing state.
- New users created during first OIDC sign-in inherit `settings.test_enabled_default`; current intended default is enabled so tests are on for brand-new users unless later overridden.

