"""API-A · authoritative task discussion read model and legacy comment ACLs."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.annotation import Annotation
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from tests.factory import create_membership, build_tool_bindings

pytestmark = pytest.mark.asyncio


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_task(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    suffix: str | None = None,
) -> tuple[Project, Task, Annotation, Annotation]:
    suffix = suffix or uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-DISC-{suffix}",
        name="discussion test",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=owner_id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db.add(project)
    await db.flush()

    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-DISC-{suffix}",
        file_name="discussion.jpg",
        file_path=f"discussion/{suffix}.jpg",
        file_type="image",
        status="in_progress",
    )
    db.add(task)
    await db.flush()

    annotations = [
        Annotation(
            id=uuid.uuid4(),
            task_id=task.id,
            project_id=project.id,
            user_id=owner_id,
            annotation_type="bbox",
            class_name="car",
            geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
            attributes={},
            is_active=True,
        ),
        Annotation(
            id=uuid.uuid4(),
            task_id=task.id,
            project_id=project.id,
            user_id=owner_id,
            annotation_type="bbox",
            class_name="car",
            geometry={"type": "bbox", "x": 0.4, "y": 0.1, "w": 0.2, "h": 0.2},
            attributes={},
            is_active=True,
        ),
    ]
    db.add_all(annotations)
    await db.flush()
    return project, task, annotations[0], annotations[1]


def _annotation_comment(
    *,
    comment_id: uuid.UUID,
    annotation_id: uuid.UUID,
    project_id: uuid.UUID,
    author_id: uuid.UUID,
    created_at: datetime,
    body: str,
) -> AnnotationComment:
    return AnnotationComment(
        id=comment_id,
        annotation_id=annotation_id,
        project_id=project_id,
        author_id=author_id,
        body=body,
        is_resolved=False,
        is_active=True,
        mentions=[
            {
                "userId": str(author_id),
                "displayName": "owner",
                "offset": 0,
                "length": 5,
            }
        ],
        attachments=[
            {
                "storageKey": f"comment-attachments/{annotation_id}/file.png",
                "fileName": "file.png",
                "mimeType": "image/png",
                "size": 7,
            }
        ],
        canvas_drawing={
            "shapes": [
                {
                    "type": "line",
                    "points": [0.1, 0.1, 0.8, 0.8],
                    "stroke": "#ef4444",
                    "id": "stroke-1",
                }
            ]
        },
        anchor={
            "kind": "video_frame",
            "frameIndex": 4,
            "trackId": "trk-1",
            "source": "manual",
        },
        created_at=created_at,
    )


def _task_feedback(
    *,
    feedback_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    author_id: uuid.UUID,
    created_at: datetime,
    body: str,
    thread_parent_id: uuid.UUID | None = None,
    kind: str = "comment",
    anchor_type: str = "task",
    annotation_id: uuid.UUID | None = None,
) -> AnnotationFeedback:
    return AnnotationFeedback(
        id=feedback_id,
        kind=kind,
        anchor_type=anchor_type,
        project_id=project_id,
        task_id=task_id,
        annotation_id=annotation_id,
        anchor_position=({"x": 0.25, "y": 0.75} if anchor_type == "pixel" else None),
        status="open",
        severity=None,
        title=None,
        body=body,
        author_id=author_id,
        attachments=[
            {
                "storageKey": "feedback-attachments/native.txt",
                "fileName": "native.txt",
                "mimeType": "text/plain",
                "size": 3,
            }
        ],
        thread_parent_id=thread_parent_id,
        is_active=True,
        created_at=created_at,
    )


async def test_mixed_pages_are_source_aware_and_exactly_ordered(
    httpx_client,
    db_session: AsyncSession,
    super_admin,
):
    user, token = super_admin
    project, task, annotation_a, annotation_b = await _seed_task(db_session, user.id)
    base = datetime.now(timezone.utc)
    equal_id = uuid.uuid4()
    same_source_high = uuid.UUID("00000000-0000-0000-0000-000000001002")
    same_source_low = uuid.UUID("00000000-0000-0000-0000-000000001001")
    rows = [
        _annotation_comment(
            comment_id=equal_id,
            annotation_id=annotation_a.id,
            project_id=project.id,
            author_id=user.id,
            created_at=base + timedelta(seconds=4),
            body="legacy equal UUID",
        ),
        _annotation_comment(
            comment_id=same_source_high,
            annotation_id=annotation_b.id,
            project_id=project.id,
            author_id=user.id,
            created_at=base + timedelta(seconds=2),
            body="legacy second",
        ),
        _annotation_comment(
            comment_id=same_source_low,
            annotation_id=annotation_a.id,
            project_id=project.id,
            author_id=user.id,
            created_at=base + timedelta(seconds=2),
            body="legacy same timestamp lower",
        ),
        _task_feedback(
            feedback_id=equal_id,
            project_id=project.id,
            task_id=task.id,
            author_id=user.id,
            created_at=base + timedelta(seconds=4),
            body="native equal UUID",
        ),
        _task_feedback(
            feedback_id=uuid.uuid4(),
            project_id=project.id,
            task_id=task.id,
            author_id=user.id,
            created_at=base + timedelta(seconds=3),
            body="native second",
        ),
        _annotation_comment(
            comment_id=uuid.uuid4(),
            annotation_id=annotation_a.id,
            project_id=project.id,
            author_id=user.id,
            created_at=base + timedelta(seconds=1),
            body="legacy third",
        ),
        _task_feedback(
            feedback_id=uuid.uuid4(),
            project_id=project.id,
            task_id=task.id,
            author_id=user.id,
            created_at=base,
            body="native third",
        ),
        # A mirror with stale body must never leak into the authoritative feed.
        _task_feedback(
            feedback_id=uuid.uuid4(),
            project_id=project.id,
            task_id=task.id,
            author_id=user.id,
            created_at=base + timedelta(seconds=5),
            body="stale annotation mirror",
            kind="comment",
            anchor_type="annotation",
            annotation_id=annotation_a.id,
        ),
        # Issue roots and replies are not task comments.
        _task_feedback(
            feedback_id=uuid.uuid4(),
            project_id=project.id,
            task_id=task.id,
            author_id=user.id,
            created_at=base + timedelta(seconds=6),
            body="issue root",
            kind="issue",
        ),
    ]
    issue = rows[-1]
    rows.append(
        _task_feedback(
            feedback_id=uuid.uuid4(),
            project_id=project.id,
            task_id=task.id,
            author_id=user.id,
            created_at=base + timedelta(seconds=7),
            body="issue reply",
            thread_parent_id=issue.id,
        )
    )
    db_session.add_all(rows)
    await db_session.flush()
    await db_session.commit()

    headers = _bearer(token)
    seen: list[tuple[str, str]] = []
    cursor = None
    pages = []
    for _ in range(3):
        params = {"limit": 3}
        if cursor:
            params["cursor"] = cursor
        response = await httpx_client.get(
            f"/api/v1/tasks/{task.id}/discussion/page",
            params=params,
            headers=headers,
        )
        assert response.status_code == 200, response.text
        page = response.json()
        pages.append(page)
        seen.extend((item["source"], item["data"]["id"]) for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(pages) == 3
    assert pages[0]["total"] == 7
    assert [item["source"] for item in pages[0]["items"]] == [
        "feedback",
        "annotation_comment",
        "feedback",
    ]
    assert pages[0]["items"][0]["data"]["id"] == str(equal_id)
    assert pages[0]["items"][1]["data"]["id"] == str(equal_id)
    assert len(seen) == 7
    assert len(set(seen)) == 7
    assert pages[-1]["next_cursor"] is None
    same_source_ids = [
        item["data"]["id"]
        for page in pages
        for item in page["items"]
        if item["source"] == "annotation_comment"
        and item["data"]["body"] in {"legacy second", "legacy same timestamp lower"}
    ]
    assert same_source_ids == [str(same_source_high), str(same_source_low)]
    assert all(
        item["data"]["body"]
        not in {"stale annotation mirror", "issue root", "issue reply"}
        for page in pages
        for item in page["items"]
    )

    legacy = next(
        item
        for page in pages
        for item in page["items"]
        if item["source"] == "annotation_comment"
    )
    assert legacy["data"]["mentions"][0]["userId"] == str(user.id)
    assert legacy["data"]["attachments"][0]["storageKey"].startswith(
        f"comment-attachments/{annotation_a.id}/"
    )
    assert legacy["data"]["canvas_drawing"]["shapes"][0]["id"] == "stroke-1"
    assert legacy["data"]["anchor"]["frameIndex"] == 4
    native = next(
        item for page in pages for item in page["items"] if item["source"] == "feedback"
    )
    assert native["data"]["author_name"] == user.name
    assert legacy["actions"] == {
        "edit": True,
        "change_status": True,
        "delete": True,
        "reply": False,
    }


async def test_scope_and_cursor_bindings_are_enforced(
    httpx_client,
    db_session: AsyncSession,
    super_admin,
):
    user, token = super_admin
    project, task, annotation, _ = await _seed_task(db_session, user.id)
    now = datetime.now(timezone.utc)
    db_session.add(
        _annotation_comment(
            comment_id=uuid.uuid4(),
            annotation_id=annotation.id,
            project_id=project.id,
            author_id=user.id,
            created_at=now,
            body="annotation only",
        )
    )
    db_session.add(
        _task_feedback(
            feedback_id=uuid.uuid4(),
            project_id=project.id,
            task_id=task.id,
            author_id=user.id,
            created_at=now - timedelta(seconds=1),
            body="task only",
        )
    )
    await db_session.commit()
    headers = _bearer(token)

    annotation_page = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/page",
        params={"scope": "annotation", "annotation_id": annotation.id},
        headers=headers,
    )
    assert annotation_page.status_code == 200
    assert annotation_page.json()["total"] == 1
    assert all(
        item["source"] == "annotation_comment"
        for item in annotation_page.json()["items"]
    )

    task_page = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/page",
        params={"scope": "task"},
        headers=headers,
    )
    assert task_page.status_code == 200
    assert task_page.json()["total"] == 1
    assert task_page.json()["items"][0]["source"] == "feedback"

    missing_annotation = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/page",
        params={"scope": "annotation"},
        headers=headers,
    )
    assert missing_annotation.status_code == 422
    contradictory = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/page",
        params={"scope": "task", "annotation_id": annotation.id},
        headers=headers,
    )
    assert contradictory.status_code == 422

    first = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/page?limit=1",
        headers=headers,
    )
    cursor = first.json()["next_cursor"]
    assert cursor
    cursor_payload = json.loads(base64.urlsafe_b64decode(cursor).decode("utf-8"))
    cursor_payload["schema_version"] = True
    bool_version_cursor = base64.urlsafe_b64encode(
        json.dumps(cursor_payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    bool_version = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/page",
        params={"cursor": bool_version_cursor},
        headers=headers,
    )
    assert bool_version.status_code == 400

    _, other_task, _, _ = await _seed_task(db_session, user.id)
    await db_session.commit()
    mismatched_task = await httpx_client.get(
        f"/api/v1/tasks/{other_task.id}/discussion/page",
        params={"cursor": cursor},
        headers=headers,
    )
    assert mismatched_task.status_code == 400

    mismatched_scope = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/page",
        params={"scope": "task", "cursor": cursor},
        headers=headers,
    )
    assert mismatched_scope.status_code == 400
    malformed = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/page?cursor=garbage",
        headers=headers,
    )
    assert malformed.status_code == 400
    oversized = await httpx_client.get(
        f"/api/v1/tasks/{task.id}/discussion/page",
        params={"cursor": "a" * 2049},
        headers=headers,
    )
    assert oversized.status_code == 400


async def test_deleted_annotation_comments_remain_readable_but_writes_require_active_annotation(
    httpx_client,
    db_session: AsyncSession,
    super_admin,
):
    user, token = super_admin
    project, task, annotation, _ = await _seed_task(db_session, user.id)
    annotation.is_active = False
    comment = _annotation_comment(
        comment_id=uuid.uuid4(),
        annotation_id=annotation.id,
        project_id=project.id,
        author_id=user.id,
        created_at=datetime.now(timezone.utc),
        body="historical comment",
    )
    db_session.add(comment)
    await db_session.commit()
    headers = _bearer(token)

    for path in (
        f"/api/v1/tasks/{task.id}/discussion/page?scope=annotation&annotation_id={annotation.id}",
        f"/api/v1/annotations/{annotation.id}/comments",
        f"/api/v1/annotations/{annotation.id}/comments/page",
    ):
        response = await httpx_client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        if path.endswith("comments") or path.endswith("comments/page"):
            items = (
                response.json()
                if path.endswith("comments")
                else response.json()["items"]
            )
            assert items[0]["body"] == "historical comment"

    create = await httpx_client.post(
        f"/api/v1/annotations/{annotation.id}/comments",
        json={"body": "new", "mentions": [], "attachments": []},
        headers=headers,
    )
    assert create.status_code == 404
    upload = await httpx_client.post(
        f"/api/v1/annotations/{annotation.id}/comment-attachments/upload-init",
        json={"file_name": "x.txt", "content_type": "text/plain"},
        headers=headers,
    )
    assert upload.status_code == 404


async def test_assigned_away_task_is_hidden_from_new_and_legacy_comment_routes(
    httpx_client,
    db_session: AsyncSession,
    super_admin,
    annotator,
    project_admin,
):
    owner, _ = super_admin
    hidden_user, hidden_token = annotator
    other_assignee, _ = project_admin
    project, _, annotation_a, _ = await _seed_task(db_session, owner.id)
    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"B-DISC-{uuid.uuid4().hex[:8]}",
        name="hidden discussion batch",
        status="active",
        annotator_id=other_assignee.id,
        assigned_user_ids=[str(other_assignee.id)],
    )
    db_session.add(batch)
    await db_session.flush()
    task = await db_session.get(Task, annotation_a.task_id)
    assert task is not None
    task.batch_id = batch.id
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=hidden_user.id,
        role="annotator",
        assigned_by=owner.id,
    )
    comment = _annotation_comment(
        comment_id=uuid.uuid4(),
        annotation_id=annotation_a.id,
        project_id=project.id,
        author_id=owner.id,
        created_at=datetime.now(timezone.utc),
        body="hidden",
    )
    db_session.add(comment)
    await db_session.commit()
    headers = _bearer(hidden_token)
    valid_key = f"comment-attachments/{annotation_a.id}/file.png"

    paths = [
        f"/api/v1/tasks/{task.id}/discussion/page",
        f"/api/v1/tasks/{task.id}/comments/page",
        f"/api/v1/annotations/{annotation_a.id}/comments",
        f"/api/v1/annotations/{annotation_a.id}/comments/page",
        f"/api/v1/comments/{comment.id}",
        f"/api/v1/annotations/{annotation_a.id}/comment-attachments/upload-init",
        f"/api/v1/annotations/{annotation_a.id}/comment-attachments/download?key={valid_key}",
        f"/api/v1/annotations/{annotation_a.id}/comment-attachments/download?as_json=true&key={valid_key}",
    ]
    for path in paths:
        if (
            path.endswith("/comments")
            or path.endswith("/comments/page")
            or "discussion/page" in path
        ):
            response = await httpx_client.get(path, headers=headers)
        elif path.endswith("upload-init"):
            response = await httpx_client.post(
                path,
                json={"file_name": "x.txt", "content_type": "text/plain"},
                headers=headers,
            )
        elif "/comments/" in path:
            response = await httpx_client.patch(
                path,
                json={"body": "attempt"},
                headers=headers,
            )
        else:
            response = await httpx_client.get(path, headers=headers)
        assert response.status_code == 404, (path, response.text)


@pytest.mark.parametrize("as_json", [False, True])
async def test_attachment_download_json_retains_visibility_and_legacy_redirect(
    httpx_client,
    db_session: AsyncSession,
    super_admin,
    monkeypatch,
    as_json: bool,
):
    from app.api.v1.annotation_comments import storage_service

    user, token = super_admin
    _, _, annotation, other_annotation = await _seed_task(db_session, user.id)
    await db_session.commit()
    key = f"comment-attachments/{annotation.id}/file.png"
    url = "https://storage.example.test/short-lived-attachment"
    issued = []

    def generate_download_url(storage_key, **options):
        issued.append((storage_key, options))
        return url

    monkeypatch.setattr(storage_service, "generate_download_url", generate_download_url)
    path = f"/api/v1/annotations/{annotation.id}/comment-attachments/download"
    params = {"as_json": str(as_json).lower(), "key": key}
    anonymous = await httpx_client.get(path, params=params, follow_redirects=False)
    assert anonymous.status_code in {401, 403}
    assert issued == []

    response = await httpx_client.get(
        path, params=params, headers=_bearer(token), follow_redirects=False
    )
    if as_json:
        assert response.status_code == 200, response.text
        assert response.json() == {"download_url": url}
    else:
        assert response.status_code == 302, response.text
        assert response.headers["location"] == url
    assert issued == [(key, {"expires_in": 300, "align": False})]

    other_path = (
        f"/api/v1/annotations/{other_annotation.id}/comment-attachments/download"
    )
    mismatch = await httpx_client.get(
        other_path, params=params, headers=_bearer(token), follow_redirects=False
    )
    assert mismatch.status_code == 400
    assert len(issued) == 1


async def test_cross_project_reviewer_and_annotator_are_hidden_from_comment_surfaces(
    httpx_client,
    db_session: AsyncSession,
    super_admin,
    reviewer,
    annotator,
):
    """An active batch is not enough without target-project membership."""

    owner, _ = super_admin
    outsider_reviewer, reviewer_token = reviewer
    outsider_annotator, annotator_token = annotator
    project, task, annotation, _ = await _seed_task(db_session, owner.id)
    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"B-DISC-{uuid.uuid4().hex[:8]}",
        name="visible only inside project",
        status="active",
        annotator_id=None,
        assigned_user_ids=[],
    )
    db_session.add(batch)
    await db_session.flush()
    task.batch_id = batch.id
    comment = _annotation_comment(
        comment_id=uuid.uuid4(),
        annotation_id=annotation.id,
        project_id=project.id,
        author_id=owner.id,
        created_at=datetime.now(timezone.utc),
        body="outside project",
    )
    db_session.add(comment)
    await db_session.commit()

    for outsider, token in (
        (outsider_reviewer, reviewer_token),
        (outsider_annotator, annotator_token),
    ):
        assert outsider.id not in {
            owner.id,
        }
        headers = _bearer(token)
        read_routes = (
            f"/api/v1/tasks/{task.id}/discussion/page",
            f"/api/v1/tasks/{task.id}/comments/page",
            f"/api/v1/annotations/{annotation.id}/comments",
            f"/api/v1/annotations/{annotation.id}/comments/page",
        )
        for path in read_routes:
            response = await httpx_client.get(path, headers=headers)
            assert response.status_code == 404, (path, response.text)

        create = await httpx_client.post(
            f"/api/v1/annotations/{annotation.id}/comments",
            json={"body": "attempt", "mentions": [], "attachments": []},
            headers=headers,
        )
        assert create.status_code == 404

        patch = await httpx_client.patch(
            f"/api/v1/comments/{comment.id}",
            json={"body": "attempt"},
            headers=headers,
        )
        assert patch.status_code == 404
        delete = await httpx_client.delete(
            f"/api/v1/comments/{comment.id}",
            headers=headers,
        )
        assert delete.status_code == 404

        upload = await httpx_client.post(
            f"/api/v1/annotations/{annotation.id}/comment-attachments/upload-init",
            json={"file_name": "outside.txt", "content_type": "text/plain"},
            headers=headers,
        )
        assert upload.status_code == 404
        download = await httpx_client.get(
            f"/api/v1/annotations/{annotation.id}/comment-attachments/download",
            params={"key": f"comment-attachments/{annotation.id}/file.png"},
            headers=headers,
        )
        assert download.status_code == 404
