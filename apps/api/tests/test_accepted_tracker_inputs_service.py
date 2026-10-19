from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone
import uuid

import pytest

from aap_assets_video_inputs import tracker as t
from aap_assets_video_inputs.tracker import TrackerFailure
from app.services.accepted_tracker_inputs import (
    TrackerInputDeferred,
    TrackerVideoInputGateway,
    ensure_tracker_video_input_ready,
)


def _status(*, state="pending", error=None):
    return SimpleNamespace(
        tracker_job_id=uuid.uuid4(),
        input_id=uuid.uuid4(),
        origin_actor_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        file_type="video",
        execution=SimpleNamespace(),
        request_digest="a" * 64,
        prompt_digest="b" * 64,
        status=state,
        plan_digest="c" * 64 if state == "ready" else None,
        error=error,
    )


@pytest.mark.asyncio
async def test_unsealed_tracker_input_defers_same_job_before_inference():
    status = _status()

    class Gateway:
        async def input(self, _request):
            return status

        async def prepare(self, request):
            assert request.input_id == status.input_id
            assert request.request_digest == status.request_digest
            return status

    with pytest.raises(TrackerInputDeferred) as caught:
        await ensure_tracker_video_input_ready(
            Gateway(),
            actor_id=status.origin_actor_id,
            tracker_job_id=status.tracker_job_id,
            task_id=status.task_id,
        )
    assert caught.value.code == "input_not_ready"


@pytest.mark.asyncio
async def test_expired_input_is_terminal_instead_of_retrying_forever():
    status = _status(state="expired")

    class Gateway:
        async def input(self, _request):
            return status

    with pytest.raises(TrackerFailure) as caught:
        await ensure_tracker_video_input_ready(
            Gateway(),
            actor_id=status.origin_actor_id,
            tracker_job_id=status.tracker_job_id,
            task_id=status.task_id,
        )
    assert caught.value.code == "input_expired"


@pytest.mark.asyncio
async def test_failed_source_input_keeps_its_terminal_error():
    status = _status(state="failed", error="source_changed")

    class Gateway:
        async def input(self, _request):
            return status

    with pytest.raises(TrackerFailure) as caught:
        await ensure_tracker_video_input_ready(
            Gateway(),
            actor_id=status.origin_actor_id,
            tracker_job_id=status.tracker_job_id,
            task_id=status.task_id,
        )
    assert caught.value.code == "source_changed"


@pytest.mark.asyncio
async def test_gateway_reads_sanitized_reference_and_rejects_wrong_url_version():
    actor, job, task, project, input_id = (uuid.uuid4() for _ in range(5))
    status = t.TrackerVideoInputStatus(
        tracker_job_id=job,
        input_id=input_id,
        origin_actor_id=actor,
        task_id=task,
        project_id=project,
        execution={
            "job_kind": "tracking",
            "model_key": "mock_bbox",
            "direction": "forward",
            "from_frame": 0,
            "to_frame": 5,
            "grid_step": 1,
            "correction_frame": None,
            "segment_id": None,
            "track_id_snapshot": None,
            "target_class_name": "car",
            "target_tool_unit_id": "bbox",
            "prompt_snapshot": {},
            "source_annotations": (),
            "correction_mask": None,
        },
        request_digest="a" * 64,
        prompt_digest="b" * 64,
        status="ready",
        plan_digest="c" * 64,
        timebase_digest="d" * 64,
        video={"frame_count": 6, "width": 80, "height": 48, "duration_ms": 1000},
        error=None,
        created_at=datetime.now(timezone.utc),
        retire_after=None,
    )
    reference = t.TrackerVideoSourceReference(
        input=status,
        source_version={
            "version_id": "retained-1",
            "etag": "retained-etag",
            "byte_size": 99,
            "sha256": "e" * 64,
        },
        url="http://storage.test/source?versionId=retained-1",
        expires_in=900,
    )
    request = t.TrackerSourceReferenceRequest(
        actor_id=actor,
        tracker_job_id=job,
        task_id=task,
        input_id=input_id,
        request_digest=status.request_digest,
        plan_digest=status.plan_digest,
    )

    class Provider:
        async def reference(self, _request):
            return self.result

    t.require_scope("reference_video_tracker_source", request, reference)
    provider = Provider()
    provider.result = reference
    gateway = TrackerVideoInputGateway(provider)
    assert await gateway.reference(request) == reference
    provider.result = reference.model_copy(
        update={"url": "http://storage.test/source?versionId=another-version"}
    )
    with pytest.raises(TrackerFailure, match="invalid_response"):
        await gateway.reference(request)
