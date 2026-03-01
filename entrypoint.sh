#!/usr/bin/env sh
set -e

# Run DB migrations before starting the app
alembic upgrade head

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
