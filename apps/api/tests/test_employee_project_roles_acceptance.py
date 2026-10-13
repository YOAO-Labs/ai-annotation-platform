"""Coordinator-owned HTTP acceptance for one employee across three projects."""

import uuid

import pytest

from app.core.security import create_access_token
from app.db.models.annotation import Annotation
from app.db.models.async_job import AsyncJob
from app.db.models.notification import Notification
from app.db.models.project_member import ProjectMember
from tests.factory import (
    create_membership,
    create_batch,
    create_project,
    create_task,
    create_user,
)


@pytest.mark.asyncio
async def test_employee_annotates_a_reviews_b_and_cannot_access_c(
    httpx_client, db_session
):
    owners = [
        await create_user(
            db_session, "project_admin", f"owner-{uuid.uuid4()}@test.local", name
        )
        for name in ("Owner A", "Owner B")
    ]
    employee, colleague = [
        await create_user(
            db_session, "employee", f"staff-{uuid.uuid4()}@test.local", name
        )
        for name in ("Employee U", "Employee V")
    ]
    project_a = await create_project(db_session, owner_id=owners[0].id, name="A")
    project_b = await create_project(db_session, owner_id=owners[1].id, name="B")
    project_c = await create_project(db_session, owner_id=owners[1].id, name="C")
    for project, owner, annotator, reviewer in (
        (project_a, owners[0], employee, colleague),
        (project_b, owners[1], colleague, employee),
    ):
        db_session.add_all(
            ProjectMember(
                project_id=project.id,
                user_id=user.id,
                role=role,
                assigned_by=owner.id,
            )
            for user, role in ((annotator, "annotator"), (reviewer, "reviewer"))
        )

    batch_a = await create_batch(
        db_session, project_id=project_a.id, status="annotating"
    )
    batch_a.annotator_id, batch_a.reviewer_id = employee.id, colleague.id
    task_a = await create_task(
        db_session, project_id=project_a.id, status="in_progress"
    )
    task_a.batch_id, task_a.assignee_id = batch_a.id, employee.id

    batch_b = await create_batch(
        db_session, project_id=project_b.id, status="reviewing"
    )
    batch_b.annotator_id, batch_b.reviewer_id = colleague.id, employee.id
    task_b = await create_task(db_session, project_id=project_b.id, status="review")
    task_b.batch_id, task_b.assignee_id = batch_b.id, colleague.id
    task_b.reviewer_id = employee.id
    task_b.annotation_contributor_ids = [str(colleague.id)]
    task_b.review_contributor_ids = [str(colleague.id)]
    task_b.review_submitter_id = colleague.id
    task_b.review_round_id = uuid.uuid4()
    pending_b = await create_task(db_session, project_id=project_b.id)
    pending_b.assignee_id = colleague.id
    task_c = await create_task(db_session, project_id=project_c.id)
    # Assignment alone must not grant project authority.
    task_c.assignee_id = employee.id
    await db_session.flush()

    headers = {
        "Authorization": "Bearer "
        + create_access_token(subject=str(employee.id), role="employee")
    }
    annotation = {
        "annotation_type": "bbox",
        "class_name": "car",
        "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
    }
    for task, operation, payload in (
        (task_a, "review/claim", None),
        (pending_b, "annotations", annotation),
        (task_c, "annotations", annotation),
    ):
        response = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/{operation}", headers=headers, json=payload
        )
        expected = 404 if task is task_c else 403
        assert response.status_code == expected, response.text
    hidden = await httpx_client.get(f"/api/v1/tasks/{task_c.id}", headers=headers)
    assert hidden.status_code == 404, hidden.text

    for operation, payload, expected in (
        ("lock", None, 200),
        ("annotations", annotation, 201),
        ("submit", None, 200),
    ):
        response = await httpx_client.post(
            f"/api/v1/tasks/{task_a.id}/{operation}", headers=headers, json=payload
        )
        assert response.status_code == expected, response.text
    for operation in ("review/claim", "review/approve"):
        response = await httpx_client.post(
            f"/api/v1/tasks/{task_b.id}/{operation}", headers=headers
        )
        assert response.status_code == 200, response.text

    await db_session.refresh(task_a)
    await db_session.refresh(task_b)
    await db_session.refresh(employee)
    assert task_a.status == "review"
    assert str(employee.id) in task_a.review_contributor_ids
    assert task_b.status == "completed"
    assert employee.role == "employee"


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_role", ["employee", "project_admin", "super_admin"])
@pytest.mark.parametrize("known_evidence", [False, True])
async def test_review_http_never_bypasses_self_or_unknown_evidence(
    httpx_client, db_session, actor_role, known_evidence
):
    is_manager = actor_role != "employee"
    owner = await create_user(
        db_session,
        actor_role if is_manager else "project_admin",
        f"owner-{uuid.uuid4()}@test.local",
        "Owner",
    )
    actor = (
        owner
        if is_manager
        else await create_user(
            db_session, "employee", f"reviewer-{uuid.uuid4()}@test.local", "Reviewer"
        )
    )
    author = await create_user(
        db_session, "employee", f"author-{uuid.uuid4()}@test.local", "Author"
    )
    project = await create_project(db_session, owner_id=owner.id)
    if not is_manager:
        await create_membership(
            db_session, project_id=project.id, user_id=actor.id, role="reviewer"
        )
    await create_membership(
        db_session, project_id=project.id, user_id=author.id, role="annotator"
    )
    task = await create_task(db_session, project_id=project.id, status="review")
    task.assignee_id, task.reviewer_id = author.id, actor.id
    task.annotation_contributor_ids = [str(actor.id)] if known_evidence else None
    task.review_contributor_ids = [str(actor.id), str(author.id)]
    task.review_submitter_id = author.id
    task.review_round_id = uuid.uuid4()
    await db_session.flush()
    headers = {
        "Authorization": "Bearer "
        + create_access_token(subject=str(actor.id), role=actor.role)
    }
    expected_status = 403 if known_evidence else 409
    expected_reason = (
        "self_review_denied" if known_evidence else "review_contributors_unknown"
    )
    for operation in ("claim", "approve", "reject"):
        response = await httpx_client.post(
            f"/api/v1/tasks/{task.id}/review/{operation}", headers=headers
        )
        assert response.status_code == expected_status, response.text
        assert response.json()["detail"]["reason"] == expected_reason
    await db_session.refresh(task)
    assert task.status == "review"
    assert task.reviewer_claimed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform_role", "project_role"),
    [("employee", "reviewer"), ("employee", "viewer"), ("viewer", "viewer")],
)
async def test_top_level_bulk_annotation_requires_annotation_phase_capability(
    httpx_client, db_session, platform_role, project_role
):
    owner = await create_user(
        db_session, "project_admin", f"owner-{uuid.uuid4()}@test.local", "Owner"
    )
    actor = await create_user(
        db_session, platform_role, f"reader-{uuid.uuid4()}@test.local", "Reader"
    )
    project = await create_project(db_session, owner_id=owner.id)
    await create_membership(
        db_session, project_id=project.id, user_id=actor.id, role=project_role
    )
    batch = await create_batch(db_session, project_id=project.id, status="active")
    task = await create_task(db_session, project_id=project.id, status="in_progress")
    task.batch_id = batch.id
    annotation = Annotation(
        project_id=project.id,
        task_id=task.id,
        user_id=owner.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
    )
    db_session.add(annotation)
    await db_session.flush()
    headers = {
        "Authorization": "Bearer "
        + create_access_token(subject=str(actor.id), role=actor.role)
    }
    response = await httpx_client.post(
        "/api/v1/annotations/bulk-update",
        headers=headers,
        json={"ids": [str(annotation.id)], "patch": {"is_hidden": True}},
    )
    assert response.status_code == 403, response.text
    await db_session.refresh(annotation)
    await db_session.refresh(task)
    assert annotation.is_hidden is False
    assert str(actor.id) not in (task.annotation_contributor_ids or [])


@pytest.mark.asyncio
@pytest.mark.parametrize("target_type", ["export", "async_job"])
@pytest.mark.parametrize("misleading_payload", [False, True])
async def test_notification_delivery_uses_actual_job_project_after_revocation(
    httpx_client, db_session, monkeypatch, target_type, misleading_payload
):
    from app.services.notification import NotificationService

    owner = await create_user(
        db_session, "project_admin", f"owner-{uuid.uuid4()}@test.local", "Owner"
    )
    employee = await create_user(
        db_session, "employee", f"staff-{uuid.uuid4()}@test.local", "Employee"
    )
    project_a = await create_project(db_session, owner_id=owner.id, name="Revoked")
    project_b = await create_project(db_session, owner_id=owner.id, name="Allowed")
    membership = ProjectMember(
        project_id=project_a.id, user_id=employee.id, role="reviewer"
    )
    db_session.add_all(
        [
            membership,
            ProjectMember(
                project_id=project_b.id, user_id=employee.id, role="reviewer"
            ),
        ]
    )
    job = AsyncJob(
        kind="export",
        project_id=project_a.id,
        user_id=employee.id,
        status="completed",
        payload={},
        result={},
    )
    db_session.add(job)
    await db_session.flush()
    secret = Notification(
        user_id=employee.id,
        type="export.completed",
        target_type=target_type,
        target_id=job.id,
        payload={"project_id": str(project_b.id)} if misleading_payload else {},
    )
    allowed = Notification(
        user_id=employee.id,
        type="task.mentioned",
        target_type="task",
        target_id=uuid.uuid4(),
        payload={"project_id": str(project_b.id)},
    )
    global_notice = Notification(
        user_id=employee.id,
        type="system.notice",
        target_type="system",
        target_id=uuid.uuid4(),
        payload={},
    )
    db_session.add_all([secret, allowed, global_notice])
    await db_session.flush()
    await db_session.delete(membership)
    await db_session.flush()
    headers = {
        "Authorization": "Bearer "
        + create_access_token(subject=str(employee.id), role="employee")
    }
    response = await httpx_client.get("/api/v1/notifications", headers=headers)
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()["items"]} == {
        str(allowed.id),
        str(global_notice.id),
    }
    assert response.json()["total"] == response.json()["unread"] == 2
    unread = await httpx_client.get(
        "/api/v1/notifications/unread-count", headers=headers
    )
    assert unread.status_code == 200, unread.text
    assert unread.json()["unread"] == 2

    published = []

    async def capture_publish(*, user_id, message):
        published.append(message["id"])

    monkeypatch.setattr("app.services.notification._publish", capture_publish)
    await NotificationService(db_session).publish_committed(
        [secret, allowed, global_notice]
    )
    assert set(published) == {str(allowed.id), str(global_notice.id)}
