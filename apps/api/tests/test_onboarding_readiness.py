"""Checklist signals must describe durable, authorized business state."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.project_member import ProjectMember
from app.db.models.task_batch import TaskBatch
from app.db.models.task_event import TaskEvent
from tests.test_dashboard_reviewer_mini import _seed_project, _seed_task
from tests.factory import create_membership


def headers(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_onboarding_uses_current_users_active_work_and_real_review(
    httpx_client, db_session, super_admin, annotator, reviewer
):
    admin, _ = super_admin
    user, token = annotator
    other, _ = reviewer
    project = await _seed_project(db_session, admin.id)
    await create_membership(
        db_session, project_id=project.id, user_id=user.id, role="annotator"
    )
    task = await _seed_task(db_session, project_id=project.id, status="review")
    task.assignee_id = user.id
    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id="B-ONBOARD",
        name="Assigned",
        status="active",
        annotator_id=user.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task.batch_id = batch.id
    now = datetime.now(timezone.utc)
    for actor, kind in [
        (user, "annotate"),
        (user, "annotate"),
        (other, "annotate"),
        (user, "review"),
    ]:
        db_session.add(
            TaskEvent(
                task_id=task.id,
                project_id=project.id,
                user_id=actor.id,
                kind=kind,
                started_at=now - timedelta(seconds=1),
                ended_at=now,
                duration_ms=1000,
            )
        )
    for actor, active, cancelled in [
        (user, True, False),
        (user, False, False),
        (user, True, True),
        (other, True, False),
    ]:
        db_session.add(
            Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=actor.id,
                class_name="object",
                geometry={"type": "bbox"},
                is_active=active,
                was_cancelled=cancelled,
            )
        )
    await db_session.flush()
    url = f"/api/v1/dashboard/annotator/projects/{project.id}/onboarding"
    response = await httpx_client.get(url, headers=headers(token))
    assert response.status_code == 200, response.text
    assert response.json() == dict(
        project_id=str(project.id),
        assigned_task_count=1,
        opened_task_count=1,
        saved_annotation_count=1,
        reviewed_task_count=0,
        reviewed_task_id=None,
        reviewed_task_display_id=None,
        reviewed_task_status=None,
        reviewed_task_reason=None,
    )
    task.status = "rejected"
    task.reviewed_at = now
    task.reject_reason = "最新退回理由"
    await db_session.flush()
    data = (await httpx_client.get(url, headers=headers(token))).json()
    assert data["reviewed_task_count"] == 1
    assert data["reviewed_task_id"] == str(task.id)
    assert data["reviewed_task_reason"] == "最新退回理由"
    task.status = "completed"
    batch.status = "archived"
    await db_session.flush()
    data = (await httpx_client.get(url, headers=headers(token))).json()
    assert data["assigned_task_count"] == 0
    assert data["reviewed_task_status"] == "completed"
    assert data["reviewed_task_reason"] is None


@pytest.mark.asyncio
async def test_checklist_endpoints_reject_out_of_scope_users(
    httpx_client, db_session, super_admin, annotator
):
    admin, _ = super_admin
    _, token = annotator
    project = await _seed_project(db_session, admin.id)
    for url in [
        f"/api/v1/dashboard/annotator/projects/{project.id}/onboarding",
        f"/api/v1/projects/{project.id}/readiness",
    ]:
        response = await httpx_client.get(url, headers=headers(token))
        assert response.status_code in {403, 404}


@pytest.mark.asyncio
async def test_readiness_counts_real_tasks_valid_recipients_and_all_jobs(
    httpx_client, db_session, super_admin, annotator, reviewer
):
    admin, token = super_admin
    anno, _ = annotator
    review, _ = reviewer
    project = await _seed_project(db_session, admin.id)
    memberships: dict[uuid.UUID, ProjectMember] = {}
    for user, role in ((anno, "annotator"), (review, "reviewer")):
        membership = ProjectMember(project_id=project.id, user_id=user.id, role=role)
        db_session.add(membership)
        memberships[user.id] = membership
    batches = []
    for status in ["draft", "active", "active", "archived"]:
        batch = TaskBatch(
            id=uuid.uuid4(),
            display_id=f"B-{uuid.uuid4().hex[:8]}",
            project_id=project.id,
            name=status,
            status=status,
            total_tasks=999,
            annotator_id=anno.id,
            reviewer_id=review.id,
        )
        db_session.add(batch)
        batches.append(batch)
    await db_session.flush()
    for batch in [batches[0], batches[1], batches[3]]:
        task = await _seed_task(db_session, project_id=project.id, status="pending")
        task.batch_id = batch.id
    old = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.add(
        AsyncJob(
            project_id=project.id,
            user_id=admin.id,
            kind="create_tasks",
            status="running",
            created_at=old,
        )
    )
    for _ in range(51):
        db_session.add(
            AsyncJob(
                project_id=project.id,
                user_id=admin.id,
                kind="create_tasks",
                status="completed",
            )
        )
    await db_session.flush()
    url = f"/api/v1/projects/{project.id}/readiness"
    response = await httpx_client.get(url, headers=headers(token))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["batch_count"] == 3
    assert data["nonempty_batch_count"] == 2
    assert data["executable_batch_count"] == data["assigned_batch_count"] == 1
    assert data["task_creation_active_jobs"] == 1
    assert data["latest_task_creation_status"] == "completed"
    review.is_active = False
    await db_session.flush()
    data = (await httpx_client.get(url, headers=headers(token))).json()
    assert data["active_reviewer_count"] == data["assigned_batch_count"] == 0
    review.is_active = True
    # Project authority now comes from the membership, not the account role:
    # changing the reviewer membership to annotator drops the reviewer count.
    memberships[review.id].role = "annotator"
    await db_session.flush()
    data = (await httpx_client.get(url, headers=headers(token))).json()
    assert data["active_reviewer_count"] == data["assigned_batch_count"] == 0


@pytest.mark.asyncio
async def test_reviewer_dashboard_filters_invisible_and_other_claimed_work(
    httpx_client, db_session, super_admin, reviewer, annotator
):
    admin, _ = super_admin
    review, token = reviewer
    other, _ = annotator
    visible = await _seed_project(db_session, admin.id)
    hidden = await _seed_project(db_session, admin.id)
    await create_membership(
        db_session, project_id=visible.id, user_id=review.id, role="reviewer"
    )
    now = datetime.now(timezone.utc)
    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=visible.id,
        display_id="B-QUEUE",
        name="Review",
        status="reviewing",
    )
    archived = TaskBatch(
        id=uuid.uuid4(),
        project_id=visible.id,
        display_id="B-ARCHIVED-QUEUE",
        name="Archived",
        status="archived",
    )
    db_session.add_all([batch, archived])
    await db_session.flush()
    pending = await _seed_task(db_session, project_id=visible.id, status="review")
    pending.batch_id = batch.id
    pending.reopened_count = 1
    claimed = await _seed_task(
        db_session,
        project_id=visible.id,
        status="review",
        reviewer_id=other.id,
        reviewer_claimed_at=now,
    )
    claimed.batch_id = batch.id
    await _seed_task(db_session, project_id=hidden.id, status="review")
    await _seed_task(db_session, project_id=visible.id, status="review")
    archived_task = await _seed_task(db_session, project_id=visible.id, status="review")
    archived_task.batch_id = archived.id
    for status in ["completed", "rejected", "review"]:
        task = await _seed_task(
            db_session,
            project_id=visible.id,
            status=status,
            reviewer_id=review.id,
            reviewed_at=now if status != "review" else None,
        )
        task.batch_id = batch.id
    await db_session.flush()
    response = await httpx_client.get(
        "/api/v1/dashboard/reviewer", headers=headers(token)
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["pending_review_count"] == 2
    assert len(data["pending_tasks"]) == 2
    assert next(
        task for task in data["pending_tasks"] if task["task_id"] == str(pending.id)
    )["is_rework"]
    assert data["approval_rate"] == data["approval_rate_24h"] == 50
