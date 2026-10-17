"""Distinct task approvals retain both atomic project counter deltas."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.application.task_review import TaskReviewWorkflow
from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.audit_origin_contract import AuditOriginContext
from app.services.labeling_tasks import LabelingTaskService
from app.services.project_access import resolve_project_access
from tests.factory import create_membership, create_project, create_task, create_user


async def test_concurrent_task_approvals_keep_both_project_deltas(
    test_engine, monkeypatch
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as setup:
        owner = await create_user(
            setup, "project_admin", f"owner-{uuid.uuid4()}@counter.test", "Owner"
        )
        author = await create_user(
            setup, "employee", f"author-{uuid.uuid4()}@counter.test", "Author"
        )
        reviewer = await create_user(
            setup, "employee", f"reviewer-{uuid.uuid4()}@counter.test", "Reviewer"
        )
        project = await create_project(setup, owner_id=owner.id)
        await create_membership(
            setup, project_id=project.id, user_id=reviewer.id, role="reviewer"
        )
        project.review_tasks, project.completed_tasks = 2, 0
        task_ids = []
        for _ in range(2):
            task = await create_task(setup, project_id=project.id, status="review")
            task.assignee_id, task.reviewer_id = author.id, reviewer.id
            task.reviewer_claimed_at = datetime.now(timezone.utc)
            task.review_round_id = uuid.uuid4()
            task.annotation_contributor_ids = [str(author.id)]
            task.review_contributor_ids = [str(author.id)]
            task.review_submitter_id = author.id
            task_ids.append(task.id)
        await setup.flush()
        project_id, reviewer_id = project.id, reviewer.id
        user_ids = [owner.id, author.id, reviewer.id]
        await setup.commit()
    arrived = 0
    both_ready = asyncio.Event()
    apply_delta = LabelingTaskService.record_project_review_approval

    async def concurrent_delta(service, current_project):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_ready.set()
        await asyncio.wait_for(both_ready.wait(), 5)
        await apply_delta(service, current_project)

    monkeypatch.setattr(
        LabelingTaskService, "record_project_review_approval", concurrent_delta
    )

    async def approve(task_id):
        async with maker() as db:
            actor = await db.get(User, reviewer_id)
            current_project = await db.get(Project, project_id)
            access = await resolve_project_access(
                db, user=actor, project=current_project
            )
            return await TaskReviewWorkflow(db).approve_task(
                task_id,
                actor=actor,
                access=access,
                origin=AuditOriginContext(None, None, None),
                expected_qc_digest=None,
                warning_issue_ids=[],
                note=None,
            )

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(approve(task_id) for task_id in task_ids)), 15
        )
        assert [result["status"] for result in results] == ["approved", "approved"]
        async with maker() as check:
            project = await check.get(Project, project_id)
            assert (project.completed_tasks, project.review_tasks) == (2, 0)
            tasks = (
                await check.scalars(select(Task).where(Task.id.in_(task_ids)))
            ).all()
            assert all(
                task.status == "completed" and task.first_review_result == "approved"
                for task in tasks
            )
            audits = (
                await check.scalars(
                    select(AuditLog.target_id).where(
                        AuditLog.action == "task.approve",
                        AuditLog.target_id.in_([str(task_id) for task_id in task_ids]),
                    )
                )
            ).all()
            assert set(audits) == {str(task_id) for task_id in task_ids}
    finally:
        async with maker() as cleanup:
            await cleanup.execute(text("SET LOCAL \"app.allow_audit_update\" = 'true'"))
            await cleanup.execute(
                delete(AuditLog).where(AuditLog.actor_id.in_(user_ids))
            )
            await cleanup.execute(delete(Task).where(Task.id.in_(task_ids)))
            await cleanup.execute(delete(Project).where(Project.id == project_id))
            await cleanup.execute(delete(User).where(User.id.in_(user_ids)))
            await cleanup.commit()
