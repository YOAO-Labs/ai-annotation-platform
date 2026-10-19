from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from aap_assets_video_inputs.tracker import TrackerFailure
from app.services.video_tracking import runner
from app.services.video_tracking.runner import (
    TrackerJobStateConflict,
    _read_tracker_input_evidence,
    _review_source_map_ids,
)
from app.db.models.video_tracker_job import VideoTrackerJob


def test_unbound_staged_tracker_candidate_fails_closed():
    job = VideoTrackerJob(annotation_id=uuid.uuid4())
    with pytest.raises(TrackerJobStateConflict, match="source evidence"):
        _read_tracker_input_evidence({"results": [{"frame_index": 0}]})
    with pytest.raises(TrackerJobStateConflict, match="source map"):
        _review_source_map_ids(job, {"results": [{"frame_index": 0}]})


@pytest.mark.asyncio
async def test_reference_storage_retry_rechecks_local_job_without_changing_version(
    monkeypatch,
):
    tracker_job_id, task_id, actor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    source_version = SimpleNamespace(
        version_id="immutable-version",
        etag="etag",
        byte_size=1234,
        sha256="b" * 64,
    )
    status = SimpleNamespace(
        tracker_job_id=tracker_job_id,
        task_id=task_id,
        input_id=uuid.uuid4(),
        request_digest="a" * 64,
        prompt_digest="c" * 64,
        plan_digest="d" * 64,
        execution=SimpleNamespace(grid_step=2),
    )
    response = SimpleNamespace(
        input=SimpleNamespace(
            execution=status.execution,
            prompt_digest=status.prompt_digest,
        ),
        source_version=source_version,
        url="https://source.test/exact-version",
    )
    attempts = []

    class Gateway:
        async def reference(self, request):
            attempts.append(request)
            if len(attempts) == 1:
                raise TrackerFailure("storage_unavailable")
            return response

    authority_checks = []

    async def check(_db, *, job_id, actor_id):
        authority_checks.append((job_id, actor_id))
        return True

    monkeypatch.setattr(runner, "_tracker_work_authorized_and_running", check)
    result = await runner._refresh_tracker_source_reference(
        Gateway(),
        status,
        db=object(),
        job=SimpleNamespace(id=tracker_job_id),
        actor_id=actor_id,
        task_id=task_id,
        expected_source_version=source_version,
    )

    assert result is response
    assert len(attempts) == 2
    assert authority_checks == [(tracker_job_id, actor_id)] * 2
    assert attempts[0] == attempts[1]


@pytest.mark.asyncio
async def test_reference_retry_stops_when_local_job_is_cancelled(monkeypatch):
    tracker_job_id, task_id, actor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    status = SimpleNamespace(
        tracker_job_id=tracker_job_id,
        task_id=task_id,
        input_id=uuid.uuid4(),
        request_digest="a" * 64,
        prompt_digest="c" * 64,
        plan_digest="d" * 64,
        execution=SimpleNamespace(grid_step=2),
    )
    reference_calls = 0

    class Gateway:
        async def reference(self, _request):
            nonlocal reference_calls
            reference_calls += 1
            raise TrackerFailure("storage_unavailable")

    authority_checks = 0

    async def check(_db, *, job_id, actor_id):
        nonlocal authority_checks
        authority_checks += 1
        return authority_checks == 1

    monkeypatch.setattr(runner, "_tracker_work_authorized_and_running", check)
    result = await runner._refresh_tracker_source_reference(
        Gateway(),
        status,
        db=object(),
        job=SimpleNamespace(id=tracker_job_id),
        actor_id=actor_id,
        task_id=task_id,
        expected_source_version=SimpleNamespace(
            version_id="immutable-version", sha256="b" * 64
        ),
    )

    assert result is None
    assert authority_checks == 2
    assert reference_calls == 1
