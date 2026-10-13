"""SQL/object agreement for viewer task visibility (B1-A).

The scheduler's SQL predicate and the object guards must agree: a viewer keeps
task-level assignee-override filtering but must not enter the annotator-only
unbatched-assignment or reviewing-rework arms.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tasks._shared import _assert_task_visible, _visible_task_ids
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services.scheduler import task_visibility_clause
from tests.factory import (
    create_membership,
    create_batch,
    create_project,
    create_task,
    create_user,
)

pytestmark = pytest.mark.asyncio


async def _sql_visible(
    db: AsyncSession, *, project_id, viewer: User, task_id: uuid.UUID
) -> bool:
    stmt = (
        select(Task.id)
        .outerjoin(TaskBatch, TaskBatch.id == Task.batch_id)
        .where(
            Task.project_id == project_id,
            Task.id == task_id,
            task_visibility_clause(viewer, project_role="viewer"),
        )
    )
    return (await db.scalar(stmt)) is not None


async def _object_visible(db: AsyncSession, task: Task, viewer: User) -> bool:
    try:
        await _assert_task_visible(db, task, viewer)
        return True
    except HTTPException:
        return False


async def _viewer_world(db: AsyncSession, owner: User):
    project = await create_project(db, owner_id=owner.id, name="Viewer Scope")
    viewer = await create_user(
        db, "viewer", f"scope-viewer-{uuid.uuid4()}@test.local", "Viewer"
    )
    foreign = await create_user(
        db, "employee", f"scope-foreign-{uuid.uuid4()}@test.local", "Foreign"
    )
    await create_membership(
        db,
        project_id=project.id,
        user_id=viewer.id,
        role="viewer",
        assigned_by=owner.id,
    )
    await db.flush()
    return project, viewer, foreign


async def test_viewer_unbatched_assignment_is_denied_in_sql_and_object(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    project, viewer, _foreign = await _viewer_world(db_session, owner)
    task = await create_task(db_session, project_id=project.id, status="pending")
    task.assignee_id = viewer.id
    await db_session.flush()

    assert (
        await _sql_visible(
            db_session, project_id=project.id, viewer=viewer, task_id=task.id
        )
        is False
    )
    assert await _object_visible(db_session, task, viewer) is False
    assert await _visible_task_ids(db_session, project, viewer, [task.id]) == set()


async def test_viewer_reviewing_rework_is_denied_in_sql_and_object(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    project, viewer, _foreign = await _viewer_world(db_session, owner)
    batch = await create_batch(db_session, project_id=project.id, status="reviewing")
    batch.annotator_id = viewer.id
    task = await create_task(db_session, project_id=project.id, status="rejected")
    task.batch_id = batch.id
    await db_session.flush()

    assert (
        await _sql_visible(
            db_session, project_id=project.id, viewer=viewer, task_id=task.id
        )
        is False
    )
    assert await _object_visible(db_session, task, viewer) is False


async def test_viewer_open_batch_task_is_allowed_in_sql_and_object(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    project, viewer, _foreign = await _viewer_world(db_session, owner)
    batch = await create_batch(db_session, project_id=project.id, status="active")
    task = await create_task(db_session, project_id=project.id, status="pending")
    task.batch_id = batch.id
    await db_session.flush()

    assert (
        await _sql_visible(
            db_session, project_id=project.id, viewer=viewer, task_id=task.id
        )
        is True
    )
    assert await _object_visible(db_session, task, viewer) is True


async def test_viewer_own_batch_foreign_explicit_assignee_is_denied(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    project, viewer, foreign = await _viewer_world(db_session, owner)
    batch = await create_batch(db_session, project_id=project.id, status="active")
    batch.annotator_id = viewer.id
    task = await create_task(db_session, project_id=project.id, status="pending")
    task.batch_id = batch.id
    task.assignee_id = foreign.id
    await db_session.flush()

    # Task-level override filtering must agree: SQL and object both deny.
    assert (
        await _sql_visible(
            db_session, project_id=project.id, viewer=viewer, task_id=task.id
        )
        is False
    )
    assert await _object_visible(db_session, task, viewer) is False
