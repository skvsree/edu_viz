#!/usr/bin/env sh
set -e

python - <<'PY'
import time

import psycopg
from app.core.config import settings

conninfo = settings.database_url.replace("+psycopg", "", 1)

for attempt in range(60):
    try:
        with psycopg.connect(conninfo):
            print("Database is ready")
            break
    except Exception as exc:
        if attempt == 59:
            raise
        print(f"Waiting for database... ({exc})")
        time.sleep(1)
PY

# Run DB migrations before starting the app
alembic upgrade head

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
