import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aap_labeling.video_control_contract import (
    VIDEO_CONTROL_FUNCTION,
    VIDEO_SEGMENT_LIFECYCLE_FUNCTION,
    ClaimVideoSegment,
    CheckReviewScopes,
    PrepareVideoSegments,
    LockWorkerJob,
    ReadJob,
    ReadWorkerJob,
    ReadWorkerSegment,
    StartWorkerJob,
    VideoControlUnavailable,
    decode_video_control_reply,
    decode_video_control_command,
    video_control_schema,
)
from aap_labeling.video_control_data import VideoControlDataClient


def _request():
    return ReadJob(
        schema="aap-labeling-video-control/v1",
        operation="read_job",
        job_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
    )


def _job_data(job_id):
    return {
        "id": str(job_id),
        "task_id": str(uuid.uuid4()),
        "dataset_item_id": str(uuid.uuid4()),
        "annotation_id": None,
        "segment_id": None,
        "target_class_name": None,
        "target_tool_unit_id": None,
        "created_by": str(uuid.uuid4()),
        "status": "queued",
        "job_kind": "tracking",
        "track_id_snapshot": None,
        "correction_frame": None,
        "model_key": "mock_bbox",
        "direction": "forward",
        "from_frame": 0,
        "to_frame": 1,
        "prompt": {},
        "staged_result": None,
        "revision": 1,
        "event_channel": f"video-tracker-job:{job_id}",
        "celery_task_id": None,
        "cancel_requested_at": None,
        "started_at": None,
        "completed_at": None,
        "error_message": None,
        "created_at": "2026-10-25T00:00:00Z",
        "updated_at": "2026-10-25T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_video_control_reply_is_bound_to_the_exact_request():
    request = _request()
    payloads = []

    async def execute(payload):
        payloads.append(payload)
        return {
            "schema": "aap-labeling-video-control/v1",
            "ok": True,
            "request": json.loads(payload),
            "data": _job_data(request.job_id),
        }

    result = await VideoControlDataClient(execute).command(request)
    assert result["id"] == request.job_id
    assert len(payloads) == 1
    assert decode_video_control_command(payloads[0]) == request

    async def mismatched(_payload):
        return {
            "schema": "aap-labeling-video-control/v1",
            "ok": True,
            "request": json.loads(request.model_dump_json(by_alias=True))
            | {"job_id": str(uuid.uuid4())},
            "data": {},
        }

    with pytest.raises(VideoControlUnavailable):
        await VideoControlDataClient(mismatched).command(request)


def test_video_control_command_rejects_untyped_authority_fields():
    raw = json.loads(_request().model_dump_json(by_alias=True))
    raw["authorized"] = True
    with pytest.raises(VideoControlUnavailable):
        decode_video_control_command(raw)


@pytest.mark.asyncio
async def test_worker_commands_bind_nullable_job_and_segment_rows():
    job_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    request = ReadWorkerJob(
        schema="aap-labeling-video-control/v1",
        operation="read_worker_job",
        job_id=job_id,
    )
    missing = {
        "schema": "aap-labeling-video-control/v1",
        "ok": True,
        "request": request.model_dump(mode="json", by_alias=True),
        "data": None,
    }
    assert decode_video_control_reply(request, missing) is None

    lock = LockWorkerJob(
        schema="aap-labeling-video-control/v1",
        operation="lock_worker_job",
        job_id=job_id,
    )
    locked = decode_video_control_reply(
        lock,
        {
            **missing,
            "request": lock.model_dump(mode="json", by_alias=True),
            "data": _job_data(job_id),
        },
    )
    assert locked["id"] == job_id

    start = StartWorkerJob(
        schema="aap-labeling-video-control/v1",
        operation="start_worker_job",
        job_id=job_id,
        celery_task_id="celery-video-worker",
        started_at=now,
    )
    with pytest.raises(VideoControlUnavailable):
        decode_video_control_reply(
            start,
            {
                **missing,
                "request": start.model_dump(mode="json", by_alias=True),
                "data": _job_data(uuid.uuid4()),
            },
        )

    segment_request = ReadWorkerSegment(
        schema="aap-labeling-video-control/v1",
        operation="read_worker_segment",
        job_id=job_id,
    )
    segment_id = uuid.uuid4()
    segment = {
        "id": str(segment_id),
        "dataset_item_id": str(uuid.uuid4()),
        "segment_index": 2,
        "start_frame": 100,
        "end_frame": 199,
        "assignee_id": None,
        "status": "locked",
        "locked_by": str(uuid.uuid4()),
        "locked_at": now.isoformat(),
        "lock_expires_at": now.isoformat(),
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    snapshot = decode_video_control_reply(
        segment_request,
        {
            **missing,
            "request": segment_request.model_dump(mode="json", by_alias=True),
            "data": {"segment": segment, "segment_count": 4},
        },
    )
    assert snapshot["segment"]["id"] == segment_id
    assert snapshot["segment_count"] == 4
    missing_segment = decode_video_control_reply(
        segment_request,
        {
            **missing,
            "request": segment_request.model_dump(mode="json", by_alias=True),
            "data": {"segment": None, "segment_count": 0},
        },
    )
    assert missing_segment == {"segment": None, "segment_count": 0}


def test_review_scope_reply_has_a_closed_typed_projection():
    request = CheckReviewScopes(
        schema="aap-labeling-video-control/v1",
        operation="check_review_scopes",
        task_id=uuid.uuid4(),
        actor_id=uuid.uuid4(),
        annotation_id=uuid.uuid4(),
        result_annotation_version=3,
        from_frame=10,
        to_frame=12,
        frame_indices=(11,),
    )
    payload = request.model_dump(mode="json", by_alias=True)
    scope_id = uuid.uuid4()
    reply = {
        "schema": "aap-labeling-video-control/v1",
        "ok": True,
        "request": payload,
        "data": {
            "scopes": [
                {
                    "scope_id": str(scope_id),
                    "frame_start": 11,
                    "frame_end": 11,
                    "region_mask_ref": {"sha256": "rle-ref"},
                    "region_digest": "a" * 64,
                    "decision": "accept",
                }
            ]
        },
    }

    scopes = decode_video_control_reply(request, reply)
    assert scopes[0].scope_id == scope_id
    assert scopes[0].decision == "accept"

    reply["data"]["scopes"][0]["project_id"] = str(uuid.uuid4())
    with pytest.raises(VideoControlUnavailable):
        decode_video_control_reply(request, reply)


def test_video_control_schema_snapshots_match_the_closed_contract():
    root = Path(__file__).resolve().parents[3]
    expected = video_control_schema()
    for snapshot in (
        root / "apps/labeling/src/aap_labeling/video-control.schema.json",
        root / "apps/api/alembic/sql/0360_labeling_video_segment_lifecycle.schema.json",
    ):
        assert json.loads(snapshot.read_text()) == expected
    assert expected["x-postgresql-functions"] == [
        VIDEO_CONTROL_FUNCTION,
        VIDEO_SEGMENT_LIFECYCLE_FUNCTION,
    ]
    assert (
        root / "apps/api/alembic/sql/0360_labeling_video_segment_lifecycle.sql"
    ).read_bytes() == (
        root
        / "apps/labeling/src/aap_labeling/sql/0360_labeling_video_segment_lifecycle.sql"
    ).read_bytes()


@pytest.mark.asyncio
async def test_segment_preparation_and_lifecycle_replies_are_closed_and_bound():
    actor_id = uuid.uuid4()
    item_id = uuid.uuid4()
    segment_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    segment = {
        "id": str(segment_id),
        "dataset_item_id": str(item_id),
        "segment_index": 0,
        "start_frame": 0,
        "end_frame": 179,
        "assignee_id": str(actor_id),
        "status": "locked",
        "locked_by": str(actor_id),
        "locked_at": now.isoformat(),
        "lock_expires_at": now.isoformat(),
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    preparation = PrepareVideoSegments(
        schema="aap-labeling-video-control/v1",
        operation="prepare_segments",
        task_id=None,
        project_id=None,
        actor_id=actor_id,
        dataset_item_id=item_id,
        frame_count=180,
        segment_size_frames=180,
        now=now,
    )

    async def prepared(payload):
        return {
            "schema": "aap-labeling-video-control/v1",
            "ok": True,
            "request": json.loads(payload),
            "data": {"segments": [segment], "segment_count": 1},
        }

    prepared_result = await VideoControlDataClient(prepared).command(preparation)
    assert prepared_result["segments"][0]["id"] == segment_id
    assert prepared_result["segments"][0]["dataset_item_id"] == item_id

    claim = ClaimVideoSegment(
        schema="aap-labeling-video-control/v1",
        operation="claim_segment",
        privileged=False,
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        actor_id=actor_id,
        expected_batch_id=None,
        expected_task_status="in_progress",
        expected_review_round_id=None,
        dataset_item_id=item_id,
        segment_id=segment_id,
        now=now,
        lock_ttl_seconds=300,
    )

    async def claimed(payload):
        return {
            "schema": "aap-labeling-video-control/v1",
            "ok": True,
            "request": json.loads(payload),
            "data": {"segment": segment, "changed": True},
        }

    result = await VideoControlDataClient(claimed).command(claim)
    assert result["segment"]["id"] == segment_id
    assert result["changed"] is True

    async def mismatched(payload):
        return {
            "schema": "aap-labeling-video-control/v1",
            "ok": True,
            "request": json.loads(payload),
            "data": {
                "segment": segment | {"id": str(uuid.uuid4())},
                "changed": True,
            },
        }

    with pytest.raises(VideoControlUnavailable):
        await VideoControlDataClient(mismatched).command(claim)
