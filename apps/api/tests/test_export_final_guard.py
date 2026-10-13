"""Real-DB final export guard regressions (B3).

Exercises the post-build/cache final boundary against real committed rows:
a revoked reviewer membership and an invisible selected-task scope must refuse
before any signed URL is minted.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.async_job import AsyncJob
from app.db.models.project_member import ProjectMember
from app.workers.export import _reauthorize_export_final_write
from tests.factory import create_membership, create_project, create_task, create_user


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
        payload=payload,
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
    job.payload = {"scope": {"task_ids": [str(task.id)]}}
    await db_session.commit()
    await db_session.refresh(job)

    # The unassigned/unbatched task is not in the reviewer's visible scope.
    with pytest.raises(ValueError):
        await _reauthorize_export_final_write(db_session, job.id, [task.id])
