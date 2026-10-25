from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_labeling.discussion_contract import AnnotationCommentRow, AnnotationFeedbackRow
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.annotation_feedback import AnnotationFeedback
from app.services.annotation_comment import AnnotationCommentService
from app.services.feedback import FeedbackService


class _ProjectionSession(AsyncSession):
    async def scalar(self, statement, *args, **kwargs):
        self.refresh_statement = statement
        return self.refresh_value


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["comment", "feedback"])
async def test_native_rows_stay_session_bound_and_refresh_through_public_projection(
    kind,
):
    session = _ProjectionSession()
    now = datetime.now(timezone.utc)
    if kind == "comment":
        row = AnnotationCommentRow(
            id=uuid.uuid4(),
            annotation_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            author_id=uuid.uuid4(),
            body="before",
            is_resolved=False,
            is_active=True,
            mentions=[],
            attachments=[],
            canvas_drawing=None,
            anchor=None,
            created_at=now,
            updated_at=now,
        )
        service = AnnotationCommentService(session)
        attach, refresh = service._attach, service.refresh
        model_type = AnnotationComment
        relation = "aap_labeling_v1.annotation_comment_facts"
    else:
        row = AnnotationFeedbackRow(
            id=uuid.uuid4(),
            kind="comment",
            anchor_type="task",
            project_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            annotation_id=None,
            anchor_position=None,
            status="open",
            severity=None,
            title=None,
            body="before",
            author_id=uuid.uuid4(),
            attachments=[],
            mentions=[],
            canvas_drawing=None,
            thread_parent_id=None,
            is_active=True,
            resolved_at=None,
            resolved_by_id=None,
            created_at=now,
            updated_at=now,
        )
        service = FeedbackService(session)
        attach, refresh = service._attach, service.refresh
        model_type = AnnotationFeedback
        relation = "aap_labeling_v1.annotation_feedback_facts"

    loaded = await attach(row)
    assert inspect(loaded).session is session.sync_session
    updated = await attach(row.model_copy(update={"body": "after"}))
    assert updated is loaded
    assert updated.body == "after"

    session.refresh_value = updated
    refreshed = await refresh(updated)
    assert refreshed is updated
    assert isinstance(refreshed, model_type)
    assert (
        session.refresh_statement.get_execution_options().get("populate_existing")
        is True
    )
    sql = str(session.refresh_statement)
    assert relation in sql
    assert "public.annotation_comments" not in sql
    assert "public.annotation_feedbacks" not in sql
    session.refresh_value = None
    with pytest.raises(InvalidRequestError):
        await refresh(updated)
    await session.close()
