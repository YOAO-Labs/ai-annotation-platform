"""Native root masks retain pixel identity through ROI enrichment and persistence."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from PIL import Image
import pytest

from aap_inference.roi import crop_inputs_from_boxes, geometry_prompts_from_boxes
from app.services.controlled_segmentation import validate_portable_masks
from app.services.raster_mask_storage import build_rle_reference
from app.utils.raster_mask_rle import coco_rle_bbox_pixels, encode_coco_rle
from tests.inference_execution_test_ports import run_client_task_pipeline


def native_shape(*, name="object", index=0, shifted=False):
    width, height = 128, 96
    pixels = [0] * (width * height)
    # A hollow region and a disconnected part belong to the same instance.
    for y in range(17, 73):
        for x in range(11, 85):
            if x < 15 or x >= 81 or y < 21 or y >= 69:
                pixels[y * width + x] = 1
    pixels[78 * width + (104 if shifted else 103)] = 1
    rle = encode_coco_rle(pixels, width, height)
    reference = build_rle_reference(rle)
    return (
        {
            "tool_unit_id": "region",
            "class_name": name,
            "instance_index": index,
            "geometry": {"type": "raster_mask", "mask": reference},
        },
        {reference["sha256"]: rle},
    )


def result(shapes, objects, *, task_id="task-1"):
    return SimpleNamespace(
        task_id=task_id, result=shapes, meta={"mask_objects": objects}
    )


def stage(number, parent=None, *, target="geometry", **kwargs):
    return {
        "stage": number,
        "parent_stage": parent,
        "ml_backend_id": str(uuid.UUID(int=1)),
        "write": {"target": target},
        "roi": {"pad": 0},
        **kwargs,
    }


async def run(stages, clients, monkeypatch, *, modes=None):
    image = Image.new("RGB", (128, 96))
    try:
        return await run_client_task_pipeline(
            SimpleNamespace(id="task-1"),
            stages,
            clients,
            [None] * len(stages),
            resolve_url=lambda task: "https://test.invalid/original",
            load_image=lambda task: image,
            stage_modes=modes,
            project_id=uuid.UUID(int=2),
            execution_snapshot_id=uuid.UUID(int=5),
        )
    finally:
        image.close()


def client(*responses):
    return SimpleNamespace(predict=AsyncMock(side_effect=responses))


def test_native_roi_uses_exact_pixels_preserves_holes_and_original_indices():
    first, objects = native_shape(name="skip")
    second, _ = native_shape(name="keep", index=1)
    original = deepcopy([first, second, objects])
    rle = next(iter(objects.values()))
    assert coco_rle_bbox_pixels(rle) == (11, 17, 104, 79)
    uploads = []

    def upload(index, data):
        from io import BytesIO

        with Image.open(BytesIO(data)) as crop:
            assert crop.size == (93, 62)
        uploads.append(index)
        return "https://test.invalid/crop"

    with Image.new("RGB", (128, 96)) as image:
        batch = crop_inputs_from_boxes(
            image,
            [first, second],
            mask_objects=objects,
            parent_class_filter=["keep"],
            pad=0,
            delivery="presigned",
            upload_fn=upload,
        )
    assert uploads == [1]
    assert batch.inputs[0]["id"] == "1"
    assert batch.skipped_geometry == 0
    assert batch.transforms["1"] == {
        "ox": 11 / 128,
        "oy": 17 / 96,
        "sx": 93 / 128,
        "sy": 62 / 96,
    }
    prompts = geometry_prompts_from_boxes(
        [first, second], mask_objects=objects, parent_class_filter=["keep"]
    )
    assert prompts.skipped_geometry == 0
    assert prompts.prompts[0]["parent_box_idx"] == 1
    assert prompts.prompts[0]["box"] == pytest.approx(
        [11 / 128, 17 / 96, 104 / 128, 79 / 96]
    )
    assert [first, second, objects] == original


@pytest.mark.parametrize("fault", ["missing", "digest", "empty", "size"])
def test_native_roi_bad_content_never_uploads_or_becomes_a_geometry_skip(fault):
    shape, objects = native_shape()
    if fault == "missing":
        objects = {}
    elif fault == "digest":
        next(iter(objects.values()))["counts"] = [0, 128 * 96]
    elif fault == "empty":
        rle = {"encoding": "coco_rle", "size": [96, 128], "counts": [128 * 96]}
        reference = build_rle_reference(rle)
        shape["geometry"]["mask"] = reference
        objects = {reference["sha256"]: rle}
    uploads = []
    with Image.new("RGB", (127 if fault == "size" else 128, 96)) as image:
        with pytest.raises(ValueError):
            crop_inputs_from_boxes(
                image,
                [shape],
                mask_objects=objects,
                delivery="presigned",
                upload_fn=lambda *args: uploads.append(args),
            )
    assert not uploads


@pytest.mark.asyncio
async def test_native_nonzero_root_depth_three_attributes_keep_same_mask(monkeypatch):
    shape, objects = native_shape(name="bound-class")
    original = deepcopy(shape["geometry"])
    root = client([result([shape], objects)])
    a = client(
        [SimpleNamespace(task_id="0", result=[{"attributes": {"color": "blue"}}])]
    )
    b = client(
        [SimpleNamespace(task_id="0", result=[{"attributes": {"state": "open"}}])]
    )
    results, extra, stats = await run(
        [
            stage(9),
            stage(13, 9, target="attributes", parent_class_filter=["bound-class"]),
            stage(21, 13, target="attributes", label="detail"),
        ],
        [root, a, b],
        monkeypatch,
    )
    assert stats[13]["ok"] == stats[21]["ok"] == 1
    assert stats[13]["skipped_geometry"] == stats[21]["skipped_geometry"] == 0
    assert results[0].result[0]["attributes"] == {
        "color": "blue",
        "detail_state": "open",
    }
    assert results[0].result[0]["geometry"] == original
    assert results[0].meta["mask_objects"] == objects
    assert results[0].result[0]["instance_index"] == 0
    assert extra["pipeline"]["shape_lineage"][0]["stage"] == 9


@pytest.mark.asyncio
async def test_native_root_geometry_prompt_and_vector_child_lineage(monkeypatch):
    shape, objects = native_shape()
    polygon = {
        "type": "polygonlabels",
        "parent_box_idx": 0,
        "value": {
            "polygonlabels": ["object"],
            "points": [[10, 20], [80, 20], [80, 75]],
        },
    }
    root = client([result([shape], objects)])
    geometry = client([SimpleNamespace(result=[polygon], meta=None)])
    attributes = client(
        [SimpleNamespace(task_id="0", result=[{"attributes": {"color": "red"}}])]
    )
    results, extra, stats = await run(
        [
            stage(5),
            stage(8, 5, target="intermediate"),
            stage(12, 8, target="attributes"),
        ],
        [root, geometry, attributes],
        monkeypatch,
        modes=["crop", "geometry", "crop"],
    )
    assert stats[8]["ok"] == stats[12]["ok"] == 1
    assert geometry.predict.call_args.args[0][0]["prompts"][0]["box"] == pytest.approx(
        [11 / 128, 17 / 96, 104 / 128, 79 / 96]
    )
    lineage = extra["pipeline"]["shape_lineage"]
    assert lineage[1]["parent_shape_id"] == lineage[0]["shape_id"]
    assert lineage[1]["stage"] == 8 and lineage[1]["parent_stage"] == 5
    assert len(results[0].result) == 1
    assert "attributes" not in results[0].result[0]
    assert results[0].meta["mask_objects"] == objects


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.asyncio
async def test_native_drop_prunes_only_unreferenced_objects_and_keeps_indices(
    shared, monkeypatch
):
    first, objects = native_shape(name="drop")
    second, second_objects = native_shape(name="keep", index=1, shifted=not shared)
    objects.update(second_objects)
    root = client([result([first, second], objects)])
    child = client(RuntimeError("offline"))
    results, extra, stats = await run(
        [
            stage(7),
            stage(
                10,
                7,
                target="attributes",
                parent_class_filter=["drop"],
                on_failure="drop_box",
            ),
        ],
        [root, child],
        monkeypatch,
    )
    final = results[0]
    assert len(final.result) == 1 and final.result[0]["instance_index"] == 1
    assert set(final.meta["mask_objects"]) == set(second_objects)
    assert stats[10]["failed"] == 1
    assert extra["pipeline"]["partial"] is True
    assert validate_portable_masks(final.result, final.meta)


@pytest.mark.asyncio
async def test_native_all_dropped_accepts_empty_final_envelope(monkeypatch):
    shape, objects = native_shape()
    results, _, _ = await run(
        [stage(3), stage(4, 3, target="attributes", on_failure="drop_box")],
        [client([result([shape], objects)]), client(RuntimeError("offline"))],
        monkeypatch,
    )
    assert results[0].result == []
    assert results[0].meta["mask_objects"] == {}
    assert validate_portable_masks([], results[0].meta) == []


@pytest.mark.asyncio
async def test_invalid_original_extra_is_rejected_before_any_child_or_pruning(
    monkeypatch,
):
    shape, objects = native_shape()
    objects["a" * 64] = next(iter(objects.values()))
    child = client(RuntimeError("must not dispatch"))
    with pytest.raises(ValueError):
        await run(
            [stage(3), stage(4, 3, target="attributes", on_failure="drop_box")],
            [client([result([shape], objects)]), child],
            monkeypatch,
        )
    child.predict.assert_not_awaited()


@pytest.mark.parametrize(
    "mode,target",
    [("crop", "attributes"), ("crop", "geometry"), ("geometry", "intermediate")],
)
@pytest.mark.parametrize("wire_format", ["native", "brushlabels"])
@pytest.mark.asyncio
async def test_unsupported_native_child_is_explicit_partial_failure(
    mode, target, wire_format, monkeypatch
):
    shape, objects = native_shape()
    child_shape, child_objects = native_shape()
    if wire_format == "brushlabels":
        child_shape, child_objects = (
            {"type": "brushlabels", "value": {"rle": [1, 2]}},
            None,
        )
    results, extra, stats = await run(
        [stage(3), stage(4, 3, target=target)],
        [
            client([result([shape], objects)]),
            client([result([child_shape], child_objects)]),
        ],
        monkeypatch,
        modes=["crop", mode],
    )
    assert stats[4]["ok"] == 0 and stats[4]["failed"] == 1
    assert (
        extra["pipeline"]["stage_errors"][0]["error_type"]
        == "NativeMaskChildOutputUnsupported"
    )
    assert len(results[0].result) == 1
    assert results[0].meta["mask_objects"] == objects


@pytest.mark.asyncio
async def test_multiple_result_envelopes_remain_separate(monkeypatch):
    first, objects = native_shape()
    second, second_objects = native_shape(shifted=True)
    child = client(
        [SimpleNamespace(task_id="0", result=[{"attributes": {"color": "red"}}])],
        [SimpleNamespace(task_id="0", result=[{"attributes": {"color": "blue"}}])],
    )
    results, _, stats = await run(
        [stage(3), stage(4, 3, target="attributes")],
        [client([result([first], objects), result([second], second_objects)]), child],
        monkeypatch,
    )
    assert stats[4]["ok"] == 2
    assert results[0].meta["mask_objects"] == objects
    assert results[1].meta["mask_objects"] == second_objects
    assert (
        results[0].result[0]["pipeline_shape_id"]
        != results[1].result[0]["pipeline_shape_id"]
    )
    assert results[0].result[0]["attributes"] == {"color": "red"}
    assert results[1].result[0]["attributes"] == {"color": "blue"}


@pytest.mark.asyncio
async def test_native_geometry_prompt_checks_actual_image_before_child_dispatch():
    shape, objects = native_shape()
    child = client([])
    image = Image.new("RGB", (127, 96))
    with pytest.raises(ValueError, match="source image"):
        await run_client_task_pipeline(
            SimpleNamespace(id="task-1"),
            [stage(3), stage(4, 3)],
            [client([result([shape], objects)]), child],
            [None, None],
            resolve_url=lambda task: "https://test.invalid/original",
            load_image=lambda task: image,
            stage_modes=["crop", "geometry"],
        )
    child.predict.assert_not_awaited()
    with pytest.raises(ValueError):
        image.getpixel((0, 0))
