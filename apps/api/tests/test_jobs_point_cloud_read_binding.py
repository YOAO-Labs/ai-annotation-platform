"""Point-cloud quality reads through the Jobs owner capability."""

from __future__ import annotations

import os
import re
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock, Mock, call

import pytest
from sqlalchemy import inspect

from app.db.models.async_job import AsyncJobStatus
from app.db.models.point_cloud_quality import PointCloudQualityRun
from app.services import async_job as async_job_svc
from app.services.point_cloud_quality import service as quality_service
from app.services.point_cloud_quality.execution import _cancel_requested
from app.services.point_cloud_quality.service import PointCloudQualityError
from tests.factory import create_project
from tests.test_jobs_single_read_native import (
    _assert_jobs_owner_read,
    _sql_capture,
    _without_private_job_select,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        ("pending", {}, False),
        (AsyncJobStatus.CANCELLED.value, {}, True),
        ("running", {"cancel_requested": True}, True),
    ],
)
async def test_cancel_probe_uses_narrow_jobs_owner_projection(
    status, payload, expected
):
    job_id = uuid.uuid4()
    result = Mock()
    result.one_or_none.return_value = SimpleNamespace(status=status, payload=payload)
    db = Mock(execute=AsyncMock(return_value=result))

    assert await _cancel_requested(db, SimpleNamespace(async_job_id=job_id)) is expected

    statement, parameters = db.execute.await_args.args
    assert " ".join(str(statement).lower().split()) == (
        "select status, payload from aap_jobs_v1.read_job("
        "cast(:job_id as pg_catalog.uuid))"
    )
    assert parameters == {"job_id": job_id}


@pytest.mark.asyncio
async def test_cancel_probe_without_job_id_does_not_query():
    db = Mock(execute=AsyncMock())

    assert await _cancel_requested(db, SimpleNamespace(async_job_id=None)) is False
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_uses_facade_and_preserves_errors(monkeypatch):
    run_id, job_id = uuid.uuid4(), uuid.uuid4()
    run = SimpleNamespace(id=run_id)
    job = SimpleNamespace(celery_task_id="quality-task")
    db = Mock(get=AsyncMock(return_value=run))
    get_job = AsyncMock(return_value=job)
    dispatcher = Mock()
    monkeypatch.setattr(quality_service, "get_job", get_job)

    await quality_service.dispatch_quality_run(
        db, run_id=run_id, async_job_id=job_id, dispatcher=dispatcher
    )

    assert get_job.await_args.args == (db, job_id)
    assert get_job.await_args.kwargs == {}
    db.get.assert_awaited_once_with(PointCloudQualityRun, run_id)
    dispatcher.dispatch_run.assert_called_once_with(
        "point_cloud_quality", run_id, execution_id="quality-task"
    )

    get_job.return_value = None
    with pytest.raises(PointCloudQualityError) as invalid:
        await quality_service.dispatch_quality_run(
            db, run_id=run_id, async_job_id=job_id, dispatcher=dispatcher
        )
    assert invalid.value.status_code == 500
    assert invalid.value.detail["reason"] == "point_cloud_quality_dispatch_invalid"

    get_job.return_value = job
    dispatcher.dispatch_run.side_effect = RuntimeError("broker unavailable")
    with pytest.raises(PointCloudQualityError) as failed:
        await quality_service.dispatch_quality_run(
            db, run_id=run_id, async_job_id=job_id, dispatcher=dispatcher
        )
    assert failed.value.status_code == 503
    assert failed.value.detail["reason"] == "point_cloud_quality_dispatch_failed"
    assert db.get.await_args_list == [call(PointCloudQualityRun, run_id)] * 3


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
    reason="requires verifier-owned strict Jobs role and disposable database",
)
async def test_quality_reads_keep_jobs_owner_and_session_behavior(
    db_session, project_admin
):
    actor, _token = project_admin
    project = await create_project(db_session, owner_id=actor.id)
    job = await async_job_svc.create_job(
        db_session,
        kind="point_cloud_quality",
        user_id=actor.id,
        project_id=project.id,
        payload={},
        celery_task_id="quality-task",
    )
    run = PointCloudQualityRun(
        project_id=project.id,
        async_job_id=job.id,
        requested_by_id=actor.id,
        scope_json={},
        config_revision=1,
        config_digest="a" * 64,
        config_snapshot={},
        source_snapshot=[],
        source_snapshot_digest="b" * 64,
        singleflight_key=uuid.uuid4().hex,
    )
    db_session.add(run)
    await db_session.flush()
    run_id, job_id = run.id, job.id
    db_session.expunge(job)
    db_session.expunge(run)

    async with _without_private_job_select(db_session):
        dispatcher = Mock()
        with _sql_capture(db_session) as statements:
            await quality_service.dispatch_quality_run(
                db_session,
                run_id=run_id,
                async_job_id=job_id,
                dispatcher=dispatcher,
            )
        dispatcher.dispatch_run.assert_called_once_with(
            "point_cloud_quality", run_id, execution_id="quality-task"
        )
        _assert_jobs_owner_read(statements)

        job = await async_job_svc.get_job(db_session, job_id)
        job.status = AsyncJobStatus.CANCELLED.value
        job.payload = {"cancel_requested": True}
        run_ref = SimpleNamespace(async_job_id=job_id)
        with db_session.no_autoflush:
            with _sql_capture(db_session) as statements:
                assert await _cancel_requested(db_session, run_ref) is False
            assert job.status == AsyncJobStatus.CANCELLED.value
            assert job.payload == {"cancel_requested": True}
            assert inspect(job).attrs.status.history.has_changes()
            assert inspect(job).attrs.payload.history.has_changes()
        _assert_jobs_owner_read(statements)
        normalized = [statement.lower() for statement in statements]
        assert any(
            re.search(
                r"\bselect\s+status\s*,\s*payload\s+from\s+"
                r"aap_jobs_v1\.read_job\(",
                statement,
            )
            for statement in normalized
        )

        with _sql_capture(db_session) as statements:
            assert await _cancel_requested(db_session, run_ref) is True
        _assert_jobs_owner_read(statements)
        assert not inspect(job).attrs.status.history.has_changes()
        assert not inspect(job).attrs.payload.history.has_changes()
