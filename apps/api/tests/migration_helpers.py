"""Run isolated migration checks against the schema's real dependency order."""

from contextlib import contextmanager
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations


@contextmanager
def without_later_migrations(sync, monkeypatch, revision):
    connection = sync.connection() if callable(sync.connection) else sync
    later = []
    for path in sorted((Path(__file__).parents[1] / "alembic/versions").glob("*.py")):
        if path.name[:4].isdigit() and int(path.name[:4]) > int(revision):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            monkeypatch.setattr(
                migration, "op", Operations(MigrationContext.configure(connection))
            )
            later.append(migration)
    for migration in reversed(later):
        migration.downgrade()
    try:
        yield
    finally:
        for migration in later:
            migration.upgrade()
