from __future__ import annotations

from types import SimpleNamespace
import uuid

import pytest

from aap_assets_video_inputs.tracker import TrackerFailure
from app.services.accepted_tracker_inputs import (
    TrackerInputDeferred,
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
