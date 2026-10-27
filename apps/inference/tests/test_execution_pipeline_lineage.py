"""Pipeline lineage, selections and portable native-mask ownership behavior."""

from __future__ import annotations

import uuid

from PIL import Image
import pytest

from aap_assets_mask_content.codec import build_rle_reference
from aap_inference.execution_pipeline import (
    NativeMaskChildOutputUnsupported,
    execute_task_pipeline,
    lineage_shape_id,
)
from aap_inference_contract.execution import (
    ExecutionProvenance,
    FrozenModelOperation,
    ModelExecutionDispatch,
    ModelExecutionObservation,
    ModelOperationResult,
    ModelTaskResult,
    thaw_json,
)

SNAPSHOT = uuid.UUID(int=77)
INSTANCE = uuid.UUID(int=101)
POOL = uuid.UUID(int=9)


class _Port:
    """Deterministic execution port; responses are consumed per stage call."""

    def __init__(self, responses, *, instance_id=INSTANCE, pool_id=POOL):
        self.responses = list(responses)
        self.instance_id = instance_id
        self.pool_id = pool_id
        self.calls: list[FrozenModelOperation] = []
        self.dispatched: list[int] = []

    async def execute(self, operation, *, before_dispatch=None):
        if before_dispatch is not None:
            await before_dispatch(
                ModelExecutionDispatch(
                    request_id=operation.request_id,
                    operation=operation.operation,
                    route_scope_id=uuid.UUID(int=7),
                    attempt_id=operation.request_id,
                    requested_registry_id=operation.requested_registry_id,
                    requested_pool_id=operation.requested_pool_id,
                    registry_id=self.instance_id,
                    instance_id=self.instance_id,
                    pool_id=self.pool_id,
                    member_id=None,
                )
            )
        self.calls.append(operation)
        self.dispatched.append(operation.correlation["stage"])
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(operation)
        return ModelOperationResult(
            request_id=operation.request_id,
            operation=operation.operation,
            results=tuple(response),
            provenance=ExecutionProvenance(
                requested_registry_id=operation.requested_registry_id,
                requested_pool_id=operation.requested_pool_id,
                registry_id=self.instance_id,
                pool_id=self.pool_id,
                instance_id=self.instance_id,
                route_scope_id=uuid.UUID(int=7),
                attempt_id=operation.request_id,
            ),
            observation=ModelExecutionObservation(
                backend_id=str(self.instance_id),
                operation="predict",
                outcome="success",
                elapsed_seconds=0.001,
            ),
            wall_time_ms=1,
        )


def _box(label="car", *, x=10, y=10, width=50, height=50):
    return {
        "type": "rectanglelabels",
        "value": {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "rectanglelabels": [label],
        },
    }


def _polygon(*, parent=0):
    return {
        "type": "polygonlabels",
        "parent_box_idx": parent,
        "value": {
            "polygonlabels": ["car"],
            "points": [[10, 20], [80, 20], [80, 75]],
        },
    }


def _result(shapes, *, task_id="task-1", meta=None):
    return ModelTaskResult(task_id=task_id, result=shapes, meta=meta)


def _stage(number, parent=None, *, mode="crop", target="attributes", **kwargs):
    return {
        "stage": number,
        "parent_stage": parent,
        "ml_backend_id": str(uuid.UUID(int=number)),
        "model_id": f"model-{number}",
        "input": {"mode": mode},
        "roi": {"pad": 0},
        "write": {"target": target, "keys": ["color"]},
        **kwargs,
    }


def _image(width=200, height=120):
    return Image.new("RGB", (width, height), (20, 30, 40))


def _rle_rectangle(*, width, height, x0, y0, x1, y1):
    pixels = [0] * (width * height)
    for y in range(y0, y1):
        for x in range(x0, x1):
            pixels[y * width + x] = 1
    column = [pixels[y * width + x] for x in range(width) for y in range(height)]
    counts: list[int] = []
    current = 0
    run = 0
    for bit in column:
        if bit == current:
            run += 1
        else:
            counts.append(run)
            current = bit
            run = 1
    counts.append(run)
    if column and column[0]:
        counts.insert(0, 0)
    return {"encoding": "coco_rle", "size": [height, width], "counts": counts}


def _native_shape(*, width=100, height=100):
    rle = _rle_rectangle(width=width, height=height, x0=10, y0=10, x1=80, y1=80)
    reference = build_rle_reference(rle).model_dump(mode="json")
    return (
        {
            "tool_unit_id": "region",
            "class_name": "car",
            "instance_index": 0,
            "geometry": {"type": "raster_mask", "mask": reference},
        },
        reference["sha256"],
        rle,
    )


async def _run(
    stages,
    ports,
    *,
    modes,
    image=None,
    snapshot=SNAPSHOT,
    max_response_bytes=None,
    task_id="task-1",
    crop_publisher=None,
):
    return await execute_task_pipeline(
        task_id,
        stages,
        ports,
        [None] * len(stages),
        source_url="https://test.invalid/source",
        project_id=uuid.UUID(int=2),
        owner_prefix="test",
        route_operation="batch_predict",
        stage_modes=modes,
        source_image=image,
        crop_publisher=crop_publisher,
        max_response_bytes=max_response_bytes,
        execution_snapshot_id=snapshot,
    )


@pytest.mark.asyncio
async def test_geometry_and_attribute_stages_keep_source_ancestry():
    root_boxes = [_box("car"), _box("person", x=60)]
    geometry_port = _Port([[_result([_polygon(parent=1)])]])
    attribute_port = _Port(
        [
            lambda operation: [
                _result(
                    [{"attributes": {"color": "blue"}}],
                    task_id=str(thaw_json(item)["id"]),
                )
                for item in operation.task_inputs
            ]
        ]
    )
    image = _image()
    try:
        output = await _run(
            [
                _stage(7, target="root"),
                _stage(9, 7, mode="geometry", target="geometry", label="det"),
                _stage(12, 9, label="detail"),
            ],
            [_Port([[_result(root_boxes)]]), geometry_port, attribute_port],
            modes=["crop", "geometry", "crop"],
            image=image,
        )
    finally:
        image.close()

    shapes = thaw_json(output.results[0].result)
    lineage = thaw_json(output.extra)["pipeline"]["shape_lineage"]
    assert lineage == [
        {
            "shape_id": lineage_shape_id(SNAPSHOT, "task-1", 7, 0, 0),
            "stage": 7,
            "parent_stage": None,
            "parent_shape_id": None,
            "parent_box_idx": None,
            "transform": None,
        },
        {
            "shape_id": lineage_shape_id(SNAPSHOT, "task-1", 7, 0, 1),
            "stage": 7,
            "parent_stage": None,
            "parent_shape_id": None,
            "parent_box_idx": None,
            "transform": None,
        },
        {
            "shape_id": lineage_shape_id(SNAPSHOT, "task-1", 9, 0, 0),
            "stage": 9,
            "parent_stage": 7,
            "parent_shape_id": lineage_shape_id(SNAPSHOT, "task-1", 7, 0, 1),
            "parent_box_idx": 1,
            "transform": None,
        },
    ]
    assert shapes[0]["pipeline_shape_id"] == lineage[0]["shape_id"]
    assert shapes[1]["pipeline_shape_id"] == lineage[1]["shape_id"]
    assert shapes[2]["pipeline_shape_id"] == lineage[2]["shape_id"]
    assert shapes[2]["attributes"] == {"detail_color": "blue"}
    assert len(shapes[0].get("attributes", {})) == 0

    selections = thaw_json(output.extra)["pipeline"]["selections"]
    assert [item["stage"] for item in selections] == [7, 9, 12]
    assert all(item["executed"] for item in selections)
    assert selections[0]["requested_registry_id"] == str(uuid.UUID(int=7))
    assert selections[0]["executed_registry_id"] == str(INSTANCE)
    assert selections[0]["pool_id"] == str(POOL)
    assert selections[1]["model_id"] == "model-9"


@pytest.mark.asyncio
async def test_crop_detected_geometry_lineage_keeps_transform_and_result_seed():
    image = _image()

    def child(operation):
        assert len(operation.task_inputs) == 1
        return [_result([_polygon()], task_id="0")]

    ports = [_Port([[_result([_box()])]]), _Port([child])]
    try:
        output = await _run(
            [_stage(3, target="root"), _stage(4, 3, target="geometry")],
            ports,
            modes=["crop", "crop"],
            image=image,
        )
    finally:
        image.close()

    lineage = thaw_json(output.extra)["pipeline"]["shape_lineage"]
    assert len(lineage) == 2
    assert lineage[1]["shape_id"] == lineage_shape_id(SNAPSHOT, "task-1", 4, "0:0", 0)
    assert lineage[1]["parent_shape_id"] == lineage[0]["shape_id"]
    assert lineage[1]["parent_box_idx"] == 0
    assert lineage[1]["transform"] == pytest.approx(
        {"ox": 0.1, "oy": 0.1, "sx": 0.5, "sy": 0.5}
    )
    results = thaw_json(output.results[0].result)
    assert results[1]["pipeline_shape_id"] == lineage[1]["shape_id"]


@pytest.mark.asyncio
async def test_without_snapshot_no_synthetic_lineage_is_published():
    image = _image()
    ports = [
        _Port([[_result([_box()])]]),
        _Port([[_result([{"attributes": {"color": "red"}}], task_id="0")]]),
    ]
    try:
        output = await _run(
            [_stage(3, target="root"), _stage(4, 3)],
            ports,
            modes=["crop", "crop"],
            image=image,
            snapshot=None,
        )
    finally:
        image.close()

    pipeline = thaw_json(output.extra)["pipeline"]
    assert "shape_lineage" not in pipeline
    assert "selections" not in pipeline
    assert "pipeline_shape_id" not in thaw_json(output.results[0].result)[0]

    single = await execute_task_pipeline(
        "task-1",
        [_stage(3, target="root")],
        [_Port([[_result([_box()])]])],
        [None],
        source_url="https://test.invalid/source",
        project_id=None,
        owner_prefix="test",
        route_operation="batch_predict",
    )
    assert single.extra is None


@pytest.mark.asyncio
async def test_filtered_drop_preserves_dropped_source_lineage_and_partial_fact():
    image = _image()
    ports = [
        _Port([[_result([_box("drop"), _box("keep", x=60)])]]),
        _Port([RuntimeError("offline")]),
    ]
    try:
        output = await _run(
            [
                _stage(3, target="root"),
                _stage(
                    5,
                    3,
                    parent_class_filter=["drop"],
                    on_failure="drop_box",
                ),
            ],
            ports,
            modes=["crop", "crop"],
            image=image,
        )
    finally:
        image.close()

    pipeline = thaw_json(output.extra)["pipeline"]
    remaining = thaw_json(output.results[0].result)
    assert len(remaining) == 1
    assert remaining[0]["pipeline_shape_id"] == lineage_shape_id(
        SNAPSHOT, "task-1", 3, 0, 1
    )
    # The dropped source record stays available for audit.
    assert [record["shape_id"] for record in pipeline["shape_lineage"]] == [
        lineage_shape_id(SNAPSHOT, "task-1", 3, 0, 0),
        lineage_shape_id(SNAPSHOT, "task-1", 3, 0, 1),
    ]
    assert pipeline["partial"] is True
    assert pipeline["stage_errors"] == [
        {"stage": 5, "error_type": "RuntimeError", "on_failure": "drop_box"}
    ]
    assert pipeline["selections"][1]["executed"] is True


@pytest.mark.asyncio
async def test_native_root_lineage_uses_instance_position_and_prunes_masks():
    shape, digest, rle = _native_shape()
    image = _image(100, 100)
    ports = [
        _Port(
            [
                [_result([shape], meta={"mask_objects": {digest: rle}})],
            ]
        ),
        _Port([RuntimeError("offline")]),
    ]
    try:
        output = await _run(
            [
                _stage(7, target="root"),
                _stage(
                    10,
                    7,
                    parent_class_filter=["car"],
                    on_failure="drop_box",
                ),
            ],
            ports,
            modes=["crop", "crop"],
            image=image,
        )
    finally:
        image.close()

    pipeline = thaw_json(output.extra)["pipeline"]
    assert pipeline["shape_lineage"][0]["shape_id"] == lineage_shape_id(
        SNAPSHOT, "task-1", 7, 0, 0
    )
    assert pipeline["shape_lineage"][0]["parent_shape_id"] is None
    assert thaw_json(output.results[0].result) == []
    assert thaw_json(output.results[0].meta)["mask_objects"] == {}
    assert pipeline["partial"] is True


@pytest.mark.asyncio
async def test_native_child_mask_output_is_a_partial_stage_failure():
    shape, digest, rle = _native_shape()
    image = _image(100, 100)
    child = _Port(
        [
            [
                _result(
                    [{"type": "brushlabels", "value": {"rle": [1, 2]}}],
                    task_id="0",
                )
            ]
        ]
    )
    try:
        output = await _run(
            [_stage(3, target="root"), _stage(4, 3, target="geometry")],
            [
                _Port(
                    [
                        [_result([shape], meta={"mask_objects": {digest: rle}})],
                    ]
                ),
                child,
            ],
            modes=["crop", "crop"],
            image=image,
        )
    finally:
        image.close()

    pipeline = thaw_json(output.extra)["pipeline"]
    assert pipeline["partial"] is True
    assert pipeline["stage_errors"][0]["error_type"] == (
        NativeMaskChildOutputUnsupported.__name__
    )
    assert len(thaw_json(output.results[0].result)) == 1
    assert thaw_json(output.results[0].meta)["mask_objects"] == {digest: rle}
    assert child.calls


@pytest.mark.asyncio
async def test_awaitable_crop_publisher_uploads_before_child_dispatch():
    image = _image()
    uploads = []

    class Publisher:
        async def publish(self, task_id, box_index, jpeg_bytes):
            uploads.append((task_id, box_index, len(jpeg_bytes)))
            return f"https://inputs.test/crops/{box_index}"

    child = _Port(
        [
            lambda operation: [
                _result(
                    [{"attributes": {"color": "blue"}}],
                    task_id=str(thaw_json(item)["id"]),
                )
                for item in operation.task_inputs
            ]
        ]
    )
    try:
        output = await _run(
            [_stage(3, target="root"), _stage(4, 3)],
            [_Port([[_result([_box()])]]), child],
            modes=["crop", "crop"],
            image=image,
            crop_publisher=Publisher(),
        )
    finally:
        image.close()

    assert len(uploads) == 1 and uploads[0][1] == 0 and uploads[0][2] > 0
    assert thaw_json(child.calls[0].task_inputs[0])["file_path"] == (
        "https://inputs.test/crops/0"
    )
    assert thaw_json(output.results[0].result)[0]["attributes"] == {"color": "blue"}


def test_crop_remap_honors_explicit_percent_coordinate_units():
    from aap_inference.roi import remap_geometry_to_image

    remapped = remap_geometry_to_image(
        [
            {
                "type": "rectanglelabels",
                "coordinate_units": "percent",
                "value": {"x": 0.2, "y": 0.2, "width": 0.4, "height": 0.4},
            }
        ],
        {"ox": 0.1, "oy": 0.1, "sx": 0.5, "sy": 0.5},
    )
    assert remapped[0]["value"] == pytest.approx(
        {"x": 10.1, "y": 10.1, "width": 0.2, "height": 0.2}
    )
