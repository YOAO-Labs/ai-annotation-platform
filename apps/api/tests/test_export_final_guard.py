"""Real-DB final export guard regressions (B3).

Exercises the post-build/cache final boundary against real committed rows:
a revoked reviewer membership and an invisible selected-task scope must refuse
before any signed URL is minted.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob
from app.db.models.project_member import ProjectMember
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.workers.export import _reauthorize_export_final_write
from app.services.exporting import output_authority
from tests.factory import (
    create_membership,
    create_project,
    create_task,
    create_user,
    create_batch,
)


async def _employee(db: AsyncSession) -> object:
    return await create_user(
        db, "employee", f"exp-guard-{uuid.uuid4()}@test.local", "Exp Guard"
    )


async def _reviewer_job(
    db: AsyncSession, *, owner, employee, payload: dict
) -> AsyncJob:
    project = await create_project(db, owner_id=owner.id, name="Export Guard")
    await create_membership(
        db,
        project_id=project.id,
        user_id=employee.id,
        role="reviewer",
        assigned_by=owner.id,
    )
    job = AsyncJob(
        kind="export",
        project_id=project.id,
        user_id=employee.id,
        status="completed",
        payload={
            **payload,
            "output_authority": await output_authority.capture(
                db, project, employee, SimpleNamespace(state=SimpleNamespace())
            ),
        },
        result={},
    )
    db.add(job)
    await db.flush()
    return job


async def test_export_final_guard_refuses_revoked_member(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    employee = await _employee(db_session)
    job = await _reviewer_job(db_session, owner=owner, employee=employee, payload={})
    await db_session.commit()

    # Reviewer membership + export capability: allowed.
    await _reauthorize_export_final_write(db_session, job.id, None)

    await db_session.execute(
        delete(ProjectMember).where(
            ProjectMember.project_id == job.project_id,
            ProjectMember.user_id == employee.id,
        )
    )
    await db_session.commit()
    await db_session.refresh(employee)

    with pytest.raises(ValueError):
        await _reauthorize_export_final_write(db_session, job.id, None)


async def test_export_final_guard_refuses_invisible_selected_task(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    employee = await _employee(db_session)
    job = await _reviewer_job(db_session, owner=owner, employee=employee, payload={})
    task = await create_task(db_session, project_id=job.project_id, status="pending")
    job.payload = {**job.payload, "scope": {"task_ids": [str(task.id)]}}
    await db_session.commit()
    await db_session.refresh(job)

    # The unassigned/unbatched task is not in the reviewer's visible scope.
    with pytest.raises(ValueError):
        await _reauthorize_export_final_write(db_session, job.id, [task.id])


async def test_final_guard_serializes_batch_archival_with_explicit_reviewer_export(
    test_engine,
):
    """Independent committed connections prove the publication lock boundary."""
    ids = {}
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        try:
            owner = await create_user(
                db, "super_admin", f"exp-owner-{uuid.uuid4()}@test.local", "Owner"
            )
            reviewer = await _employee(db)
            ids["users"] = [owner.id, reviewer.id]
            project = await create_project(db, owner_id=owner.id)
            ids["project"] = project.id
            await create_membership(
                db, project_id=project.id, user_id=reviewer.id, role="reviewer"
            )
            batch = await create_batch(db, project_id=project.id, status="active")
            ids["batch"] = batch.id
            task = await create_task(db, project_id=project.id)
            task.batch_id = batch.id
            ids["task"] = task.id
            job = AsyncJob(
                kind="export",
                project_id=project.id,
                user_id=reviewer.id,
                status="running",
                payload={
                    "task_ids": [str(task.id)],
                    "output_authority": await output_authority.capture(
                        db, project, reviewer, SimpleNamespace(state=SimpleNamespace())
                    ),
                },
                result={},
            )
            db.add(job)
            await db.flush()
            ids["job"] = job.id
            await db.commit()

            await _reauthorize_export_final_write(db, job.id, [task.id])
            async with AsyncSession(test_engine) as contender:
                await contender.execute(text("SET LOCAL lock_timeout = '100ms'"))
                with pytest.raises(DBAPIError) as blocked:
                    await contender.execute(
                        update(TaskBatch)
                        .where(TaskBatch.id == batch.id)
                        .values(status="archived")
                    )
                assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
                await contender.rollback()

            await db.rollback()
            async with AsyncSession(test_engine) as contender:
                await contender.execute(
                    update(TaskBatch)
                    .where(TaskBatch.id == ids["batch"])
                    .values(status="archived")
                )
                await contender.commit()
            with pytest.raises(ValueError, match="visible"):
                await _reauthorize_export_final_write(db, ids["job"], [ids["task"]])
        finally:
            await db.rollback()
            if "job" in ids:
                await db.execute(delete(AsyncJob).where(AsyncJob.id == ids["job"]))
            if "task" in ids:
                await db.execute(delete(Task).where(Task.id == ids["task"]))
            if "batch" in ids:
                await db.execute(delete(TaskBatch).where(TaskBatch.id == ids["batch"]))
            if "project" in ids:
                await db.execute(
                    delete(ProjectMember).where(
                        ProjectMember.project_id == ids["project"]
                    )
                )
                await db.execute(delete(Project).where(Project.id == ids["project"]))
            if "users" in ids:
                await db.execute(delete(User).where(User.id.in_(ids["users"])))
            await db.commit()


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "running"])
async def test_late_duplicate_progress_cannot_change_terminal_job(
    db_session, super_admin, status
):
    from app.db.models.async_job import AsyncJob
    from app.workers.export import _update_export_progress
    from app.services.exporting.reference_media import ExportCancelled

    actor, _ = super_admin
    job = AsyncJob(
        kind="export",
        user_id=actor.id,
        status=status,
        progress_pct=100,
        payload={},
        result={},
    )
    db_session.add(job)
    await db_session.flush()
    if status == "running":
        await _update_export_progress(db_session, job.id, 90)
        assert job.progress_pct == 90
    else:
        with pytest.raises(ExportCancelled if status == "cancelled" else ValueError):
            await _update_export_progress(db_session, job.id, 90)
        assert job.progress_pct == 100
