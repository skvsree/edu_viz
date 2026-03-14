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
- Review page styling is intentionally separate from the dark shell used elsewhere. Reusing generic topbar styles blindly can make review mode feel heavy.
- Modal flows in dashboard/settings depend on query params + client-side dialog wiring. It is easy to break edit/create reopen behavior if IDs/data attributes drift.
- Static asset changes may appear stale only if you bypass `static_asset_url()`; always use the helper in templates for CSS/images/icons.
- Organization/admin logic is sensitive: visibility and management permissions are not the same thing. Check `app/services/access.py` before changing deck/user behavior.

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

### Static assets
- Current logo asset: `app/static/brand/logo.jpg`.
- Reference assets through `static_asset_url(...)` so cache-busting keeps working.

## Useful files to inspect first
- `README.md`
- `app/api/routers/pages.py`
- `app/services/access.py`
- `app/services/microsoft_identity.py`
- `app/templates/base.html`
- `app/templates/dashboard.html`
- `app/templates/review/page.html`
- `app/static/styles.css`

## Working rule for future agents
- Keep changes practical and small.
- Don’t leak secrets into docs, code, commits, or screenshots.
- If you deploy UI changes, verify the live page after the container restarts.
