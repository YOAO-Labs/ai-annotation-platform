"""Export execution cannot regain write rights through a replacement session."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.workers.export import (
    ExportBuildInProgress,
    ExportExecutionLost,
    _ExportExecution,
)


@pytest.mark.asyncio
async def test_invalidated_execution_never_probes_or_recovers():
    connection = SimpleNamespace(invalidated=True)
    db = SimpleNamespace(scalar=AsyncMock(return_value=101))
    execution = _ExportExecution(db, connection)
    with pytest.raises(ExportExecutionLost):
        await execution.assert_owned()
    connection.invalidated = False
    with pytest.raises(ExportExecutionLost):
        await execution.assert_owned()
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_replaced_backend_cannot_regain_execution_rights():
    db = SimpleNamespace(scalar=AsyncMock(side_effect=[101, True, 202]))
    execution = _ExportExecution(db, SimpleNamespace(invalidated=False))
    await execution.acquire(f"job:{uuid.uuid4()}")
    with pytest.raises(ExportExecutionLost, match="replaced"):
        await execution.assert_owned()
    with pytest.raises(ExportExecutionLost):
        await execution.assert_owned()
    assert db.scalar.await_count == 3


@pytest.mark.asyncio
async def test_busy_execution_remains_retryable():
    db = SimpleNamespace(scalar=AsyncMock(side_effect=[101, False]))
    execution = _ExportExecution(db, SimpleNamespace(invalidated=False))
    with pytest.raises(ExportBuildInProgress):
        await execution.acquire(f"job:{uuid.uuid4()}")
    assert execution.lost is False


@pytest.mark.parametrize("fail_during_lock", [False, True])
@pytest.mark.asyncio
async def test_disconnect_during_ownership_query_is_retryable(fail_during_lock):
    connection = SimpleNamespace(invalidated=False)
    calls = 0

    async def scalar(_statement):
        nonlocal calls
        calls += 1
        if fail_during_lock and calls == 1:
            return 101
        connection.invalidated = True
        raise ConnectionError("connection lost")

    execution = _ExportExecution(SimpleNamespace(scalar=scalar), connection)
    with pytest.raises(ExportExecutionLost):
        await execution.acquire(f"job:{uuid.uuid4()}")
    assert execution.lost is True


@pytest.mark.asyncio
async def test_non_connection_query_error_is_preserved():
    db = SimpleNamespace(scalar=AsyncMock(side_effect=ValueError("bad query")))
    execution = _ExportExecution(db, SimpleNamespace(invalidated=False))
    with pytest.raises(ValueError, match="bad query"):
        await execution.assert_owned()
    assert execution.lost is False


@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires the explicitly owned module-data verifier",
)
@pytest.mark.asyncio
async def test_postgres_export_lock_survives_commit_and_releases_on_session_loss(
    test_engine,
):
    from sqlalchemy.engine import make_url

    from tests.test_asset_storage_cleanup_runtime import _verify_postgres
    from tests.test_module_data_roles import _role_url

    _verify_postgres(test_engine.url)
    url = make_url(
        _role_url("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL", "identity_api_consumer")
    )
    assert url.database == test_engine.url.database and str(url.database).endswith(
        "_test"
    )
    engine = create_async_engine(url, poolclass=NullPool)
    key = f"job:{uuid.uuid4()}"
    try:
        async with engine.connect() as connection, AsyncSession(bind=connection) as db:
            execution = _ExportExecution(db, connection)
            await execution.acquire(key)
            await db.commit()
            await execution.assert_owned()
            async with (
                engine.connect() as other,
                AsyncSession(bind=other) as contender_db,
            ):
                contender = _ExportExecution(contender_db, other)
                with pytest.raises(ExportBuildInProgress):
                    await contender.acquire(key)
            await connection.invalidate()
            await db.rollback()
            with pytest.raises(ExportExecutionLost):
                await execution.assert_owned()
        async with (
            engine.connect() as replacement,
            AsyncSession(bind=replacement) as db,
        ):
            recovered = _ExportExecution(db, replacement)
            await recovered.acquire(key)
            assert recovered.backend_pid != execution.backend_pid
            await db.commit()
    finally:
        await engine.dispose()
