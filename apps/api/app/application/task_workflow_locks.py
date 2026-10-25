from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.task_workflow_errors import TaskWorkflowError
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services import task_access
from app.services.labeling_core_read_relations import (
    execute_core_read,
    lock_batch_rows_for_core_read,
    lock_task_for_core_read,
    task_row,
)
from aap_workspace_contract.access import ProjectAccess
from app.services.project_access import lock_current_project_access
from app.services.identity_contracts import IdentityActorFacts

_LOCK_FAILURE_SQLSTATES = {"55P03", "40001", "40P01"}


def _identity_actor_facts(user: IdentityActorFacts) -> IdentityActorFacts:
    """Keep the Identity value obtained under the current account lock."""
    return user


async def lock_row_nowait(db: AsyncSession, statement: Any, *, reason: str):
    """Execute one bounded row-lock query and map lock contention consistently."""
    try:
        with db.no_autoflush:
            result = await db.execute(statement)
    except DBAPIError as exc:
        orig = exc.orig
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        if sqlstate in _LOCK_FAILURE_SQLSTATES:
            raise TaskWorkflowError(409, {"reason": reason}) from exc
        raise
    return result.scalar_one_or_none()


async def lock_rows_nowait(db: AsyncSession, statement: Any, *, reason: str):
    """Execute a bounded multi-row lock query with the same public error mapping."""
    try:
        with db.no_autoflush:
            result = await db.execute(statement)
    except DBAPIError as exc:
        orig = exc.orig
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        if sqlstate in _LOCK_FAILURE_SQLSTATES:
            raise TaskWorkflowError(409, {"reason": reason}) from exc
        raise
    return result.scalars().all()


async def lock_core_nowait(db: AsyncSession, operation, *, reason: str):
    """Run a fixed Labeling lock capability without flushing pending writes."""
    try:
        with db.no_autoflush:
            return await operation()
    except DBAPIError as exc:
        orig = exc.orig
        sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        if sqlstate in _LOCK_FAILURE_SQLSTATES:
            raise TaskWorkflowError(409, {"reason": reason}) from exc
        raise


async def lock_current_batch_target(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    batch_id: uuid.UUID,
    expected_access: ProjectAccess,
) -> tuple[IdentityActorFacts, Project, ProjectAccess, TaskBatch]:
    """Lock current project authority, then the matching batch row."""
    actor, project, current_access = await lock_current_project_access(
        db, actor_id=actor_id, project_id=project_id
    )
    task_access.assert_access_binding(
        expected_access, actor_id=actor.id, project_id=project_id
    )
    batches = await lock_core_nowait(
        db,
        lambda: lock_batch_rows_for_core_read(
            db,
            [batch_id],
            project_id=project_id,
            lock_mode="update",
            nowait=True,
            populate_existing=True,
        ),
        reason="batch_busy",
    )
    batch = batches[0] if batches else None
    if batch is None:
        raise TaskWorkflowError(404, "Batch not found")
    return _identity_actor_facts(actor), project, current_access, batch


async def lock_current_task_target(
    db: AsyncSession,
    *,
    task_id: uuid.UUID,
    actor_id: uuid.UUID,
    expected_access: ProjectAccess,
) -> tuple[Task, IdentityActorFacts, Project, ProjectAccess]:
    """Revalidate authority, then lock a task's captured batch and fresh row."""
    with db.no_autoflush:
        target = (
            await execute_core_read(
                db,
                select(task_row.project_id, task_row.batch_id).where(
                    task_row.id == task_id
                ),
            )
        ).one_or_none()
    if target is None:
        raise TaskWorkflowError(404, "Task not found")
    project_id, target_batch_id = target

    actor, project, current_access = await lock_current_project_access(
        db, actor_id=actor_id, project_id=project_id
    )
    task_access.assert_access_binding(
        expected_access, actor_id=actor.id, project_id=project_id
    )
    if target_batch_id is not None:
        batches = await lock_core_nowait(
            db,
            lambda: lock_batch_rows_for_core_read(
                db,
                [target_batch_id],
                project_id=project_id,
                lock_mode="update",
                nowait=True,
                populate_existing=True,
            ),
            reason="batch_busy",
        )
        batch = batches[0] if batches else None
        if batch is None:
            raise TaskWorkflowError(409, {"reason": "task_target_changed"})

    task = await lock_core_nowait(
        db,
        lambda: lock_task_for_core_read(
            db,
            task_id,
            project_id=project_id,
            lock_mode="update",
            nowait=True,
            populate_existing=True,
        ),
        reason="task_busy",
    )
    if task is None:
        raise TaskWorkflowError(404, "Task not found")
    if task.batch_id != target_batch_id:
        raise TaskWorkflowError(409, {"reason": "task_target_changed"})
    return task, _identity_actor_facts(actor), project, current_access
