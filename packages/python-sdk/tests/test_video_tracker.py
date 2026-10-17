from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from ai_annotation.errors import ConflictError
from ai_annotation.video_tracker import VideoTracker
from ai_annotation.video_tracker_models import (
    CocoRleMaskContent,
    VideoMaskCorrectionRequest,
    VideoTrackerCreateRequest,
    VideoTrackerDecisionRequest,
    VideoTrackerJob,
    VideoTrackerJobPreview,
    VideoTrackerJobsResponse,
)
from pydantic import ValidationError

from .conftest import API


def _id() -> str:
    return str(uuid4())


def _job(*, task_id: str, job_id: str | None = None, status: str = "pending_review"):
    return {
        "id": job_id or _id(),
        "task_id": task_id,
        "dataset_item_id": _id(),
        "annotation_id": None,
        "touched_annotation_ids": None,
        "segment_id": None,
        "created_by": _id(),
        "status": status,
        "job_kind": "tracking",
        "track_id_snapshot": None,
        "correction_frame": None,
        "revision": 3,
        "review_replayed": False,
        "model_key": "sam3_video_custom",
        "direction": "forward",
        "from_frame": 1,
        "to_frame": 4,
        "prompt": {"touched_annotation_ids": [_id()]},
        "event_channel": "video-tracker:test",
        "celery_task_id": None,
        "cancel_requested_at": None,
        "started_at": None,
        "completed_at": None,
        "error_message": None,
        "created_at": "2026-10-17T00:00:00Z",
        "updated_at": None,
    }


def _correction_request() -> VideoMaskCorrectionRequest:
    return VideoMaskCorrectionRequest(
        correction_frame=5,
        from_frame=4,
        to_frame=8,
        model_key="sam3_video_custom",
        model_id="sam3-v2",
        backend_id=uuid4(),
        direction="forward",
        source_annotation_version=7,
        corrected_mask_digest="a" * 64,
        text="red car",
    )


def _resource(client) -> VideoTracker:
    return VideoTracker(client._http)


def test_tracker_admin_list_filters_and_cursor_response(client, respx_mock):
    job_id, task_id, project_id = _id(), _id(), _id()
    item = {
        "id": job_id,
        "task_id": task_id,
        "project_id": project_id,
        "project_name": "project",
        "project_display_id": "P-1",
        "dataset_item_id": _id(),
        "annotation_id": None,
        "segment_id": None,
        "created_by": _id(),
        "status": "pending_review",
        "model_key": "sam3_video_custom",
        "direction": "forward",
        "from_frame": 1,
        "to_frame": 4,
        "error_message": None,
        "started_at": None,
        "completed_at": None,
        "created_at": "2026-10-17T00:00:00Z",
    }
    route = respx_mock.get(f"{API}/video-tracker-jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [item],
                "next_cursor": "next-page",
                "counts": {"pending_review": 2, "completed": 1},
            },
        )
    )

    result = _resource(client).list_jobs(
        project_id=project_id,
        status="pending_review",
        model_key="sam3_video_custom",
        cursor="previous-page",
        limit=9,
    )

    assert isinstance(result, VideoTrackerJobsResponse)
    assert str(result.items[0].id) == job_id
    assert result.counts.pending_review == 2
    assert dict(route.calls.last.request.url.params) == {
        "project_id": project_id,
        "status": "pending_review",
        "model_key": "sam3_video_custom",
        "cursor": "previous-page",
        "limit": "9",
    }


def test_tracker_job_read_mutations_and_mask_json(client, respx_mock):
    task_id, job_id = _id(), _id()
    job = _job(task_id=task_id, job_id=job_id)
    get = respx_mock.get(f"{API}/video-tracker-jobs/{job_id}").mock(
        return_value=httpx.Response(200, json=job)
    )
    cancel = respx_mock.delete(f"{API}/video-tracker-jobs/{job_id}").mock(
        return_value=httpx.Response(200, json={**job, "status": "cancelled"})
    )
    accept = respx_mock.post(f"{API}/video-tracker-jobs/{job_id}/accept").mock(
        return_value=httpx.Response(200, json={**job, "status": "accepted"})
    )
    discard = respx_mock.post(f"{API}/video-tracker-jobs/{job_id}/discard").mock(
        return_value=httpx.Response(200, json={**job, "status": "discarded"})
    )
    preview = {
        "job_id": job_id,
        "status": "pending_review",
        "annotation_id": None,
        "job_kind": "tracking",
        "correction_frame": None,
        "direction": "forward",
        "from_frame": 1,
        "to_frame": 4,
        "results": [
            {
                "frame_index": 1,
                "geometry": {"type": "bbox", "x": 0.1},
                "candidate_key": "candidate-1",
                "geometry_digest": "sha256:" + "b" * 64,
            }
        ],
        "expected_source_versions": {},
    }
    respx_mock.get(f"{API}/video-tracker-jobs/{job_id}/preview").mock(
        return_value=httpx.Response(200, json=preview)
    )
    sha256 = "c" * 64
    mask = respx_mock.get(
        f"{API}/video-tracker-jobs/{job_id}/mask-content/{sha256}"
    ).mock(
        return_value=httpx.Response(
            200, json={"encoding": "coco_rle", "size": [1, 2], "counts": [1, 1]}
        )
    )

    resource = _resource(client)
    assert isinstance(resource.get_job(job_id), VideoTrackerJob)
    assert resource.cancel_job(job_id).status == "cancelled"
    assert resource.accept_job(job_id).status == "accepted"
    assert resource.discard_job(job_id).status == "discarded"
    assert isinstance(resource.get_preview(job_id), VideoTrackerJobPreview)
    content = resource.get_mask_content(job_id, sha256)
    assert isinstance(content, CocoRleMaskContent)
    assert content.counts == [1, 1]
    assert all(route.called for route in (get, cancel, accept, discard, mask))


def test_tracker_task_recovery_and_create_workflows(client, respx_mock):
    task_id, annotation_id = _id(), _id()
    job = _job(task_id=task_id)
    active = respx_mock.get(f"{API}/tasks/{task_id}/video/tracker-jobs/active").mock(
        return_value=httpx.Response(200, json=[{**job, "status": "running"}])
    )
    reviewable = respx_mock.get(
        f"{API}/tasks/{task_id}/video/tracker-jobs/reviewable"
    ).mock(return_value=httpx.Response(200, json=[job]))
    tracked = respx_mock.post(f"{API}/tasks/{task_id}/video:track").mock(
        return_value=httpx.Response(202, json=job)
    )
    propagated = respx_mock.post(
        f"{API}/tasks/{task_id}/video/tracks/{annotation_id}:propagate"
    ).mock(return_value=httpx.Response(202, json=job))
    correction = respx_mock.post(
        f"{API}/tasks/{task_id}/video/tracks/{annotation_id}/correction-jobs"
    ).mock(return_value=httpx.Response(202, json={**job, "job_kind": "correction"}))

    resource = _resource(client)
    assert resource.list_active(task_id)[0].status == "running"
    assert resource.list_reviewable(task_id)[0].status == "pending_review"

    track_body = VideoTrackerCreateRequest(
        from_frame=1, to_frame=4, model_key="sam3_video_custom", text="red car"
    )
    result = resource.track(task_id, track_body)
    assert result.status == "pending_review"
    assert json.loads(tracked.calls.last.request.content) == {
        "from_frame": 1,
        "to_frame": 4,
        "model_key": "sam3_video_custom",
        "text": "red car",
    }
    resource.propagate(task_id, annotation_id, track_body)
    assert propagated.called

    correction_body = _correction_request()
    corrected = resource.create_correction(task_id, annotation_id, correction_body)
    assert corrected.job_kind == "correction"
    assert (
        json.loads(correction.calls.last.request.content)["source_annotation_version"]
        == 7
    )
    assert active.called and reviewable.called


def test_decision_requires_one_selector_and_preserves_conflict(client, respx_mock):
    with pytest.raises(ValidationError):
        VideoTrackerDecisionRequest(
            decision="accept", job_revision=1, instance_ids=["car"], from_frame=0
        )

    job_id, source_id = _id(), uuid4()
    route = respx_mock.post(f"{API}/video-tracker-jobs/{job_id}/decisions").mock(
        return_value=httpx.Response(409, json={"detail": {"reason": "stale_source"}})
    )
    body = VideoTrackerDecisionRequest(
        instance_ids=[" car "],
        from_frame=1,
        to_frame=4,
        decision="accept",
        expected_source_versions={source_id: 7},
        job_revision=3,
    )

    with pytest.raises(ConflictError):
        _resource(client).decide_candidates(job_id, body)

    assert len(route.calls) == 1
    assert json.loads(route.calls.last.request.content) == {
        "instance_ids": ["car"],
        "from_frame": 1,
        "to_frame": 4,
        "decision": "accept",
        "expected_source_versions": {str(source_id): 7},
        "job_revision": 3,
    }


def test_tracker_request_models_validate_geometry_and_correction_window():
    with pytest.raises(ValidationError):
        VideoTrackerCreateRequest(
            from_frame=4,
            to_frame=1,
            exemplars=[{"bbox": [0, 0, 2, 1]}],
        )
    with pytest.raises(ValidationError):
        invalid = _correction_request().model_dump()
        invalid["correction_frame"] = 8
        VideoMaskCorrectionRequest.model_validate(invalid)


@pytest.mark.parametrize(
    "kwargs", [{"limit": 0}, {"limit": 101}, {"limit": True}, {"model_key": "x" * 81}]
)
def test_tracker_list_bounds_no_http(client, respx_mock, kwargs):
    with pytest.raises(ValueError):
        client.video_tracker.list_jobs(**kwargs)
    assert not respx_mock.calls


def test_tracker_bad_digest_no_http(client, respx_mock):
    with pytest.raises(ValueError):
        client.video_tracker.get_mask_content(_id(), "../another-resource")
    assert not respx_mock.calls


def test_uncertain_creation_is_not_retried(client, respx_mock):
    task_id = _id()
    route = respx_mock.post(f"{API}/tasks/{task_id}/video:track").mock(
        side_effect=httpx.ReadError("lost acknowledgement")
    )
    with pytest.raises(httpx.ReadError):
        client.video_tracker.track(
            task_id, VideoTrackerCreateRequest(from_frame=1, to_frame=4)
        )
    assert route.call_count == 1


def test_tracker_response_keeps_server_touched_ids_authoritative():
    payload = _job(task_id=_id())
    payload["prompt"]["touched_annotation_ids"] = ["opaque future prompt value"]
    assert VideoTrackerJob.model_validate(payload).touched_annotation_ids is None
