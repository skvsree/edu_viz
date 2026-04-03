# edu selviz

A self-hostable, server-rendered spaced repetition web app (AnkiWeb-like) built with:
- FastAPI (Python 3.12)
- PostgreSQL (source of truth)
- HTMX + Jinja templates
- FSRS-style scheduler adapter
- Docker Compose (recommended) or bare Python

## Security checklist before going live

- [ ] Set a strong, random `SECRET_KEY` (min 32 chars)
- [ ] Enable `FORCE_SECURE_COOKIES=true` to add `Secure` flag on session cookies
- [ ] Configure `ALLOWED_ORIGINS` to your domain (prevents CSRF on POST endpoints)
- [ ] Use HTTPS in production (handled by your reverse proxy)
- [ ] Set `DEBUG=false` (or unset `DEBUG`)
- [ ] Set a strong `BULK_IMPORT_API_KEY` if using bulk import
- [ ] Do not commit `.env` — keep secrets out of version control

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

---

## Configuration (.env)

### Required

| Variable | Description |
|----------|-------------|
| `SECRET_KEY` | Random string (min 32 chars) used to sign session cookies. **Generate one with:** `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `DATABASE_URL` | PostgreSQL connection string. Default: `postgresql+psycopg://srs:srs@db:5432/srs` (dev only) |

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
| `SYSTEM_ADMIN_BOOTSTRAP_EMAIL` | `skv.sree@outlook.com` | First user with this email is promoted to system_admin on login |
| `OPENAI_API_KEY` | empty | Enables AI-assisted card generation from PDFs/DOCXs |
| `OPENAI_MODEL` | `gpt-4.1-mini` | OpenAI model for AI features |
| `BULK_IMPORT_API_KEY` | empty | API key for bulk deck import via `POST /api/v1/import/*` |
| `FOOTER_COPYRIGHT_TEXT` | `SelViz Software Solutions` | Shown in page footer |
| `TEST_DAILY_LIMIT` | `0` (unlimited) | Max tests per user per day |
| `TEST_COOLDOWN_SECONDS` | `0` | Minimum seconds between test attempts |
| `FORCE_SECURE_COOKIES` | `false` | Set to `true` to add `Secure` flag on session cookies (use in production) |
| `ALLOWED_ORIGINS` | `*` | Comma-separated allowed origins for CSRF protection on form POSTs |

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
edu.your-domain.com {
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
