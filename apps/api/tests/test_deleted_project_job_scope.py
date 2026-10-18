"""Deleted and unresolved project scopes stay closed for jobs and notices."""

from __future__ import annotations

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from app.application.async_job_contracts import AsyncJobOperationFailure
from app.application.async_job_read import SqlAsyncJobReadAdapter
from app.db.models.async_job import AsyncJob
from app.db.models.project import Project
from app.db.models.user import User
from app.services.notification import NotificationService
from tests.factory import create_membership, create_project

pytestmark = pytest.mark.asyncio


async def _assert_update_rejected(db_session, statement):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(statement)


async def test_deleted_and_unresolved_project_job_scopes_stay_restricted(
    db_session, super_admin, annotator
):
    owner, _ = super_admin
    actor, _ = annotator
    deleted_project = await create_project(
        db_session, owner_id=owner.id, name="Deleted job scope"
    )
    surviving_project = await create_project(
        db_session, owner_id=owner.id, name="Surviving job scope"
    )
    owner_id = owner.id
    actor_id = actor.id
    deleted_project_id = deleted_project.id
    surviving_project_id = surviving_project.id
    for project in (deleted_project, surviving_project):
        await create_membership(
            db_session,
            project_id=project.id,
            user_id=actor_id,
            role="reviewer",
            assigned_by=owner_id,
        )

    deleted_job = AsyncJob(
        kind="batch_predict",
        project_id=deleted_project_id,
        user_id=actor_id,
        status="completed",
        payload={"project_id": str(surviving_project_id)},
        result={"private": "deleted-project result"},
    )
    detached_job = AsyncJob(
        kind="batch_predict",
        project_id=deleted_project_id,
        user_id=actor_id,
        status="completed",
        payload={},
        result={},
    )
    surviving_job = AsyncJob(
        kind="batch_predict",
        project_id=surviving_project_id,
        user_id=actor_id,
        status="completed",
        payload={"project_id": str(deleted_project_id)},
        result={},
    )
    global_job = AsyncJob(
        kind="dataset_import",
        user_id=actor_id,
        status="completed",
        payload={"project_id": str(deleted_project_id)},
        result={},
    )
    # Represents a pre-0200 required-kind job with no authoritative scope.
    orphan_job = AsyncJob(
        kind="create_tasks",
        user_id=actor_id,
        status="failed",
        payload={"project_id": str(surviving_project_id)},
        result={"error": "legacy orphan"},
    )
    db_session.add_all(
        [deleted_job, detached_job, surviving_job, global_job, orphan_job]
    )
    await db_session.flush()
    deleted_job_id = deleted_job.id
    detached_job_id = detached_job.id
    surviving_job_id = surviving_job.id
    global_job_id = global_job.id
    orphan_job_id = orphan_job.id

    notifications = NotificationService(db_session)
    notice_ids = {}
    invalid_export_ids = {}
    for job in (deleted_job, detached_job, surviving_job, global_job, orphan_job):
        for recipient_id in (actor_id, owner_id):
            notice = await notifications.notify(
                user_id=recipient_id,
                type="async_job.completed",
                target_type="async_job",
                target_id=job.id,
                payload={"project_id": str(surviving_project_id)},
            )
            assert notice is not None
            notice_ids.setdefault(recipient_id, {})[job.id] = notice.id
    for recipient_id in (actor_id, owner_id):
        notice = await notifications.notify(
            user_id=recipient_id,
            type="export.completed",
            target_type="export",
            target_id=global_job_id,
            payload={"project_id": str(surviving_project_id)},
        )
        assert notice is not None
        invalid_export_ids[recipient_id] = notice.id

    # Retained scope is database-owned: callers cannot forge or replace it.
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                AsyncJob(
                    kind="batch_predict",
                    retained_project_id=surviving_project_id,
                    user_id=actor_id,
                    status="completed",
                    payload={},
                    result={},
                )
            )
            await db_session.flush()
    await _assert_update_rejected(
        db_session,
        update(AsyncJob)
        .where(AsyncJob.id == global_job_id)
        .values(retained_project_id=deleted_project_id),
    )
    await db_session.refresh(global_job)
    assert global_job.retained_project_id is None

    # A live scope cannot move to another project or be assigned to a global job.
    await _assert_update_rejected(
        db_session,
        update(AsyncJob)
        .where(AsyncJob.id == deleted_job_id)
        .values(project_id=surviving_project_id),
    )
    await _assert_update_rejected(
        db_session,
        update(AsyncJob)
        .where(AsyncJob.id == global_job_id)
        .values(project_id=deleted_project_id),
    )

    # A direct SET NULL preserves the still-live project's current scope.
    await db_session.execute(
        update(AsyncJob).where(AsyncJob.id == detached_job_id).values(project_id=None)
    )
    await db_session.refresh(detached_job)
    assert detached_job.retained_project_id == deleted_project_id
    await _assert_update_rejected(
        db_session,
        update(AsyncJob)
        .where(AsyncJob.id == detached_job_id)
        .values(project_id=deleted_project_id),
    )
    await db_session.refresh(detached_job)
    assert detached_job.project_id is None
    assert detached_job.retained_project_id == deleted_project_id

    actor_read = SqlAsyncJobReadAdapter(db_session)
    admin_read = SqlAsyncJobReadAdapter(db_session)
    assert (
        await actor_read.get_job(job_id=detached_job_id, actor_id=actor_id)
    ).project_id == deleted_project_id
    pre_delete_page = await actor_read.list_jobs(
        actor_id=actor_id,
        status=None,
        kind=None,
        project_id=None,
        search=None,
        limit=50,
        offset=0,
    )
    assert pre_delete_page.total == len(pre_delete_page.items) == 4

    # The FK's ON DELETE SET NULL also runs the retention trigger.
    await db_session.execute(delete(Project).where(Project.id == deleted_project_id))
    db_session.expire_all()
    await db_session.refresh(deleted_job)
    await db_session.refresh(detached_job)
    await db_session.refresh(orphan_job)
    assert deleted_job.project_id is None
    assert deleted_job.retained_project_id == deleted_project_id
    assert detached_job.project_id is None
    assert detached_job.retained_project_id == deleted_project_id
    assert orphan_job.retained_project_id is None

    for read, user_id in ((actor_read, actor_id), (admin_read, owner_id)):
        page = await read.list_jobs(
            actor_id=user_id,
            status=None,
            kind=None,
            project_id=None,
            search=None,
            limit=50,
            offset=0,
        )
        assert page.total == len(page.items) == 2
        assert {job.id for job in page.items} == {surviving_job_id, global_job_id}
        for hidden_job_id in (deleted_job_id, detached_job_id, orphan_job_id):
            with pytest.raises(AsyncJobOperationFailure) as denied:
                await read.get_job(job_id=hidden_job_id, actor_id=user_id)
            assert denied.value.status_code == 403
        assert (
            await read.get_job(job_id=global_job_id, actor_id=user_id)
        ).project_id is None

    for recipient_id in (actor_id, owner_id):
        rows, total, _ = await notifications.list_for_user(recipient_id)
        assert total == 2
        assert {row.target_id for row in rows} == {surviving_job_id, global_job_id}
        assert not await notifications.notification_deliverable(
            invalid_export_ids[recipient_id], recipient_id
        )
        for hidden_job_id in (deleted_job_id, detached_job_id, orphan_job_id):
            assert not await notifications.notification_deliverable(
                notice_ids[recipient_id][hidden_job_id], recipient_id
            )
        for visible_job_id in (surviving_job_id, global_job_id):
            assert await notifications.notification_deliverable(
                notice_ids[recipient_id][visible_job_id], recipient_id
            )

    await db_session.execute(
        update(User).where(User.id == actor_id).values(is_active=False)
    )
    page = await actor_read.list_jobs(
        actor_id=actor_id,
        status=None,
        kind=None,
        project_id=None,
        search=None,
        limit=50,
        offset=0,
    )
    assert page.items == []
    assert page.total == 0
    with pytest.raises(AsyncJobOperationFailure) as disabled:
        await actor_read.get_job(job_id=global_job_id, actor_id=actor_id)
    assert disabled.value.status_code == 403
