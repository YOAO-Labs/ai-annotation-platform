"""Current authority and waiting task fences through the existing Identity owner."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.async_job import AsyncJob, AsyncJobKind
from app.db.models.project import Project
from app.db.models.user import User
from app.services.async_job_access import _can_access_job
from app.services.project_access import resolve_project_access
from app.services.task_lock import assert_task_user_active
from tests.factory import create_project, create_user


async def test_null_task_actor_remains_inactive_without_uuid_validation_error(
    db_session,
):
    assert await assert_task_user_active(db_session, None) is False
    assert await assert_task_user_active(db_session, None, lock=False) is False


async def test_task_guard_waits_for_disable_and_retains_account_lock(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as setup:
        actor = await create_user(
            setup,
            "reviewer",
            f"task-fence-{uuid.uuid4()}@test.local",
            "Historical actor",
        )
        actor_id = actor.id
    waiter = None
    try:
        async with maker() as holder, maker() as caller, maker() as observer:
            await holder.execute(
                select(User.id).where(User.id == actor_id).with_for_update()
            )
            await holder.execute(
                update(User).where(User.id == actor_id).values(is_active=False)
            )
            pid = await caller.scalar(text("SELECT pg_backend_pid()"))
            waiter = asyncio.create_task(assert_task_user_active(caller, actor_id))
            observed = False
            for _ in range(200):
                state = await observer.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
                    {"pid": pid},
                )
                await observer.rollback()
                if state == "Lock":
                    observed = True
                    break
                await asyncio.sleep(0.01)
            assert observed and not waiter.done()
            await holder.commit()
            assert await asyncio.wait_for(waiter, timeout=5) is False
            with pytest.raises(DBAPIError) as held:
                await observer.execute(
                    select(User.id)
                    .where(User.id == actor_id)
                    .with_for_update(nowait=True)
                )
            assert getattr(held.value.orig, "sqlstate", None) == "55P03"
            await observer.rollback()
            await caller.commit()
            assert (
                await observer.scalar(
                    select(User.id)
                    .where(User.id == actor_id)
                    .with_for_update(nowait=True)
                )
                == actor_id
            )
            await observer.rollback()
    finally:
        if waiter is not None and not waiter.done():
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        async with maker.begin() as cleanup:
            await cleanup.execute(delete(User).where(User.id == actor_id))


async def test_project_access_refreshes_workspace_rows_after_identity_status(
    db_session,
):
    actor = await create_user(
        db_session, "project_admin", f"old-owner-{uuid.uuid4()}@test.local", "Old owner"
    )
    receiver = await create_user(
        db_session, "project_admin", f"new-owner-{uuid.uuid4()}@test.local", "New owner"
    )
    project = await create_project(db_session, owner_id=actor.id)
    assert (
        await resolve_project_access(db_session, actor_id=actor.id, project=project)
    ).is_owner
    await db_session.execute(
        update(Project)
        .where(Project.id == project.id)
        .values(owner_id=receiver.id, video_collaboration={"enabled": False})
        .execution_options(synchronize_session=False)
    )
    assert project.owner_id == actor.id
    with pytest.raises(HTTPException) as hidden:
        await resolve_project_access(db_session, actor_id=actor.id, project=project)
    assert (hidden.value.status_code, hidden.value.detail) == (404, "项目不存在")
    assert project.owner_id == receiver.id
    assert project.video_collaboration == {"enabled": False}


async def test_global_job_authority_reads_current_status_without_user_hydration(
    db_session,
):
    actor = await create_user(
        db_session, "super_admin", f"job-reader-{uuid.uuid4()}@test.local", "Reader"
    )
    owner = await create_user(
        db_session, "employee", f"job-owner-{uuid.uuid4()}@test.local", "Owner"
    )
    job = AsyncJob(
        kind=AsyncJobKind.AUDIT_ARCHIVE.value,
        user_id=owner.id,
        project_id=None,
        payload={},
        status="pending",
    )
    db_session.add(job)
    await db_session.flush()
    assert await _can_access_job(db_session, job=job, actor_id=actor.id)
    await db_session.execute(
        update(User)
        .where(User.id == actor.id)
        .values(role="viewer")
        .execution_options(synchronize_session=False)
    )
    assert actor.role == "super_admin"
    assert not await _can_access_job(db_session, job=job, actor_id=actor.id)
    assert actor.role == "super_admin"
    await db_session.execute(
        update(User)
        .where(User.id == owner.id)
        .values(is_active=False)
        .execution_options(synchronize_session=False)
    )
    assert not await _can_access_job(db_session, job=job, actor_id=owner.id)
