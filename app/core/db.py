import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


def _int_env(name: str, default: int) -> int:
    """Read an integer override from the environment, ignoring junk."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Pool sizing. The SQLAlchemy defaults (5 + 10 overflow) were tight for this app:
# the job worker holds one session for a whole run, every SSE stream opens its
# own, and each request takes one - so a busy admin page plus a bulk run could
# exhaust the pool and leave requests queued for the full 30s timeout.
POOL_SIZE = _int_env("DB_POOL_SIZE", 10)
MAX_OVERFLOW = _int_env("DB_MAX_OVERFLOW", 20)
POOL_TIMEOUT = _int_env("DB_POOL_TIMEOUT", 30)
POOL_RECYCLE = _int_env("DB_POOL_RECYCLE", 1800)
# A statement that never returns holds its connection forever: that is how one
# stuck query can stall every writer behind it. 0 disables the timeout.
STATEMENT_TIMEOUT_MS = _int_env("DB_STATEMENT_TIMEOUT_MS", 300000)

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_timeout=POOL_TIMEOUT,
    pool_recycle=POOL_RECYCLE,
)

if STATEMENT_TIMEOUT_MS:
    # It cannot ride connect_args: pgbouncer refuses the `options` startup
    # parameter outright ("unsupported startup parameter in options:
    # statement_timeout") and the app then fails to boot. Setting it per
    # connection works talking to Postgres directly; behind pgbouncer's
    # transaction pooling the durable place is the database's own default:
    #     ALTER DATABASE <db> SET statement_timeout = '300s';
    @event.listens_for(engine, "connect")
    def _set_statement_timeout(dbapi_connection, connection_record):  # pragma: no cover
        try:
            with dbapi_connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = %d" % STATEMENT_TIMEOUT_MS)
        except Exception:
            # Best effort: never stop a connection over this.
            pass


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
