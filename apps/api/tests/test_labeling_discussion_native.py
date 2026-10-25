"""Saved owned-role proof for Labeling discussion and task-event capabilities."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_labeling.discussion_contract import (
    DISCUSSION_PROTOCOL,
    TASK_EVENT_PROTOCOL,
    CreateAnnotationComment,
    InsertTaskEvents,
    LockAnnotationComment,
    LockAnnotationFeedbackLineage,
    MirrorAnnotationCommentFeedback,
    ReadTaskEvents,
    SoftDeleteAnnotationComment,
    TaskEventInsertRow,
)
from aap_labeling.discussion_data import (
    LabelingDiscussionDataClient,
    LabelingTaskEventDataClient,
)
from app.db.models.annotation import Annotation
from tests.factory import create_project, create_task, create_user
from tests.test_labeling_class_usage_reads_legacy import _cleanup_owned
from tests.workspace_project_query_helpers import _dsn

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable Labeling discussion capabilities",
    ),
]


async def test_discussion_and_task_event_owner_keep_rows_and_retry_semantics(
    test_engine,
):
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    task_ids: list[uuid.UUID] = []
    connection = None
    comment_id = uuid.uuid4()
    feedback_id = uuid.uuid4()
    event_id = uuid.uuid4()
    try:
        async with maker.begin() as db:
            actor = await create_user(
                db,
                "project_admin",
                f"labeling-discussion-{uuid.uuid4().hex}@example.test",
                "Labeling discussion native owner",
            )
            actor_ids.append(actor.id)
            project = await create_project(db, owner_id=actor.id)
            project_ids.append(project.id)
            task = await create_task(db, project_id=project.id, status="in_progress")
            task_ids.append(task.id)
            annotation = Annotation(
                task_id=task.id,
                project_id=project.id,
                user_id=actor.id,
                class_name="object",
                geometry={"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                attributes={},
            )
            db.add(annotation)
            await db.flush()
            annotation_id = annotation.id

        connection = await asyncpg.connect(
            _dsn(
                test_engine,
                "AAP_MODULE_DATA_LABELING_DISCUSSION_URL",
                "labeling_discussion_client",
            ),
            timeout=5,
            command_timeout=10,
        )
        assert await connection.fetchval("SELECT current_user") == (
            "labeling_discussion_client"
        )
        assert await connection.fetchval(
            "SELECT has_function_privilege(current_user, "
            "'aap_labeling_v1.discussion_command(jsonb)', 'EXECUTE')"
        )
        assert await connection.fetchval(
            "SELECT has_function_privilege(current_user, "
            "'aap_labeling_v1.task_event_command(jsonb)', 'EXECUTE')"
        )
        for table in (
            "public.annotation_comments",
            "public.annotation_feedbacks",
            "public.task_events",
        ):
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert not await connection.fetchval(
                    "SELECT has_table_privilege(current_user, $1, $2)",
                    table,
                    privilege,
                )

        async def discussion_call(payload: str):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.discussion_command($1::jsonb)", payload
            )

        async def event_call(payload: str):
            return await connection.fetchval(
                "SELECT aap_labeling_v1.task_event_command($1::jsonb)", payload
            )

        discussions = LabelingDiscussionDataClient(discussion_call)
        events = LabelingTaskEventDataClient(event_call)
        now = datetime.now(timezone.utc)
        started_at = now - timedelta(seconds=2)
        event = TaskEventInsertRow(
            id=event_id,
            task_id=task_ids[0],
            user_id=actor_ids[0],
            project_id=project_ids[0],
            kind="annotate",
            started_at=started_at,
            ended_at=started_at + timedelta(seconds=1),
            duration_ms=1000,
            annotation_count=1,
            was_rejected=False,
            collector_version="session-v2",
            collection_source="session",
            collection_coverage="qualified",
        )

        transaction = connection.transaction()
        await transaction.start()
        comment = await discussions.command(
            CreateAnnotationComment(
                schema=DISCUSSION_PROTOCOL,
                operation="create_comment",
                comment_id=comment_id,
                annotation_id=annotation_id,
                project_id=project_ids[0],
                author_id=actor_ids[0],
                body="native comment",
                mentions=[],
                attachments=[],
                canvas_drawing=None,
                anchor=None,
            )
        )
        assert comment.comment is not None
        mirror = await discussions.command(
            MirrorAnnotationCommentFeedback(
                schema=DISCUSSION_PROTOCOL,
                operation="mirror_annotation_comment",
                feedback_id=feedback_id,
                comment_id=comment_id,
                annotation_id=annotation_id,
                task_id=task_ids[0],
                project_id=project_ids[0],
                author_id=actor_ids[0],
                body="native comment",
                attachments=[],
                mentions=[],
            )
        )
        assert mirror.feedback is not None
        locked_lineage = await discussions.command(
            LockAnnotationFeedbackLineage(
                schema=DISCUSSION_PROTOCOL,
                operation="lock_feedback_lineage",
                feedback_ids=[feedback_id],
            )
        )
        assert locked_lineage.feedbacks is not None
        assert [row.id for row in locked_lineage.feedbacks] == [feedback_id]
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM aap_labeling_v1.annotation_comment_facts "
                "WHERE id = $1",
                comment_id,
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM aap_labeling_v1.annotation_feedback_facts "
                "WHERE id = $1",
                feedback_id,
            )
            == 1
        )

        inserted = await events.command(
            InsertTaskEvents(
                schema=TASK_EVENT_PROTOCOL, operation="insert", rows=[event]
            )
        )
        assert inserted.inserted == 1
        retry = await events.command(
            InsertTaskEvents(
                schema=TASK_EVENT_PROTOCOL, operation="insert", rows=[event]
            )
        )
        assert (retry.inserted, retry.conflicts) == (0, [])
        conflict = await events.command(
            InsertTaskEvents(
                schema=TASK_EVENT_PROTOCOL,
                operation="insert",
                rows=[event.model_copy(update={"annotation_count": 2})],
            )
        )
        assert (conflict.inserted, conflict.conflicts) == (0, [event_id])
        event_read = await events.command(
            ReadTaskEvents(
                schema=TASK_EVENT_PROTOCOL,
                operation="read_ids",
                event_ids=[event_id],
            )
        )
        assert event_read.event_rows
        comment_lock = await discussions.command(
            LockAnnotationComment(
                schema=DISCUSSION_PROTOCOL,
                operation="lock_comment",
                comment_id=comment_id,
            )
        )
        assert comment_lock.comment is not None
        await discussions.command(
            SoftDeleteAnnotationComment(
                schema=DISCUSSION_PROTOCOL,
                operation="soft_delete_comment",
                comment_id=comment_id,
            )
        )
        assert (
            await connection.fetchval(
                "SELECT is_active FROM aap_labeling_v1.annotation_comment_facts "
                "WHERE id = $1",
                comment_id,
            )
            is False
        )
        await transaction.rollback()
    finally:
        if connection is not None:
            await connection.close()
        await _cleanup_owned(
            maker,
            actor_ids=actor_ids,
            project_ids=project_ids,
            task_ids=task_ids,
        )
