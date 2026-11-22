"""Native task-comment drawings and authoritative annotation badge counts."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models.annotation import Annotation
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.annotation_feedback import AnnotationFeedback
from tests.factory import create_project, create_task

pytestmark = pytest.mark.asyncio


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _drawing() -> dict:
    return {
        "shapes": [
            {
                "type": "line",
                "points": [0.1, 0.2, 0.8, 0.9],
                "stroke": "#ef4444",
                "id": "stroke-1",
            }
        ]
    }


async def test_task_drawing_round_trips_through_feedback_and_mixed_feed(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id)
    task = await create_task(db_session, project_id=project.id)
    headers = _headers(token)
    payload = {
        "kind": "comment",
        "anchor_type": "task",
        "project_id": str(project.id),
        "task_id": str(task.id),
        "body": "",
        "canvas_drawing": _drawing(),
    }

    created = await httpx_client.post(
        "/api/v1/feedbacks", json=payload, headers=headers
    )
    assert created.status_code == 200, created.text
    expected = {
        "shapes": [
            {
                **_drawing()["shapes"][0],
                "started_at": None,
                "ended_at": None,
            }
        ]
    }
    assert created.json()["canvas_drawing"] == expected

    listed = await httpx_client.get(
        "/api/v1/feedbacks",
        params={"project_id": str(project.id), "task_id": str(task.id)},
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["canvas_drawing"] == expected

    feed = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/page",
        params={"scope": "task"},
        headers=headers,
    )
    assert feed.status_code == 200, feed.text
    assert feed.json()["items"][0]["data"]["canvas_drawing"] == expected

    patched = await httpx_client.patch(
        f"/api/v1/feedbacks/{created.json()['id']}",
        json={"body": ""},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["body"] == ""
    assert patched.json()["canvas_drawing"] == expected


async def test_task_drawing_rejects_empty_and_unsupported_destinations(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id)
    task = await create_task(db_session, project_id=project.id)
    headers = _headers(token)
    base = {
        "kind": "comment",
        "anchor_type": "task",
        "project_id": str(project.id),
        "task_id": str(task.id),
        "body": "text",
    }

    empty = await httpx_client.post(
        "/api/v1/feedbacks",
        json={**base, "canvas_drawing": {"shapes": []}},
        headers=headers,
    )
    assert empty.status_code == 422, empty.text

    annotation = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        attributes={},
        is_active=True,
    )
    db_session.add(annotation)
    await db_session.flush()
    annotation_target = await httpx_client.post(
        "/api/v1/feedbacks",
        json={
            **base,
            "anchor_type": "annotation",
            "annotation_id": str(annotation.id),
            "canvas_drawing": _drawing(),
        },
        headers=headers,
    )
    assert annotation_target.status_code == 422, annotation_target.text

    task.file_type = "video"
    await db_session.flush()
    video_target = await httpx_client.post(
        "/api/v1/feedbacks",
        json={**base, "canvas_drawing": _drawing()},
        headers=headers,
    )
    assert video_target.status_code == 422, video_target.text

    task.file_type = "image"
    await db_session.flush()
    issue_target = await httpx_client.post(
        "/api/v1/feedbacks",
        json={**base, "kind": "issue", "canvas_drawing": _drawing()},
        headers=headers,
    )
    assert issue_target.status_code == 422, issue_target.text

    root = await httpx_client.post("/api/v1/feedbacks", json=base, headers=headers)
    assert root.status_code == 200, root.text
    reply_target = await httpx_client.post(
        f"/api/v1/feedbacks/{root.json()['id']}/replies",
        json={"body": "reply", "canvas_drawing": _drawing()},
        headers=headers,
    )
    assert reply_target.status_code == 422, reply_target.text


async def test_annotation_comment_counts_are_sparse_authoritative_and_exact(
    httpx_client, db_session, super_admin
):
    user, token = super_admin
    project = await create_project(db_session, owner_id=user.id)
    task = await create_task(db_session, project_id=project.id)
    annotations = [
        Annotation(
            id=uuid.uuid4(),
            task_id=task.id,
            project_id=project.id,
            user_id=user.id,
            annotation_type="bbox",
            class_name="car",
            geometry={"type": "bbox", "x": x, "y": 0.1, "w": 0.2, "h": 0.2},
            attributes={},
            is_active=True,
        )
        for x in (0.1, 0.4)
    ]
    unavailable = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.7, "y": 0.1, "w": 0.2, "h": 0.2},
        attributes={},
        is_active=False,
    )
    db_session.add_all([*annotations, unavailable])
    await db_session.flush()
    other_task = await create_task(db_session, project_id=project.id)
    other_annotation = Annotation(
        id=uuid.uuid4(),
        task_id=other_task.id,
        project_id=project.id,
        user_id=user.id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.6, "w": 0.2, "h": 0.2},
        attributes={},
        is_active=True,
    )
    db_session.add(other_annotation)
    await db_session.flush()

    now = datetime.now(timezone.utc)
    comments = [
        AnnotationComment(
            id=uuid.uuid4(),
            annotation_id=annotations[0].id,
            project_id=project.id,
            author_id=user.id,
            body=f"comment-{index}",
            is_active=True,
            created_at=now + timedelta(microseconds=index),
        )
        for index in range(55)
    ]
    comments.extend(
        AnnotationComment(
            id=uuid.uuid4(),
            annotation_id=annotations[1].id,
            project_id=project.id,
            author_id=user.id,
            body=f"second-{index}",
            is_active=True,
            is_resolved=index == 0,
        )
        for index in range(2)
    )
    comments.append(
        AnnotationComment(
            id=uuid.uuid4(),
            annotation_id=annotations[0].id,
            project_id=project.id,
            author_id=user.id,
            body="deleted",
            is_active=False,
        )
    )
    comments.append(
        AnnotationComment(
            id=uuid.uuid4(),
            annotation_id=unavailable.id,
            project_id=project.id,
            author_id=user.id,
            body="unavailable annotation",
            is_active=True,
        )
    )
    comments.append(
        AnnotationComment(
            id=uuid.uuid4(),
            annotation_id=other_annotation.id,
            project_id=project.id,
            author_id=user.id,
            body="other task",
            is_active=True,
        )
    )
    db_session.add(
        AnnotationFeedback(
            id=uuid.uuid4(),
            kind="comment",
            anchor_type="annotation",
            project_id=project.id,
            task_id=task.id,
            annotation_id=annotations[0].id,
            body="mirror",
            author_id=user.id,
            attachments=[],
            is_active=True,
        )
    )
    db_session.add_all(comments)
    await db_session.commit()

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/annotation-counts",
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "counts": {
            str(annotations[0].id): 55,
            str(annotations[1].id): 2,
        }
    }


async def test_annotation_comment_counts_require_task_visibility(
    httpx_client, db_session, super_admin, annotator
):
    owner, _ = super_admin
    _outsider, outsider_token = annotator
    project = await create_project(db_session, owner_id=owner.id)
    task = await create_task(db_session, project_id=project.id)

    response = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/annotation-counts",
        headers=_headers(outsider_token),
    )
    assert response.status_code == 404, response.text
