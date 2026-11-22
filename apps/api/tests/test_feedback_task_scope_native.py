from __future__ import annotations

import os

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from app.application import annotation_feedback
from app.application.task_workflow_errors import TaskWorkflowError
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.audit_log import AuditLog
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.schemas.annotation_feedback import AnnotationFeedbackCreate
from app.services.audit_origin_contract import AuditOriginContext
from app.services.feedback import FeedbackService
from app.services.identity_account_lock_data import IdentityAccountLockData
from app.services.original_content_access import visible_task
from tests.factory import create_batch, create_task
from tests.test_workspace_project_authority_locking import _world

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable module-data verifier and actual API LOGIN",
    ),
]


@pytest.fixture
async def feedback_target(test_engine):
    async with _world(test_engine) as (owner, api, world, _url):
        project_id, actor_id = world["project_id"], world["member_id"]
        try:
            async with owner.begin() as db:
                batch = await create_batch(db, project_id=project_id, status="active")
                draft = await create_batch(db, project_id=project_id, status="draft")
                task = await create_task(
                    db, project_id=project_id, status="in_progress"
                )
                task.batch_id = batch.id
                task.file_type = "video"
                task.file_name = "feedback-source.mp4"
            payload = AnnotationFeedbackCreate.model_validate(
                {
                    "kind": "issue",
                    "anchor_type": "pixel",
                    "project_id": project_id,
                    "task_id": task.id,
                    "anchor_position": {"x": 0.25, "y": 0.5, "frame": 3},
                    "body": "Source-frame issue",
                }
            )
            yield owner, api, payload, actor_id, batch.id, draft.id
        finally:
            async with owner.begin() as db:
                await db.execute(text("SET LOCAL app.allow_audit_update = 'true'"))
                await db.execute(delete(AuditLog).where(AuditLog.actor_id == actor_id))
                await db.execute(
                    delete(AnnotationFeedback).where(
                        AnnotationFeedback.project_id == project_id
                    )
                )
                await db.execute(delete(Task).where(Task.project_id == project_id))
                await db.execute(
                    delete(TaskBatch).where(TaskBatch.project_id == project_id)
                )


async def _create(db, payload, actor_id):
    return await annotation_feedback.AnnotationFeedbackWorkflow(db).create(
        payload,
        actor_id=actor_id,
        origin=AuditOriginContext("POST", "/api/v1/feedbacks", None),
    )


async def _feedback_ids(db, task_id):
    return (
        await db.scalars(
            select(AnnotationFeedback.id).where(AnnotationFeedback.task_id == task_id)
        )
    ).all()


async def test_feedback_create_commits_while_same_original_visibility_reader_holds_locks(
    feedback_target,
):
    _owner, api, payload, actor_id, batch_id, _draft_id = feedback_target
    async with api() as reader, api() as writer:
        assert (
            await writer.scalar(text("SELECT current_user")) == "identity_api_consumer"
        )
        for relation in (
            "public.task_batches",
            "public.tasks",
            "public.annotation_feedbacks",
        ):
            assert not await writer.scalar(
                text(
                    "SELECT has_any_column_privilege(current_user, :relation, 'SELECT,UPDATE')"
                ),
                {"relation": relation},
            )
        reader_pid = await reader.scalar(text("SELECT pg_backend_pid()"))
        assert await writer.scalar(text("SELECT pg_backend_pid()")) != reader_pid
        actor = await IdentityAccountLockData(reader).read_actor_status(actor_id)
        assert actor is not None
        task = await visible_task(reader, payload.task_id, actor, lock=True)
        assert task.batch_id == batch_id

        with pytest.raises(DBAPIError) as conflict:
            async with writer.begin_nested():
                await annotation_feedback.lock_batch_rows_for_core_read(
                    writer,
                    [batch_id],
                    project_id=payload.project_id,
                    lock_mode="update",
                    nowait=True,
                )
        assert conflict.value.orig.sqlstate == "55P03"

        result = await _create(writer, payload, actor_id)
        assert result.project_role == "annotator" and not result.is_manager
        entry = result.entry
        assert (entry.task_id, entry.author_id) == (payload.task_id, actor_id)
        assert entry.anchor_position["frame"] == 3

        persisted = await FeedbackService(reader).get(entry.id)
        assert persisted is not None and persisted.body == payload.body
        assert persisted.anchor_position == entry.anchor_position


@pytest.mark.parametrize("target", ["batch", "task"])
async def test_feedback_create_refuses_concurrent_row_edit(feedback_target, target):
    owner, api, payload, actor_id, batch_id, draft_id = feedback_target
    async with owner.begin() as editor, api() as db:
        if target == "batch":
            await editor.execute(
                text("UPDATE public.task_batches SET status='draft' WHERE id=:id"),
                {"id": batch_id},
            )
        else:
            await editor.execute(
                text("UPDATE public.tasks SET batch_id=:batch WHERE id=:id"),
                {"batch": draft_id, "id": payload.task_id},
            )
        await db.execute(text("SET LOCAL lock_timeout = '25ms'"))
        with pytest.raises(TaskWorkflowError) as caught:
            async with db.begin_nested():
                await _create(db, payload, actor_id)
        assert caught.value.status_code == 409
        assert caught.value.detail == {"reason": f"feedback_{target}_busy"}
        assert await db.scalar(text("SHOW lock_timeout")) == "25ms"

    async with api() as db:
        with pytest.raises(HTTPException) as denied:
            await _create(db, payload, actor_id)
        assert denied.value.status_code == 404
        assert denied.value.detail == "Task not found"
    async with owner() as observer:
        assert await _feedback_ids(observer, payload.task_id) == []


async def test_feedback_create_rejects_committed_batch_rebind_after_lookup(
    feedback_target, monkeypatch
):
    owner, api, payload, actor_id, batch_id, draft_id = feedback_target
    lock_batch = annotation_feedback.lock_batch_rows_for_core_read

    async def rebind_after_old_batch_lock(db, batch_ids, **options):
        batches = await lock_batch(db, batch_ids, **options)
        assert [batch.id for batch in batches] == [batch_id]
        async with owner.begin() as editor:
            await editor.execute(text("SET LOCAL lock_timeout = '250ms'"))
            await editor.execute(
                text("UPDATE public.tasks SET batch_id=:batch WHERE id=:id"),
                {"batch": draft_id, "id": payload.task_id},
            )
        return batches

    monkeypatch.setattr(
        annotation_feedback,
        "lock_batch_rows_for_core_read",
        rebind_after_old_batch_lock,
    )
    async with api() as db:
        with pytest.raises(TaskWorkflowError) as caught:
            await _create(db, payload, actor_id)
        assert caught.value.status_code == 409
        assert caught.value.detail == {"reason": "task_target_changed"}
    async with owner() as observer:
        assert await _feedback_ids(observer, payload.task_id) == []
