"""Business hard deletes must commit independent attachment retirement atomically."""

from datetime import datetime, timedelta, timezone
import hashlib
from unittest.mock import Mock
import uuid

import pytest
from sqlalchemy import delete, select

from app.db.models.annotation import Annotation
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.comment_attachment_upload import CommentAttachmentUpload
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.project import Project
from app.db.models.task import Task
from app.services import comment_attachment_storage as storage
from app.services.dataset import DatasetService
from app.services.feedback import FeedbackService
from app.services.project_delete import delete_project_records
from tests.factory import create_project, create_task


async def world(db, actor, *, linked=False, project=None):
    project = project or await create_project(db, owner_id=actor.id)
    task = await create_task(db, project_id=project.id)
    dataset = None
    if linked:
        dataset = Dataset(
            display_id="DS-" + uuid.uuid4().hex[:12],
            name="Retirement fixture",
            created_by=actor.id,
        )
        db.add(dataset)
        await db.flush()
        item = DatasetItem(
            dataset_id=dataset.id, file_name="source.jpg", file_path="test/source.jpg"
        )
        db.add(item)
        db.add(ProjectDataset(project_id=project.id, dataset_id=dataset.id))
        await db.flush()
        task.dataset_item_id = item.id
    annotation = Annotation(
        task_id=task.id,
        project_id=project.id,
        user_id=actor.id,
        source="manual",
        annotation_type="bbox",
        tool_unit_id="bbox",
        class_name="car",
        geometry={"x": 1, "y": 1, "width": 2, "height": 2},
    )
    db.add(annotation)
    await db.flush()
    comment = AnnotationComment(
        annotation_id=annotation.id,
        project_id=project.id,
        author_id=actor.id,
        body="Own a general file",
        attachments=[],
    )
    db.add(comment)
    await db.flush()
    created = datetime.now(timezone.utc)
    owner = CommentAttachmentUpload(
        id=uuid.uuid4(),
        owner_id=actor.id,
        annotation_id=annotation.id,
        task_id=task.id,
        project_id=project.id,
        bound_comment_id=comment.id,
        state="ready",
        sha256=hashlib.sha256(b"").hexdigest(),
        size_bytes=0,
        mime_type="application/octet-stream",
        file_name="empty.dat",
        created_at=created,
        expires_at=created + timedelta(hours=2160),
    )
    owner.storage_ref = await storage.allocate(db, owner)
    db.add(owner)
    await db.flush()
    comment.attachments = [
        {
            "storageKey": storage.resolve(owner).object_key,
            "fileName": owner.file_name,
            "mimeType": owner.mime_type,
            "size": 0,
        }
    ]
    await db.flush()
    await FeedbackService(db).mirror_annotation_comment(comment, task_id=task.id)
    return project, dataset, comment.id, owner.id, owner.storage_ref


@pytest.mark.parametrize("operation", ["project", "unlink", "orphan"])
async def test_hard_delete_keeps_exact_owner_and_retires_before_losing_comment(
    db_session, super_admin, httpx_client, monkeypatch, operation
):
    actor, token = super_admin
    project, dataset, comment_id, owner_id, saved_ref = await world(
        db_session, actor, linked=operation == "unlink"
    )
    physical_delete = Mock()
    monkeypatch.setattr(
        storage.storage_service.client, "delete_object", physical_delete
    )
    if operation == "project":
        await delete_project_records(db_session, project)
    elif operation == "unlink":
        result = await DatasetService(db_session).unlink_project(dataset.id, project.id)
        assert result["deleted_tasks"] == 1
    else:
        response = await httpx_client.post(
            f"/api/v1/projects/{project.id}/orphan-tasks/cleanup",
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status_code == 200, response.text
        assert response.json()["deleted_tasks"] == 1
    await db_session.flush()
    owner = await db_session.scalar(
        select(CommentAttachmentUpload)
        .where(CommentAttachmentUpload.id == owner_id)
        .execution_options(populate_existing=True)
    )
    assert (
        owner is not None and owner.state == "retired" and owner.retired_at is not None
    )
    assert owner.bound_comment_id == comment_id and owner.storage_ref == saved_ref
    assert (
        await db_session.scalar(
            select(AnnotationComment.id).where(AnnotationComment.id == comment_id)
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(AnnotationFeedback.id).where(
                AnnotationFeedback.project_id == project.id
            )
        )
        is None
    )
    physical_delete.assert_not_called()


async def test_project_deletion_rollback_also_rolls_back_retirement(
    db_session, super_admin
):
    actor, _ = super_admin
    project, _, comment_id, owner_id, saved_ref = await world(db_session, actor)
    project_id = project.id
    with pytest.raises(RuntimeError, match="rollback"):
        async with db_session.begin_nested():
            await delete_project_records(db_session, project)
            raise RuntimeError("rollback")
    owner = await db_session.scalar(
        select(CommentAttachmentUpload)
        .where(CommentAttachmentUpload.id == owner_id)
        .execution_options(populate_existing=True)
    )
    assert (
        owner.state == "ready"
        and owner.retired_at is None
        and owner.storage_ref == saved_ref
    )
    assert await db_session.get(AnnotationComment, comment_id) is not None
    assert await db_session.get(Project, project_id) is not None


async def test_orphan_cleanup_does_not_expand_after_freezing_targets(
    db_session, super_admin, httpx_client, monkeypatch
):
    actor, token = super_admin
    project, _, _, retired_id, _ = await world(db_session, actor)
    _, dataset, retained_comment, retained_id, _ = await world(
        db_session, actor, linked=True, project=project
    )
    retained_owner = await db_session.get(CommentAttachmentUpload, retained_id)
    retained_task = retained_owner.task_id

    async def membership_changes_after_freeze(db, **kwargs):
        await db.execute(
            delete(ProjectDataset).where(
                ProjectDataset.project_id == project.id,
                ProjectDataset.dataset_id == dataset.id,
            )
        )

    monkeypatch.setattr(
        "app.services.active_learning_pool.guard_source",
        membership_changes_after_freeze,
    )
    response = await httpx_client.post(
        f"/api/v1/projects/{project.id}/orphan-tasks/cleanup",
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 200, response.text
    assert response.json()["deleted_tasks"] == 1
    await db_session.refresh(retained_owner)
    assert retained_owner.state == "ready" and retained_owner.retired_at is None
    assert await db_session.get(Task, retained_task) is not None
    assert await db_session.get(AnnotationComment, retained_comment) is not None
    retired = await db_session.get(CommentAttachmentUpload, retired_id)
    await db_session.refresh(retired)
    assert retired.state == "retired"
