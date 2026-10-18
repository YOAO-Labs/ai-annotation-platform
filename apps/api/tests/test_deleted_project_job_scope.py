"""Deleted project scope stays attached to its asynchronous jobs and notices."""

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


async def test_deleted_project_job_scope_stays_restricted(
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
    actor_id = actor.id
    deleted_project_id = deleted_project.id
    surviving_project_id = surviving_project.id
    for project in (deleted_project, surviving_project):
        await create_membership(
            db_session,
            project_id=project.id,
            user_id=actor.id,
            role="reviewer",
            assigned_by=owner.id,
        )

    deleted_job = AsyncJob(
        kind="batch_predict",
        project_id=deleted_project.id,
        user_id=actor.id,
        status="completed",
        payload={"project_id": str(surviving_project_id)},
        result={"private": "deleted-project result"},
    )
    detached_job = AsyncJob(
        kind="batch_predict",
        project_id=surviving_project.id,
        user_id=actor.id,
        status="completed",
        payload={"project_id": str(deleted_project_id)},
        result={},
    )
    global_job = AsyncJob(
        kind="batch_predict",
        user_id=actor.id,
        status="completed",
        payload={"project_id": str(deleted_project_id)},
        result={},
    )
    db_session.add_all([deleted_job, detached_job, global_job])
    await db_session.flush()

    notifications = NotificationService(db_session)
    for job in (deleted_job, detached_job, global_job):
        await notifications.notify(
            user_id=actor_id,
            type="async_job.completed",
            target_type="async_job",
            target_id=job.id,
            payload={"project_id": str(surviving_project.id)},
        )

    # A direct FK-style SET NULL update and a project DELETE both preserve scope.
    await db_session.execute(
        update(AsyncJob).where(AsyncJob.id == detached_job.id).values(project_id=None)
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(AsyncJob)
                .where(AsyncJob.id == detached_job.id)
                .values(project_id=deleted_project_id)
            )
    await db_session.execute(delete(Project).where(Project.id == deleted_project_id))
    db_session.expire_all()
    await db_session.refresh(deleted_job)
    await db_session.refresh(detached_job)
    assert deleted_job.project_id is None
    assert deleted_job.retained_project_id == deleted_project_id
    assert detached_job.project_id is None
    assert detached_job.retained_project_id == surviving_project_id

    read = SqlAsyncJobReadAdapter(db_session)
    page = await read.list_jobs(
        actor_id=actor_id,
        status=None,
        kind=None,
        project_id=None,
        search=None,
        limit=50,
        offset=0,
    )
    assert {job.id for job in page.items} == {detached_job.id, global_job.id}

    with pytest.raises(AsyncJobOperationFailure) as denied:
        await read.get_job(job_id=deleted_job.id, actor_id=actor_id)
    assert denied.value.status_code == 403
    assert (
        await read.get_job(job_id=detached_job.id, actor_id=actor_id)
    ).project_id == surviving_project_id
    assert (
        await read.get_job(job_id=global_job.id, actor_id=actor_id)
    ).project_id is None

    rows, total, _ = await notifications.list_for_user(actor_id)
    assert total == 2
    assert {row.target_id for row in rows} == {detached_job.id, global_job.id}

    await db_session.execute(
        update(User).where(User.id == actor_id).values(is_active=False)
    )
    page = await read.list_jobs(
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
        await read.get_job(job_id=global_job.id, actor_id=actor_id)
    assert disabled.value.status_code == 403
