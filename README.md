# edu selviz

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> **Built with OpenClaw + Nanobot.** This project was vibe-coded — designed, built, and deployed using AI agents with minimal hand-holding. See it live at [edu.selviz.in](https://edu.selviz.in).

A self-hostable, server-rendered spaced repetition web app (AnkiWeb-like) built with:
- FastAPI (Python 3.12)
- PostgreSQL (source of truth)
- HTMX + Jinja templates
- FSRS-style scheduler adapter
- Docker Compose (recommended) or bare Python

---

## Quick start (Docker)

```bash
git clone https://github.com/skvsree/edu_viz.git
cd edu_viz
cp .env.example .env
# edit .env with your values (see .env.example for all options)
docker compose up --build -d
```

Open http://localhost:8000 and log in via Microsoft or Google.

### Using the pre-built image (no build step)

```bash
git clone https://github.com/skvsree/edu_viz.git
cd edu_viz
cp .env.example .env
# edit .env with your values
docker compose -f docker-compose.pull.yml up -d
```

Or pull manually:

```bash
docker pull ghcr.io/skvsree/edu_viz:latest
```

To pin a specific version, use a tag (e.g. `ghcr.io/skvsree/edu_viz:v1.2.3`).

---

## Configuration (.env)

### Required

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | Random string (min 32 chars) used to sign session cookies. **Generate one with:** `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `DATABASE_URL` | PostgreSQL connection string. Default: `postgresql+psycopg://srs:srs@db:5432/srs` (dev only) |
| `SYSTEM_ADMIN_BOOTSTRAP_EMAIL` | First user with this email is promoted to system_admin on login |

### Auth (pick one provider)

**Microsoft Entra External ID** (preferred):

| Variable | Description |
|----------|-------------|
| `MICROSOFT_ENTRA_EXTERNAL_ID_CLIENT_ID` | App registration client ID |
| `MICROSOFT_ENTRA_EXTERNAL_ID_CLIENT_SECRET` | App registration secret |
| `MICROSOFT_ENTRA_EXTERNAL_ID_METADATA_URL` | OIDC metadata URL, e.g. `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration` |
| `MICROSOFT_ENTRA_EXTERNAL_ID_REDIRECT_URI` | Callback URL, e.g. `https://your-domain.com/auth/callback` |

**Google OAuth** (alternative):

| Variable | Description |
|----------|-------------|
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `GOOGLE_REDIRECT_URI` | Callback URL, e.g. `https://your-domain.com/auth/google/callback` |

Add the callback URL in your provider's app registration before starting.

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `SYSTEM_ADMIN_BOOTSTRAP_EMAIL` | `johdoe@email.com` | First user with this email is promoted to system_admin on login |
| `AI_PROVIDER` | `openai` | AI provider: `openai`, `minimax`, or `claude` |
| `AI_API_KEY` | empty | API key for AI content generation |
| `BULK_IMPORT_API_KEY` | empty | API key for bulk deck import via `POST /api/v1/import/*` |
| `FOOTER_COPYRIGHT_TEXT` | `SelViz Software Solutions` | Shown in page footer |
| `TEST_DAILY_LIMIT` | `0` (unlimited) | Max tests per user per day |
| `TEST_COOLDOWN_SECONDS` | `0` | Minimum seconds between test attempts |
| `FORCE_SECURE_COOKIES` | `false` | Set to `true` to add `Secure` flag on session cookies (use in production) |
| `ALLOWED_ORIGINS` | `*` | Comma-separated allowed origins for CSRF protection on form POSTs |
| `FORCE_SECURE_COOKIES` | set `true` as default. When true adds `Secure` flag on session cookies  |
| `ALLOWED_ORIGINS` | set `*` as default. Set to your domain (prevents CSRF on POST endpoints)  |
| `DEBUG` | set `false` as default  |


## Security checklist before going live

- [ ] Set a strong, random `SECRET_KEY` (min 32 chars)
- [ ] Use HTTPS in production (handled by your reverse proxy)
- [ ] Set a strong `BULK_IMPORT_API_KEY` if using bulk import
- [ ] Do not commit `.env` — keep secrets out of version control


---

## Docker Compose services

| Service | Port | Description |
|---------|------|-------------|
| `app` | 18000 | FastAPI application server |
| `pgbouncer` | 6432 | PostgreSQL connection pooler (transaction mode) |
| `db` | 5432 | PostgreSQL 16 database |

### Using an external database

Remove `db` and `pgbouncer` from `docker-compose.yml`, then set `DATABASE_URL` to your external Postgres:

```env
DATABASE_URL=postgresql+psycopg://user:pass@your-postgres-host:5432/srs
```

### Reverse proxy (production)

Point your reverse proxy (Caddy, Nginx, etc.) to `http://localhost:18000`.

Caddy example (`Caddyfile`):
```
your-domain.com {
    reverse_proxy localhost:18000
}
```

Set `MICROSOFT_ENTRA_EXTERNAL_ID_REDIRECT_URI` and `GOOGLE_REDIRECT_URI` to match your domain.

---

## Bare Python (local development)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# create .env with your settings
cp .env.example .env

# run migrations
alembic upgrade head

# start dev server
uvicorn app.main:app --reload
```

Requires PostgreSQL 15+ running locally.

---

## First login (initial setup)

1. Start the app and open the login page.
2. Authenticate with your configured provider (Microsoft/Google).
3. The first user who logs in is promoted to **system_admin**.
4. Subsequent users start as **user** role.
5. As system_admin, visit `/settings` to create organizations, manage users, and create decks.

---

## Recent UI notes

- Browse page supports role-based tabs: All, Global, Org, Mine (visibility depends on role).
- Browse deck cards on mobile use a compact single-row layout with the favorite star kept as the last control.
- Global/Org badge and deck access controls are intended to render below the deck title/description on mobile, not as a separate side column.
- Template/CSS changes require rebuilding the Docker image because the app code is baked into the container.

---

## AI Content Generation (Alpha)

> **Alpha feature** — API and behavior may change in future releases.

AI-assisted card generation creates flashcards and MCQs from existing study content.

### Supported Providers

| Provider | Description |
|----------|-------------|
| OpenAI | GPT-4.1-mini (default) |
| Minimax | MiniMax-Text-01 |
| Claude | Claude Sonnet 4 |

### Configuration

1. Set the provider: `AI_PROVIDER=openai` (or `minimax`/`claude`)
2. Set the API key: `AI_API_KEY=<your-key>`
3. Enable AI generation in organization settings (`/settings/organizations`)

### Provider Override

Organizations and users can override the global AI provider:

- **Organization-level**: Edit org → Enable AI → Override global → Select provider + enter API key
- **User-level**: Edit user → Enable AI → Override org → Select provider + enter API key

Resolution order: user > organization > global env

### Usage

- **AI Generate MCQs**: Creates MCQs from existing flashcards in a deck
- **AI Upload**: Upload PDF/DOCX to generate flashcards and MCQs

AI buttons are only visible when the organization has AI generation enabled.

### MCQ generation flow

Deck overview includes an **AI Generate MCQs** action for managers/admins.

Current flow:
1. `POST /decks/{id}/generate-mcqs/start` starts a generation run
2. A background worker processes source flashcards in batches
3. `GET /decks/{id}/generate-mcqs/stream` streams DB-backed status updates to the browser via SSE
4. Completed source cards are marked as done immediately so reruns skip already-completed items
5. If AI-generated MCQs are bulk-deleted, generation state is reset for the deck so regeneration is possible

Notes:
- Browser `EventSource` requires the stream endpoint to be `GET`
- Progress is tracked per source card, not only per run
- Reruns process only items not already completed
- Failed items can be retried in later runs
- The MCQ/flashcard bulk-select UI uses toggle-switch controls on the management pages

---

## Roles

| Role | Permissions |
|------|-------------|
| `system_admin` | Full platform access — manage all organizations, users, decks, deck scope, and sharing |
| `admin` | Organization admin — manage users/org settings in their org, create/manage decks they own, and write/delete org-scope decks in their organization |
| `user` | Standard user — create and manage their own decks, review accessible decks, and take tests when enabled |

## Deck access and sharing

Deck visibility uses a two-layer model:

1. **Deck scope** (`Deck.access_level`) controls the default audience.
2. **Per-user grants** (`DeckAccess`) can give specific users additional access to a deck.

### Deck scope

- `global` — anyone can read the deck
- `org` — users in the same organization can read the deck
- `user` — only the owner can read the deck unless it is explicitly shared

### Per-user grants

Per-user grants are stored per deck and per user, with one unique grant row per `(deck_id, user_id)`.

Supported grant levels:

- `none`
- `read`
- `write`
- `delete`

These grants extend access beyond the base deck scope:

- `read` allows viewing a deck
- `write` allows modifying a deck
- `delete` allows deleting a deck

### Effective permissions

- Deck owner always has full access to their own deck.
- `system_admin` always has full access.
- `admin` users can write/delete org-scope decks in their own organization.
- Explicit per-user grants can expand access even for `user`-scope decks.

### Who can change what

- **Change deck scope** (`global` / `org` / `user`):
  - owner
  - system admin
- **Grant or revoke per-user deck access**:
  - owner
  - system admin
  - org admin for org-scope decks in their own organization

Notes:

- Regular users cannot promote a deck to `global`.
- Setting a deck to `org` scope requires org-admin capability.
- In the UI, the deck scope selector is separate from the user access/sharing control.

---

## Key routes

| Route | Description |
|-------|-------------|
| `GET /` | Home |
| `GET /dashboard` | Personal dashboard with starred decks |
| `GET /decks/browse` | Browse and search all accessible decks |
| `GET /decks/{id}` | Deck overview (cards, MCQs, tests) |
| `GET /review` | Spaced repetition review session |
| `GET /decks/{id}/ai-upload` | AI-assisted PDF/DOCX card upload |
| `GET /settings` | Admin settings (orgs, users) |
| `GET /analytics` | Study analytics (admin) |
| `GET /decks/{id}/anki-export.csv` | Export deck as Anki-compatible CSV |

---

## File Storage (SeaweedFS)

Media files (images, PDFs, etc.) are stored via SeaweedFS, a distributed object store.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SEAWEEDFS_URL` | `http://seaweedfs:9333` | SeaweedFS master server |
| `SEAWEEDFS_COLLECTION` | `eduviz-media` | Collection name for media storage |
| `SEAWEEDFS_ENABLED` | `false` | Enable SeaweedFS storage |
| `BACKUP_S3_ENABLED` | `false` | Also sync to S3-compatible backup |
| `BACKUP_S3_URL` | empty | S3 endpoint URL |
| `BACKUP_S3_KEY` | empty | S3 access key |
| `BACKUP_S3_SECRET` | empty | S3 secret key |
| `BACKUP_S3_BUCKET` | empty | S3 bucket name |

### Docker service

SeaweedFS runs as a Docker Compose service with master, volume, filer, and S3 gateway:

```yaml
seaweedfs:
  image: chrislusf/seaweedfs:latest
  ports:
    - "9333:9333"  # master
    - "8888:8888"  # filer
    - "8333:8333"  # S3 gateway
    - "8080:8080"  # volume
  command: "server -dir=/data -ip=seaweedfs -master.port=9333 -volume.port=8080 -filer=true -filer.port=8888 -s3 -s3.port=8333"
  volumes:
    - seaweedfs_data:/data
```

### Accessing stored files

Files are served via `/media/{file_id}` endpoint, which routes through the storage service. URLs are generated automatically when cards with media are created/imported.

### Backfill existing media

To migrate existing local media files to SeaweedFS:

```bash
python scripts/backfill_media_to_s3.py
```

---

## Folder organization

EduViz supports nested folders for deck organization.

- Root browse view shows root folders and unfiled decks.
- Folder browse view shows that folder's direct subfolders and decks.
- Search stays flat and does not drill into folder navigation.
- Breadcrumbs are shown while browsing inside folders.
- Folder names allow only letters, numbers, and underscore (`[a-zA-Z0-9_]+`).
- Moving a folder into itself or one of its descendants is blocked server-side and prevented in the browse picker UI.
- Browse keeps the search + action area sticky while the folder/deck list scrolls below it.
- Mobile browse layout removes extra left padding and tightens action/search spacing to avoid dead space.
- Dashboard favorites show each deck's folder path label.

Relevant APIs:

- `GET /api/v1/folders`
- `GET /api/v1/folders/{folder_id}`
- `POST /api/v1/folders`
- `PUT /api/v1/folders/{folder_id}`
- `DELETE /api/v1/folders/{folder_id}`
- `PUT /api/v1/folders/{folder_id}/move`
- `GET /api/v1/folders/tree`
- `POST /api/v1/decks/move`

---

## Database migrations

Migrations run automatically on container start (`entrypoint.sh`).

Manual run:
```bash
alembic upgrade head
```

---

## Development

```bash
# run tests
pytest

# run with coverage
pytest --cov=app tests/
```

---

## Project structure

```
app/
  api/routers/     # FastAPI route handlers (pages, content, auth)
  core/            # Config, DB setup
  models/          # SQLAlchemy models
  services/        # Business logic (access, scheduling, AI, auth)
  templates/       # Jinja2 HTML templates
  static/          # CSS, JS, images
alembic/versions/  # DB migrations
docker-compose.yml
Dockerfile
entrypoint.sh
requirements.txt
```

---

<p align="center">
  <a href="https://github.com/skvsree/edu_viz"><img src="https://img.shields.io/badge/GitHub-Repo-blue?logo=github" alt="GitHub"></a>
</p>

Questions, bugs, or feature requests? [Open an issue](https://github.com/skvsree/edu_viz/issues) on GitHub.
