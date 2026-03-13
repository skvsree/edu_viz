# SRS Web

A minimal, server-authoritative spaced repetition web app (AnkiWeb-like) built with:
- FastAPI (Python)
- PostgreSQL (source of truth)
- HTMX + Jinja templates
- FSRS-style scheduler adapter (placeholder, easy to swap)

## Authentication

EduViz now treats Microsoft sign-in as a generic OpenID Connect integration, with
**Microsoft Entra External ID** as the preferred target configuration.

### Preferred configuration

Copy `.env.example` to `.env` and set:

- `MICROSOFT_ENTRA_EXTERNAL_ID_CLIENT_ID`
- either `MICROSOFT_ENTRA_EXTERNAL_ID_AUTHORITY` **or** `MICROSOFT_ENTRA_EXTERNAL_ID_METADATA_URL`
- `MICROSOFT_ENTRA_EXTERNAL_ID_CLIENT_SECRET` if your app registration uses one
- `MICROSOFT_ENTRA_EXTERNAL_ID_REDIRECT_URI`

If you already know the exact OpenID metadata endpoint for your Entra External ID tenant,
use `MICROSOFT_ENTRA_EXTERNAL_ID_METADATA_URL`. That is the least ambiguous option.

### Legacy Azure AD B2C compatibility

The app still accepts the older `AZURE_B2C_*` variables as a fallback while migrating.
That keeps current deployments working until you switch over to the new env var names.

## Run (Docker)

1) Configure env:
```bash
cp .env.example .env
# edit .env and set Microsoft Entra External ID / OIDC vars
```

2) Start:
```bash
docker compose up --build
```

Open: http://localhost:8000

Login flow: click **Login** → Microsoft redirects back to `/auth/callback`.

### Production
Set:
- `MICROSOFT_ENTRA_EXTERNAL_ID_REDIRECT_URI=https://edu.selviz.in/auth/callback`

And add that exact Redirect URI in your Microsoft Entra app registration / External ID configuration.

## Run (local)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# ensure DATABASE_URL points to a running postgres
uvicorn app.main:app --reload
```

## MVP features
- Register/login (signed cookie session)
- Decks
- Cards
- Review flow: `/review` → HTMX loads `/review/next` → rate via `/review/rate`

## Migrations (Alembic)

This project uses Alembic for schema migrations.

### Docker
Migrations run automatically on container start (see `entrypoint.sh`).

### Local
```bash
# ensure DATABASE_URL points to a running postgres
alembic upgrade head
uvicorn app.main:app --reload
```
