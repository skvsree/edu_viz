# SRS Web

A minimal, server-authoritative spaced repetition web app (AnkiWeb-like) built with:
- FastAPI (Python)
- PostgreSQL (source of truth)
- HTMX + Jinja templates
- FSRS-style scheduler adapter (placeholder, easy to swap)

## Run (Docker)

1) Configure env:
```bash
cp .env.example .env
# edit .env and set Azure AD B2C vars
```

2) Start:
```bash
docker compose up --build
```

Open: http://localhost:8000

Login flow: click **Login (Azure AD B2C)** → Azure redirects back to `/auth/callback`.

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
