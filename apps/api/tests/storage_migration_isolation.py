"""Owned-schema isolation for durable storage-migration ledger tests.

The source Gate ledger is global: any committed ``storage_operations`` row at the
legacy location is real registered access, so a test must never rely on the
shared public schema being drained.  These fixtures clone the structure (and its
guard triggers) of the tables the migration control plane touches into a
disposable schema whose name is unique per test; the whole namespace is dropped
afterwards.  Unresolved facts in the public schema stay untouched - they are
simply not part of the window under test.

Fixture names deliberately mirror ``tests/conftest.py`` so a module can shadow
the public-schema versions by importing them.
"""

import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base

# Required control-plane tables plus every registered business root consulted
# by scope-wide observations; no row lookup may fall through to public data.
TABLES = (
    "users",
    "api_keys",
    "async_jobs",
    "notifications",
    "audit_logs",
    "storage_connections",
    "storage_locations",
    "storage_bindings",
    "storage_maintenance_gates",
    "storage_operations",
    "storage_usage_mutations",
    "storage_migration_inventories",
    "storage_migration_copies",
    "storage_migration_copy_attempts",
    "storage_migration_copy_deltas",
    "storage_migration_windows",
    "storage_migration_finalizations",
    "storage_migration_read_publications",
    "storage_migration_read_routes",
    "storage_migration_cutovers",
    "managed_original_reclaim_claims",
    "managed_original_reclaim_attempts",
)


def _checked(names):
    missing = [name for name in TABLES if name not in names]
    if missing:
        raise AssertionError(f"Isolated schema tables are missing: {missing}")
    return tuple(sorted(set(TABLES) | (set(Base.metadata.tables) & names)))


async def _clone(source, schema):
    async with source.begin() as connection:
        present = set(
            await connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        )
        tables = _checked(present)
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        for name in tables:
            await connection.execute(
                text(
                    f'CREATE TABLE "{schema}"."{name}" '
                    f'(LIKE public."{name}" INCLUDING ALL)'
                )
            )
        triggers = list(
            await connection.execute(
                text(
                    "SELECT relation.relname, pg_get_triggerdef(trigger.oid), "
                    "pg_get_functiondef(trigger.tgfoid) "
                    "FROM pg_trigger AS trigger "
                    "JOIN pg_class AS relation ON relation.oid = trigger.tgrelid "
                    "JOIN pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE NOT trigger.tgisinternal "
                    "AND namespace.nspname = 'public'"
                )
            )
        )
        foreign_keys = list(
            await connection.execute(
                text(
                    "SELECT child.relname, parent.relname, con.conname, "
                    "pg_get_constraintdef(con.oid) "
                    "FROM pg_constraint AS con "
                    "JOIN pg_class AS child ON child.oid = con.conrelid "
                    "JOIN pg_class AS parent ON parent.oid = con.confrelid "
                    "JOIN pg_namespace AS ns ON ns.oid = child.relnamespace "
                    "WHERE con.contype = 'f' AND ns.nspname = 'public' "
                    "AND con.conparentid = 0"
                )
            )
        )
    async with source.begin() as connection:
        # Table names and guard functions must resolve inside the owned namespace
        # (migration downgrades drop them by unqualified name); the engine's
        # session search_path does the same for the fixtures' own connections.
        await connection.execute(text(f'SET LOCAL search_path = "{schema}", public'))
        # LIKE omits foreign keys and their SET NULL/CASCADE behavior.
        for child, parent, name, definition in foreign_keys:
            if child not in tables:
                continue
            if parent not in tables:
                raise AssertionError(
                    f"Isolated foreign key escapes schema: {child}.{name}"
                )
            definition = definition.replace(
                "REFERENCES public.", f'REFERENCES "{schema}".'
            )
            await connection.exec_driver_sql(
                f'ALTER TABLE "{schema}"."{child}" ADD CONSTRAINT "{name}" {definition}'
            )
        functions = []
        for relation, _, definition in triggers:
            if relation in tables and definition not in functions:
                functions.append(definition)
        for definition in functions:
            await connection.exec_driver_sql(
                definition.replace(
                    "CREATE OR REPLACE FUNCTION public.",
                    "CREATE OR REPLACE FUNCTION ",
                    1,
                )
            )
        for relation, definition, _ in triggers:
            if relation in tables:
                await connection.exec_driver_sql(
                    definition.replace(" ON public.", " ON ")
                    .replace(
                        "EXECUTE FUNCTION public.", f'EXECUTE FUNCTION "{schema}".'
                    )
                    .replace(
                        "EXECUTE PROCEDURE public.", f'EXECUTE PROCEDURE "{schema}".'
                    )
                )


@pytest.fixture
async def migration_engine(test_engine):
    """A disposable schema with the migration control-plane structure."""
    schema = "storage_migration_test_" + uuid.uuid4().hex
    engine = None
    try:
        await _clone(test_engine, schema)
        engine = create_async_engine(
            test_engine.url,
            connect_args={"server_settings": {"search_path": schema + ",public"}},
        )
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


@pytest.fixture
async def db_session(migration_engine):
    """SAVEPOINT-isolated session inside the owned schema."""
    connection = await migration_engine.connect()
    transaction = await connection.begin()
    maker = async_sessionmaker(connection, class_=AsyncSession, expire_on_commit=False)
    session = maker()
    await connection.begin_nested()
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        from app.services.system_settings_service import SystemSettingsService

        SystemSettingsService.invalidate()


@pytest.fixture
async def httpx_client(db_session):
    from app.deps import get_db
    from app.main import app

    async def override():
        yield db_session

    app.dependency_overrides[get_db] = override
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


async def _identity(db, role, email, name):
    from app.core.security import create_access_token
    from tests.factory import create_user

    user = await create_user(db, role, email, name)
    return user, create_access_token(subject=str(user.id), role=role)


@pytest.fixture
async def super_admin(db_session):
    return await _identity(db_session, "super_admin", "admin@test.local", "Admin")


@pytest.fixture
async def project_admin(db_session):
    return await _identity(db_session, "project_admin", "pm@test.local", "PM")


@pytest.fixture
async def annotator(db_session):
    return await _identity(db_session, "employee", "anno@test.local", "Annotator")


@pytest.fixture
async def reviewer(db_session):
    return await _identity(db_session, "employee", "qa@test.local", "Reviewer")
