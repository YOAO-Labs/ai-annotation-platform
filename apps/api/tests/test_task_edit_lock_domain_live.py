"""Live PostgreSQL checks for the task edit-lock advisory-domain cutover.

Run against a disposable migrated test database with pytest.

The holder connections take only advisory keys, so they stay independent of
the uncommitted fixture rows in ``db_session``.  The fixture session calls the
installed owner functions on its own connection.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.factory import create_project, create_task, create_user
from yoao_labeling.task_locks_contract import (
    TASK_LOCK_SCOPE_PREFIX,
    task_locks_command,
)

pytestmark = pytest.mark.asyncio

LEGACY_PREFIX = "aap:task-edit-lock:"


def _task_lock_request(task_id: uuid.UUID) -> str:
    return task_locks_command("try_final_scope", task_id=task_id).model_dump_json(
        by_alias=True
    )


async def _take_key(session, prefix: str, task_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"{prefix}{task_id}"},
    )


@pytest.fixture
async def world(db_session):
    user = await create_user(
        db_session,
        "employee",
        f"task-lock-domain-{uuid.uuid4().hex}@example.test",
        "Task lock domain",
    )
    project = await create_project(db_session, owner_id=user.id)
    task = await create_task(db_session, project_id=project.id)
    task.assignee_id = user.id
    task.status = "pending"
    await db_session.flush()
    return user, project, task


@pytest.fixture
async def holder(test_engine):
    session = async_sessionmaker(test_engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()


async def test_current_domain_excludes_python_and_all_sql_consumers(
    db_session, holder, world
):
    user, project, task = world
    await _take_key(holder, TASK_LOCK_SCOPE_PREFIX, task.id)

    from app.services.task_lock import TaskLockConflictError, TaskLockService

    with pytest.raises(TaskLockConflictError):
        await TaskLockService(db_session)._lock_task_scope(task.id, nowait=True)

    available = await db_session.scalar(
        text("SELECT yoao_labeling_v1.task_edit_lock_command(CAST(:request AS jsonb))"),
        {"request": _task_lock_request(task.id)},
    )
    assert available["data"] == {"available": False}

    membership = await db_session.scalar(
        text(
            "SELECT yoao_labeling_v1.lock_membership_work("
            ":project, :user, 'annotator', false)"
        ),
        {"project": project.id, "user": user.id},
    )
    assert membership["ok"] is False
    assert membership["error"]["reason"] == "resource_busy"

    offboarding = await db_session.scalar(
        text(
            "SELECT yoao_labeling_v1.lock_identity_offboarding_work("
            ":user, ARRAY[CAST(:project AS uuid)]::uuid[])"
        ),
        {"user": user.id, "project": project.id},
    )
    assert offboarding is False


async def test_legacy_domain_fails_closed_instead_of_dual_locking(
    db_session, holder, world
):
    user, project, task = world
    await _take_key(holder, LEGACY_PREFIX, task.id)

    from app.services.task_lock import TaskLockService

    with pytest.raises(DBAPIError) as error:
        async with db_session.begin_nested():
            await db_session.scalar(
                text(
                    "SELECT yoao_labeling_v1.task_edit_lock_command("
                    "CAST(:request AS jsonb))"
                ),
                {"request": _task_lock_request(task.id)},
            )
    assert error.value.orig.sqlstate == "55006"

    with pytest.raises(HTTPException) as error:
        async with db_session.begin_nested():
            await TaskLockService(db_session)._lock_task_scope(task.id, nowait=True)
    assert error.value.status_code == 503

    membership = await db_session.scalar(
        text(
            "SELECT yoao_labeling_v1.lock_membership_work("
            ":project, :user, 'annotator', false)"
        ),
        {"project": project.id, "user": user.id},
    )
    assert membership["ok"] is False
    assert membership["error"]["reason"] == "resource_busy"

    offboarding = await db_session.scalar(
        text(
            "SELECT yoao_labeling_v1.lock_identity_offboarding_work("
            ":user, ARRAY[CAST(:project AS uuid)]::uuid[])"
        ),
        {"user": user.id, "project": project.id},
    )
    assert offboarding is False


async def test_shared_read_scope_uses_the_current_domain(db_session, holder, world):
    _, _, task = world
    await _take_key(holder, TASK_LOCK_SCOPE_PREFIX, task.id)

    from app.services.task_lock import TaskLockConflictError, TaskLockService

    with pytest.raises(TaskLockConflictError):
        await TaskLockService(db_session)._lock_task_scope(
            task.id, nowait=True, shared=True
        )
