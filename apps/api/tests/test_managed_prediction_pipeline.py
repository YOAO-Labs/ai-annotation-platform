"""Owned ROI crop publication and source-image custody for the managed pipeline.

The behavior lives in the portable pipeline owner (``aap_inference.roi`` /
``aap_inference.execution_pipeline``) plus the application adapter that owns the
original image handle. These tests exercise those current owners directly; no
database or object storage is required.
"""

import asyncio
import io
import uuid

from PIL import Image
import pytest

from aap_inference.execution_pipeline import _publish_crop_inputs
from aap_inference.roi import RoIContentTooLarge, crop_inputs_from_boxes
from app.application import preannotation_execution
from app.db.models.task import Task


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


def image():
    value = Image.new("RGB", (200, 120), "red")
    with Image.new("RGB", (100, 120), "blue") as right:
        value.paste(right, (100, 0))
    return value


def test_deferred_crop_keeps_bytes_private_and_does_not_upload():
    with image() as source:
        batch = crop_inputs_from_boxes(
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
    monkeypatch.setattr("aap_inference.roi.MAX_CROP_BYTES", 32)
    with image() as source, pytest.raises(RoIContentTooLarge) as failure:
        crop_inputs_from_boxes(source, [box()], delivery="deferred")
    assert failure.value.code == "prediction_input_too_large"


@pytest.mark.asyncio
async def test_publish_is_sequential_and_sibling_cache_keeps_class_indices():
    task_id = str(uuid.uuid4())
    uploaded, cache, active = [], {}, 0

    class Publisher:
        async def publish(self, current, index, payload):
            nonlocal active
            active += 1
            assert active == 1 and current == task_id
            await asyncio.sleep(0)
            uploaded.append((index, payload))
            active -= 1
            return f"https://input.invalid/crop/{index}"

    with image() as source:
        boxes = [box(), box("person", x=55)]
        batches = []
        for allowed in (["car"], ["person"], ["person"]):
            batches.append(
                await _publish_crop_inputs(
                    source,
                    boxes,
                    task_id=task_id,
                    publisher=Publisher(),
                    cache=cache,
                    pad=0,
                    parent_class_filter=allowed,
                    min_crop_side_px=0,
                    mask_objects=None,
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
async def test_failed_publish_never_enters_sibling_cache():
    cache, calls = {}, 0

    class Publisher:
        def publish(self, *args):
            nonlocal calls
            calls += 1
            raise TimeoutError("synthetic upload failure")

    with image() as source:
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await _publish_crop_inputs(
                    source,
                    [box()],
                    task_id=str(uuid.uuid4()),
                    publisher=Publisher(),
                    cache=cache,
                    pad=0,
                    parent_class_filter=None,
                    min_crop_side_px=0,
                    mask_objects=None,
                )
    assert calls == 2 and cache == {}


@pytest.mark.asyncio
async def test_source_cancellation_closes_the_owned_image(monkeypatch):
    source = image()

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(preannotation_execution, "execute_task_pipeline", cancelled)
    task = Task(id=uuid.uuid4(), file_path="key/img.jpg", file_type="image")
    with pytest.raises(asyncio.CancelledError):
        await preannotation_execution._run_task_pipeline(
            task,
            [{"stage": 7, "parent_stage": None}, {"stage": 9, "parent_stage": 7}],
            [],
            [None, None],
            resolve_url=lambda _: "https://input.invalid/full",
            load_image=lambda _: source,
            project_id=None,
            owner_prefix="test",
            route_operation="batch_predict",
        )
    with pytest.raises(ValueError):
        source.getpixel((0, 0))
