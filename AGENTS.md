# EduViz agent notes

## Project overview
- EduViz (`edu selviz` in the UI) is a server-rendered spaced-repetition study app.
- Product shape: FastAPI backend, Jinja templates, HTMX interactions, PostgreSQL persistence, FSRS-style scheduling adapter.
- UX direction is intentionally calm, minimal, and professional rather than "gamified".

## Branch strategy
- `main`: stable base branch.
- `feature/deck_segregation`: current active deploy branch in `/opt/edu_viz` for role-based browse tabs, folder UX, and deck access work.
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

## Deployment notes
- Active deploy path: `/opt/edu_viz`
- Rebuild command:
  - `docker compose build app && docker compose up -d app`
- Quick verification:
  - `docker compose logs app --tail=20`

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

## Changing access
- Changing the deck scope (`global` / `org` / `user`) is separate from granting per-user deck access.
- Only the owner can change a deck's scope by default.
- System admin can change any deck scope.
- Regular users cannot promote a deck to `global`.
- `org` scope requires org-admin capability.
- Granting/revoking per-user deck access is allowed for:
  - deck owner
  - system admin
  - org admin for org-scope decks in their own organization
- In the UI, the scope selector controls deck scope, while the security/manage-access action is for per-user grants.
