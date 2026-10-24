from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy import select

from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.db.models.project_member import ProjectMember
from app.services.notification import NotificationService
from tests.factory import create_membership, create_project


@pytest.mark.asyncio
async def test_delivery_scope_reader_flushes_pending_and_dirty_jobs_by_default(
    db_session, annotator
):
    user, _ = annotator
    service = NotificationService(db_session)

    pending_id = uuid.uuid4()
    pending = AsyncJob(
        id=pending_id,
        kind="dataset_import",
        user_id=user.id,
        status="completed",
        payload={},
        result={},
    )
    db_session.add(pending)
    row = SimpleNamespace(
        target_type="async_job", target_id=pending_id, payload={}, user_id=user.id
    )

    assert await service._resolve_delivery_scopes([row]) == [(False, None, False)]
    assert pending not in db_session.new

    dirty = AsyncJob(
        id=uuid.uuid4(),
        kind="dataset_import",
        user_id=user.id,
        status="completed",
        payload={},
        result={},
    )
    db_session.add(dirty)
    await db_session.flush()
    dirty_id = dirty.id
    dirty.kind = "batch_predict"
    dirty_row = SimpleNamespace(
        target_type="async_job", target_id=dirty_id, payload={}, user_id=user.id
    )

    assert await service._resolve_delivery_scopes([dirty_row]) == [(True, None, False)]
    assert dirty.kind == "batch_predict"
    assert dirty not in db_session.dirty


@pytest.mark.asyncio
async def test_delivery_scope_reader_observes_database_only_under_no_autoflush(
    db_session, annotator
):
    user, _ = annotator
    service = NotificationService(db_session)

    pending_id = uuid.uuid4()
    pending = AsyncJob(
        id=pending_id,
        kind="dataset_import",
        user_id=user.id,
        status="completed",
        payload={},
        result={},
    )
    db_session.add(pending)
    row = SimpleNamespace(
        target_type="async_job", target_id=pending_id, payload={}, user_id=user.id
    )

    with db_session.no_autoflush:
        assert await service._resolve_delivery_scopes([row]) == [(True, None, False)]
    assert pending in db_session.new
    await db_session.flush()

    dirty = AsyncJob(
        id=uuid.uuid4(),
        kind="dataset_import",
        user_id=user.id,
        status="completed",
        payload={},
        result={},
    )
    db_session.add(dirty)
    await db_session.flush()
    dirty_id = dirty.id
    dirty.kind = "batch_predict"
    dirty_row = SimpleNamespace(
        target_type="async_job", target_id=dirty_id, payload={}, user_id=user.id
    )

    with db_session.no_autoflush:
        assert await service._resolve_delivery_scopes([dirty_row]) == [
            (False, None, False)
        ]
    assert dirty in db_session.dirty


@pytest.mark.asyncio
async def test_delivery_scope_autoflush_remains_inside_callers_rollback(
    db_session, annotator
):
    user, _ = annotator
    job_id = uuid.uuid4()
    db_session.add(
        AsyncJob(
            id=job_id,
            kind="dataset_import",
            user_id=user.id,
            status="completed",
            payload={},
            result={},
        )
    )
    row = SimpleNamespace(
        target_type="async_job", target_id=job_id, payload={}, user_id=user.id
    )

    assert await NotificationService(db_session)._resolve_delivery_scopes([row]) == [
        (False, None, False)
    ]
    await db_session.rollback()

    assert (
        await db_session.scalar(select(AsyncJob.id).where(AsyncJob.id == job_id))
        is None
    )


@pytest.mark.asyncio
async def test_inbox_fails_closed_for_missing_and_unknown_job_kinds(
    db_session, annotator
):
    user, _ = annotator
    global_job = AsyncJob(
        id=uuid.uuid4(),
        kind="dataset_import",
        user_id=user.id,
        status="completed",
        payload={},
        result={},
    )
    unknown_job = AsyncJob(
        id=uuid.uuid4(),
        kind="future_unrecognized_job_kind",
        user_id=user.id,
        status="completed",
        payload={},
        result={},
    )
    missing_job_id = uuid.uuid4()
    notifications = [
        Notification(
            user_id=user.id,
            type="job.completed",
            target_type="async_job",
            target_id=job_id,
            payload={"project_id": str(uuid.uuid4())},
        )
        for job_id in (global_job.id, unknown_job.id, missing_job_id)
    ]
    db_session.add_all([global_job, unknown_job, *notifications])
    await db_session.flush()

    service = NotificationService(db_session)
    page, total, unread = await service.list_for_user(user.id)

    assert total == unread == 1
    assert [row.target_id for row in page] == [global_job.id]
    assert await service.notification_deliverable(notifications[0].id, user.id)
    assert not await service.notification_deliverable(notifications[1].id, user.id)
    assert not await service.notification_deliverable(notifications[2].id, user.id)


@pytest.mark.asyncio
async def test_delivery_rechecks_current_project_membership(
    db_session, super_admin, annotator
):
    owner, _ = super_admin
    user, _ = annotator
    project = await create_project(
        db_session, owner_id=owner.id, name="Notification scope membership"
    )
    membership = await create_membership(
        db_session,
        project_id=project.id,
        user_id=user.id,
        role="reviewer",
        assigned_by=owner.id,
    )
    job = AsyncJob(
        id=uuid.uuid4(),
        kind="batch_predict",
        project_id=project.id,
        user_id=user.id,
        status="completed",
        payload={},
        result={},
    )
    notice = Notification(
        user_id=user.id,
        type="job.completed",
        target_type="async_job",
        target_id=job.id,
        payload={},
    )
    db_session.add_all([job, notice])
    await db_session.flush()

    service = NotificationService(db_session)
    assert await service.notification_deliverable(notice.id, user.id)

    await db_session.execute(
        delete(ProjectMember).where(ProjectMember.id == membership.id)
    )
    assert not await service.notification_deliverable(notice.id, user.id)
