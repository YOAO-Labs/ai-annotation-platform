"""Actual transport callbacks and result validation on frozen video stages."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from app.services.content_snapshot import ContentError
from app.workers import tasks


def stage(number=7):
    return {
        "stage": number,
        "parent_stage": None,
        "ml_backend_id": str(uuid.uuid4()),
    }


def result(task_id):
    return SimpleNamespace(task_id=str(task_id), result=[], meta=None)


@pytest.mark.asyncio
async def test_real_selected_transport_and_nonzero_root_are_checked_before_result_tags():
    task = SimpleNamespace(id=uuid.uuid4())
    transport = object()
    events = []
    original = result(task.id)
    converted = result(task.id)
    converted.result = [{"type": "video_track_bbox", "marker": "converted"}]
    context = {"from_frame": 2, "to_frame": 5, "direction": "backward"}

    class Client:
        async def predict(
            self, inputs, *, context, before_dispatch, max_response_bytes
        ):
            assert inputs == [{"id": str(task.id), "file_path": "fixed-version"}]
            assert max_response_bytes == 64 * 1024**2
            events.append("selected")
            await before_dispatch(transport)
            events.append("http")
            return [original]

    async def before(actual, number):
        assert actual is transport and number == 7
        events.append("guard")

    async def validate(received, number, wire):
        assert received == [original] and wire == context and number == 7
        assert "pipeline_shape_id" not in converted.result[0]
        events.append("validate")
        return [converted]

    snapshot_id = uuid.uuid4()
    output, extra, _ = await tasks._run_task_pipeline(
        task,
        [stage()],
        [Client()],
        [context],
        resolve_url=lambda _: "fixed-version",
        before_transport=before,
        max_response_bytes=64 * 1024**2,
        settle_model_requests=True,
        validate_results=validate,
        execution_snapshot_id=snapshot_id,
    )
    assert output == [converted]
    assert events == ["selected", "guard", "http", "validate"]
    assert extra["pipeline"]["shape_lineage"][0]["stage"] == 7
    assert converted.result[0]["pipeline_shape_id"] == str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{snapshot_id}:{task.id}:7:0:0")
    )
    assert original.result == []


@pytest.mark.asyncio
async def test_invalid_video_envelope_never_gets_lineage_or_child_dispatch():
    task = SimpleNamespace(id=uuid.uuid4())
    returned = result(task.id)
    returned.result = [{"type": "video_track_bbox", "marker": "untrusted"}]
    untouched = deepcopy(returned.result)

    class Client:
        async def predict(self, inputs, context):
            return [returned]

    def validate(*_):
        raise ContentError("video_prediction_result_frame_invalid", 502)

    with pytest.raises(ContentError, match="frame_invalid"):
        await tasks._run_task_pipeline(
            task,
            [stage()],
            [Client()],
            [None],
            resolve_url=lambda _: "fixed-version",
            validate_results=validate,
            execution_snapshot_id=uuid.uuid4(),
        )
    assert returned.result == untouched


@pytest.mark.asyncio
async def test_repeated_cancellation_drains_actual_request_before_releasing_source():
    task = SimpleNamespace(id=uuid.uuid4())
    started, release = asyncio.Event(), asyncio.Event()
    physical_done = False
    validated = False

    class Client:
        async def predict(self, inputs, context):
            nonlocal physical_done
            started.set()
            await release.wait()
            physical_done = True
            return [result(task.id)]

    def validate(*_):
        nonlocal validated
        validated = True

    pending = asyncio.create_task(
        tasks._run_task_pipeline(
            task,
            [stage()],
            [Client()],
            [None],
            resolve_url=lambda _: "fixed-version",
            settle_model_requests=True,
            validate_results=validate,
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done() and not physical_done
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert physical_done and not validated


@pytest.mark.parametrize("unit", [None, "video"])
def test_whole_video_never_falls_back_to_a_live_original_url(unit):
    project = SimpleNamespace(data_type="video")
    task = SimpleNamespace(file_type="video")
    with pytest.raises(ContentError, match="requires_new_request"):
        tasks._require_video_job_proof(project, None, unit)
    with pytest.raises(ContentError, match="requires_new_request"):
        tasks._require_video_inputs(task, None, None, unit)


def test_existing_frame_fanout_keeps_its_separate_input_contract():
    project = SimpleNamespace(data_type="video")
    task = SimpleNamespace(file_type="video")
    tasks._require_video_job_proof(project, None, "frame")
    tasks._require_video_inputs(task, None, None, "frame")
