"""Run isolated migration checks against the schema's real dependency order."""

from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

VERSIONS = Path(__file__).parents[1] / "alembic/versions"


def load(filename):
    """Import one revision module without binding it to a connection."""
    path = VERSIONS / filename
    spec = importlib.util.spec_from_file_location(
        "migration_%s_%s" % (path.stem, uuid.uuid4().hex), path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bind(module, connection):
    """Bind a revision module's ``op`` to this connection and return it."""
    module.op = Operations(MigrationContext.configure(connection))
    return module


def relation(connection, schema, table):
    """Exact ``to_regclass`` for the owned schema, independent of ``public``."""
    return connection.scalar(
        text("SELECT to_regclass(:name)"), {"name": "%s.%s" % (schema, table)}
    )


def upgrade_chain(connection, revision):
    """Apply the production upgrade chain through ``revision`` on this connection.

    Filename prefixes follow the linear ``down_revision`` chain, so the ordered
    modules are the real dependency order rather than a test-local subset.
    """
    for path in sorted(VERSIONS.glob("*.py")):
        if not path.name[:4].isdigit() or int(path.name[:4]) > int(revision):
            continue
        bind(load(path.name), connection).upgrade()


@asynccontextmanager
async def historical_schema(test_engine, revision):
    """Yield ``(schema, engine)`` for the real schema at ``revision``, with no rows.

    Later production migrations legitimately retain committed facts (observation
    buckets, maintenance operations, accounting ledgers) in the shared test
    database. Historical checks cannot roll that schema back, so build the
    required chain in a namespace this test owns and drop it afterwards. The
    search path keeps ``public`` last only for extension functions; assert
    relation existence with a schema-qualified name.
    """
    schema = "historical_%s_test_%s" % (revision, uuid.uuid4().hex)
    async with test_engine.begin() as connection:
        await connection.execute(text('CREATE SCHEMA "%s"' % schema))
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: upgrade_chain(sync, revision))
        yield schema, engine
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text('DROP SCHEMA "%s" CASCADE' % schema))
