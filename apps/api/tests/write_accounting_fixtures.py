"""Independent commits for tests that persist only storage mutation facts."""

from contextlib import AsyncExitStack

import pytest_asyncio
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models.storage_usage import StorageUsageMutation
from app.services import storage_usage_accounting
from tests.managed_image_inference_fixtures import managed_database


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def write_accounting_databases():
    async with AsyncExitStack() as owned:
        yield owned, []


async def _clear_mutations(control, created):
    async with control.begin():
        client = await control.scalar(
            text(
                "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                "AND pid <> pg_backend_pid() "
                "AND backend_type IS DISTINCT FROM 'autovacuum worker' LIMIT 1"
            )
        )
        assert client is None, f"Accounting database still has client {client}"
        if created:
            await control.execute(
                delete(StorageUsageMutation).where(StorageUsageMutation.id.in_(created))
            )
        foreign = await control.scalar(select(StorageUsageMutation.id).limit(1))
        assert foreign is None, f"Unowned storage_usage_mutations row {foreign}"


def _expired(*_):
    raise RuntimeError("Accounting case has ended")


@pytest_asyncio.fixture(loop_scope="module")
async def accounting_factory(
    write_accounting_databases, test_db_url, monkeypatch, request
):
    owned, clean = write_accounting_databases
    if clean:
        control = clean.pop()
    else:
        private = await owned.enter_async_context(managed_database(test_db_url))
        control = await owned.enter_async_context(private.connect())

    created = set()
    await _clear_mutations(control, created)
    engine = create_async_engine(control.engine.url, poolclass=NullPool)
    begin = storage_usage_accounting.begin_in_transaction

    async def track_begin(db, **kwargs):
        handle = await begin(db, **kwargs)
        if getattr(db, "bind", None) is engine and handle.fresh:
            created.add(handle.id)
        return handle

    monkeypatch.setattr(storage_usage_accounting, "begin_in_transaction", track_begin)
    failures = request.session.testsfailed
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        event.listen(engine.sync_engine, "checkout", _expired)
        await engine.dispose()

    if request.session.testsfailed == failures:
        await _clear_mutations(control, created)
        clean.append(control)
