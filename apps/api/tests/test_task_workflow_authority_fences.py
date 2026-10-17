from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.task_lifecycle import TaskLifecycleWorkflow
from app.application.task_review import TaskReviewWorkflow
from app.services.audit import AuditOriginContext
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
    stale_access = await resolve_project_access(db_session, user=actor, project=project)

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
            actor=actor,
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
    stale_access = await resolve_project_access(db_session, user=actor, project=project)
    member.role = "annotator"
    member.version += 1
    await db_session.commit()

    with pytest.raises(HTTPException) as denied:
        await TaskReviewWorkflow(db_session).claim_review(
            task.id,
            actor=actor,
            access=stale_access,
            origin=AuditOriginContext(None, None, None),
        )

    assert denied.value.status_code == 403
    await db_session.refresh(task)
    assert task.status == "review" and task.reviewer_claimed_at is None
