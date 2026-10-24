"""Validate native detector and SAM video results without dense RLE decoding."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import video_prediction_results as subject
from app.services.content_snapshot import ContentError


TASK = uuid.UUID("14f5c7e4-8a35-4aa0-b60f-2e4a1fc6ca7b")


def probe():
    return {
        "probe_contract": 1,
        "video": {
            "width": 4,
            "height": 3,
            "fps": 2.0,
            "frame_count": 8,
            "duration_ms": 4000,
            "codec": "h264",
        },
    }


def bbox():
    return {"type": "bbox", "x": 0.0, "y": 0.25, "w": 0.5, "h": 0.5}


def rle(*, empty=False):
    return {
        "encoding": "coco_rle",
        "size": [3, 4],
        "counts": [12] if empty else [0, 4, 8],
    }


def frame(index=0, instance="1", geometry=None, **kwargs):
    return {
        "frame_index": index,
        "instance_id": instance,
        "geometry": geometry or bbox(),
        "confidence": 0.8,
        "outside": False,
        **kwargs,
    }


def envelope(rows=None, *, meta=None):
    return [
        SimpleNamespace(
            task_id=str(TASK),
            result=rows if rows is not None else [frame()],
            score=0.8,
            meta=meta,
        )
    ]


def detector():
    return {
        "type": "video_track_bbox",
        "track_id": 4,
        "class_name": "car",
        "score": 0.8,
        "keyframes": [
            {
                "frame_index": 0,
                "bbox": {key: value for key, value in bbox().items() if key != "type"},
                "score": 0.75,
            },
            {
                "frame_index": 2,
                "bbox": {key: value for key, value in bbox().items() if key != "type"},
                "score": 0.85,
            },
        ],
        "attributes": {"color": "red"},
    }


@pytest.mark.parametrize(
    "rows,direction",
    [
        ([], None),
        ([detector()], None),
        ([frame(0), frame(1)], "forward"),
        ([frame(3), frame(1)], "backward"),
        ([frame(0, "2"), frame(0, "1"), frame(1, "2"), frame(1, "1")], "forward"),
        (
            [
                frame(
                    0, geometry={"type": "polygon", "points": [[0, 0], [1, 0], [0, 1]]}
                )
            ],
            None,
        ),
        ([frame(0, geometry={"type": "polygon", "points": []}, outside=True)], None),
        ([frame(0, geometry={"type": "mask", "rle": rle()})], None),
        (
            [frame(0, geometry={"type": "mask", "rle": rle(empty=True)}, outside=True)],
            None,
        ),
    ],
)
def test_actual_backend_output_forms_are_preserved_without_mutation(rows, direction):
    results = envelope(rows)
    before = deepcopy(results)
    assert (
        subject.validate_results(results, TASK, probe(), direction=direction) is results
    )
    assert vars(results[0]) == vars(before[0])


@pytest.mark.parametrize(
    "mutation",
    [
        "task",
        "duplicate_envelope",
        "missing_envelope",
        "frame_boolean",
        "frame_oob",
        "frame_unsorted",
        "frame_duplicate",
        "nan",
        "infinity",
        "numeric_string",
        "bounds",
        "negative_size",
        "duplicate_track",
        "track_unsorted",
        "track_boolean_id",
        "primary_conflict",
        "class_drift",
        "kind_drift",
        "mixed_implicit",
        "bad_outside",
        "static_image",
    ],
)
def test_untrusted_identity_time_geometry_and_numbers_are_rejected(mutation):
    results = envelope([frame(0), frame(1)])
    rows = results[0].result
    if mutation == "task":
        results[0].task_id = str(uuid.uuid4())
    elif mutation == "duplicate_envelope":
        results.append(deepcopy(results[0]))
    elif mutation == "missing_envelope":
        results.clear()
    elif mutation == "frame_boolean":
        rows[0]["frame_index"] = True
    elif mutation == "frame_oob":
        rows[1]["frame_index"] = 8
    elif mutation == "frame_unsorted":
        rows.extend([frame(0)])
    elif mutation == "frame_duplicate":
        rows[1] = deepcopy(rows[0])
    elif mutation == "nan":
        rows[0]["geometry"]["x"] = float("nan")
    elif mutation == "infinity":
        rows[0]["confidence"] = float("inf")
    elif mutation == "numeric_string":
        rows[0]["geometry"]["w"] = "0.5"
    elif mutation == "bounds":
        rows[0]["geometry"].update(x=0.9, w=0.5)
    elif mutation == "negative_size":
        rows[0]["geometry"]["w"] = -0.1
    elif mutation == "duplicate_track":
        results[0].result = [detector(), detector()]
    elif mutation == "track_unsorted":
        value = detector()
        value["keyframes"].reverse()
        results[0].result = [value]
    elif mutation == "track_boolean_id":
        value = detector()
        value["track_id"] = True
        results[0].result = [value]
    elif mutation == "primary_conflict":
        rows[:] = [frame(0, "1", primary=True), frame(0, "2", primary=True)]
    elif mutation == "class_drift":
        rows[0]["class_name"], rows[1]["class_name"] = "car", "dog"
    elif mutation == "kind_drift":
        rows[1]["geometry"] = {"type": "polygon", "points": [[0, 0], [1, 0], [0, 1]]}
    elif mutation == "mixed_implicit":
        rows[0].pop("instance_id")
    elif mutation == "bad_outside":
        rows[0]["outside"] = "false"
    else:
        results[0].result = [
            {
                "type": "rectanglelabels",
                "value": {"x": 0, "y": 0, "width": 10, "height": 10},
            }
        ]
    with pytest.raises(ContentError):
        subject.validate_results(results, TASK, probe(), direction="forward")


def test_actual_request_window_rejects_another_valid_source_frame():
    with pytest.raises(ContentError, match="frame_invalid"):
        subject.validate_results(envelope([frame(1)]), TASK, probe(), window=(3, 5))
    subject.validate_results(
        envelope([frame(5), frame(3)]),
        TASK,
        probe(),
        window=(5, 3),
        direction="backward",
    )


@pytest.mark.parametrize(
    "change",
    [
        "size",
        "sum",
        "boolean",
        "compressed",
        "empty_visible",
        "extra",
        "mask_ref_without_object",
    ],
)
def test_mask_contract_is_verified_before_any_decoder_or_object_lookup(
    change, monkeypatch
):
    from app.utils import raster_mask_rle

    monkeypatch.setattr(
        raster_mask_rle,
        "decode_coco_rle",
        lambda *args: pytest.fail("Dense decoding is forbidden"),
    )
    geometry = {"type": "mask", "rle": rle()}
    value = geometry["rle"]
    if change == "size":
        value["size"] = [4, 3]
    elif change == "sum":
        value["counts"] = [1, 2]
    elif change == "boolean":
        value["counts"] = [0, True, 11]
    elif change == "compressed":
        value["counts"] = "encoded COCO string"
    elif change == "empty_visible":
        value["counts"] = [12]
    elif change == "extra":
        value["external_url"] = "https://invalid.test/mask"
    else:
        geometry = {"type": "mask", "mask": {"sha256": "a" * 64}}
    with pytest.raises(ContentError):
        subject.validate_results(envelope([frame(0, geometry=geometry)]), TASK, probe())


def test_portable_video_masks_require_exact_full_receipts_and_no_unused_objects():
    from app.services.raster_mask_storage import build_rle_reference

    value = rle()
    reference = build_rle_reference(value)
    geometry = {
        "type": "video_track_mask",
        "track_id": "trk_valid",
        "keyframes": [{"frame_index": 0, "mask": reference, "source": "prediction"}],
        "outside": [],
    }
    results = envelope(
        [{"type": "video_track_mask", "geometry": geometry}],
        meta={"mask_objects": {reference["sha256"]: value}},
    )
    subject.validate_results(results, TASK, probe())
    before = deepcopy(results)
    for change in ("missing", "unused", "digest", "bytes", "runs", "key"):
        changed = deepcopy(before)
        current = changed[0].result[0]["geometry"]["keyframes"][0]["mask"]
        if change == "missing":
            changed[0].meta["mask_objects"] = {}
        elif change == "unused":
            changed[0].meta["mask_objects"]["b" * 64] = value
        elif change == "digest":
            changed[0].meta["mask_objects"][reference["sha256"]]["counts"] = [0, 5, 7]
        elif change in {"bytes", "runs"}:
            current[change] += 1
        else:
            current["object_key"] = "other/location.json"
        with pytest.raises(ContentError):
            subject.validate_results(changed, TASK, probe())


@pytest.mark.parametrize(
    "limit,value",
    [
        ("MAX_INSTANCES", 1),
        ("MAX_FRAME_RESULTS", 1),
        ("MAX_VERTICES", 2),
        ("MAX_RETAINED_RUNS", 2),
        ("MAX_MASK_ELEMENTS", 11),
        ("MAX_RESPONSE_BYTES", 8),
    ],
)
def test_cumulative_budgets_reject_before_materialization(limit, value, monkeypatch):
    monkeypatch.setattr(subject, limit, value)
    if limit == "MAX_INSTANCES":
        rows = [frame(0, "1"), frame(0, "2")]
    elif limit == "MAX_VERTICES":
        rows = [
            frame(0, geometry={"type": "polygon", "points": [[0, 0], [1, 0], [0, 1]]})
        ]
    else:
        rows = [
            frame(0, geometry={"type": "mask", "rle": rle()}),
            frame(1, geometry={"type": "mask", "rle": rle()}),
        ]
    with pytest.raises(ContentError, match="budget_exceeded"):
        subject.validate_results(envelope(rows), TASK, probe())


@pytest.mark.parametrize("kind", ["bbox", "polygon", "mask"])
async def test_adapter_preserves_native_frames_metadata_and_valid_compact_geometry(
    kind, monkeypatch
):
    from app.services.video_tracking import runner
    from app.services.raster_mask_storage import build_rle_reference
    from app.schemas._jsonb_types import (
        VideoTrackGeometry,
        VideoTrackMaskGeometry,
        VideoTrackPolygonGeometry,
    )

    store = AsyncMock(side_effect=lambda value, **kwargs: build_rle_reference(value))
    monkeypatch.setattr(runner, "store_coco_rle", store)
    native = (
        bbox()
        if kind == "bbox"
        else {"type": "polygon", "points": [[0, 0], [1, 0], [0, 1]]}
        if kind == "polygon"
        else {"type": "mask", "rle": rle()}
    )
    empty = (
        {"type": "bbox", "x": 0, "y": 0, "w": 0, "h": 0}
        if kind == "bbox"
        else {"type": "polygon", "points": []}
        if kind == "polygon"
        else {"type": "mask", "rle": rle(empty=True)}
    )
    rows = [
        frame(2, geometry=native, primary=True, class_name="vehicle"),
        frame(3, geometry=empty, outside=True, primary=True, class_name="vehicle"),
        frame(4, geometry=native, primary=True, class_name="vehicle"),
    ]
    results = envelope(rows, meta={"model": "real declared identity"})
    frozen = deepcopy(results)
    qualified = await subject.adapt_results(results, TASK, probe(), window=(2, 4))
    assert vars(results[0]) == vars(frozen[0])
    shape = qualified.results[0].result[0]
    geometry = shape["geometry"]
    {
        "bbox": VideoTrackGeometry,
        "polygon": VideoTrackPolygonGeometry,
        "mask": VideoTrackMaskGeometry,
    }[kind].model_validate(geometry)
    assert shape["track_id"] == geometry["track_id"]
    assert geometry["type"] == "video_track_" + kind
    assert [row["frame_index"] for row in geometry["keyframes"]] == [2, 4]
    assert geometry["outside"] == [{"from": 3, "to": 3, "source": "prediction"}]
    assert shape["primary"] and shape["class_name"] == "vehicle"
    assert shape["tracker_frames"][1]["outside"] is True
    assert all(not ({"geometry", "rle"} & set(row)) for row in shape["tracker_frames"])
    proof = subject.validate_adaptation(
        qualified, qualified.results, TASK, probe(), window=(2, 4), direction="forward"
    )
    assert proof["schema"] == 1 and proof["raw_digest"] != proof["converted_digest"]
    assert qualified.results[0].meta["model"] == "real declared identity"
    assert store.await_count == (2 if kind == "mask" else 0)
    if kind == "mask":
        assert all(call.kwargs["db"] is None for call in store.await_args_list)


async def test_yolo_aggregate_preserves_identity_class_attributes_and_does_not_remap_twice():
    from app.services.prediction import _remap_track_ids

    results = envelope([detector()])
    qualified = await subject.adapt_results(results, TASK, probe())
    shape = qualified.results[0].result[0]
    assert shape["class_name"] == "car" and shape["attributes"] == {"color": "red"}
    assert (
        shape["instance_id"] == "4" and shape["geometry"]["semantic_label"] == "car_4"
    )
    assert _remap_track_ids([shape]) == [shape]
    assert shape["tracker_frames"][0]["confidence"] == 0.75


async def test_all_outside_keeps_honest_metadata_without_invalid_empty_track_or_put(
    monkeypatch,
):
    from app.services.video_tracking import runner

    store = AsyncMock(
        side_effect=AssertionError("Empty outside instances must not upload masks")
    )
    monkeypatch.setattr(runner, "store_coco_rle", store)
    results = envelope(
        [frame(0, geometry={"type": "mask", "rle": rle(empty=True)}, outside=True)]
    )
    qualified = await subject.adapt_results(results, TASK, probe())
    assert qualified.results[0].result == []
    assert (
        qualified.results[0].meta["video_tracker_outside_instances"][0]["all_outside"]
        is True
    )
    store.assert_not_awaited()


async def test_bad_late_frame_prevents_every_earlier_mask_put(monkeypatch):
    from app.services.video_tracking import runner

    store = AsyncMock()
    monkeypatch.setattr(runner, "store_coco_rle", store)
    results = envelope(
        [
            frame(0, geometry={"type": "mask", "rle": rle()}),
            frame(8, geometry={"type": "mask", "rle": rle()}),
        ]
    )
    with pytest.raises(ContentError):
        await subject.adapt_results(results, TASK, probe())
    store.assert_not_awaited()


async def test_qualification_is_private_exact_task_probe_window_direction_and_result():
    qualified = await subject.adapt_results(envelope(), TASK, probe(), window=(0, 2))
    for change in (
        "forged",
        "issuer",
        "task",
        "probe",
        "window",
        "direction",
        "result",
    ):
        value, results, task, given, window, direction = (
            qualified,
            deepcopy(qualified.results),
            TASK,
            probe(),
            (0, 2),
            "forward",
        )
        if change == "forged":
            value = {"results": results, "converted_digest": "a" * 64}
        elif change == "issuer":
            value = replace(qualified, _issuer=object())
        elif change == "task":
            task = uuid.uuid4()
        elif change == "probe":
            given["video"]["frame_count"] += 1
        elif change == "window":
            window = (0, 3)
        elif change == "direction":
            direction = "backward"
        else:
            results[0].result[0]["geometry"]["keyframes"][0]["bbox"]["w"] = 0.75
        with pytest.raises(ContentError, match="qualification_invalid"):
            subject.validate_adaptation(
                value, results, task, given, window=window, direction=direction
            )


async def test_mask_materialization_drains_repeated_cancellation_and_preserves_unknown(
    monkeypatch,
):
    from app.services.video_tracking import runner
    from app.services.raster_mask_storage import build_rle_reference
    from app.services.storage_access import mark_unsettled

    started, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def store(value, **kwargs):
        started.set()
        try:
            await release.wait()
            return build_rle_reference(value)
        finally:
            completed.set()

    monkeypatch.setattr(runner, "store_coco_rle", store)
    results = envelope([frame(0, geometry={"type": "mask", "rle": rle()})])
    pending = asyncio.create_task(subject.adapt_results(results, TASK, probe()))
    await started.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done() and not completed.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert completed.is_set()
    unknown = mark_unsettled(RuntimeError("provider close remains unknown"))
    monkeypatch.setattr(runner, "store_coco_rle", AsyncMock(side_effect=unknown))
    with pytest.raises(RuntimeError) as caught:
        await subject.adapt_results(results, TASK, probe())
    assert getattr(caught.value, "storage_operation_unsettled", False)


def test_saved_preflight_checks_declared_refs_without_claiming_physical_qualification(
    monkeypatch,
):
    from app.services.raster_mask_storage import build_rle_reference

    reference = build_rle_reference(rle())
    shape = {
        "type": "video_track_mask",
        "track_id": "trk_saved",
        "geometry": {
            "type": "video_track_mask",
            "track_id": "trk_saved",
            "keyframes": [
                {"frame_index": 0, "mask": reference, "source": "prediction"}
            ],
            "outside": [],
        },
    }
    assert subject.validate_saved_results([shape], probe()) == [shape]
    # The raw model path cannot borrow the saved-declaration preflight.
    with pytest.raises(ContentError, match="mask_invalid"):
        subject.validate_results(envelope([shape]), TASK, probe())
    for change in (
        "frame",
        "dimension",
        "runs",
        "bytes",
        "path",
        "pixel_budget",
        "run_budget",
        "byte_budget",
    ):
        saved = deepcopy(shape)
        ref = saved["geometry"]["keyframes"][0]["mask"]
        with monkeypatch.context() as context:
            if change == "frame":
                saved["geometry"]["keyframes"][0]["frame_index"] = 8
            elif change == "dimension":
                ref["size"] = [4, 3]
            elif change == "runs":
                ref["runs"] = subject.MAX_MASK_RUNS + 1
            elif change == "bytes":
                ref["bytes"] = subject.MAX_RLE_BYTES + 1
            elif change == "path":
                ref["object_key"] = "unknown/object"
            elif change == "pixel_budget":
                context.setattr(subject, "MAX_MASK_ELEMENTS", 11)
            elif change == "run_budget":
                context.setattr(subject, "MAX_RETAINED_RUNS", 2)
            else:
                context.setattr(subject, "MAX_TOTAL_MASK_BYTES", 1)
            with pytest.raises(ContentError):
                subject.validate_saved_results([saved], probe())


def test_hidden_attribute_mask_references_cannot_bypass_declared_frame_budgets():
    from app.services.raster_mask_storage import build_rle_reference

    shape = {
        "type": "video_track_bbox",
        "track_id": "trk_saved",
        "geometry": {
            "type": "video_track_bbox",
            "track_id": "trk_saved",
            "keyframes": [
                {
                    "frame_index": 0,
                    "bbox": {
                        key: value for key, value in bbox().items() if key != "type"
                    },
                    "source": "prediction",
                }
            ],
            "outside": [],
        },
        "attributes": {"hidden": build_rle_reference(rle())},
    }
    with pytest.raises(ContentError, match="mask_invalid"):
        subject.validate_saved_results([shape], probe())
    results = envelope()
    results[0].result[0]["attributes"] = {
        "nested": {"type": "raster_mask", "mask": build_rle_reference(rle())}
    }
    with pytest.raises(ContentError, match="mask_invalid"):
        subject.validate_results(results, TASK, probe())


@pytest.mark.parametrize(
    "field", ["kind", "hidden_kind", "encoding", "storage_encoding"]
)
def test_unhashable_json_kind_values_have_stable_content_errors(field):
    from app.services.raster_mask_storage import build_rle_reference

    results = envelope()
    if field == "kind":
        results[0].result[0]["type"] = []
        results[0].result[0]["geometry"]["type"] = []
    elif field == "hidden_kind":
        results[0].result[0]["attributes"] = {"type": []}
        # Arbitrary JSON attributes are allowed, including a non-string type.
        assert subject.validate_results(results, TASK, probe()) is results
        return
    else:
        reference = build_rle_reference(rle())
        reference[field] = []
        results = envelope(
            [frame(0, geometry={"type": "mask", "mask": reference})],
            meta={"mask_objects": {reference["sha256"]: rle()}},
        )
    with pytest.raises(ContentError):
        subject.validate_results(results, TASK, probe())


async def test_adapter_preserves_frame_attributes_occlusion_and_outside_aliases():
    value = detector()
    value["keyframes"][0].update(occluded=True, attributes={"state": "stopped"})
    value["outside"] = [{"from": 3, "to": 4, "source": "prediction"}]
    qualified = await subject.adapt_results(envelope([value]), TASK, probe())
    geometry = qualified.results[0].result[0]["geometry"]
    assert geometry["keyframes"][0]["occluded"] is True
    assert geometry["keyframes"][0]["attributes"] == {"state": "stopped"}
    assert geometry["outside"] == value["outside"]
    value["outside"] = [{"from": 1, "to": 3, "source": "prediction"}]
    with pytest.raises(ContentError, match="frame_invalid"):
        subject.validate_results(envelope([value]), TASK, probe())


def test_saved_geometry_is_closed_before_any_mask_load():
    value = {
        "type": "video_track_bbox",
        "track_id": "trk_saved",
        "geometry": {
            "type": "video_track_bbox",
            "track_id": "trk_saved",
            "keyframes": [
                {
                    "frame_index": 0,
                    "bbox": {
                        key: value for key, value in bbox().items() if key != "type"
                    },
                    "source": "prediction",
                }
            ],
            "outside": [],
        },
    }
    for where in ("track", "keyframe", "bbox", "outside"):
        shape = deepcopy(value)
        geometry = shape["geometry"]
        target = (
            geometry
            if where == "track"
            else geometry["keyframes"][0]
            if where == "keyframe"
            else geometry["keyframes"][0]["bbox"]
            if where == "bbox"
            else {"from": 2, "to": 3, "source": "prediction"}
        )
        if where == "outside":
            geometry["outside"] = [target]
        target["unrecognized"] = True
        with pytest.raises(ContentError, match="geometry_unsupported"):
            subject.validate_saved_results([shape], probe())
