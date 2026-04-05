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

---

## Roles

| Role | Permissions |
|------|-------------|
| `system_admin` | Full access — manage all orgs, users, decks |
| `admin` | Manage decks in their organization, manage card content |
| `user` | Review and take tests on assigned/organization decks |

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
