"""Helpers for statements whose ``IN`` list can outgrow Postgres' parameter cap.

Postgres refuses any statement carrying more than 65,535 bind parameters
(``number of parameters must be between 0 and 65535``). psycopg reports that as
``psycopg.OperationalError``, SQLAlchemy wraps it in
``sqlalchemy.exc.OperationalError`` — https://sqlalche.me/e/20/e3q8 — and the
whole transaction is lost, so a user-triggered delete fails outright.

``WHERE column IN (<python list>)`` therefore has a hard ceiling that grows with
the user's own data: a deck holding more than 65,535 cards, or a "select all"
screen posting that many ids, is enough to break it. Id lists must never be
bound wholesale. Two ways out, in order of preference:

* **Subquery** — when a query describes the ids
  (``select(Card.id).where(Card.deck_id == deck_id)``), use it directly as the
  ``IN`` argument: no bind parameters at all, and still a single statement.
* **Chunking** — when the ids arrive from outside (a form post, a checkbox
  selection), split them with :func:`iter_chunks` and issue one statement per
  chunk through :func:`delete_in_chunks` / :func:`select_scalars_in_chunks`.
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Sequence

from sqlalchemy import delete

# Well under Postgres' 65,535 ceiling, and small enough that each statement
# stays cheap to plan.
IN_CLAUSE_CHUNK_SIZE = 5000


def iter_chunks(
    items: Sequence[Any] | Iterable[Any], size: int = IN_CLAUSE_CHUNK_SIZE
) -> Iterator[list[Any]]:
    """Yield ``items`` as lists of at most ``size`` entries."""
    if size < 1:
        raise ValueError("chunk size must be >= 1")
    materialised = list(items)
    for start in range(0, len(materialised), size):
        yield materialised[start : start + size]


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def delete_in_chunks(
    db,
    model,
    column,
    ids: Sequence[Any] | Iterable[Any],
    *,
    chunk_size: int = IN_CLAUSE_CHUNK_SIZE,
) -> int:
    """Delete ``model`` rows whose ``column`` is in ``ids``, one chunk at a time.

    Returns the total number of rows removed. Caller-visible ordering is
    preserved: chunks are issued in sequence inside the caller's transaction.
    """
    removed = 0
    for chunk in iter_chunks(ids, chunk_size):
        removed += _rowcount(db.execute(delete(model).where(column.in_(chunk))))
    return removed


def select_scalars_in_chunks(
    db,
    build_statement,
    ids: Sequence[Any] | Iterable[Any],
    *,
    chunk_size: int = IN_CLAUSE_CHUNK_SIZE,
) -> list[Any]:
    """Run ``build_statement(chunk)`` once per chunk and concatenate the scalars.

    The caller dedupes when it needs to (an id can appear in more than one
    chunk's result set, e.g. a ``DISTINCT`` lookup over a many-to-many table).
    """
    values: list[Any] = []
    for chunk in iter_chunks(ids, chunk_size):
        values.extend(db.execute(build_statement(chunk)).scalars().all())
    return values
