"""Exercise owned image callbacks without a database or object-storage dependency."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import io
from types import SimpleNamespace
import uuid

from PIL import Image
import pytest

from app.services.content_snapshot import ContentError
from app.workers import predictions_retry, roi, tasks


@dataclass
class Result:
    task_id: str
    result: list
    meta: dict | None = None


class Client:
    def __init__(self, results):
        self.results, self.calls = results, []

    async def predict(self, inputs, context=None):
        self.calls.append(deepcopy(inputs))
        return deepcopy(self.results)


def box(label="car", *, x=5, width=40, parent=None):
    shape = {
        "type": "rectanglelabels",
        "value": {
            "x": x,
            "y": 5,
            "width": width,
            "height": 90,
            "rectanglelabels": [label],
        },
    }
    if parent is not None:
        shape["parent_box_idx"] = parent
    return shape


def stage(number, parent=None, *, mode="crop", target="attributes", classes=None):
    return {
        "stage": number,
        "parent_stage": parent,
        "ml_backend_id": str(uuid.uuid4()),
        "input": {"mode": mode},
        "roi": {"pad": 0},
        "write": {"target": target, "keys": ["color"]},
        "parent_class_filter": classes,
    }


def image():
    value = Image.new("RGB", (200, 120), "red")
    with Image.new("RGB", (100, 120), "blue") as right:
        value.paste(right, (100, 0))
    return value


def test_deferred_crop_keeps_bytes_private_and_does_not_upload():
    with image() as source:
        batch = roi.crop_inputs_from_boxes(
            source,
            [box(), box("person", x=55)],
            delivery="deferred",
            parent_class_filter=["person"],
            pad=0,
        )
    assert batch.inputs == [{"id": "1", "file_path": None}]
    assert set(batch.contents) == {"1"}
    assert batch.transforms["1"]["ox"] == 0.55
    with Image.open(io.BytesIO(batch.contents["1"])) as crop:
        assert crop.width == 80 and crop.height == 108
        assert crop.getpixel((40, 40))[2] > 200
    assert "contents=" not in repr(batch)


def test_deferred_crop_enforces_encoded_limit_before_buffer_growth(monkeypatch):
    monkeypatch.setattr(roi, "MAX_BYTES", 32)
    with image() as source, pytest.raises(ContentError) as failure:
        roi.crop_inputs_from_boxes(source, [box()], delivery="deferred")
    assert failure.value.code == "prediction_input_too_large"


@pytest.mark.asyncio
async def test_async_crop_upload_is_sequential_and_sibling_cache_keeps_class_indices():
    task = SimpleNamespace(id=uuid.uuid4())
    uploaded, cache, active = [], {}, 0

    async def upload(current, index, payload):
        nonlocal active
        active += 1
        assert active == 1 and current is task
        await asyncio.sleep(0)
        uploaded.append((index, payload))
        active -= 1
        return f"https://input.invalid/crop/{index}"

    with image() as source:
        boxes = [box(), box("person", x=55)]
        batches = []
        for allowed in (["car"], ["person"], ["person"]):
            batches.append(
                await tasks._upload_crop_inputs(
                    source,
                    boxes,
                    task=task,
                    upload_crop=upload,
                    before_dispatch=None,
                    cache=cache,
                    pad=0,
                    parent_class_filter=allowed,
                )
            )
    assert [[entry["id"] for entry in batch.inputs] for batch in batches] == [
        ["0"],
        ["1"],
        ["1"],
    ]
    assert [index for index, _ in uploaded] == [0, 1]
    assert all(set(value) == {"input", "transform"} for value in cache.values())
    assert all(isinstance(value["input"]["file_path"], str) for value in cache.values())


@pytest.mark.asyncio
async def test_failed_async_upload_never_enters_sibling_cache():
    cache, calls = {}, 0

    async def upload(*args):
        nonlocal calls
        calls += 1
        raise TimeoutError("synthetic upload failure")

    with image() as source:
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await tasks._upload_crop_inputs(
                    source,
                    [box()],
                    task=SimpleNamespace(id=uuid.uuid4()),
                    upload_crop=upload,
                    before_dispatch=None,
                    cache=cache,
                )
    assert calls == 2 and cache == {}


@pytest.mark.asyncio
async def test_owned_full_image_geometry_and_deep_rois_keep_nonzero_root_lineage(
    monkeypatch,
):
    task = SimpleNamespace(id=uuid.uuid4(), file_type="image")
    source, uploads, guarded = image(), [], []
    root = Client([Result(str(task.id), [box(), box("person", x=55)])])
    geometry = Client([Result(str(task.id), [box("face", x=60, width=30, parent=1)])])
    deep = Client([Result("0", [{"attributes": {"color": "blue"}}])])
    sibling = Client([Result("0", [{"attributes": {"color": "red"}}])])
    stages = [
        stage(7),
        stage(9, 7, mode="geometry", target="geometry", classes=["person"]),
        stage(12, 9),
        stage(13, 7, classes=["car"]),
    ]

    async def guard():
        guarded.append(True)

    async def upload(current, index, payload):
        assert current is task
        with Image.open(io.BytesIO(payload)) as crop:
            uploads.append((index, crop.getpixel((crop.width // 2, crop.height // 2))))
        return f"https://input.invalid/crop/{len(uploads)}"

    def forbidden(*args, **kwargs):
        raise AssertionError("owned pipeline reread source or allocated a second owner")

    monkeypatch.setattr(tasks, "_load_task_image", forbidden)
    from app.services.prediction_inputs import PredictionInputs

    monkeypatch.setattr(PredictionInputs, "create", forbidden)
    results, extra, stats = await tasks._run_task_pipeline(
        task,
        stages,
        [root, geometry, deep, sibling],
        [None] * 4,
        resolve_url=lambda _: "https://input.invalid/full",
        load_image=lambda _: source,
        upload_crop=upload,
        stage_modes=["crop", "geometry", "crop", "crop"],
        execution_snapshot_id=uuid.uuid4(),
        before_dispatch=guard,
    )
    assert root.calls[0][0]["file_path"] == geometry.calls[0][0]["file_path"]
    assert geometry.calls[0][0]["prompts"][0]["parent_box_idx"] == 1
    assert [index for index, _ in uploads] == [0, 0]
    assert uploads[0][1][2] > 200 and uploads[1][1][0] > 200
    assert deep.calls[0][0]["file_path"] != sibling.calls[0][0]["file_path"]
    assert len(guarded) == 6
    shapes = results[0].result
    assert shapes[0]["attributes"]["color"] == "red"
    assert shapes[2]["attributes"]["color"] == "blue"
    lineage = extra["pipeline"]["shape_lineage"]
    assert lineage[2]["stage"] == 9 and lineage[2]["parent_stage"] == 7
    assert lineage[2]["parent_shape_id"] == lineage[1]["shape_id"]
    assert stats[7]["detected"] == 2 and stats[12]["ok"] == 1
    with pytest.raises(ValueError):
        source.getpixel((0, 0))


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["keep_parent", "drop_box"])
@pytest.mark.parametrize("stop_at", [2, 3])
async def test_source_guard_failure_cannot_become_partial_success(policy, stop_at):
    task = SimpleNamespace(id=uuid.uuid4(), file_type="image")
    source, dispatched, uploads = image(), 0, []
    root = Client([Result(str(task.id), [box()])])
    child = Client([Result("0", [{"attributes": {"color": "red"}}])])

    async def guard():
        nonlocal dispatched
        dispatched += 1
        if dispatched == stop_at:
            raise ContentError("content_binding_changed", 409)

    async def upload(*args):
        uploads.append(True)
        return "https://input.invalid/crop"

    child_stage = {**stage(9, 7), "on_failure": policy}
    with pytest.raises(ContentError) as failure:
        await tasks._run_task_pipeline(
            task,
            [stage(7), child_stage],
            [root, child],
            [None, None],
            resolve_url=lambda _: "https://input.invalid/full",
            load_image=lambda _: source,
            upload_crop=upload,
            before_dispatch=guard,
        )
    assert failure.value.code == "content_binding_changed"
    assert len(uploads) == (1 if stop_at == 3 else 0)
    assert child.calls == []
    with pytest.raises(ValueError):
        source.getpixel((0, 0))


@pytest.mark.asyncio
async def test_async_crop_cancellation_closes_owned_image_without_child_dispatch():
    task = SimpleNamespace(id=uuid.uuid4(), file_type="image")
    source = image()
    child = Client([])

    async def upload(*args):
        await asyncio.sleep(0)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await tasks._run_task_pipeline(
            task,
            [stage(7), stage(9, 7)],
            [Client([Result(str(task.id), [box()])]), child],
            [None, None],
            resolve_url=lambda _: "https://input.invalid/full",
            load_image=lambda _: source,
            upload_crop=upload,
        )
    assert child.calls == []
    with pytest.raises(ValueError):
        source.getpixel((0, 0))


@pytest.mark.parametrize("managed", [False, True])
def test_frozen_failure_binding_and_service_receipt_use_separate_provenance(managed):
    identifier, receipt = uuid.uuid4(), object()
    binding = {
        "task_id": str(identifier),
        "original" if managed else "reference": {"sha256": "a" * 64},
    }
    owner = SimpleNamespace(
        bindings={identifier: binding},
        failure_binding=lambda _: deepcopy(binding),
        is_managed=lambda _: managed,
    )
    recorded = tasks._failure_input(owner, identifier)
    key = "original_input" if managed else "reference_input"
    assert recorded == {key: binding}
    recorded[key].clear()
    assert binding["task_id"] == str(identifier)
    argument = "original_receipt" if managed else "reference_receipt"
    assert tasks._input_receipt_kwargs(owner, identifier, receipt) == {
        argument: receipt
    }
    if managed:
        with pytest.raises(ContentError):
            tasks._input_receipt_kwargs(owner, identifier, None)


def test_unqualified_image_callbacks_are_rejected_but_nonimage_paths_remain():
    with pytest.raises(ContentError) as failure:
        tasks._require_image_job_proof(SimpleNamespace(data_type="image"), None)
    assert failure.value.code == "prediction_original_requires_new_request"
    with pytest.raises(ContentError):
        tasks._require_image_inputs(SimpleNamespace(file_type="image"), None, None)
    tasks._require_image_job_proof(SimpleNamespace(data_type="video"), None)
    tasks._require_image_inputs(SimpleNamespace(file_type="video"), None, None)
    predictions_retry._require_qualified_image_retry(
        SimpleNamespace(file_type="video"), SimpleNamespace(data_type="video"), None
    )


class Session:
    def __init__(self, rows):
        self.rows = rows
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, model, identifier, **kwargs):
        return self.rows.get(model.__name__)

    async def scalar(self, *args, **kwargs):
        return self.rows["FailedPrediction"]

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [False, True])
async def test_late_image_retry_rejects_before_source_resolution_or_inference(
    monkeypatch, snapshot
):
    identifier, actor = uuid.uuid4(), uuid.uuid4()
    failure = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=identifier,
        task_id=uuid.uuid4(),
        ml_backend_id=uuid.uuid4(),
        execution_snapshot_id=uuid.uuid4() if snapshot else None,
        extra={},
        dismissed_at=None,
    )
    db = Session(
        {
            "FailedPrediction": failure,
            "Task": SimpleNamespace(id=failure.task_id, file_type="image"),
            "Project": SimpleNamespace(id=identifier, data_type="image"),
            "User": SimpleNamespace(id=actor),
            "MLBackendRegistry": SimpleNamespace(id=failure.ml_backend_id),
        }
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("historical image request reached current source")

    from app.services import storage, prediction_execution

    monkeypatch.setattr(storage, "resolve_task_url", forbidden)
    monkeypatch.setattr(prediction_execution, "load_snapshot", forbidden)
    if not snapshot:
        with pytest.raises(ContentError) as rejected:
            await predictions_retry._run_retry_attempt(
                lambda: db, str(failure.id), str(actor)
            )
        assert rejected.value.code == "prediction_original_requires_new_request"
    else:
        counted = []

        async def bump(*args, **kwargs):
            counted.append(True)

        monkeypatch.setattr(predictions_retry, "_bump_retry_counter", bump)
        result = await predictions_retry._run_snapshot_retry(
            lambda: db, failure.id, actor
        )
        assert result["status"] == "failed"
        assert result["reason"] == "prediction_original_requires_new_request"
        assert counted == [True]
    assert failure.extra == {} and not hasattr(failure, "deleted")
