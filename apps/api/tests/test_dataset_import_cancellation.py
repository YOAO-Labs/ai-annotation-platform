from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.async_job_contracts import AsyncJobOperationFailure
from app.application.job_cancellation import SqlJobCancellation
from app.services.assets_import import (
    DATASET_IMPORT_PROTOCOL,
    DatasetImportCapabilities,
)


def _job(*, payload: dict | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        kind="dataset_import",
        payload=payload or {},
        status="pending",
        user_id=uuid.uuid4(),
        project_id=None,
        celery_task_id=None,
    )


@pytest.mark.asyncio
async def test_protocol_import_uses_native_cancel_for_another_owners_job(monkeypatch):
    job = _job(payload={"import_protocol": DATASET_IMPORT_PROTOCOL})
    admin_id = uuid.uuid4()
    db = AsyncMock(spec=AsyncSession)
    monkeypatch.setattr("app.services.async_job.get_job", AsyncMock(return_value=job))
    native_cancel = AsyncMock(return_value=SimpleNamespace(stage="cancelled"))
    generic_access = AsyncMock(return_value=False)
    monkeypatch.setattr(DatasetImportCapabilities, "cancel", native_cancel)
    monkeypatch.setattr("app.services.async_job_access._can_access_job", generic_access)

    result = await SqlJobCancellation(db, execution_control=Mock()).cancel(
        job_id=job.id, actor_id=admin_id
    )

    assert result == "cancelled"
    native_cancel.assert_awaited_once_with(actor_id=admin_id, job_id=job.id)
    generic_access.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_import_cancel_keeps_generic_owner_authorization(monkeypatch):
    job = _job()
    actor_id = uuid.uuid4()
    db = AsyncMock(spec=AsyncSession)
    monkeypatch.setattr("app.services.async_job.get_job", AsyncMock(return_value=job))
    monkeypatch.setattr(
        "app.application.job_cancellation.IdentityAccountLockData.read_actor_status",
        AsyncMock(return_value=SimpleNamespace(is_active=True)),
    )
    native_cancel = AsyncMock()
    generic_access = AsyncMock(return_value=False)
    monkeypatch.setattr(DatasetImportCapabilities, "cancel", native_cancel)
    monkeypatch.setattr("app.services.async_job_access._can_access_job", generic_access)

    with pytest.raises(AsyncJobOperationFailure) as failure:
        await SqlJobCancellation(db, execution_control=Mock()).cancel(
            job_id=job.id, actor_id=actor_id
        )

    assert failure.value.status_code == 403
    assert failure.value.detail == "not your job"
    generic_access.assert_awaited_once_with(db, job=job, actor_id=actor_id)
    native_cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_import_cancel_keeps_existing_generic_transition(monkeypatch):
    job = _job(payload={"old_import_payload": True})
    actor_id = job.user_id
    db = AsyncMock(spec=AsyncSession)
    monkeypatch.setattr("app.services.async_job.get_job", AsyncMock(return_value=job))
    native_cancel = AsyncMock()
    generic_access = AsyncMock(return_value=True)
    mark_cancelled = AsyncMock()
    notify_terminal = AsyncMock()
    monkeypatch.setattr(DatasetImportCapabilities, "cancel", native_cancel)
    monkeypatch.setattr("app.services.async_job_access._can_access_job", generic_access)
    monkeypatch.setattr("app.services.async_job.mark_cancelled", mark_cancelled)
    monkeypatch.setattr(
        "app.application.job_cancellation.notify_job_terminal", notify_terminal
    )

    result = await SqlJobCancellation(db, execution_control=Mock()).cancel(
        job_id=job.id, actor_id=actor_id
    )

    assert result == "cancelled"
    generic_access.assert_awaited_once_with(db, job=job, actor_id=actor_id)
    mark_cancelled.assert_awaited_once_with(db, job.id)
    notify_terminal.assert_awaited_once_with(db, job_id=job.id)
    native_cancel.assert_not_awaited()
