"""Account lifecycle workflow tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_lock import TaskLock
from app.db.models.user import User
from app.schemas.user import OffboardingCommitRequest
from app.services.user_lifecycle import UserLifecycleService
from tests.factory import create_project, create_user


pytestmark = pytest.mark.asyncio


def _headers(user: User) -> dict[str, str]:
    token = create_access_token(subject=str(user.id), role=user.role)
    return {"Authorization": f"Bearer {token}"}


async def test_user_status_filter_and_reactivate_metadata(
    httpx_client, super_admin, db_session
):
    actor, actor_token = super_admin
    target = await create_user(
        db_session, "employee", f"lifecycle-{uuid.uuid4()}@test.local", "Lifecycle"
    )
    headers = {"Authorization": f"Bearer {actor_token}"}

    response = await httpx_client.post(
        f"/api/v1/users/{target.id}/deactivate", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["disabled_kind"] == "suspended"
    assert body["disabled_at"] is not None
    assert body["disabled_by"] == str(actor.id)
    assert body["disabled_reason"] == "管理员停用账号"

    inactive = await httpx_client.get("/api/v1/users?status=inactive", headers=headers)
    assert inactive.status_code == 200
    assert any(row["id"] == str(target.id) for row in inactive.json())
    active_default = await httpx_client.get("/api/v1/users", headers=headers)
    assert active_default.status_code == 200
    assert all(row["is_active"] for row in active_default.json())

    restored = await httpx_client.post(
        f"/api/v1/users/{target.id}/reactivate",
        headers=headers,
        json={"reason": "returned"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["is_active"] is True
    assert restored.json()["disabled_kind"] is None


async def test_preview_commit_transfers_active_and_rejected_tasks(
    httpx_client, super_admin, db_session
):
    owner, owner_token = super_admin
    target = await create_user(
        db_session, "employee", f"handoff-{uuid.uuid4()}@test.local", "Leaving"
    )
    receiver = await create_user(
        db_session, "employee", f"receiver-{uuid.uuid4()}@test.local", "Receiver"
    )
    project = await create_project(
        db_session, owner_id=owner.id, name="Handoff project"
    )
    db_session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=target.id,
                role="annotator",
                assigned_by=owner.id,
            ),
            ProjectMember(
                project_id=project.id,
                user_id=receiver.id,
                role="annotator",
                assigned_by=owner.id,
            ),
        ]
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-{uuid.uuid4().hex[:8]}",
        name="handoff batch",
        status="active",
        annotator_id=target.id,
    )
    db_session.add(batch)
    await db_session.flush()
    active_task = Task(
        project_id=project.id,
        batch_id=batch.id,
        display_id=f"T-{uuid.uuid4().hex[:8]}",
        file_name="active.jpg",
        file_path="active.jpg",
        status="in_progress",
        assignee_id=target.id,
    )
    rejected_task = Task(
        project_id=project.id,
        batch_id=batch.id,
        display_id=f"T-{uuid.uuid4().hex[:8]}",
        file_name="rejected.jpg",
        file_path="rejected.jpg",
        status="rejected",
        assignee_id=target.id,
    )
    completed_task = Task(
        project_id=project.id,
        batch_id=batch.id,
        display_id=f"T-{uuid.uuid4().hex[:8]}",
        file_name="completed.jpg",
        file_path="completed.jpg",
        status="completed",
        assignee_id=target.id,
    )
    db_session.add_all([active_task, rejected_task, completed_task])
    await db_session.flush()
    db_session.add(
        TaskLock(
            task_id=active_task.id,
            user_id=target.id,
            expire_at=datetime.now(timezone.utc),
        )
    )
    await db_session.flush()

    headers = {"Authorization": f"Bearer {owner_token}"}
    preview = await httpx_client.get(
        f"/api/v1/users/{target.id}/offboarding-preview", headers=headers
    )
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["can_commit"] is True
    project_body = preview_body["projects"][0]
    assert project_body["tasks"]["annotator"]["in_progress"] == 1
    assert project_body["tasks"]["annotator"]["rejected"] == 1
    receiver_ids = {
        option["id"]
        for option in project_body["roles"]["annotator"]["receiver_options"]
    }
    assert str(receiver.id) in receiver_ids

    committed = await httpx_client.post(
        f"/api/v1/users/{target.id}/offboarding",
        headers=headers,
        json={
            "preview_version": preview_body["preview_version"],
            "reason": "handoff",
            "mode": "handoff",
            "projects": [
                {
                    "project_id": str(project.id),
                    "annotator_receiver_id": str(receiver.id),
                }
            ],
        },
    )
    assert committed.status_code == 200, committed.text
    result = committed.json()
    assert result["user"]["disabled_kind"] == "suspended"
    assert result["transfers"][0]["task_count"] == 2

    await db_session.refresh(batch)
    await db_session.refresh(active_task)
    await db_session.refresh(rejected_task)
    await db_session.refresh(completed_task)
    assert batch.annotator_id == receiver.id
    assert active_task.assignee_id == receiver.id
    assert rejected_task.assignee_id == receiver.id
    assert completed_task.assignee_id == target.id
    assert (
        await db_session.scalar(
            select(TaskLock.id).where(TaskLock.user_id == target.id)
        )
    ) is None

    # The receiver can use the existing project/task read path immediately.
    receiver_response = await httpx_client.get(
        f"/api/v1/tasks?project_id={project.id}&batch_id={batch.id}",
        headers=_headers(receiver),
    )
    assert receiver_response.status_code == 200, receiver_response.text
    assert {item["id"] for item in receiver_response.json()["items"]} >= {
        str(active_task.id),
        str(rejected_task.id),
    }

    old_session = await httpx_client.get(
        f"/api/v1/projects/{project.id}", headers=_headers(target)
    )
    assert old_session.status_code == 401


async def test_reviewer_and_owner_roles_use_separate_receivers(db_session, super_admin):
    actor, _ = super_admin
    reviewer = await create_user(
        db_session, "employee", f"reviewer-{uuid.uuid4()}@test.local", "Reviewer"
    )
    reviewer_receiver = await create_user(
        db_session,
        "employee",
        f"reviewer-receiver-{uuid.uuid4()}@test.local",
        "Reviewer receiver",
    )
    owner = await create_user(
        db_session, "project_admin", f"owner-{uuid.uuid4()}@test.local", "Owner"
    )
    owner_receiver = await create_user(
        db_session,
        "project_admin",
        f"owner-receiver-{uuid.uuid4()}@test.local",
        "Owner receiver",
    )
    project = await create_project(
        db_session, owner_id=owner.id, name="Mixed role project"
    )
    db_session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=reviewer.id,
                role="reviewer",
                assigned_by=actor.id,
            ),
            ProjectMember(
                project_id=project.id,
                user_id=reviewer_receiver.id,
                role="reviewer",
                assigned_by=actor.id,
            ),
        ]
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-{uuid.uuid4().hex[:8]}",
        name="review batch",
        status="reviewing",
        reviewer_id=reviewer.id,
    )
    db_session.add(batch)
    await db_session.flush()
    review_task = Task(
        project_id=project.id,
        batch_id=batch.id,
        display_id=f"T-{uuid.uuid4().hex[:8]}",
        file_name="review.jpg",
        file_path="review.jpg",
        status="review",
        reviewer_id=reviewer.id,
    )
    db_session.add(review_task)
    await db_session.flush()

    reviewer_preview = await UserLifecycleService.preview(
        db_session, target_id=reviewer.id, actor=actor
    )
    assert reviewer_preview.projects[0].roles["reviewer"].present is True
    assert reviewer_preview.projects[0].roles["owner"].present is False
    await UserLifecycleService.offboard(
        db_session,
        target_id=reviewer.id,
        actor=actor,
        payload=OffboardingCommitRequest(
            preview_version=reviewer_preview.preview_version,
            mode="handoff",
            projects=[
                {
                    "project_id": project.id,
                    "reviewer_receiver_id": reviewer_receiver.id,
                }
            ],
        ),
    )
    await db_session.refresh(review_task)
    assert review_task.reviewer_id == reviewer_receiver.id

    owner_preview = await UserLifecycleService.preview(
        db_session, target_id=owner.id, actor=actor
    )
    assert owner_preview.projects[0].roles["owner"].present is True
    await UserLifecycleService.offboard(
        db_session,
        target_id=owner.id,
        actor=actor,
        payload=OffboardingCommitRequest(
            preview_version=owner_preview.preview_version,
            mode="handoff",
            projects=[
                {
                    "project_id": project.id,
                    "owner_receiver_id": owner_receiver.id,
                }
            ],
        ),
    )
    await db_session.refresh(project)
    assert project.owner_id == owner_receiver.id
