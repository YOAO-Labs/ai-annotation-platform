"""Feedback mutation authorization and parent validation regressions."""

from __future__ import annotations

import pytest

from app.db.models.annotation_feedback import AnnotationFeedback
from tests.factory import create_membership, create_batch, create_project, create_task


@pytest.mark.asyncio
async def test_reviewer_mixed_patch_is_rejected_as_a_whole(
    httpx_client, db_session, super_admin, reviewer
):
    owner, owner_token = super_admin
    qa, qa_token = reviewer
    project = await create_project(db_session, owner_id=owner.id)
    batch = await create_batch(db_session, project_id=project.id, status="active")
    task = await create_task(db_session, project_id=project.id)
    task.batch_id = batch.id
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=qa.id,
        role="reviewer",
        assigned_by=owner.id,
    )
    await db_session.flush()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    qa_headers = {"Authorization": f"Bearer {qa_token}"}
    created = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "issue",
            "anchor_type": "task",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "body": "original",
            "severity": "warn",
        },
        headers=owner_headers,
    )
    assert created.status_code == 200, created.text
    feedback_id = created.json()["id"]

    mixed = await httpx_client.patch(
        f"/api/v1/feedbacks/{feedback_id}",
        json={"status": "resolved", "body": "smuggled"},
        headers=qa_headers,
    )
    assert mixed.status_code == 403, mixed.text
    unchanged = await db_session.get(AnnotationFeedback, feedback_id)
    assert unchanged.status == "open"
    assert unchanged.body == "original"

    status_only = await httpx_client.patch(
        f"/api/v1/feedbacks/{feedback_id}",
        json={"status": "resolved"},
        headers=qa_headers,
    )
    assert status_only.status_code == 200, status_only.text
    assert status_only.json()["status"] == "resolved"

    reviewer_owned = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "issue",
            "anchor_type": "task",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "body": "reviewer original",
            "severity": "info",
        },
        headers=qa_headers,
    )
    assert reviewer_owned.status_code == 200, reviewer_owned.text
    reviewer_edit = await httpx_client.patch(
        f"/api/v1/feedbacks/{reviewer_owned.json()['id']}",
        json={"body": "reviewer edited", "severity": "blocker"},
        headers=qa_headers,
    )
    assert reviewer_edit.status_code == 200, reviewer_edit.text
    assert reviewer_edit.json()["body"] == "reviewer edited"
    assert reviewer_edit.json()["severity"] == "blocker"


@pytest.mark.asyncio
async def test_direct_parent_create_rejects_cross_task_and_deleted_root(
    httpx_client, db_session, super_admin
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    task_a = await create_task(db_session, project_id=project.id)
    task_b = await create_task(db_session, project_id=project.id)
    headers = {"Authorization": f"Bearer {token}"}
    root = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "issue",
            "anchor_type": "task",
            "project_id": str(project.id),
            "task_id": str(task_a.id),
            "body": "root",
        },
        headers=headers,
    )
    assert root.status_code == 200, root.text
    root_id = root.json()["id"]
    cross_task = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "task",
            "project_id": str(project.id),
            "task_id": str(task_b.id),
            "body": "cross task",
            "thread_parent_id": root_id,
        },
        headers=headers,
    )
    assert cross_task.status_code == 422, cross_task.text

    deleted = await httpx_client.delete(f"/api/v1/feedbacks/{root_id}", headers=headers)
    assert deleted.status_code == 204, deleted.text
    unavailable = await httpx_client.post(
        f"/api/v1/feedbacks/{root_id}/replies",
        json={"body": "too late"},
        headers=headers,
    )
    assert unavailable.status_code == 404, unavailable.text


@pytest.mark.asyncio
async def test_whitespace_task_comment_requires_text_unless_attached(
    httpx_client, db_session, super_admin
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    task = await create_task(db_session, project_id=project.id)
    headers = {"Authorization": f"Bearer {token}"}
    bodyless = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "task",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "body": " \n\t",
        },
        headers=headers,
    )
    assert bodyless.status_code == 422, bodyless.text
    attached = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "task",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "body": " \n\t",
            "attachments": [{"key": "discussion/test.txt"}],
        },
        headers=headers,
    )
    assert attached.status_code == 200, attached.text


@pytest.mark.asyncio
async def test_native_feedback_replies_and_pixel_reply_text_guards(
    httpx_client, db_session, super_admin
):
    owner, token = super_admin
    project = await create_project(db_session, owner_id=owner.id)
    task = await create_task(db_session, project_id=project.id)
    headers = {"Authorization": f"Bearer {token}"}

    native_root = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "task",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "body": "native root",
        },
        headers=headers,
    )
    assert native_root.status_code == 200, native_root.text
    native_reply = await httpx_client.post(
        f"/api/v1/feedbacks/{native_root.json()['id']}/replies",
        json={"body": "native reply"},
        headers=headers,
    )
    assert native_reply.status_code == 200, native_reply.text
    assert native_reply.json()["body"] == "native reply"

    pixel_position = {"x": 0.25, "y": 0.75, "frame": 3}
    pixel_root = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "issue",
            "anchor_type": "pixel",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "anchor_position": pixel_position,
            "body": "pixel root",
        },
        headers=headers,
    )
    assert pixel_root.status_code == 200, pixel_root.text
    pixel_root_id = pixel_root.json()["id"]

    endpoint_blank = await httpx_client.post(
        f"/api/v1/feedbacks/{pixel_root_id}/replies",
        json={"body": " \n\t"},
        headers=headers,
    )
    assert endpoint_blank.status_code == 422, endpoint_blank.text

    direct_blank = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            "kind": "comment",
            "anchor_type": "pixel",
            "project_id": str(project.id),
            "task_id": str(task.id),
            "anchor_position": pixel_position,
            "body": " \n\t",
            "thread_parent_id": pixel_root_id,
        },
        headers=headers,
    )
    assert direct_blank.status_code == 422, direct_blank.text

    pixel_reply = await httpx_client.post(
        f"/api/v1/feedbacks/{pixel_root_id}/replies",
        json={"body": "pixel reply"},
        headers=headers,
    )
    assert pixel_reply.status_code == 200, pixel_reply.text
    patch_blank = await httpx_client.patch(
        f"/api/v1/feedbacks/{pixel_reply.json()['id']}",
        json={"body": " \n\t"},
        headers=headers,
    )
    assert patch_blank.status_code == 422, patch_blank.text

    attached_blank = await httpx_client.post(
        f"/api/v1/feedbacks/{pixel_root_id}/replies",
        json={
            "body": " \n\t",
            "attachments": [{"key": "discussion/pixel.txt"}],
        },
        headers=headers,
    )
    assert attached_blank.status_code == 200, attached_blank.text
    assert attached_blank.json()["attachments"] == [{"key": "discussion/pixel.txt"}]
