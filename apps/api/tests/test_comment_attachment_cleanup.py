from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models.annotation import Annotation
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.project import Project
from app.db.models.task import Task
from app.services.annotation_comment import AnnotationCommentService
from tests.factory import build_tool_bindings

pytestmark = pytest.mark.asyncio


async def _seed_annotation(db, author_id: uuid.UUID) -> Annotation:
    suffix = uuid.uuid4().hex[:8]
    project = Project(
        id=uuid.uuid4(),
        display_id=f"P-CA-{suffix}",
        name="comment attachment cleanup",
        type_label="图像-检测",
        type_key="image-det",
        owner_id=author_id,
        tool_bindings=build_tool_bindings(["car"]),
    )
    db.add(project)
    await db.flush()

    task = Task(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"T-CA-{suffix}",
        file_name="x.jpg",
        file_path="x.jpg",
        file_type="image",
        status="in_progress",
    )
    db.add(task)
    await db.flush()

    annotation = Annotation(
        id=uuid.uuid4(),
        task_id=task.id,
        project_id=project.id,
        user_id=author_id,
        annotation_type="bbox",
        class_name="car",
        geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        confidence=1.0,
        is_active=True,
    )
    db.add(annotation)
    await db.flush()
    return annotation


def _comment(
    annotation_id: uuid.UUID,
    author_id: uuid.UUID,
    *,
    updated_at: datetime,
    attachments: list[dict] | None = None,
    is_active: bool = False,
) -> AnnotationComment:
    return AnnotationComment(
        id=uuid.uuid4(),
        annotation_id=annotation_id,
        author_id=author_id,
        body="cleanup fixture",
        is_active=is_active,
        attachments=attachments if attachments is not None else [],
        updated_at=updated_at,
    )


async def test_expired_comment_selection_uses_strict_cutoff_and_clears_one_row(
    db_session, super_admin
):
    author, _ = super_admin
    annotation = await _seed_annotation(db_session, author.id)
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    older = now - timedelta(days=7, microseconds=1)
    cutoff = now - timedelta(days=7)
    newer = now - timedelta(days=7) + timedelta(microseconds=1)

    with_key = _comment(
        annotation.id,
        author.id,
        updated_at=older,
        attachments=[{"storage_key": "comment-attachments/old.png"}],
    )
    empty = _comment(annotation.id, author.id, updated_at=older)
    malformed = _comment(
        annotation.id,
        author.id,
        updated_at=older,
        attachments=[{"storageKey": "comment-attachments/legacy-casing.png"}, None],
    )
    active = _comment(
        annotation.id,
        author.id,
        updated_at=older,
        is_active=True,
        attachments=[{"storage_key": "comment-attachments/active.png"}],
    )
    at_cutoff = _comment(
        annotation.id,
        author.id,
        updated_at=cutoff,
        attachments=[{"storage_key": "comment-attachments/boundary.png"}],
    )
    too_new = _comment(
        annotation.id,
        author.id,
        updated_at=newer,
        attachments=[{"storage_key": "comment-attachments/new.png"}],
    )
    db_session.add_all([with_key, empty, malformed, active, at_cutoff, too_new])
    await db_session.flush()

    service = AnnotationCommentService(db_session)
    selected = await service.select_expired_attachment_comments(now=now)
    assert {comment.id for comment in selected} == {
        with_key.id,
        empty.id,
        malformed.id,
    }

    await service.clear_attachments(with_key.id)
    stored = await db_session.scalar(
        select(AnnotationComment.attachments).where(AnnotationComment.id == with_key.id)
    )
    assert stored == []
    assert await db_session.scalar(
        select(AnnotationComment.attachments).where(
            AnnotationComment.id == malformed.id
        )
    ) == [{"storageKey": "comment-attachments/legacy-casing.png"}, None]


async def test_comment_selection_caps_all_expired_rows_at_500(db_session, super_admin):
    author, _ = super_admin
    annotation = await _seed_annotation(db_session, author.id)
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    expired = now - timedelta(days=7, microseconds=1)
    rows = [
        _comment(
            annotation.id,
            author.id,
            updated_at=expired,
            attachments=(
                [{"storage_key": f"comment-attachments/{index}.png"}]
                if index == 500
                else []
            ),
        )
        for index in range(501)
    ]
    db_session.add_all(rows)
    await db_session.flush()

    selected = await AnnotationCommentService(
        db_session
    ).select_expired_attachment_comments(now=now)
    assert len(selected) == 500
    assert all(not comment.is_active for comment in selected)
    assert all(comment.updated_at < now - timedelta(days=7) for comment in selected)
