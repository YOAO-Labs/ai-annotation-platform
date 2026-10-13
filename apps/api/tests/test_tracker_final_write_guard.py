"""Direct runner final-write authority regressions for the tracker (B3).

No HTTP: the guard lives inside the runner after the fresh Task lock, so these
tests exercise the actual final-write path with a stale/current revocation and
an explicit actor requirement.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.user import User
from app.services.video_tracking.runner import (
    TrackerJobStateConflict,
    _assert_tracker_actor_authority,
    _lock_task_bounded,
    accept_tracker_job,
    decide_tracker_job,
)
from tests.factory import create_membership, create_project, create_task, create_user
from tests.test_video_tracker_jobs_list import _make_job, _make_video_task


async def _add_member(
    db: AsyncSession, *, project_id, user_id, role, assigned_by
) -> None:
    await create_membership(
        db,
        project_id=project_id,
        user_id=user_id,
        role=role,
        assigned_by=assigned_by,
    )


async def test_runner_guard_requires_explicit_actor(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id, name="Guard Actor")
    task = await create_task(db_session, project_id=project.id, status="pending")
    await db_session.flush()

    with pytest.raises(TrackerJobStateConflict) as exc:
        await _assert_tracker_actor_authority(db_session, task, None)
    assert exc.value.detail["reason"] == "permission_changed"


async def test_runner_guard_denies_revoked_employee_actor(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id, name="Guard Revoke")
    employee = await create_user(
        db_session, "employee", f"guard-{uuid.uuid4()}@test.local", "Guard"
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user_id=employee.id,
        role="annotator",
        assigned_by=owner.id,
    )
    task = await create_task(db_session, project_id=project.id, status="pending")
    task.assignee_id = employee.id
    await db_session.commit()

    # Current membership + effective assignee: allowed.
    await _assert_tracker_actor_authority(db_session, task, employee.id)

    await db_session.execute(
        delete(ProjectMember).where(
            ProjectMember.project_id == project.id,
            ProjectMember.user_id == employee.id,
        )
    )
    await db_session.commit()
    await db_session.refresh(employee)

    with pytest.raises(TrackerJobStateConflict) as exc:
        await _assert_tracker_actor_authority(db_session, task, employee.id)
    assert exc.value.detail["reason"] == "permission_changed"


async def test_accept_runner_authorizes_before_replay_exit(
    db_session: AsyncSession, super_admin
):
    """An idempotent ACCEPTED replay must not bypass current authority."""

    owner, _ = super_admin
    task, item = await _make_video_task(db_session, owner.id)
    employee = await create_user(
        db_session, "employee", f"replay-{uuid.uuid4()}@test.local", "Replay"
    )
    job = await _make_job(db_session, task, item, employee.id, status="accepted")
    await db_session.commit()

    with pytest.raises(TrackerJobStateConflict) as exc:
        await accept_tracker_job(db_session, job.id, actor_id=employee.id)
    assert exc.value.detail["reason"] == "permission_changed"

    with pytest.raises(TrackerJobStateConflict) as exc:
        await accept_tracker_job(db_session, job.id, actor_id=None)
    assert exc.value.detail["reason"] == "permission_changed"


async def test_accept_runner_denies_inactive_actor(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    task, item = await _make_video_task(db_session, owner.id)
    employee = await create_user(
        db_session, "employee", f"inactive-{uuid.uuid4()}@test.local", "Inactive"
    )
    await _add_member(
        db_session,
        project_id=task.project_id,
        user_id=employee.id,
        role="annotator",
        assigned_by=owner.id,
    )
    employee.is_active = False
    job = await _make_job(db_session, task, item, employee.id, status="pending_review")
    await db_session.commit()

    with pytest.raises(TrackerJobStateConflict) as exc:
        await accept_tracker_job(db_session, job.id, actor_id=employee.id)
    assert exc.value.detail["reason"] == "permission_changed"


async def test_decide_runner_authorizes_before_selector_checks(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    task, item = await _make_video_task(db_session, owner.id)
    employee = await create_user(
        db_session, "employee", f"decide-{uuid.uuid4()}@test.local", "Decide"
    )
    job = await _make_job(db_session, task, item, employee.id, status="pending_review")
    await db_session.commit()

    with pytest.raises(TrackerJobStateConflict) as exc:
        await decide_tracker_job(
            db_session,
            job.id,
            instance_ids=["missing"],
            from_frame=0,
            to_frame=0,
            decision="reject",
            expected_source_versions={},
            job_revision=1,
            actor_id=employee.id,
        )
    assert exc.value.detail["reason"] == "permission_changed"


async def test_tracker_task_lock_is_bounded_nowait(test_engine):
    """An independently committed Task lock yields a retryable conflict.

    The dataset is committed in its own session so two independent connections
    (the lock holder and the guard) actually observe the same row.
    """

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    ids: dict = {}
    async with maker() as setup:
        owner = await create_user(
            setup,
            "super_admin",
            f"busy-owner-{uuid.uuid4()}@test.local",
            "Owner",
        )
        project = await create_project(setup, owner_id=owner.id, name="Guard Busy")
        task = await create_task(setup, project_id=project.id, status="pending")
        task.assignee_id = owner.id
        await setup.commit()
        ids = {"project": project.id, "task": task.id, "owner": owner.id}

    try:
        async with maker() as writer:
            locked = await writer.scalar(
                select(Task.id).where(Task.id == ids["task"]).with_for_update()
            )
            assert locked == ids["task"]
            async with maker() as guard:
                with pytest.raises(TrackerJobStateConflict) as exc:
                    await _lock_task_bounded(guard, ids["task"])
                assert exc.value.detail["reason"] == "task_locked"
                await guard.rollback()
            await writer.rollback()
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(Task).where(Task.id == ids["task"]))
            await cleanup.execute(delete(Project).where(Project.id == ids["project"]))
            await cleanup.execute(delete(User).where(User.id == ids["owner"]))
            await cleanup.commit()


async def test_runner_guard_denies_self_review_evidence(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    project = await create_project(db_session, owner_id=owner.id, name="Guard Self")
    employee = await create_user(
        db_session, "employee", f"guard-self-{uuid.uuid4()}@test.local", "Guard Self"
    )
    await _add_member(
        db_session,
        project_id=project.id,
        user_id=employee.id,
        role="reviewer",
        assigned_by=owner.id,
    )
    task = await create_task(db_session, project_id=project.id, status="review")
    task.annotation_contributor_ids = [str(employee.id)]
    task.review_round_id = uuid.uuid4()
    task.review_contributor_ids = [str(employee.id)]
    task.review_submitter_id = str(employee.id)
    await db_session.commit()

    with pytest.raises(TrackerJobStateConflict) as exc:
        await _assert_tracker_actor_authority(db_session, task, employee.id)
    assert exc.value.detail["reason"] == "self_review_denied"
