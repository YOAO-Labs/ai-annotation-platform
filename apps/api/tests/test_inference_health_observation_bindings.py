"""Real health observation through the independent HTTP owner process."""

from __future__ import annotations

from contextlib import AsyncExitStack
import os
import uuid

import pytest
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.ml_backend import MLBackendService
from tests.inference_health_observation_helpers import backend_server, select_binding
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _record_phases(test_engine, trace):
    from tests.test_inference_health_legacy import _phase

    def observe(conn, cursor, statement, parameters, context, many):
        phase = _phase(statement)
        if phase:
            trace.extend(phase if isinstance(phase, list) else [phase])

    event.listen(test_engine.sync_engine, "before_cursor_execute", observe)
    return observe


async def test_nonfinite_backend_metadata_keeps_original_post_clock_write_failure(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await select_binding(stack, monkeypatch, test_engine)
        with backend_server(nonfinite=True) as (origin, calls):
            async with maker() as caller:
                row = MLBackendRegistry(id=uuid.uuid4(), name="Nonfinite", url=origin)
                caller.add(row)
                await caller.flush()
                trace = []
                observe = _record_phases(test_engine, trace)
                try:
                    with pytest.raises(DBAPIError):
                        await MLBackendService(caller).check_health(row.id)
                finally:
                    event.remove(
                        test_engine.sync_engine, "before_cursor_execute", observe
                    )
                assert len(calls) == 2
                assert trace.index("clock") < trace.index("backend_write")
                await caller.rollback()


async def test_null_health_json_keeps_legacy_type_error_before_clock(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await select_binding(stack, monkeypatch, test_engine)
        with backend_server(null_health=True) as (origin, calls):
            async with maker() as caller:
                row = MLBackendRegistry(id=uuid.uuid4(), name="Null health", url=origin)
                caller.add(row)
                await caller.flush()
                trace = []
                observe = _record_phases(test_engine, trace)
                try:
                    with pytest.raises(TypeError, match="NoneType"):
                        await MLBackendService(caller).check_health(row.id)
                finally:
                    event.remove(
                        test_engine.sync_engine, "before_cursor_execute", observe
                    )
                assert calls == [("/health", None)]
                assert "clock" not in trace and "backend_write" not in trace
                assert row.last_checked_at is None
                await caller.rollback()
