# EduViz agent notes

## Project overview
- EduViz (`edu selviz` in the UI) is a server-rendered spaced-repetition study app.
- Product shape: FastAPI backend, Jinja templates, HTMX interactions, PostgreSQL persistence, FSRS-style scheduling adapter.
- UX direction is intentionally calm, minimal, and professional rather than "gamified".

## Branch strategy
- `main`: stable base branch AND the active deploy branch in `/opt/edu_viz`. The bulk AI upload flow, jobs page (Active/History tabs), storage-backed upload handling, parent/child file schema, and deck-ID-based retry were all merged here ahead of `feature/bulk-ai-upload`.
- `feature/bulk-ai-upload`: **stale**. Last commit `7dbf240 chore: checkpoint pending branch changes for review` and is now superseded by `main`. Delete only after Kanboard task #55 passes live validation.
- `feature/deck_segregation`: older branch used for role-based browse tabs, folder UX, and deck access work.
- `phase-2`: completed — organization-aware access, settings, review/dashboard polish.
- As of 2026-06-24, `main` is ahead of `feature/bulk-ai-upload`; HEAD on `main` is `faad437 Use deck IDs for bulk retry targets`. Other local branches exist unpushed — do not push or delete without confirmation.
- `feature/concept-maps`: current active work branch — async concept-map generation job pipeline + AI revision notes PDF (see "Concept map + revision notes" below). Unpushed; do not push without confirmation.
- Unless told otherwise, check the branch in `/opt/edu_viz` before starting and deploy from `main`.

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

## AI model

- AI provider/model string is currently hardcoded in `app/services/ai_generation.py` (study generation at line 476, title generation at line 141). There is no `AI_MODEL` env var yet.
- As of 2026-06-24 the live model is **`MiniMax-M3`** for both the deck-title pass and the study-pack generation pass.
- Provider config in `.env`:
  - `AI_PROVIDER=minimax`
  - `MINIMAX_API_KEY=sk-cp-...` (shared across all MiniMax calls)
  - Endpoint: `https://api.minimax.io/v1/text/chatcompletion_v2`
- The model string is the value of the JSON `model` field sent to MiniMax; do not change it without verifying the new model is actually accepted by `https://api.minimax.io/v1/text/chatcompletion_v2` first (a direct probe with a 1-token request is the quickest check).
- `MiniMax-M2` is the previous default and is not in use; if you see it in code, that is a regression and should be replaced with `MiniMax-M3`.

## Bulk AI upload + jobs flow

- Current active work in `/opt/edu_viz` is on `main`; the `feature/bulk-ai-upload` branch is stale and should be cleaned up only after task #55 passes live validation.
- Jobs page is split into two tabs (Active / History) rendered by `_jobs_response()` in `app/api/routers/pages.py` and `app/templates/settings/jobs.html`. Active shows pending/processing/in-progress with live updates; History is paginated and live polling/SSE is required only for the Active tab.
- The CSS-settle loading overlay should also cover `/settings/jobs`; if it works elsewhere but not here, fix the shared/base wiring rather than inventing a jobs-only loader. Validation selectors: `app-loading-overlay`, `Loading…`, and `window.__eduVizFinishPageLoad` (NOT `page-loading-overlay`).
- Each bulk job must show one stable file row per uploaded file. Retries add `BulkAIUploadFile` attempts under the same `BulkAIUploadChildFile`; UI row identity comes from `child_file_id`, not filename or attempt id.
- Bulk retry must preserve each file row's own `created_deck_id`; resetting all files to one shared deck causes cross-file card mixing and duplicated-looking question sets across the batch.
- Job-level bulk cancel must propagate immediately to all pending/processing file rows so the UI and worker state agree; do not leave child file rows appearing active after a whole-job cancel.
- Bulk AI upload must keep strict per-file deck ownership: worker generation and retry flows must use each file row's `created_deck_id` and must not fall back to a shared `bulk.deck_id` for multi-file output decks.
- Retry flows identify the target output deck by persisted deck ID (`BulkAIUploadFile.created_deck_id`) and reuse that exact deck via `existing_deck_id`; do not infer the retry target from filenames or generated display names.
- Bulk AI upload title generation must pass the archive filename (`bulk.filename`) into `build_title_generation_prompt()` and warn the AI not to copy repeated archive/PDF filenames unless the source text confirms them. `BulkAIUpload.filename` is the ZIP/archive name; per-file names live on `BulkAIUploadChildFile.original_filename` and `BulkAIUploadFile.original_filename`.
- Title prompt includes both `Archive filename` and `PDF filename` context for the AI.
- The first successful attempt writes the resolved title into both `BulkAIUploadFile.extracted_title` and `BulkAIUploadChildFile.display_title`. Fresh retries copy the prior `extracted_title` into `child_file.display_title` so retry names stay aligned.
- Jobs page should show each bulk file row's own generated counts (`flashcards_generated`, `mcqs_generated`, `duplicate_count`) and offer cancel actions for pending/processing bulk jobs and file rows.
- Jobs page bulk-job cards should use a file-first layout: one visible row per uploaded file under each ZIP/job, with deck/attempt detail hidden by default.
- Jobs page should use a job-level `Show files` / `Hide files` toggle for each uploaded ZIP/job instead of per-file deck toggles.
- Jobs page file rows should show the extracted chapter/book title as the primary label once available; fall back to the uploaded filename only before title extraction succeeds. Retries must keep that visible title aligned with the latest successful extracted title.
- The generated deck name should remain small secondary text underneath the primary file title.
- Bulk AI upload queuing lives in `app/api/routers/bulk_ai_upload.py` via `enqueue_ai_upload_job(...)`.
- Uploads store source PDFs in SeaweedFS through `app/services/storage.py` when storage is available; file rows fall back to inline text only if storage save fails.
- Single-PDF AI import can target an existing deck; ZIP uploads must create/reuse per-file decks and cannot target one existing deck.
- UX intent: bulk upload popup should stay in the same modal during file submission and show only a simple upload progress bar/status before redirecting to `/settings/jobs`.
- While upload submission is active, modal cancel/close actions should abort the in-flight upload request instead of silently dismissing the dialog.
- `/settings/jobs` is the place to monitor background bulk upload progress after submission and should stay mobile-friendly.
- Jobs page should be split into two tabs: an active tab for pending/processing/in-progress items with live updates, and a history tab for failed/completed items with pagination. Live polling/SSE is required only for the active tab.
- The CSS-settle loading overlay should also cover `/settings/jobs`; if it works elsewhere but not here, fix the shared/base wiring rather than inventing a jobs-only loader.
- Bulk `Retry all` must preserve each file row's own `created_deck_id`; resetting all files to one shared deck causes cross-file card mixing and duplicated-looking question sets across the batch.
- Jobs retry modal UX should switch to a simple result state after any retry starts: show only retry details plus an `OK` button, and refresh the current job-details view when `OK` is clicked.
- Jobs page should show each bulk file row's own generated counts (`flashcards_generated`, `mcqs_generated`, `duplicate_count`) and offer cancel actions for pending/processing bulk jobs and file rows.
- Jobs page bulk-job cards should use a file-first layout: one visible row per uploaded file under each ZIP/job, with deck/attempt detail hidden by default.
- Jobs page should use a job-level `Show files` / `Hide files` toggle for each uploaded ZIP/job instead of per-file deck toggles.
- Jobs page file rows should show the extracted chapter/book title as the primary label once available; fall back to the uploaded filename only before title extraction succeeds. Retries must keep that visible title aligned with the latest successful extracted title.
- The generated deck name should remain small secondary text underneath the primary file title.
- Job-level bulk cancel must propagate immediately to all pending/processing file rows so the UI and worker state agree; do not leave child file rows appearing active after a whole-job cancel.
- Bulk AI upload must keep strict per-file deck ownership: worker generation and retry flows must use each file row's `created_deck_id` and must not fall back to a shared `bulk.deck_id` for multi-file output decks.
- Retry flows should identify the target output deck by persisted deck ID (`BulkAIUploadFile.created_deck_id`) and reuse that exact deck with `existing_deck_id`; do not infer the retry target from filenames or generated display names.
- Deck overview live metadata in `app/templates/decks/overview.html` should continue using server count endpoints/SSE only for count refresh, not for inferring separate processing state.
- New users created during first OIDC sign-in inherit `settings.test_enabled_default`; current intended default is enabled so tests are on for brand-new users unless later overridden.

## Parent/Child bulk upload schema (current architecture)

- Three tables (Alembic head: `0026_bulk_child_files`):
  - `bulk_ai_uploads` (job row, one per ZIP/single-PDF upload): status, totals, provider, error_message, deck_id, user_id.
  - `bulk_ai_upload_child_files` (one row per uploaded file inside the job): `bulk_upload_id`, `child_key`, `original_filename`, `display_title`, `storage_key`, `file_size`, `latest_attempt_id`. Unique on `(bulk_upload_id, child_key)`.
  - `bulk_ai_upload_files` (attempt rows): `bulk_upload_id`, `child_file_id`, `original_filename`, `extracted_title`, `extracted_description`, `content_text`, `storage_key`, `status`, counts, `created_deck_id`, `error_message`, timestamps.
- One uploaded file ⇒ exactly one `BulkAIUploadChildFile` for the lifetime of the job. Retries append new `BulkAIUploadFile` attempt rows and re-point `child_file.latest_attempt_id`.
- The UI shows one row per `BulkAIUploadChildFile` (not per attempt). Attempt history is detail, not the primary list.
- Models live in `app/models/bulk_ai_upload.py`; worker lives in `app/services/job_worker.py`; router in `app/api/routers/bulk_ai_upload.py`.
- Earlier concern "next Jobs refactor should move from repeated attempt rows to a parent/child upload schema so retries stay under one child file instead of creating confusing duplicate file rows" is now resolved by this schema.

## Kanboard task #55 acceptance (live validation required)

Task #55 — "Validate bulk upload schema refactor live" — must be evidenced before closing. Required proof, not assumptions:

1. `python3 -m compileall app tests` clean, repo tests green.
2. App rebuilt via `docker compose build app && docker compose up -d app` so the image reflects the code.
3. Real bulk upload (ZIP with ≥2 PDFs) submitted against the running app and observed in DB:
   - One `bulk_ai_uploads` row.
   - N `bulk_ai_upload_child_files` rows (one per uploaded file), unique `(bulk_upload_id, child_key)`.
   - At least one `bulk_ai_upload_files` attempt row per child file.
4. Real bulk retry (after at least one file fails) and observed:
   - No new `bulk_ai_upload_child_files` rows created.
   - New attempt rows appended with `child_file_id` re-pointed to the original child.
   - `child_file.latest_attempt_id` updated.
   - `BulkAIUploadFile.created_deck_id` preserved on retry (deck ID, not filename, drives the target deck).
5. UI verification: `/settings/jobs?tab=active` shows one stable row per uploaded file across retry, with the same `display_title` (or aligned title) and the same deck reference.
6. No synthetic / static / mock data used for the validation evidence.

If any of the above fails, task #55 stays open and the failure becomes the next scoped work item.

## Concept map + revision notes (async job pipeline)

Work lives on `feature/concept-maps` (unpushed). Core idea: long-running AI work moved out of request handlers into the `Job` worker.

- Concept map generation is a **background job**. `POST /decks/{id}/concept-map/generate` enqueues a `Job` (`job_type="concept_map"`, reference_id = ConceptMap id) and returns `{status:"pending"}` immediately. It is ALWAYS a hard regenerate: the ConceptMap row is reset/overwritten in place so the UUID stays stable for Job references.
- Dedup: a pending/running `concept_map` job for the same deck blocks re-queuing (checked in the endpoint).
- Polling: `GET /decks/{id}/concept-map/status` → `{status: none|pending|processing|ready|failed, node_count?, error?}`.
- Worker handlers in `app/services/job_worker.py`: `process_concept_map_gen`, `process_concept_map_revision`. Both resolve the deck owner's AI credential via `_resolve_ai_provider_and_credential`; failure to resolve → heuristic-only path, never a hard error.
- AI-as-selector: when a credential resolves, each heuristic topic is enriched with AI-picked verbatim key-point bullets (`select_topic_recall_bullets`); per-topic failure falls back to heuristic bullets.

### Revision notes PDF
- `POST /decks/{id}/concept-map/generate-revision-pdf` queues `job_type="concept_map_revision"`; refuses to double-queue, serves immediately if already ready.
- Progress on `ConceptMap.revision_pdf_status` (none → pending → processing → ready/failed); PDF stored in SeaweedFS under `revision_pdf_storage_key`.
- Download: `GET /decks/{id}/concept-map/revision-notes.pdf` (inline, filename from deck name).
- AI path: DeepSeek v4 Pro (`revision_notes_model=deepseek-v4-pro`) synthesizes topics + bullets as strict JSON in ONE call (`build_revision_notes_prompt` / `parse_ai_revision_response` in `app/services/revision_notes.py`), then falls back to heuristic + verbatim selector when AI fails.
- Source cap: `revision_notes_max_source_chars` default 60000 (env `REVISION_NOTES_MAX_SOURCE_CHARS`). The old hardcoded 22000 silently dropped trailing sections of long NCERT chapters — do not lower this casually.
- `DeepSeekRevisionProvider` in `app/services/ai_generation.py`: OpenAI-compatible call to opencode.ai endpoint; strips `reasoning_content` (CoT) — only `content` is used. Registered as provider name `deepseek`.
- NCERT text cleaning: `app/services/text_cleaner.py` — zero-config artifact stripper (repeated standalone header/footer lines by page frequency ≥30%, InDesign timestamps, lone page numbers/numeric figure rows, form-feed normalisation). Page-marker-agnostic: uses `\f` pages if present, else 40-line pseudo-pages.
- Alembic `0030_concept_map_revision_pdf`: adds `revision_pdf_status` (indexed) + `revision_pdf_storage_key` to `concept_maps`.
- Standalone CLI (offline mirror of the pipeline): `scripts/run_revision_notes.py <chapter.pdf> <out.pdf> [chapter_label] [deck_name] [source_title]`.
- Frontend: `app/templates/decks/concept_map.html` — revision PDF request + status polling wired to the new endpoints.
- New config keys (`.env`): `REVISION_NOTES_MODEL`, `REVISION_NOTES_API_ENDPOINT`, `REVISION_NOTES_MAX_TOKENS`, `REVISION_NOTES_MAX_SOURCE_CHARS` (see `app/core/config.py`).

### OpenCode Go request headers (do not remove)
- The opencode.ai Go endpoint enforces the client contract from https://opencode.ai/docs/go/#where-can-i-use-it: a missing `x-opencode-session` returns **400 MissingSessionID** and a missing/generic `User-Agent` is rejected at the edge with **403 (Cloudflare error code 1010)**.
- Both opencode call sites therefore build headers through `_opencode_headers()` in `app/services/ai_generation.py` (`OpencodeStudyPackProvider.generate_text` for study packs, `DeepSeekRevisionProvider._call_api` for revision notes).
- Session ids come from `opencode_session_id(suffix)`: one id per conversation — `job-<job id>` for bulk AI uploads (pinned in `job_worker.process_bulk_ai_upload`) and `rev-<child_file_id>` for revision notes (pinned once per chapter in `generate_revision_notes_for_child` and shared across topic calls). Client UA is configurable via `OPENCODE_CLIENT_UA` (default `eduviz/1.0`).

## Testing notes
- Run the suite with `.venv/bin/python -m pytest -q` (currently **126 passing, 0 failing**), then `./scripts/lint_pep8.sh`. The pre-commit hook runs the PEP 8 check and blocks the commit on failure.
- `tests/test_dashboard_routes.py` owns the shared `FakeDB` session double, re-used by `test_feature_flags.py` and `test_settings_routes.py`. It answers both `db.execute(select(...))` and the legacy `db.query(Model).filter(...).first()` API — the latter reads its `query_results` list. `deck_overview` uses `db.query` for `has_concept_map`, so a fake without `query()` fails with `AttributeError: 'FakeDB' object has no attribute 'query'`.
- `JobsSettingsDB` in `tests/test_settings_routes.py` matches results on the table named in the emitted statement (`jobs`, `bulk_ai_uploads`, `bulk_ai_upload_files`, `bulk_ai_upload_child_files`, `decks`) via keyword arguments. Do **not** reintroduce a positional result queue: it silently hands rows to the wrong query as soon as the route gains a new one (that is exactly how the retry/error tests broke).
- When a route starts using a new Session API or query, extend the fake — do not reshape app code around the fake.
- The jobs page renders **one tab at a time** (`?tab=active` is the default). Failed/stopped/completed uploads only appear on `?tab=history`, so tests asserting the retry action or the bulk error message must request that tab — otherwise they assert against an empty page and pass for the wrong reason.
