"""Qualified segment installation and lossless, bounded original-size mask output."""

import copy
import hashlib
import json
import os
import time
import uuid
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from managed_models import InstallRequest, ManagedModelStore, digest
from managed_segmentation import emit_instances, validate_budget
from schemas import Context, model_key_for_context
from training_contract.model_return import segmentation_identity


def receipt():
    return {
        "model_version_id": str(uuid.uuid4()),
        "sha256": "a" * 64,
        "classes": ["car", "person"],
        "classes_digest": digest(["car", "person"]),
        "framework": "ultralytics",
        "framework_version": "8.4.177",
        "task": "segmentation",
        **segmentation_identity("yolo11n-seg"),
    }


def context(ref):
    ctx = Context(
        task="segment",
        model_id=f"trained:{ref['model_version_id']}",
        frozen_model_ref={
            key: ref[key] for key in ("model_version_id", "sha256", "classes_digest")
        },
        params={"max_det": 3},
    )
    ctx._managed_receipt = ref
    return ctx


def fixture():
    masks = np.zeros((2, 7, 11), dtype=np.uint8)
    masks[0, 1:6, 1:7] = 1
    masks[0, 2:5, 2:6] = 0  # hole
    masks[0, 0, 10] = 1  # disconnected component of the same instance
    masks[1, 1:6, 1:7] = 1  # overlap and equal boxes, distinct instance pixels
    return SimpleNamespace(
        names={0: "car", 1: "person"},
        orig_shape=(7, 11),
        masks=SimpleNamespace(data=masks),
        boxes=SimpleNamespace(
            xyxy=np.array([[0, 0, 11, 7]] * 2),
            conf=np.array([0.8, 0.7]),
            cls=np.array([0, 1]),
        ),
    )


def decode(rle):
    flat, value = [], 0
    for count in rle["counts"]:
        flat.extend([value] * count)
        value = 1 - value
    return np.array(flat, dtype=np.uint8).reshape(rle["size"], order="F")


def test_original_pixel_rle_preserves_holes_components_overlap_and_identity():
    result, ref = fixture(), receipt()
    items, meta = emit_instances(result, ref, 11, 7, 3)
    assert [item["instance_index"] for item in items] == [0, 1]
    assert [item["native_class_index"] for item in items] == [0, 1]
    assert [item["class_name"] for item in items] == ref["classes"]
    for index, item in enumerate(items):
        assert (
            item["geometry"]["type"] == "raster_mask"
            and item["tool_unit_id"] == "region"
        )
        assert item["score"] == item["confidence"]
        mask_ref = item["geometry"]["mask"]
        rle = meta["mask_objects"][mask_ref["sha256"]]
        np.testing.assert_array_equal(decode(rle), result.masks.data[index])
        raw = json.dumps(rle, ensure_ascii=False, separators=(",", ":")).encode()
        assert hashlib.sha256(raw).hexdigest() == mask_ref["sha256"]
        assert mask_ref["bytes"] == len(raw) and mask_ref["runs"] == len(rle["counts"])
    assert meta["segmentation"]["dimensions"] == [7, 11]
    assert meta["segmentation"]["mask_processing"].endswith("process_mask_native")


@pytest.mark.parametrize(
    "fault",
    [
        "count",
        "mask_dimensions",
        "class",
        "noninteger",
        "confidence",
        "names",
        "foreground",
        "binary",
        "dimensions",
        "box",
    ],
)
def test_native_misalignment_or_invalid_values_never_fall_back(fault):
    result = fixture()
    if fault == "count":
        result.boxes.conf = np.array([0.8])
    elif fault == "mask_dimensions":
        result.masks.data = np.zeros((2, 11, 7))
    elif fault == "class":
        result.boxes.cls[1] = 2
    elif fault == "noninteger":
        result.boxes.cls = np.array([0.5, 1])
    elif fault == "confidence":
        result.boxes.conf[1] = np.nan
    elif fault == "names":
        result.names = {0: "person", 1: "car"}
    elif fault == "foreground":
        result.masks.data[1] = 0
    elif fault == "binary":
        result.masks.data[1, 0, 0] = 2
    elif fault == "dimensions":
        result.orig_shape = (11, 7)
    elif fault == "box":
        result.boxes.xyxy[0, 2] = 12
    with pytest.raises(ValueError):
        emit_instances(result, receipt(), 11, 7, 3)


def test_empty_result_is_explicit_and_has_no_dangling_objects():
    result = fixture()
    result.boxes = SimpleNamespace(
        xyxy=np.empty((0, 4)), conf=np.array([]), cls=np.array([])
    )
    result.masks = None
    items, meta = emit_instances(result, receipt(), 11, 7, 3)
    assert items == [] and meta["mask_objects"] == {}


@pytest.mark.parametrize(
    "shape", [(4097, 1, 1), (4096, 4096, 8), (11, 7, 301), (0, 1, 1)]
)
def test_original_shape_and_worst_case_allocation_are_checked_before_native_predict(
    shape,
):
    with pytest.raises(ValueError):
        validate_budget(*shape)


def test_rle_run_and_total_metadata_budgets(monkeypatch):
    import managed_segmentation as module

    monkeypatch.setattr(module, "MAX_MASK_RUNS", 2)
    with pytest.raises(ValueError, match="run budget"):
        emit_instances(fixture(), receipt(), 11, 7, 3)
    monkeypatch.setattr(module, "MAX_MASK_RUNS", 1_000_000)
    monkeypatch.setattr(module, "MAX_MASK_RESPONSE_BYTES", 20)
    with pytest.raises(ValueError, match="byte budget"):
        emit_instances(fixture(), receipt(), 11, 7, 3)


def test_segment_request_has_separate_task_key_and_rejects_prompts_or_partial_classes():
    ref = receipt()
    ctx = context(ref)
    assert model_key_for_context(ctx)[0] == "segment"
    for change in (
        {"task": "pose"},
        {"classes": [0]},
        {"text": "car"},
        {"params": {"max_det": 301}},
    ):
        with pytest.raises(ValidationError):
            Context.model_validate({**ctx.model_dump(), **change})


def test_segment_install_rejects_foreign_runtime_or_output_identity():
    values = {**receipt(), "size_bytes": 1, "download_url": "http://storage.test/own"}
    assert InstallRequest.model_validate(values).output_contract == "aap-instance-rle-1"
    for change in (
        {"framework": "libreyolo"},
        {"task": "detection"},
        {"model_size": "s"},
        {"output_contract": "unknown"},
        {"runtime_qualification": {}},
    ):
        with pytest.raises(ValidationError):
            InstallRequest.model_validate({**values, **change})


def test_predictor_forces_original_mask_path_and_checks_budget_before_call(monkeypatch):
    from predictor import YoloPredictor

    ref, result, calls = receipt(), fixture(), []
    model = SimpleNamespace(names=result.names, device="cpu")

    def predict(_img, **kwargs):
        calls.append(kwargs)
        return [result]

    model.predict = predict
    monkeypatch.setattr("predictor.fetch_image", lambda _: Image.new("RGB", (11, 7)))
    ctx = context(ref)
    items, _ = YoloPredictor._predict_closed_sync(model, "own", ctx)
    assert len(items) == 2 and calls[0]["retina_masks"] is True
    assert calls[0]["imgsz"] == 640 and calls[0]["half"] is False
    assert ctx._segmentation_meta["mask_objects"]
    calls.clear()
    monkeypatch.setattr(
        "predictor.fetch_image", lambda _: Image.new("RGB", (4096, 4096))
    )
    ctx.params.max_det = 300
    with pytest.raises(ValueError, match="budget"):
        YoloPredictor._predict_closed_sync(model, "own", ctx)
    assert calls == []


async def test_prediction_wire_retains_mask_objects_and_rejects_cross_task_receipt(
    monkeypatch,
):
    import main
    from fastapi import HTTPException
    from schemas import BatchPredictRequest

    from managed_models import runtime_receipt

    ref = runtime_receipt(receipt())
    ctx = context(ref)
    ctx.frozen_model_ref.inference_runtime = ref["inference_runtime"]
    items, metadata = emit_instances(fixture(), ref, 11, 7, 3)
    calls = []

    async def predict(_path, current):
        calls.append(current.task)
        current._segmentation_meta = metadata
        return items, False, None, 1

    monkeypatch.setattr(main, "_model_pool", object())
    monkeypatch.setattr(main, "_predictor", SimpleNamespace(predict_one=predict))
    monkeypatch.setattr(main._managed_models, "receipt", lambda _: ref)
    request = BatchPredictRequest(
        tasks=[{"id": "owned", "file_path": "own"}], context=ctx
    )
    result = (await main._run_predict(request))[0].model_dump(mode="json")
    assert result["task"] == "owned" and result["result"] == items
    assert result["meta"]["mask_objects"] == metadata["mask_objects"]
    assert result["meta"]["trained_model"] == {
        key: ref[key] for key in ("model_version_id", "sha256", "classes_digest")
    }
    assert result["meta"]["inference_runtime"] == ref["inference_runtime"]
    monkeypatch.setattr(
        main._managed_models, "receipt", lambda _: {**ref, "task": "detection"}
    )
    with pytest.raises(HTTPException) as error:
        await main._run_predict(request)
    assert error.value.status_code == 409 and calls == ["segment"]


@pytest.mark.skipif(
    os.environ.get("AAP_NATIVE_SEGMENTATION") != "1",
    reason="Pinned native CPU qualification is explicit",
)
def test_pinned_native_checkpoint_reload_postprocessing_and_non_square_inference(
    tmp_path, monkeypatch
):
    import torch
    from ultralytics import YOLO
    from ultralytics.utils import ops
    from predictor import YoloPredictor

    torch.set_num_threads(2)
    model = YOLO("yolo11n-seg.yaml")
    target = tmp_path / "own.pt"
    model.save(target)
    ref = receipt()
    import ultralytics

    ref.update(
        framework_version=ultralytics.__version__,
        **segmentation_identity(
            "yolo11n-seg", framework_version=ultralytics.__version__
        ),
    )
    ref["classes"] = [model.names[index] for index in range(len(model.names))]
    ref["classes_digest"] = digest(ref["classes"])
    loaded = ManagedModelStore.inspect(target, ref)
    # Actual pinned native processing keeps the hole and disconnected foreground.
    mask = fixture().masks.data[0]
    proto = torch.tensor(np.where(mask, 1, -1), dtype=torch.float32).unsqueeze(0)
    actual = ops.process_mask_native(
        proto, torch.ones((1, 1)), torch.tensor([[0, 0, 11, 7]]), (7, 11)
    )
    np.testing.assert_array_equal(actual[0].cpu().numpy(), mask)
    monkeypatch.setattr("predictor.fetch_image", lambda _: Image.new("RGB", (160, 96)))
    ctx = context(ref)
    items, _ = YoloPredictor._predict_closed_sync(loaded, "own", ctx)
    assert ctx._segmentation_meta["segmentation"]["dimensions"] == [96, 160]
    for item in items:
        assert item["geometry"]["mask"]["size"] == [96, 160]
    from sample_evaluations import EvaluationRequest
    from segmentation_evaluations import qualification_profile

    profile = qualification_profile("cpu", uuid.uuid4())
    issued = int(time.time())
    evaluation = EvaluationRequest(
        request_id=uuid.uuid4(),
        binding_digest="b" * 64,
        profile_digest=profile["profile_digest"],
        storage_identity=profile["storage_identity"],
        input_sha256="c" * 64,
        issued_at=issued,
        expires_at=issued + 86400,
    )
    evaluated = Context(
        task="segment",
        model_id=f"trained:{ref['model_version_id']}",
        frozen_model_ref={
            key: ref[key] for key in ("model_version_id", "sha256", "classes_digest")
        },
        params={"max_det": 100},
        sample_evaluation=evaluation,
    )
    evaluated._managed_receipt, evaluated._evaluation_profile = ref, profile
    YoloPredictor._predict_closed_sync(loaded, "own", evaluated)
    assert evaluated._evaluation_meta["options"] == profile["options"]
    assert evaluated._evaluation_meta["options"]["retina_masks"] is True
    assert evaluated._evaluation_meta["width"] == 160
    assert evaluated._evaluation_meta["height"] == 96
    invalid = copy.deepcopy(ref)
    invalid["classes"] = list(reversed(invalid["classes"]))
    invalid["classes_digest"] = digest(invalid["classes"])
    with pytest.raises(ValueError, match="classes"):
        ManagedModelStore.inspect(target, invalid)
