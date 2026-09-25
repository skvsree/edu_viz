"""Guards on the Alembic revision chain's version table.

``alembic_version.version_num`` starts life as VARCHAR(32), so a revision id
longer than 32 characters cannot be stamped: the migration body runs, then the
final ``UPDATE alembic_version`` fails with StringDataRightTruncation and the
whole transaction - including the schema changes - rolls back.

That is what took prod down: the 39-character id
``0028_bulk_revision_notes_section_titles`` did not fit a VARCHAR(32) column, the
stamp died, ``entrypoint.sh`` (``set -e``) aborted before uvicorn started, and the
site answered 502 on every path. These tests encode the rule so a future long id
cannot repeat it, and they are file-level on purpose: the chain is data, and a
live Postgres is not required to check its shape.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

VERSIONS_DIR = Path('alembic/versions')

# The version table is created narrow by Alembic itself; anything longer than
# this cannot be written without widening the column first.
NARROW_WIDTH = 32

# Tolerate the annotated form used by older revisions, e.g.
# ``revision: str = '0013'`` / ``down_revision: Union[str, None] = '0012'``.
_REVISION_RE = re.compile(
    r"^revision(?:\s*:\s*[^=\n]+)?\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE
)
_DOWN_REVISION_RE = re.compile(r"^down_revision(?:\s*:\s*[^=\n]+)?\s*=\s*(.+)$", re.MULTILINE)


class Migration:
    """One revision file, with just the parts the chain tests need."""

    def __init__(self, path: Path, source: str):
        self.path = path
        self.source = source
        revision = _REVISION_RE.search(source)
        assert revision, f'{path} has no revision id'
        self.id = revision.group(1)
        self.down_revision = self._parse_down_revision(source, path)

    @staticmethod
    def _parse_down_revision(source: str, path: Path) -> str | tuple[str, ...] | None:
        match = _DOWN_REVISION_RE.search(source)
        assert match, f'{path} has no down_revision'
        # Evaluate the literal, not the file: the chain is declared as a plain
        # string, a tuple of strings, or None.
        value = ast.literal_eval(match.group(1).strip())
        assert value is None or isinstance(value, (str, tuple)), f'{path}: bad down_revision'
        return value

    @property
    def widens_version_table(self) -> bool:
        """True if this revision widens alembic_version.version_num."""
        return (
            'alter_column' in self.source
            and 'version_num' in self.source
            and str(255) in self.source
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f'<Migration {self.id} down={self.down_revision}>'


def _load_migrations() -> dict[str, Migration]:
    migrations: dict[str, Migration] = {}
    for path in sorted(VERSIONS_DIR.glob('*.py')):
        if path.name.startswith('__'):
            continue
        migration = Migration(path, path.read_text(encoding='utf-8'))
        assert migration.id not in migrations, f'duplicate revision id {migration.id}'
        migrations[migration.id] = migration
    assert migrations, 'no migration files found'
    return migrations


def _chain_from_base(migrations: dict[str, Migration]) -> list[Migration]:
    """Walk base -> head, asserting the chain is intact and linear."""
    parents = {m.down_revision for m in migrations.values()}
    bases = [m for m in migrations.values() if m.down_revision is None]
    assert len(bases) == 1, f'expected exactly one base revision, found {sort_ids(bases)}'

    children: dict[str, list[Migration]] = {}
    for migration in migrations.values():
        down = migration.down_revision
        for parent in (down,) if isinstance(down, str) else (down or ()):
            assert parent in migrations, f'{migration.id} has unknown down_revision {parent}'
            children.setdefault(parent, []).append(migration)

    for parent, kids in children.items():
        assert len(kids) == 1, f'chain branches at {parent}: {sort_ids(kids)}'
    assert len(children) == len(migrations) - 1, (
        'the revision graph is not a single chain: '
        f'{len(migrations) - 1 - len(children)} revision(s) are unreachable from the base'
    )
    assert parents - {None} <= set(children), 'a revision has no children and is not the head'

    chain = [bases[0]]
    while chain[-1].id in children:
        chain.append(children[chain[-1].id][0])
    assert len(chain) == len(migrations), 'chain walk did not reach every revision'
    return chain


def sort_ids(items) -> list[str]:
    return sorted(item.id if isinstance(item, Migration) else item for item in items)


def test_migration_chain_is_intact():
    migrations = _load_migrations()
    chain = _chain_from_base(migrations)
    assert chain[-1].id == '0033_widen_alembic_version', (
        'unexpected head; if you added a migration, update this test deliberately'
    )


def test_long_revision_ids_are_always_preceded_by_a_widen():
    """A >32-char id may only be stamped once the column has been widened.

    The widen may be in the long revision itself (0028 widens and *then* stamps)
    or in an earlier one (0033 widens for every database that reaches it).
    """
    migrations = _load_migrations()
    widened = False
    offenders = []
    for migration in _chain_from_base(migrations):
        if migration.widens_version_table:
            widened = True
        elif len(migration.id) > NARROW_WIDTH and not widened:
            offenders.append((migration.id, len(migration.id), migration.path.name))
    assert not offenders, (
        'these revision ids exceed the VARCHAR(32) version column and no earlier '
        f'migration widens it: {offenders}'
    )


def test_widen_migration_can_be_stamped_by_a_narrow_column():
    """0033's own id must fit the narrow column it may be running against."""
    migrations = _load_migrations()
    widen = migrations['0033_widen_alembic_version']
    assert len(widen.id) <= NARROW_WIDTH, (
        f'{widen.id} is {len(widen.id)} chars and could not be stamped by a '
        f'VARCHAR({NARROW_WIDTH}) version column'
    )
    assert widen.widens_version_table, '0033 no longer widens alembic_version'
    assert widen.down_revision == '0032_job_visibility'


def test_widen_migration_downgrade_does_not_narrow_the_column():
    """Narrowing on downgrade would make long ids unstampable again."""
    source = (VERSIONS_DIR / '0033_widen_alembic_version.py').read_text(encoding='utf-8')
    downgrade = source.split('def downgrade()', 1)[1]
    assert 'alter_column' not in downgrade and '255' not in downgrade, (
        'downgrade must not narrow alembic_version.version_num'
    )


if __name__ == '__main__':  # pragma: no cover
    pytest.main([__file__, '-v'])
