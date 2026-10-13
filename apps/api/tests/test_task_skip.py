"""v0.8.7 F7 · POST /tasks/{id}/skip 单测。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit_log import AuditLog
from app.db.models.project import Project
from app.db.models.project_member import ProjectMember
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from sqlalchemy import select
from tests.factory import create_membership, create_user, build_tool_bindings


async def _seed_project_task(
    db: AsyncSession, owner_id: uuid.UUID, status: str = "pending"
) -> tuple[Project, Task]:
    suffix = uuid.uuid4().hex[:6]
    p = Project(
        id=uuid.uuid4(),
        display_id=f"P-SK-{suffix}",
        name=f"skip-{suffix}",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner_id,
    )
    db.add(p)
    await db.flush()
    t = Task(
        id=uuid.uuid4(),
        project_id=p.id,
        display_id=f"T-SK-{suffix}",
        file_name="x.jpg",
        file_path="/tmp/x.jpg",
        file_type="image",
        tags=[],
        status=status,
    )
    db.add(t)
    await db.flush()
    return p, t


@pytest.mark.asyncio
async def test_skip_task_success_transitions_to_review(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    _, task = await _seed_project_task(db_session, user.id, status="pending")
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/skip",
        json={"reason": "image_corrupt"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "skipped"
    assert body["skip_reason"] == "image_corrupt"

    await db_session.refresh(task)
    assert task.status == "review"
    assert task.skip_reason == "image_corrupt"
    assert task.skipped_at is not None
    assert task.assignee_id == user.id
    assert task.first_review_contributor_ids == [str(user.id)]


@pytest.mark.asyncio
async def test_skip_task_invalid_reason_422(httpx_client, db_session, super_admin):
    user, token = super_admin
    _, task = await _seed_project_task(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/skip",
        json={"reason": "garbage"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert "invalid_skip_reason" in resp.text


@pytest.mark.asyncio
async def test_skip_task_status_review_409(httpx_client, db_session, super_admin):
    user, token = super_admin
    _, task = await _seed_project_task(db_session, user.id, status="review")
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/skip",
        json={"reason": "no_target"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_skip_task_audit_log_emitted(httpx_client, db_session, super_admin):
    user, token = super_admin
    _, task = await _seed_project_task(db_session, user.id)
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/skip",
        json={"reason": "unclear", "note": "图像模糊"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200

    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "task.skip",
                    AuditLog.target_id == str(task.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].detail_json["skip_reason"] == "unclear"
    assert rows[0].detail_json["note"] == "图像模糊"


@pytest.mark.asyncio
async def test_skip_task_assigns_to_caller_when_unassigned(
    httpx_client, db_session, annotator
):
    """v0.8.7 F7 · 跳过未派任务时，自动把 assignee 设为当前用户（同 submit 行为）。"""
    user, token = annotator
    project, task = await _seed_project_task(db_session, user.id, status="pending")
    assert task.assignee_id is None
    # Literal employee: explicit annotator membership grants the write authority,
    # and an active open-pool batch makes the unassigned task visible/claimable.
    await create_membership(
        db_session, project_id=project.id, user_id=user.id, role="annotator"
    )
    batch = TaskBatch(
        project_id=project.id,
        display_id=f"B-SK-OPEN-{uuid.uuid4().hex[:6]}",
        name="open pool",
        status="active",
        assigned_user_ids=[],
    )
    db_session.add(batch)
    await db_session.flush()
    task.batch_id = batch.id
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/skip",
        json={"reason": "other", "note": "其他"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(task)
    assert task.assignee_id == user.id
    assert task.assigned_at is not None
    assert task.skip_reason == "other"


@pytest.mark.asyncio
async def test_skip_task_rejects_task_assigned_to_another_annotator(
    httpx_client, db_session, super_admin, annotator
):
    owner, _ = super_admin
    actor, token = annotator
    other = await create_user(
        db_session,
        "employee",
        f"skip-owner-{uuid.uuid4()}@test.local",
        "Other annotator",
    )
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-SK-ASSIGN-{uuid.uuid4().hex[:6]}",
        name="skip assignment guard",
        type_label="image-det",
        type_key="image-det",
        owner_id=owner.id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db_session.add(project)
    await db_session.flush()
    db_session.add_all(
        [
            ProjectMember(
                project_id=project.id,
                user_id=actor.id,
                role="annotator",
                assigned_by=owner.id,
            ),
            ProjectMember(
                project_id=project.id,
                user_id=other.id,
                role="annotator",
                assigned_by=owner.id,
            ),
        ]
    )
    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"B-SK-ASSIGN-{uuid.uuid4().hex[:6]}",
        name="assigned batch",
        status="active",
        annotator_id=other.id,
    )
    db_session.add(batch)
    await db_session.flush()
    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        batch_id=batch.id,
        display_id=f"T-SK-ASSIGN-{uuid.uuid4().hex[:6]}",
        file_name="assigned.jpg",
        file_path="/tmp/assigned.jpg",
        file_type="image",
        status="pending",
        assignee_id=other.id,
    )
    db_session.add(task)
    await db_session.flush()
    await db_session.commit()

    resp = await httpx_client.post(
        f"/api/v1/tasks/{task.id}/skip",
        json={"reason": "no_target"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 404
    await db_session.refresh(task)
    assert task.status == "pending"
    assert task.assignee_id == other.id
