import json
import uuid

import pytest

from aap_labeling.video_control_contract import (
    CheckReviewScopes,
    ReadJob,
    VideoControlUnavailable,
    decode_video_control_reply,
    decode_video_control_command,
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
