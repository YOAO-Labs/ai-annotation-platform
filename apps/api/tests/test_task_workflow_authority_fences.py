from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.task_lifecycle import TaskLifecycleWorkflow
from app.application.task_review import TaskReviewWorkflow
from app.db.models.audit_log import AuditLog
from app.db.models.notification import Notification
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.user import User
from app.services.audit_origin_contract import AuditOriginContext
from app.services.notification import NotificationService
from app.services.original_content_access import visible_task
from app.services.project_access import resolve_project_access
from tests.factory import (
    create_batch,
    create_membership,
    create_project,
    create_task,
    create_user,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    ("revocation", "expected_status"),
    [("membership", 404), ("account", 401), ("ownership", 404)],
)
async def test_stale_access_cannot_accept_rejection_after_authority_change(
    db_session: AsyncSession,
    super_admin,
    revocation: str,
    expected_status: int,
):
    platform_admin, _ = super_admin
    member = None
    if revocation == "ownership":
        actor = await create_user(
            db_session,
            "project_admin",
            f"wf-old-owner-{uuid.uuid4()}@test.local",
            "Old",
        )
        new_owner = await create_user(
            db_session,
            "project_admin",
            f"wf-new-owner-{uuid.uuid4()}@test.local",
            "New",
        )
        project = await create_project(db_session, owner_id=actor.id)
    else:
        actor = await create_user(
            db_session, "employee", f"wf-member-{uuid.uuid4()}@test.local", "Member"
        )
        project = await create_project(db_session, owner_id=platform_admin.id)
        member = await create_membership(
            db_session,
            project_id=project.id,
            user_id=actor.id,
            role="annotator",
            assigned_by=platform_admin.id,
        )

    task = await create_task(db_session, project_id=project.id, status="rejected")
    task.assignee_id = actor.id
    stale_access = await resolve_project_access(
        db_session, actor_id=actor.id, project=project
    )

    if revocation == "membership":
        assert member is not None
        await db_session.delete(member)
    elif revocation == "account":
        actor.is_active = False
    else:
        project.owner_id = new_owner.id
    await db_session.commit()

    with pytest.raises(HTTPException) as denied:
        await TaskLifecycleWorkflow(db_session).accept_rejection(
            task.id,
            actor_id=actor.id,
            access=stale_access,
            origin=AuditOriginContext(
                method="POST", path="/tasks/rejected/accept", ip=None
            ),
        )

    assert denied.value.status_code == expected_status
    await db_session.refresh(task)
    assert task.status == "rejected"


async def test_review_claim_rechecks_current_capability_after_role_change(
    db_session, super_admin
):
    owner, _ = super_admin
    actor = await create_user(
        db_session,
        "employee",
        f"demoted-reviewer-{uuid.uuid4()}@test.local",
        "Reviewer",
    )
    project = await create_project(db_session, owner_id=owner.id)
    member = await create_membership(
        db_session, project_id=project.id, user_id=actor.id, role="reviewer"
    )
    batch = await create_batch(db_session, project_id=project.id, status="active")
    task = await create_task(db_session, project_id=project.id, status="review")
    task.batch_id = batch.id
    task.reviewer_id = actor.id
    task.review_round_id = uuid.uuid4()
    task.review_contributor_ids = [str(owner.id)]
    task.review_submitter_id = owner.id
    stale_access = await resolve_project_access(
        db_session, actor_id=actor.id, project=project
    )
    member.role = "annotator"
    member.version += 1
    await db_session.commit()

    with pytest.raises(HTTPException) as denied:
        await TaskReviewWorkflow(db_session).claim_review(
            task.id,
            actor_id=actor.id,
            access=stale_access,
            origin=AuditOriginContext(None, None, None),
        )

    assert denied.value.status_code == 403
    await db_session.refresh(task)
    assert task.status == "review" and task.reviewer_claimed_at is None


async def test_review_audits_and_notifies_from_the_locked_actor_profile(
    db_session, super_admin, monkeypatch
):
    owner, _ = super_admin
    actor = await create_user(
        db_session,
        "employee",
        f"reviewer-stale-{uuid.uuid4()}@test.local",
        "Initial Reviewer",
    )
    project = await create_project(db_session, owner_id=owner.id)
    await create_membership(
        db_session, project_id=project.id, user_id=actor.id, role="reviewer"
    )
    task = await create_task(db_session, project_id=project.id, status="review")
    task.assignee_id = owner.id
    task.reviewer_id = actor.id
    task.reviewer_claimed_at = datetime.now(timezone.utc)
    task.review_round_id = uuid.uuid4()
    task.annotation_contributor_ids = [str(owner.id)]
    task.review_contributor_ids = [str(owner.id)]
    task.review_submitter_id = owner.id
    project.review_tasks = 1
    access = await resolve_project_access(
        db_session, actor_id=actor.id, project=project
    )

    current_name = "Current Reviewer"
    current_email = f"reviewer-current-{uuid.uuid4()}@test.local"
    await db_session.execute(
        update(User)
        .where(User.id == actor.id)
        .values(name=current_name, email=current_email)
        .execution_options(synchronize_session=False)
    )
    assert actor.name == "Initial Reviewer"
    assert actor.email != current_email

    async def skip_publish(_service, _notifications):
        return None

    monkeypatch.setattr(NotificationService, "publish_committed", skip_publish)
    result = await TaskReviewWorkflow(db_session).approve_task(
        task.id,
        actor_id=actor.id,
        access=access,
        origin=AuditOriginContext(None, None, None),
        expected_qc_digest=None,
        warning_issue_ids=[],
        note=None,
    )

    assert result == {"status": "approved", "task_id": str(task.id)}
    audit = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "task.approve",
            AuditLog.target_id == str(task.id),
        )
    )
    assert audit is not None
    assert (audit.actor_email, audit.actor_role) == (current_email, "employee")
    notification = await db_session.scalar(
        select(Notification).where(
            Notification.user_id == owner.id,
            Notification.type == "task.approved",
            Notification.target_id == task.id,
        )
    )
    assert notification is not None
    assert notification.payload["actor_name"] == current_name
    # Identity facts drive the write without refreshing an unrelated cached ORM row.
    assert actor.name == "Initial Reviewer"
    assert actor.email != current_email


@pytest.fixture
async def review_claim_rows(test_engine):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker.begin() as db:
        owner = await create_user(
            db, "project_admin", f"claim-owner-{uuid.uuid4()}@test.local", "Owner"
        )
        project = await create_project(db, owner_id=owner.id)
        reviewers = [
            await create_user(
                db, "employee", f"claim-{uuid.uuid4()}@test.local", "Reviewer"
            )
            for _ in range(2)
        ]
        for reviewer in reviewers:
            await create_membership(
                db, project_id=project.id, user_id=reviewer.id, role="reviewer"
            )
        batch = await create_batch(db, project_id=project.id, status="reviewing")
        task = await create_task(db, project_id=project.id, status="review")
        task.batch_id = batch.id
        task.assignee_id = owner.id
        task.annotation_contributor_ids = [str(owner.id)]
        task.review_contributor_ids = [str(owner.id)]
        task.review_submitter_id = owner.id
        task.review_round_id = uuid.uuid4()
        reviewer_ids = tuple(reviewer.id for reviewer in reviewers)
        project_id, batch_id, task_id = project.id, batch.id, task.id
        user_ids = (owner.id, *reviewer_ids)

    try:
        yield maker, project_id, batch_id, task_id, reviewer_ids
    finally:
        async with maker.begin() as db:
            await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
            await db.execute(delete(AuditLog).where(AuditLog.target_id == str(task_id)))
            await db.execute(delete(Task).where(Task.id == task_id))
            await db.execute(delete(TaskBatch).where(TaskBatch.id == batch_id))
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.execute(delete(User).where(User.id.in_(user_ids)))


async def _claim_review(db, project_id, task_id, actor_id):
    project = await db.get(Project, project_id)
    access = await resolve_project_access(db, actor_id=actor_id, project=project)
    return await TaskReviewWorkflow(db).claim_review(
        task_id,
        actor_id=actor_id,
        access=access,
        origin=AuditOriginContext(None, None, None),
    )


@pytest.mark.parametrize(
    "is_self", [True, False], ids=["claim-owner", "other-reviewer"]
)
async def test_repeated_review_claim_releases_locks_before_original_read(
    review_claim_rows, is_self
):
    maker, project_id, _, task_id, (reviewer, other) = review_claim_rows
    async with maker() as claimant, maker() as reader:
        saved = await _claim_review(claimant, project_id, task_id, reviewer)
        actor_id = reviewer if is_self else other
        actor = await reader.get(User, actor_id)
        repeated = await _claim_review(claimant, project_id, task_id, actor_id)

        task = await visible_task(reader, task_id, actor, lock=True)
        assert repeated == {**saved, "is_self": is_self}
        assert task.id == task_id
        assert task.reviewer_id == reviewer
        assert task.reviewer_claimed_at == saved["reviewer_claimed_at"]
        (audit,) = (
            await reader.scalars(
                select(AuditLog).where(
                    AuditLog.action == "task.review_claim",
                    AuditLog.target_id == str(task_id),
                )
            )
        ).all()
        assert audit.detail_json["review_round_id"] == str(task.review_round_id)
