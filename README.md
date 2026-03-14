# edu selviz

A minimal, server-authoritative spaced repetition web app (AnkiWeb-like) built with:
- FastAPI (Python)
- PostgreSQL (source of truth)
- HTMX + Jinja templates
- FSRS-style scheduler adapter (placeholder, easy to swap)

## Authentication

edu selviz now treats Microsoft sign-in as a generic OpenID Connect integration, with
**Microsoft Entra External ID** as the preferred target configuration.

### Preferred configuration

Copy `.env.example` to `.env` and set:

- `MICROSOFT_ENTRA_EXTERNAL_ID_CLIENT_ID`
- either `MICROSOFT_ENTRA_EXTERNAL_ID_AUTHORITY` **or** `MICROSOFT_ENTRA_EXTERNAL_ID_METADATA_URL`
- `MICROSOFT_ENTRA_EXTERNAL_ID_CLIENT_SECRET` if your app registration uses one
- `MICROSOFT_ENTRA_EXTERNAL_ID_REDIRECT_URI`

If you already know the exact OpenID metadata endpoint for your Entra External ID tenant,
use `MICROSOFT_ENTRA_EXTERNAL_ID_METADATA_URL`. That is the least ambiguous option.

Important: keep the authorize endpoint and token/metadata authority aligned. A broad
Microsoft authorize authority like `https://login.microsoftonline.com/common` or
`https://login.microsoftonline.com/organizations` cannot safely be paired with a
tenant-scoped metadata/token endpoint; Azure rejects the callback code exchange with
`AADSTS700005` in that mixed setup.

For true multi-tenant Microsoft login, use `common` consistently for discovery and
authorization, for example:

- `MICROSOFT_ENTRA_EXTERNAL_ID_AUTHORITY=https://login.microsoftonline.com/common/v2.0`
- leave `MICROSOFT_ENTRA_EXTERNAL_ID_AUTHORIZE_AUTHORITY` empty unless you need an explicit override
- leave `MICROSOFT_ENTRA_EXTERNAL_ID_METADATA_URL` empty unless you need an explicit override

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
- Persistent login/session lifetime defaults to 45 days for both the app auth cookie and OIDC state session
- Decks
- Cards
- Review flow: `/review` → HTMX loads `/review/next` → rate via `/review/rate`
- AI-assisted PDF/DOCX ingestion into flashcards + MCQs using OpenAI
- Separate per-deck flashcard, MCQ, and AI upload pages, including MCQ JSON import and item editing
- Deck-level Anki CSV export
- System-admin-created tests with multiple user attempts and analysis reports

## Phase 2 foundation

This branch starts the move from personal decks to organization-aware access:

- `users.role` now distinguishes `user`, `admin`, and `system_admin`
- `users.organization_id` links users to exactly one organization when assigned
- `decks` now support organization scope, global scope, soft delete metadata, and normalized names for scope-aware uniqueness
- `tags` are organization-scoped and attached to decks through `deck_tags`
- dashboard deck listings now use accessible deck rules and expose basic progress signals:
  `Cards Reviewed`, `Cards Due`, `Accuracy`, and `Last Reviewed`

Current implementation notes:

- existing users are backfilled to one personal organization each and promoted to `admin` during migration so pre-Phase-2 data keeps working
- newly created users still auto-register on first OIDC login, but default to the `user` role and have no organization until a system admin assigns one
- the configured bootstrap email (`SYSTEM_ADMIN_BOOTSTRAP_EMAIL`, default `skv.sree@outlook.com`) is promoted to `system_admin` on login, and any existing matching user is re-promoted on app startup
- deck create/edit/import remains available only to `admin` and `system_admin`
- accuracy is currently calculated as the share of reviews rated `3` or `4`

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
