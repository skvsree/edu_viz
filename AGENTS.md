# AGENTS.md

## Project
- Name: edu_viz
- Stack: FastAPI, Jinja2 templates, HTMX, PostgreSQL, Docker Compose
- Active deploy path: `/opt/edu_viz`
- Primary live branch used here: `feature/deck_segregation`

## Working rules
- Read this file before making changes.
- Update this file during commits with notes useful to future agents.
- For UI changes in deployed app code, rebuild and restart the `app` service with Docker Compose.
- Code is baked into the image, not bind-mounted. Template/CSS changes require rebuild.
- Prefer small targeted template/CSS fixes over broad layout rewrites.

## Mobile browse layout notes
- File: `app/templates/decks/browse.html`
- Deck card markup: `app/templates/components/browse_item.html`
- Mobile browse deck cards are sensitive to DOM order and flex behavior.
- Keep the main mobile deck row simple: checkbox, deck link/content, favorite star.
- Do not switch mobile deck cards to CSS grid unless the DOM structure is redesigned too.
- The favorite star should remain the last control in the row.
- Global/Org badge belongs under title/description inside `.deck-item__text`.
- Access controls should live inside `.deck-item__text` on mobile so they render below description instead of taking a separate side column.
- Badge and access controls may sit side by side under the description if space allows.
- Narrower-feeling mobile cards are achieved by adjusting scroll-area padding and card padding, not by breaking the row structure.

## Deployment notes
- Rebuild command:
  - `docker compose build app && docker compose up -d app`
- Quick verification:
  - `docker compose logs app --tail=20`

## Current UI intent for browse page
- Mobile deck card order: checkbox -> content -> star
- Badge/access row under description
- Star on the right, not overlapping checkbox
- Avoid extra right-edge clipping by keeping some right padding in `.browse-content-scroll`
