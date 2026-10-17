"""Feedback root filtering, exact counts and recursive thread reads."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.db.models.annotation import Annotation
from app.db.models.annotation_feedback import AnnotationFeedback
from tests.factory import create_project, create_task


def _feedback(
    *,
    author_id,
    project_id,
    task_id,
    body: str,
    parent_id=None,
    status: str = "open",
    active: bool = True,
    created_at: datetime,
    kind: str = "issue",
    anchor_type: str = "task",
    annotation_id=None,
    anchor_position=None,
):
    return AnnotationFeedback(
        id=uuid4(),
        kind=kind,
        anchor_type=anchor_type,
        project_id=project_id,
        task_id=task_id,
        annotation_id=annotation_id,
        anchor_position=anchor_position,
        body=body,
        author_id=author_id,
        status=status,
        thread_parent_id=parent_id,
        is_active=active,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_feedback_roots_counts_and_deleted_intermediate_thread(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id)
    task = await create_task(db_session, project_id=project.id)
    other_task = await create_task(db_session, project_id=project.id)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    root = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=task.id,
        body="root",
        created_at=start,
    )
    resolved_root = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=task.id,
        body="resolved root",
        status="resolved",
        created_at=start + timedelta(seconds=1),
    )
    first_reply = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=task.id,
        body="first reply",
        parent_id=root.id,
        created_at=start + timedelta(seconds=2),
    )
    deleted_intermediate = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=task.id,
        body="deleted intermediate",
        parent_id=root.id,
        active=False,
        created_at=start + timedelta(seconds=3),
    )
    surviving_grandchild = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=task.id,
        body="surviving grandchild",
        parent_id=deleted_intermediate.id,
        created_at=start + timedelta(seconds=4),
    )
    deleted_root = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=other_task.id,
        body="deleted root",
        active=False,
        created_at=start + timedelta(seconds=5),
    )
    orphan = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=other_task.id,
        body="orphan descendant",
        parent_id=deleted_root.id,
        created_at=start + timedelta(seconds=6),
    )
    db_session.add_all(
        [
            root,
            resolved_root,
            first_reply,
            deleted_intermediate,
            surviving_grandchild,
            deleted_root,
            orphan,
        ]
    )
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}

    listed = await httpx_client.get(
        "/api/v1/feedbacks",
        params={
            "project_id": str(project.id),
            "task_id": str(task.id),
            "kind": "issue",
            "root_only": "true",
            "include_counts": "true",
        },
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert [item["id"] for item in payload["items"]] == [
        str(resolved_root.id),
        str(root.id),
    ]
    assert payload["items"][0]["author_name"] == user.name
    assert payload["total"] == 2
    assert payload["status_counts"] == {
        "open": 1,
        "resolved": 1,
        "wont_fix": 0,
    }

    all_rows = await httpx_client.get(
        "/api/v1/feedbacks",
        params={
            "project_id": str(project.id),
            "task_id": str(task.id),
            "kind": "issue",
            "include_counts": "true",
        },
        headers=headers,
    )
    assert all_rows.status_code == 200, all_rows.text
    all_payload = all_rows.json()
    ids = {item["id"] for item in all_payload["items"]}
    assert ids == {
        str(root.id),
        str(resolved_root.id),
        str(first_reply.id),
        str(surviving_grandchild.id),
    }
    assert all_payload["total"] == 4

    first_page = await httpx_client.get(
        f"/api/v1/feedbacks/{root.id}/thread",
        params={"limit": 1},
        headers=headers,
    )
    assert first_page.status_code == 200, first_page.text
    first_payload = first_page.json()
    assert first_payload["total"] == 2
    assert len(first_payload["items"]) == 1
    assert first_payload["items"][0]["id"] == str(surviving_grandchild.id)
    assert first_payload["items"][0]["thread_parent_id"] == str(deleted_intermediate.id)
    assert first_payload["items"][0]["body"] == "surviving grandchild"

    second_page = await httpx_client.get(
        f"/api/v1/feedbacks/{root.id}/thread",
        params={"limit": 1, "cursor": first_payload["next_cursor"]},
        headers=headers,
    )
    assert second_page.status_code == 200, second_page.text
    assert [item["id"] for item in second_page.json()["items"]] == [str(first_reply.id)]
    assert second_page.json()["next_cursor"] is None

    unavailable = await httpx_client.get(
        f"/api/v1/feedbacks/{deleted_root.id}/thread", headers=headers
    )
    assert unavailable.status_code == 404
    malformed = await httpx_client.get(
        "/api/v1/feedbacks",
        params={"project_id": str(project.id), "cursor": "not-a-cursor"},
        headers=headers,
    )
    assert malformed.status_code == 400

    naive_cursor = base64.urlsafe_b64encode(
        f"2026-01-01T00:00:00|{root.id.hex}".encode()
    ).decode()
    malformed_timestamp = await httpx_client.get(
        "/api/v1/feedbacks",
        params={"project_id": str(project.id), "cursor": naive_cursor},
        headers=headers,
    )
    assert malformed_timestamp.status_code == 400


@pytest.mark.asyncio
async def test_project_and_annotated_pixel_roots_are_counted_and_threaded(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id)
    task = await create_task(db_session, project_id=project.id)
    annotation = Annotation(
        id=uuid4(),
        project_id=project.id,
        task_id=task.id,
        user_id=user.id,
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
    )
    db_session.add(annotation)
    start = datetime(2026, 2, 1, tzinfo=timezone.utc)
    project_root = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=None,
        anchor_type="project",
        body="project root",
        created_at=start,
    )
    pixel_root = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=task.id,
        annotation_id=annotation.id,
        anchor_type="pixel",
        anchor_position={"x": 0.2, "y": 0.3, "frame": 4},
        body="pixel root",
        created_at=start + timedelta(seconds=1),
    )
    pixel_reply = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=task.id,
        annotation_id=annotation.id,
        anchor_type="pixel",
        anchor_position={"x": 0.2, "y": 0.3, "frame": 4},
        body="pixel reply",
        parent_id=pixel_root.id,
        kind="comment",
        created_at=start + timedelta(seconds=2),
    )
    db_session.add_all([project_root, pixel_root, pixel_reply])
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}

    listed = await httpx_client.get(
        "/api/v1/feedbacks",
        params={
            "project_id": str(project.id),
            "root_only": "true",
            "include_counts": "true",
        },
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert {item["id"] for item in payload["items"]} == {
        str(project_root.id),
        str(pixel_root.id),
    }
    assert payload["total"] == 2
    assert payload["status_counts"] == {
        "open": 2,
        "resolved": 0,
        "wont_fix": 0,
    }

    thread = await httpx_client.get(
        f"/api/v1/feedbacks/{pixel_root.id}/thread",
        headers=headers,
    )
    assert thread.status_code == 200, thread.text
    thread_payload = thread.json()
    assert thread_payload["root"]["id"] == str(pixel_root.id)
    assert [item["id"] for item in thread_payload["items"]] == [str(pixel_reply.id)]
    assert thread_payload["total"] == 1
    assert thread_payload["items"][0]["author_name"] == user.name

    naive_thread_cursor = base64.urlsafe_b64encode(
        f"thread-v1|{pixel_root.id.hex}|2026-01-01T00:00:00|{pixel_reply.id.hex}".encode()
    ).decode()
    malformed_thread = await httpx_client.get(
        f"/api/v1/feedbacks/{pixel_root.id}/thread",
        params={"cursor": naive_thread_cursor},
        headers=headers,
    )
    assert malformed_thread.status_code == 400


@pytest.mark.asyncio
async def test_root_queries_hide_cross_scope_parent_chains(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id)
    other_project = await create_project(db_session, owner_id=user.id)
    task = await create_task(db_session, project_id=project.id)
    other_task = await create_task(db_session, project_id=other_project.id)
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    root = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=task.id,
        body="root",
        created_at=start,
    )
    cross_task = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=other_task.id,
        body="cross-task child",
        parent_id=root.id,
        created_at=start + timedelta(seconds=1),
    )
    other_root = _feedback(
        author_id=user.id,
        project_id=other_project.id,
        task_id=other_task.id,
        body="other project root",
        created_at=start + timedelta(seconds=2),
    )
    cross_project = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=task.id,
        body="cross-project child",
        parent_id=other_root.id,
        created_at=start + timedelta(seconds=3),
    )
    cross_anchor = _feedback(
        author_id=user.id,
        project_id=project.id,
        task_id=task.id,
        anchor_type="pixel",
        anchor_position={"x": 0.1, "y": 0.2, "frame": 1},
        body="cross-anchor child",
        parent_id=root.id,
        kind="comment",
        created_at=start + timedelta(seconds=4),
    )
    db_session.add_all([root, cross_task, other_root, cross_project, cross_anchor])
    await db_session.flush()
    headers = {"Authorization": f"Bearer {token}"}

    listed = await httpx_client.get(
        "/api/v1/feedbacks",
        params={"project_id": str(project.id)},
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [str(root.id)]

    thread = await httpx_client.get(
        f"/api/v1/feedbacks/{root.id}/thread", headers=headers
    )
    assert thread.status_code == 200, thread.text
    assert thread.json()["items"] == []
    assert thread.json()["total"] == 0
    unavailable = await httpx_client.delete(
        f"/api/v1/feedbacks/{cross_anchor.id}", headers=headers
    )
    assert unavailable.status_code == 404, unavailable.text
