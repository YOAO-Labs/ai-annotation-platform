from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from aap_labeling.discussion_contract import (
    DISCUSSION_PROTOCOL,
    TASK_EVENT_PROTOCOL,
    CreateAnnotationComment,
    InsertTaskEvents,
    LabelingDiscussionUnavailable,
    LabelingTaskEventsUnavailable,
    ReadTaskEvents,
    TaskEventInsertRow,
    decode_discussion_command,
    discussion_command_json,
)
from aap_labeling.discussion_data import LabelingDiscussionDataClient
from aap_labeling.discussion_data import LabelingTaskEventDataClient


@pytest.mark.asyncio
async def test_comment_command_keeps_json_nulls_and_binds_returned_row():
    now = datetime.now(timezone.utc)
    request = CreateAnnotationComment(
        schema=DISCUSSION_PROTOCOL,
        operation="create_comment",
        comment_id=uuid.uuid4(),
        annotation_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        author_id=uuid.uuid4(),
        body="visible note",
        mentions=[],
        attachments=[],
        canvas_drawing=None,
        anchor=None,
    )

    async def execute(payload):
        echo = json.loads(payload)
        assert echo["canvas_drawing"] is None
        assert echo["anchor"] is None
        return {
            "schema": DISCUSSION_PROTOCOL,
            "ok": True,
            "request": echo,
            "data": {
                "comment": {
                    "id": str(echo["comment_id"]),
                    "annotation_id": echo["annotation_id"],
                    "project_id": echo["project_id"],
                    "author_id": echo["author_id"],
                    "body": echo["body"],
                    "is_resolved": False,
                    "is_active": True,
                    "mentions": echo["mentions"],
                    "attachments": echo["attachments"],
                    "canvas_drawing": echo["canvas_drawing"],
                    "anchor": echo["anchor"],
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }
            },
        }

    data = await LabelingDiscussionDataClient(execute).command(request)
    assert data.comment is not None
    assert data.comment.id == request.comment_id
    assert data.comment.canvas_drawing is None
    assert data.comment.anchor is None

    unknown = json.loads(discussion_command_json(request))
    unknown["skip_authorization"] = True
    with pytest.raises(LabelingDiscussionUnavailable):
        decode_discussion_command(unknown)


@pytest.mark.asyncio
async def test_task_event_contract_binds_ids_and_closed_insert_payload():
    event_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    started_at = now - timedelta(seconds=2)
    ended_at = started_at + timedelta(seconds=1)
    row = TaskEventInsertRow(
        id=event_id,
        task_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        kind="annotate",
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=1000,
        annotation_count=1,
        was_rejected=False,
        collector_version="session-v2",
        collection_source="session",
        collection_coverage="qualified",
    )
    request = InsertTaskEvents(
        schema=TASK_EVENT_PROTOCOL,
        operation="insert",
        rows=[row],
    )

    async def execute(payload):
        return {
            "schema": TASK_EVENT_PROTOCOL,
            "ok": True,
            "request": json.loads(payload),
            "data": {"inserted": 1, "conflicts": []},
        }

    result = await LabelingTaskEventDataClient(execute).command(request)
    assert result.inserted == 1

    read = ReadTaskEvents(
        schema=TASK_EVENT_PROTOCOL,
        operation="read_ids",
        event_ids=[event_id],
    )

    async def wrong_id(payload):
        return {
            "schema": TASK_EVENT_PROTOCOL,
            "ok": True,
            "request": json.loads(payload),
            "data": {
                "event_rows": [
                    {
                        **row.model_dump(mode="json"),
                        "id": str(uuid.uuid4()),
                        "created_at": now.isoformat(),
                        "reject_reason_type": None,
                    }
                ]
            },
        }

    with pytest.raises(LabelingTaskEventsUnavailable):
        await LabelingTaskEventDataClient(wrong_id).command(read)
