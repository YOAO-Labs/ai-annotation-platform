"""Keep existing fake-provider lifecycle tests at the transport boundary."""

import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import training_storage_io
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.storage import storage_service


def mock_legacy_transport(monkeypatch):
    async def run(
        db,
        owner,
        purpose,
        callback,
        *args,
        target=None,
        on_acquired=None,
        expected_revision=None,
        node_id=None,
        timeout=None,
        accounting=None,
        source_attempt=None,
        source_boot=None,
        source_deletion=None,
        source_key=None,
        **kwargs,
    ):
        assert owner.location_id == LEGACY_LOCATION_ID
        if on_acquired is not None:
            await on_acquired(None)
        return await asyncio.to_thread(callback, storage_service, *args, **kwargs)

    async def prepare(db, owner, **kwargs):
        return owner

    def physical(target, callback, *args, accounting=None, source=None, **kwargs):
        return callback(storage_service, *args, **kwargs)

    monkeypatch.setattr(training_storage_io, "run", run)
    monkeypatch.setattr(training_storage_io, "prepare", prepare)
    monkeypatch.setattr(training_storage_io, "physical", physical)
    monkeypatch.setattr(
        training_storage_io.artifact_reads, "resolve", AsyncMock(return_value=None)
    )


@asynccontextmanager
async def learning_consumer(test_engine):
    if os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        yield async_sessionmaker(test_engine, expire_on_commit=False)
        return

    url = make_url(os.environ["AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL"])
    assert url.username == "identity_embedded_api_consumer"
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    engine = create_async_engine(url, hide_parameters=True)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as db:
            assert await db.scalar(text("SELECT current_user")) == url.username
        yield maker
    finally:
        await engine.dispose()
